"""
Windows 右键菜单注册/卸载工具
以管理员权限运行此脚本：
  python code/installer/right_menu.py install    — 安装右键菜单
  python code/installer/right_menu.py uninstall  — 卸载右键菜单

命名统一取自 utils/constants.py。卸载时会**同时清理上一代
（ArchiveTool）遗留的注册表项**，避免老用户升级后残留孤立菜单。
"""
import os
import sys

if sys.platform != "win32":
    print("右键菜单功能仅在 Windows 上可用")
    sys.exit(0)

import winreg

# 本脚本可能被直接执行，需先把 code/ 加入模块搜索路径
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from utils.constants import (  # noqa: E402
    APP_NAME_SLUG,
    APP_REPAIR_SLUG,  # noqa: F401  (保持导入以便未来扩展)
    LEGACY_APP_SLUG,
)
from utils.paths import dist_dir  # noqa: E402


MENU_NAME = "归档工具"
COMPRESS_LABEL = "压缩到此文件夹"
DECOMPRESS_LABEL = "解压到当前文件夹"

# 键名后缀：安装用新名，卸载时新旧都清
KEY_SLUGS = (APP_NAME_SLUG, LEGACY_APP_SLUG)

REG_PATHS = {
    "*": r"*\shell",
    "directory": r"Directory\shell",
    "archive": r"SystemFileAssociations\.zip\shell",
}

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def get_exe_path() -> str:
    """主程序 exe 路径（新名优先，回退旧名）。"""
    for name in (APP_NAME_SLUG, LEGACY_APP_SLUG):
        candidate = os.path.join(dist_dir(), f"{name}{_EXE_SUFFIX}")
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(dist_dir(), f"{APP_NAME_SLUG}{_EXE_SUFFIX}")


def _delete_tree(root, path: str) -> bool:
    """删除注册表键及其子键，不存在则静默跳过。返回是否删掉了东西。"""
    removed = False
    try:
        winreg.DeleteKey(root, f"{path}\\command")
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        pass
    try:
        winreg.DeleteKey(root, path)
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return removed


def install():
    exe = get_exe_path()
    if not os.path.exists(exe):
        print(f"错误: 找不到 {exe}，请先运行 build.py 生成可执行文件")
        return

    for name, reg_path in REG_PATHS.items():
        try:
            if name in ("*", "directory"):
                key_path = f"{reg_path}\\{APP_NAME_SLUG}Compress"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, COMPRESS_LABEL)
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, exe)
                cmd_path = f"{key_path}\\command"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, cmd_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" compress "%1"')
                print(f"已安装: {name} → {COMPRESS_LABEL}")
            elif name == "archive":
                key_path = f"{reg_path}\\{APP_NAME_SLUG}Decompress"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, DECOMPRESS_LABEL)
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, exe)
                cmd_path = f"{key_path}\\command"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, cmd_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" decompress "%1"')
                print(f"已安装: .zip → {DECOMPRESS_LABEL}")
        except PermissionError:
            print("权限不足，请以管理员身份运行此脚本")
            return

    # 顺手清掉旧名遗留项，避免升级后右键菜单出现重复条目
    cleaned = _cleanup_legacy()
    if cleaned:
        print(f"已清理 {cleaned} 个旧版（{LEGACY_APP_SLUG}）右键菜单项")


def _cleanup_legacy() -> int:
    """删除旧英文名留下的注册表项，返回清理数量。"""
    cleaned = 0
    for reg_path in REG_PATHS.values():
        for suffix in ("Compress", "Decompress"):
            if _delete_tree(winreg.HKEY_CLASSES_ROOT,
                            f"{reg_path}\\{LEGACY_APP_SLUG}{suffix}"):
                cleaned += 1
    return cleaned


def uninstall():
    for name, reg_path in REG_PATHS.items():
        try:
            if name in ("*", "directory"):
                _delete_tree(winreg.HKEY_CLASSES_ROOT,
                             f"{reg_path}\\{APP_NAME_SLUG}Compress")
            elif name == "archive":
                _delete_tree(winreg.HKEY_CLASSES_ROOT,
                             f"{reg_path}\\{APP_NAME_SLUG}Decompress")
        except PermissionError:
            print("权限不足，请以管理员身份运行此脚本")
            return

    cleaned = _cleanup_legacy()
    print("右键菜单已卸载" + (f"（另清理 {cleaned} 个旧版项）" if cleaned else ""))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python code/installer/right_menu.py install|uninstall")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    else:
        print(f"未知命令: {cmd}")
