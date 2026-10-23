#!/usr/bin/env python3
"""修复"整个项目被移动过"之后 .venv 内部残留的旧绝对路径。

背景（真实踩坑）：`virtualenv`/`venv` 会把**创建时**的绝对路径写进 venv 内部：

- `.venv/bin/pip`、`pyinstaller`、`pyuic6` … 的 shebang（`#!<venv>/bin/python`）
- `.venv/bin/activate*` 里的 `VIRTUAL_ENV=<venv>`
- `.venv/pyvenv.cfg` 的 `command =` 行

`bin/python` 本身只是指向 `/usr/local/bin/python3.x` 的符号链接，所以**移动项目后
Python 仍能 import**，但 `pip` / `pyinstaller` 这类控制台脚本会以
`No such file or directory` 失败，IDE（PyCharm 的 SDK 表）也会因为记住旧路径而报
`Cannot run program "<旧路径>/.venv/bin/python"`。

本脚本只做一件事：把 venv 内部指向**别处**的旧 venv 路径改写成本 venv 现在的真实
路径。默认 **dry-run**（只报告，有残留则退出码 1），加 `--apply` 才写入。

用法：
    python code/tools/fix_venv_paths.py            # 检查（有残留则退出码 1）
    python code/tools/fix_venv_paths.py --apply    # 修复
    python code/tools/fix_venv_paths.py --venv /path/to/.venv        # 指定 venv

范围与边界：
- 只扫 `pyvenv.cfg`、`bin/`（Windows 为 `Scripts/`）、以及 site-packages 下的
  `*.pth` / `*.egg-link` / `direct_url.json` —— 这些才是 venv 路径可能落地的位置；
  不去遍历整个 site-packages（几十 MB，纯属浪费）。
- 只改 venv 内部文件，**不碰仓库源码、不碰 IDE 配置**；PyCharm 的解释器路径需要
  在 Settings → Project → Python Interpreter 里重新选一次（或改全局 SDK 表）。
- Windows 的 `Scripts/*.exe` 是二进制 shim，内部同样嵌了旧路径，本脚本不处理
  （重建 venv 即可），但会把它们列出来提醒。
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

BINARY_SUFFIXES = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".a", ".o", ".whl", ".exe"}
# 形如 …/某目录/.venv，后面跟 / 或空白/引号/行尾
VENV_PATH_RE = re.compile(rb"(/[^\s'\"<>()\[\]:,;]*?/\.venv)(?=/|\s|'|\"|$)")


def find_venv(start: Path) -> Path:
    """从 start 向上找第一个含 pyvenv.cfg 的 .venv / venv / env 目录。"""
    cur = start.resolve()
    for cand in [cur, *cur.parents]:
        for name in (".venv", "venv", "env"):
            v = cand / name
            if (v / "pyvenv.cfg").is_file():
                return v
    raise SystemExit(f"✗ 在 {start} 及其上级目录里没找到 .venv（缺 pyvenv.cfg）")


def _candidates(venv: Path) -> list[Path]:
    out = [venv / "pyvenv.cfg"]
    for scripts in ("bin", "Scripts"):
        d = venv / scripts
        if d.is_dir():
            out += [p for p in sorted(d.rglob("*")) if p.is_file()]
    lib = venv / "lib"
    if lib.is_dir():
        out += [p for p in sorted(lib.rglob("*"))
                if p.is_file() and p.name in ("direct_url.json",)
                or p.is_file() and p.suffix in (".pth", ".egg-link")]
    return [p for p in out if p.exists()]


def scan(venv: Path) -> tuple[dict[Path, list[tuple[bytes, bytes]]], list[Path]]:
    """→ ({需改写的文件: [(旧, 新)]}, [无法改写的二进制 shim])"""
    current = str(venv.resolve()).encode("utf-8")
    hits: dict[Path, list[tuple[bytes, bytes]]] = {}
    binary_shims: list[Path] = []
    for path in _candidates(venv):
        if path.is_symlink():
            continue
        if path.suffix in BINARY_SUFFIXES:
            try:
                if b".venv" in path.read_bytes():
                    binary_shims.append(path)
            except OSError:
                pass
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b".venv" not in data:
            continue
        uniq, seen = [], set()
        for m in VENV_PATH_RE.finditer(data):
            old = m.group(1)
            if old != current and old not in seen:
                seen.add(old)
                uniq.append((old, current))
        if uniq:
            hits[path] = uniq
    return hits, binary_shims


def main() -> int:
    ap = argparse.ArgumentParser(description="修复 .venv 内部残留的旧绝对路径")
    ap.add_argument("--venv", default="", help="venv 目录（默认从本文件位置向上查找）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只检查）")
    args = ap.parse_args()

    here = Path(__file__).resolve()
    venv = Path(args.venv).resolve() if args.venv else find_venv(here.parent)
    print(f"venv：{venv}")
    hits, binary_shims = scan(venv)

    for p in binary_shims:
        print(f"  ! 二进制 shim 含旧路径（无法文本改写，建议重建 venv）：{p.relative_to(venv)}")

    if not hits:
        print("✓ venv 内部路径已全部指向当前位置，无需修复")
        return 0

    print(f"{'将修复' if args.apply else '发现'} {len(hits)} 个文件：")
    for path, pairs in hits.items():
        olds = sorted({o.decode('utf-8', 'replace') for o, _ in pairs})
        print(f"  - {path.relative_to(venv)}  ← {', '.join(olds)}")

    if not args.apply:
        print("\n（dry-run：加 --apply 才会写入）")
        return 1

    for path, pairs in hits.items():
        data = path.read_bytes()
        for old, new in pairs:
            data = data.replace(old, new)
        mode = path.stat().st_mode
        if not mode & stat.S_IWUSR:          # pip/console 脚本可能是只读的
            os.chmod(path, mode | stat.S_IWUSR)
        path.write_bytes(data)
        os.chmod(path, mode)
    print(f"\n✓ 已修复 {len(hits)} 个文件")
    print("提示：IDE 的解释器路径仍需在 PyCharm → Settings → Project → Python Interpreter"
          " 重新选一次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
