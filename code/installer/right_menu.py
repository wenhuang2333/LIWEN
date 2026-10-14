"""
Windows 右键菜单注册/卸载工具
以管理员权限运行此脚本：
  python right_menu.py install    — 安装右键菜单
  python right_menu.py uninstall  — 卸载右键菜单
"""
import sys
import os

if sys.platform != "win32":
    print("右键菜单功能仅在 Windows 上可用")
    sys.exit(0)

import winreg


MENU_NAME = "归档工具"
COMPRESS_LABEL = "压缩到此文件夹"
DECOMPRESS_LABEL = "解压到当前文件夹"

REG_PATHS = {
    "*": r"*\shell",
    "directory": r"Directory\shell",
    "archive": r"SystemFileAssociations\.zip\shell",
}


def get_exe_path():
    # installer/ 位于 code/ 下一级 → 上溯两级即项目根目录
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "dist", "ArchiveTool.exe")


def install():
    exe = get_exe_path()
    if not os.path.exists(exe):
        print(f"错误: 找不到 {exe}，请先运行 build.py 生成可执行文件")
        return

    for name, reg_path in REG_PATHS.items():
        try:
            if name == "*":
                key_path = f"{reg_path}\\ArchiveToolCompress"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, COMPRESS_LABEL)
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, exe)
                cmd_path = f"{key_path}\\command"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, cmd_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" compress "%1"')
                print(f"已安装: {name} → {COMPRESS_LABEL}")
            elif name == "directory":
                key_path = f"{reg_path}\\ArchiveToolCompress"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, COMPRESS_LABEL)
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, exe)
                cmd_path = f"{key_path}\\command"
                with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, cmd_path) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" compress "%1"')
                print(f"已安装: {name} → {COMPRESS_LABEL}")
            elif name == "archive":
                key_path = f"{reg_path}\\ArchiveToolDecompress"
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


def uninstall():
    for name, reg_path in REG_PATHS.items():
        try:
            if name == "*" or name == "directory":
                full = f"{reg_path}\\ArchiveToolCompress"
                try:
                    winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, f"{full}\\command")
                except FileNotFoundError:
                    pass
                try:
                    winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, full)
                except FileNotFoundError:
                    pass
            elif name == "archive":
                full = f"{reg_path}\\ArchiveToolDecompress"
                try:
                    winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, f"{full}\\command")
                except FileNotFoundError:
                    pass
                try:
                    winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, full)
                except FileNotFoundError:
                    pass
        except PermissionError:
            print("权限不足，请以管理员身份运行此脚本")
            return
    print("右键菜单已卸载")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python right_menu.py install|uninstall")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    else:
        print(f"未知命令: {cmd}")
