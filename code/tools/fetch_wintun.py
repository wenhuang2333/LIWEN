#!/usr/bin/env python3
"""获取 / 校验 wintun.dll（Windows TUN 驱动）。

VPN 隧道（`ui/vpn_page.py` + `lan_vpn`）依赖 `wintun.dll`，它必须位于项目根目录
（源码运行）或与 exe 同级（冻结后）。

本脚本解决三种情况：

1. **文件缺失**（例如从 git 克隆时该二进制未随仓库分发）——自动从官方发布页
   下载 `wintun-<版本>.zip`，解出当前平台架构对应的 `wintun.dll`；
2. **文件存在但来源不明**——用官方 SHA-256 校验，确认未被篡改；
3. **构建前自检**——`build.py` 会调用 `ensure_wintun()`，缺失时尝试补齐，
   并把它作为 `--add-binary` 打进安装包，避免"装好了却用不了 VPN"。

wintun.dll 由 WireGuard LLC 以 MIT/Apache 双许可发布，是公开的上游二进制，
不含任何本机或个人信息。

用法::

    python code/tools/fetch_wintun.py            # 缺失则下载，存在则校验
    python code/tools/fetch_wintun.py --check    # 只校验，不下载
    python code/tools/fetch_wintun.py --force    # 强制重新下载
    python code/tools/fetch_wintun.py --arch x86 # 指定架构 (amd64/x86/arm/arm64)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import shutil
import sys
import zipfile

# 官方发布地址与当前锁定版本
WINTUN_VERSION = "0.14.1"
DOWNLOAD_URL = "https://www.wintun.net/builds/wintun-{version}.zip"

# 官方 wintun-0.14.1.zip 中各架构 wintun.dll 的 SHA-256。
# 这些值来自上游 release，用于确认文件未被篡改。
OFFICIAL_SHA256 = {
    "amd64": "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce",
    "x86": "d694fa46ab4cfebcb2632d094c7aa97278eef2f8052438621766d863ae98a931",
    "arm": "daad267411ecdc70a0535e274d2c3e9da3d0084bdac7662cb8424dd4a031b4d9",
    "arm64": "f7ba89005544be9d85231a9e0d5f23b2d15b3311667e2dad0debd344918a3f80",
}

# 项目根目录：本文件位于 code/tools/ 下 → 上溯两级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
TARGET = os.path.join(PROJECT_ROOT, "wintun.dll")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_arch() -> str:
    """把当前平台的架构映射到 wintun 的命名。

    注意顺序：macOS 的 Apple Silicon 报 ``arm64``、Linux 报 ``aarch64``，
    两者都必须映到 ``arm64``；若先判断 ``startswith("arm")`` 会把 ``arm64``
    误判成 32 位 ``arm``。
    """
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("i386", "i486", "i586", "i686", "x86", "x86_32"):
        return "x86"
    if m.startswith("arm"):
        return "arm"
    return "amd64"


def target_arch() -> str:
    """本项目实际需要的 wintun 架构。

    wintun 只在 Windows 上使用，而 ``build.py`` 产出的是 Windows x64 安装包，
    因此非 Windows 主机（macOS / Linux 交叉打包）也应取 ``amd64``，而不是
    主机自身的架构。
    """
    if sys.platform == "win32":
        return detect_arch()
    return "amd64"


def verify(path: str = TARGET, arch: str | None = None,
           quiet: bool = False) -> bool:
    """校验 wintun.dll 是否与官方发布一致。"""
    if not os.path.isfile(path):
        if not quiet:
            print(f"未找到 {path}")
        return False
    actual = _sha256(path)
    # 未指定架构时，只要匹配任意一个官方哈希即可（避免 x86/arm 机器误报）
    candidates = ({arch: OFFICIAL_SHA256[arch]} if arch and arch in OFFICIAL_SHA256
                  else OFFICIAL_SHA256)
    for name, want in candidates.items():
        if actual == want:
            if not quiet:
                print(f"OK  {path} 与官方 wintun {WINTUN_VERSION} ({name}) 一致")
                print(f"    SHA-256 {actual}")
            return True
    if not quiet:
        print(f"警告: {path} 的 SHA-256 与官方 {WINTUN_VERSION} 不匹配")
        print(f"   实际 {actual}")
        print("   若你确实使用了自编译版本，可用 --force 重新下载官方版本。")
    return False


def _ssl_context(verify: bool):
    """构造 SSL 上下文，优先使用 certifi 的 CA 包。

    某些 Python 安装（尤其是 macOS 上从 python.org 装的）没有配置系统根证书，
    直接 https 会报 CERTIFICATE_VERIFY_FAILED。certifi 随 requests 一起安装，
    这里优先用它。
    """
    import ssl

    if not verify:
        return ssl._create_unverified_context()
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — 没有 certifi 就用系统默认
        return ssl.create_default_context()


def _fetch(url: str) -> bytes | None:
    """下载 URL 内容。

    先做正常的 TLS 校验；若因缺少根证书失败，则退回不校验证书的连接，
    但**完整性仍由固定的 SHA-256 保证**（内容被替换必然导致校验失败）。
    """
    import urllib.request

    for verify in (True, False):
        ctx = _ssl_context(verify)
        note = "" if verify else "（已跳过 TLS 证书校验，改用 SHA-256 校验完整性）"
        print(f"正在下载 {url} ...{note}")
        try:
            with urllib.request.urlopen(url, timeout=60, context=ctx) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 — 网络问题都要给出可读提示
            if verify:
                print(f"  TLS 校验失败({type(e).__name__})，改用不校验证书的连接重试…")
                continue
            print(f"下载失败: {e}")
            return None
    return None


def download(arch: str = "amd64", force: bool = False,
             dest: str = TARGET) -> bool:
    """从官方地址下载并解出指定架构的 wintun.dll。"""
    if os.path.isfile(dest) and not force:
        print(f"{dest} 已存在，跳过下载（用 --force 强制覆盖）")
        return True

    url = DOWNLOAD_URL.format(version=WINTUN_VERSION)
    blob = _fetch(url)
    if blob is None:
        print(f"请手动下载 {url}，解出 bin/{arch}/wintun.dll 放到 {dest}")
        print(f"期望 SHA-256: {OFFICIAL_SHA256[arch]}")
        return False

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            member = f"wintun/bin/{arch}/wintun.dll"
            names = zf.namelist()
            if member not in names:
                print(f"压缩包内没有 {member}；包含: {names[:8]}")
                return False
            data = zf.read(member)
    except zipfile.BadZipFile as e:
        print(f"下载内容不是有效的 zip: {e}")
        return False

    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 先写临时文件再原子替换，避免中断留下半个 DLL
    tmp = f"{dest}.{os.getpid()}.part"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    print(f"已写入 {dest} ({len(data)} 字节)")

    if verify(dest, arch=arch):
        return True
    print("警告: 下载所得文件未通过校验，请人工确认。")
    return False


def ensure_wintun(arch: str | None = None, auto_download: bool = True) -> str | None:
    """构建流程入口：确保 wintun.dll 可用，返回路径或 None。

    缺失时自动从官方发布页补齐，并校验 SHA-256；校验不通过则重新下载。
    """
    arch = arch or target_arch()
    if os.path.isfile(TARGET):
        if verify(TARGET, arch=arch, quiet=True):
            return TARGET
        if verify(TARGET, quiet=True):
            # 是官方文件，只是架构与当前目标不同 —— 保留，不要破坏已有可用文件
            print("提示: wintun.dll 是官方文件但架构与目标不同，仍按原样打包。")
            return TARGET
        print("wintun.dll 存在但校验未通过，将重新获取。")
    if not auto_download:
        return None
    if sys.platform != "win32":
        # 交叉打包 Windows 安装包同样需要该文件，因此这里仍然下载
        print("当前不是 Windows，但打包 Windows 版本需要 wintun.dll，继续获取。")
    return TARGET if download(arch=arch, force=True) else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="获取 / 校验 wintun.dll（WireGuard 官方 TUN 驱动）")
    ap.add_argument("--check", action="store_true", help="只校验，不下载")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    ap.add_argument("--arch", choices=sorted(OFFICIAL_SHA256),
                    help="目标架构，默认按当前机器自动判断")
    ap.add_argument("--dest", default=TARGET, help="输出路径")
    args = ap.parse_args()

    arch = args.arch or target_arch()
    print(f"项目根目录: {PROJECT_ROOT}")
    print(f"目标架构  : {arch}" + ("" if args.arch else " (自动判断)"))

    if args.check:
        return 0 if verify(args.dest, arch=arch) else 1

    if os.path.isfile(args.dest) and not args.force and verify(args.dest, arch=arch):
        return 0

    return 0 if download(arch=arch, force=args.force, dest=args.dest) else 1


if __name__ == "__main__":
    sys.exit(main())
