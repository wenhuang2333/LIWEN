"""Path resolution between main app and repair EXE onedirs.

名称全部取自 `utils/constants.py`（单一来源），并保留对上一代英文名
`ArchiveTool` 的查找回退 —— 老版本安装目录仍叫 `ArchiveTool/`，
升级后修复工具必须还能找到主程序。
"""
import os
import sys

from utils.constants import (
    APP_NAME_SLUG,
    APP_REPAIR_SLUG,
    LEGACY_APP_REPAIR_SLUG,
    LEGACY_APP_SLUG,
)
from utils.paths import dist_dir

REPAIR_ONEDIR_NAME = APP_REPAIR_SLUG
MAIN_ONEDIR_NAME = APP_NAME_SLUG

# 旧命名（仅用于查找回退）
_LEGACY_REPAIR_ONEDIR = LEGACY_APP_REPAIR_SLUG
_LEGACY_MAIN_ONEDIR = LEGACY_APP_SLUG

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
REPAIR_EXE_NAME = f"{APP_REPAIR_SLUG}{_EXE_SUFFIX}"
MAIN_EXE_NAME = f"{APP_NAME_SLUG}{_EXE_SUFFIX}"


def _find_executable(dir_path: str, base_name: str) -> str:
    """Find executable in dir_path, handling platform-specific suffix."""
    candidates = []
    if sys.platform == "win32":
        candidates = [f"{base_name}.exe"]
    else:
        candidates = [base_name, f"{base_name}.exe"]
    for c in candidates:
        p = os.path.join(dir_path, c)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return os.path.join(dir_path, candidates[0])


def _locate(base_dir: str, onedir: str, legacy_onedir: str, exe_base: str,
            legacy_exe_base: str) -> str:
    """在 base_dir 下按 新名称 → 旧名称 的顺序查找可执行文件。

    返回第一个真实存在的路径；都不存在时返回新名称对应的路径
    （调用方据此报错，提示的是当前期望的名字）。
    """
    for name, exe in ((onedir, exe_base), (legacy_onedir, legacy_exe_base)):
        candidate = os.path.join(base_dir, name)
        if not os.path.isdir(candidate):
            continue
        found = _find_executable(candidate, exe)
        if os.path.isfile(found):
            return found
    return _find_executable(os.path.join(base_dir, onedir), exe_base)


def find_repair_exe() -> str:
    """Return the absolute path to the repair executable."""
    if getattr(sys, "frozen", False):
        install_base = os.path.dirname(os.path.dirname(sys.executable))
    else:
        install_base = dist_dir()
    return _locate(install_base, REPAIR_ONEDIR_NAME, _LEGACY_REPAIR_ONEDIR,
                   REPAIR_ONEDIR_NAME, _LEGACY_REPAIR_ONEDIR)


def find_main_exe() -> str:
    """Return the absolute path to the main executable."""
    if getattr(sys, "frozen", False):
        install_base = os.path.dirname(os.path.dirname(sys.executable))
    else:
        install_base = dist_dir()
    return _locate(install_base, MAIN_ONEDIR_NAME, _LEGACY_MAIN_ONEDIR,
                   MAIN_ONEDIR_NAME, _LEGACY_MAIN_ONEDIR)
