"""
帽兜工具 — 构建脚本
用法 (在项目根目录或 code/ 下均可执行):
  python code/build.py app             仅构建主程序 (onedir)
  python code/build.py repair          仅构建修复工具 (onedir)
  python code/build.py installer       构建主程序 + 修复工具 + 打包 + 生成安装程序 (仅 Windows)
  python code/build.py setup           仅打包已有 onedirs 为安装程序 (不重新构建, 仅 Windows)
  python code/build.py dmg             构建 macOS .dmg (仅 macOS)
  python code/build.py tarball         构建 Linux 便携 .tar.gz
  python code/build.py all [windows|macos|linux]
                                  完整构建 (app + repair + 指定的平台包)。
                                  无参数时依次尝试构建三个平台的包
                                  (当前 OS 对应的包直接构建，其他平台仅提示)。
  python code/build.py clean           清理构建产物
"""
import os
import sys
import shutil
import subprocess
import lzma
import tarfile

# ── 目录布局: 源码在 code/, 构建产物与静态资源位于项目根目录 ──
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CODE_DIR)
MAIN_FILE = os.path.join(CODE_DIR, "main.py")
REPAIR_SCRIPT = os.path.join(CODE_DIR, "repair_tool.py")
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build")
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
# 应用标识统一来自 utils/constants.py（中文名「帽兜工具」/ 英文名「LIWEN Tool」）
sys.path.insert(0, CODE_DIR)
from utils.constants import (  # noqa: E402
    APP_NAME_SLUG as APP_NAME,
    APP_NAME_ZH,
    APP_VENDOR_SLUG,
    APP_VERSION,
    APP_REPAIR_SLUG as REPAIR_NAME,
    APP_SETUP_SLUG as SETUP_NAME,
    LEGACY_APP_SLUG,
    LEGACY_APP_REPAIR_SLUG,
)


def _clean():
    for path in [DIST_DIR, BUILD_DIR]:
        if os.path.exists(path):
            shutil.rmtree(path)
    for spec in [f for f in os.listdir(PROJECT_ROOT) if f.endswith(".spec")]:
        os.remove(os.path.join(PROJECT_ROOT, spec))
    print("构建产物已清理")


def _looks_like_ico(path: str) -> bool:
    """校验文件是否真的是 Windows ICO。

    只判断"文件存在"是不够的：本仓库里 ``static/logo.ico`` 曾经就是一个
    改名的 PNG，PyInstaller 会因此静默打出没有图标的 exe（或直接报错）。
    这里按魔数校验：ICONDIR 为 ``00 00 01 00`` + 数量字段。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(6)
        return (len(head) == 6 and head[0:2] == b"\x00\x00"
                and head[2:4] == b"\x01\x00")
    except OSError:
        return False


def _generate_icon(png_path: str) -> str:
    """把 logo 转成正方形多尺寸 ICO。返回是否成功（写入 png_path）。"""
    try:
        from PIL import Image
    except ImportError:
        print("警告: 未安装 Pillow，无法生成图标。运行: pip install Pillow")
        return False
    try:
        src = Image.open(os.path.join(STATIC_DIR, "logo.jpg")).convert("RGBA")
        # 源图非正方形时先补透明边，否则 Pillow 会生成 16x12 这类非方形图标，
        # Windows 任务栏/资源管理器会显示变形。
        side = max(src.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(src, ((side - src.width) // 2, (side - src.height) // 2))
        Image.Image.save(
            canvas.resize((512, 512), Image.LANCZOS), png_path,
            format="ICO",
            sizes=[(256, 256), (128, 128), (64, 64),
                   (48, 48), (32, 32), (16, 16)])
        return True
    except (OSError, ValueError) as e:
        print(f"警告: 生成 .ico 失败: {e}")
        return False


def _icon_path():
    """Return platform-appropriate icon path for PyInstaller."""
    logo_src = os.path.join(STATIC_DIR, "logo.jpg")
    if not os.path.exists(logo_src):
        return None

    if sys.platform == "win32":
        logo_ico = os.path.join(STATIC_DIR, "logo.ico")
        # 内容不对（例如是改名的 PNG）也必须重新生成，而不是直接采用
        if not _looks_like_ico(logo_ico):
            if os.path.exists(logo_ico):
                print(f"检测到无效的 .ico（魔数不符），重新生成: {logo_ico}")
            if _generate_icon(logo_ico):
                print("已从 logo.jpg 生成 logo.ico")
        return logo_ico if _looks_like_ico(logo_ico) else None
    elif sys.platform == "darwin":
        logo_png = os.path.join(STATIC_DIR, "logo.png")
        if not os.path.exists(logo_png):
            _generate_icon_png(logo_png)
        return logo_png if os.path.exists(logo_png) else None
    else:
        logo_png = os.path.join(STATIC_DIR, "logo.png")
        if not os.path.exists(logo_png):
            _generate_icon_png(logo_png)
        return logo_png if os.path.exists(logo_png) else None


def _generate_icon_png(png_path: str) -> bool:
    """png 平台图标（macOS/Linux）。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        Image.open(os.path.join(STATIC_DIR, "logo.jpg")).convert("RGBA").save(
            png_path, format="PNG")
        return True
    except (OSError, ValueError):
        return False


def _pyinstaller_cmd(script, name, onefile=True, extra_data=None,
                     extra_binaries=None, hidden_imports=None, excludes=None,
                     console=False, upx=True):
    cmd = [sys.executable, "-m", "PyInstaller"]
    cmd += ["--onefile"] if onefile else ["--onedir"]
    cmd += ["--windowed"] if not console else []
    # 不再请求管理员权限：联机已改为纯应用层组网，不需要虚拟网卡/驱动，
    # 应用本体没有任何需要提权的操作（右键菜单由安装程序单独以管理员身份注册）。
    cmd += ["--name", name, "--clean", "--noconfirm"]
    if not upx:
        cmd += ["--noupx"]

    # 源码位于 code/ 下, 显式指定模块搜索路径;
    # 构建产物固定落在项目根目录, 避免污染 code/。
    cmd += ["--paths", CODE_DIR]
    cmd += ["--distpath", DIST_DIR]
    cmd += ["--workpath", BUILD_DIR]
    cmd += ["--specpath", PROJECT_ROOT]

    icon = _icon_path()
    if icon:
        cmd += ["--icon", icon]

    for imp in (hidden_imports or []):
        cmd += ["--hidden-import", imp]

    for exc in (excludes or []):
        cmd += ["--exclude-module", exc]

    for data in (extra_data or []):
        cmd += ["--add-data", data]

    for binary in (extra_binaries or []):
        cmd += ["--add-binary", binary]

    cmd.append(script)
    return cmd


def _strip_onedir(onedir: str):
    """Remove unnecessary files from the onedir to reduce size."""
    removed_size = 0
    removed_count = 0

    # Qt WebEngine translations — keep only zh-CN and en-US
    locales_dir = os.path.join(onedir, "PyQt6", "Qt6", "translations")
    qt_locales = os.path.join(onedir, "qtwebengine_locales")
    for loc_base in [locales_dir, qt_locales]:
        if not os.path.isdir(loc_base):
            continue
        for f in list(os.listdir(loc_base)):
            fp = os.path.join(loc_base, f)
            if not os.path.isfile(fp):
                continue
            if f.startswith("qtwebengine") and f.endswith(".pak"):
                base = f.replace("qtwebengine_", "").replace(".pak", "")
                if base not in ("zh-CN", "en-US"):
                    removed_size += os.path.getsize(fp)
                    removed_count += 1
                    os.remove(fp)
            if f.endswith(".qm"):
                base = os.path.splitext(f)[0]
                try:
                    parts = base.split("_", 1)
                    if len(parts) == 2:
                        lang = parts[1]
                        if lang not in ("zh", "en", "zh_CN", "en_US"):
                            removed_size += os.path.getsize(fp)
                            removed_count += 1
                            os.remove(fp)
                except Exception:
                    pass

    for root, dirs, files in os.walk(onedir):
        for d in list(dirs):
            if d == "__pycache__":
                dp = os.path.join(root, d)
                try:
                    for f in os.listdir(dp):
                        fp = os.path.join(dp, f)
                        removed_size += os.path.getsize(fp)
                        removed_count += 1
                        os.remove(fp)
                    os.rmdir(dp)
                except Exception:
                    pass

        for f in files:
            fp = os.path.join(root, f)
            if f.endswith((".lib", ".a", ".pdb", ".ilk", ".exp")):
                removed_size += os.path.getsize(fp)
                removed_count += 1
                os.remove(fp)

    print(f"已清理 {removed_count} 个冗余文件 ({removed_size / (1024*1024):.1f} MB)")
    return removed_size


def _create_combined_tarxz(main_onedir: str, repair_onedir: str) -> str:
    """Create tar.xz containing BOTH onedirs (main + repair). Returns path."""
    import tempfile
    tarball = os.path.join(DIST_DIR, f"{APP_NAME}.tar.xz")
    print(f"正在创建 {tarball} (包含主程序 + 修复工具, LZMA2 极限压缩)...")

    with tempfile.TemporaryDirectory() as tmpdir:
        print("  复制主程序…")
        shutil.copytree(main_onedir, os.path.join(tmpdir, APP_NAME))
        print("  复制修复工具…")
        shutil.copytree(repair_onedir, os.path.join(tmpdir, REPAIR_NAME))

        total_files = sum(1 for _ in _walk_files(tmpdir))
        processed = 0

        with lzma.open(tarball, "w",
                       preset=9 | lzma.PRESET_EXTREME,
                       format=lzma.FORMAT_XZ,
                       check=lzma.CHECK_SHA256) as f:
            with tarfile.open(fileobj=f, mode="w") as tf:
                for root, dirs, files in os.walk(tmpdir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        arcname = os.path.relpath(full, tmpdir)
                        tf.add(full, arcname)
                        processed += 1
                        if processed % 100 == 0 or processed == total_files:
                            pct = processed * 100 // total_files
                            print(f"\r  进度: {pct}% ({processed}/{total_files})",
                                  end="", flush=True)
        print()

    size_mb = os.path.getsize(tarball) / (1024 * 1024)
    print(f"打包完成: {tarball} ({size_mb:.1f} MB)")
    return tarball


def _walk_files(base: str):
    for root, dirs, files in os.walk(base):
        for fn in files:
            yield os.path.join(root, fn)


def _report_onedir_size(onedir: str):
    total = sum(os.path.getsize(os.path.join(r, fn))
                for r, ds, fs in os.walk(onedir) for fn in fs)
    print(f"Onedir 总大小: {total / (1024*1024):.0f} MB")
    return total


def build_app():
    """Build the main application as a directory (onedir)."""
    print("=" * 50)
    print("构建主程序 (onedir)...")
    print("=" * 50)

    _clean()

    extra_data = [
        f"{STATIC_DIR}{os.pathsep}static",
    ]

    _unused_qt_modules = [
        "PyQt5",
        "PyQt6.QtBluetooth", "PyQt6.QtDBus", "PyQt6.QtDesigner",
        "PyQt6.QtHelp", "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtNfc", "PyQt6.QtOpenGLWidgets", "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets", "PyQt6.QtPositioning", "PyQt6.QtPrintSupport",
        "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuick3D",
        "PyQt6.QtQuickWidgets", "PyQt6.QtRemoteObjects", "PyQt6.QtSensors",
        "PyQt6.QtSerialPort", "PyQt6.QtSpatialAudio", "PyQt6.QtSql",
        "PyQt6.QtStateMachine", "PyQt6.QtSvg", "PyQt6.QtSvgWidgets",
        "PyQt6.QtTest", "PyQt6.QtTextToSpeech", "PyQt6.QtUiTools",
        "PyQt6.QtWebSockets", "PyQt6.QtXml",
    ]

    cmd = _pyinstaller_cmd(
        MAIN_FILE, APP_NAME,
        onefile=False,
        hidden_imports=[
            "Crypto", "Crypto.Cipher.AES", "Crypto.Protocol.KDF",
            "Crypto.Hash.SHA256", "tarfile", "gzip", "bz2", "lzma",
            "sounddevice",
            "PyQt6.QtWebChannel",
        ],
        excludes=_unused_qt_modules,
        extra_data=extra_data,
    )
    print(f"命令: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)

    onedir_output = os.path.join(DIST_DIR, APP_NAME)
    print(f"\n主程序构建完成: {onedir_output}")

    print("正在清理冗余文件...")
    _strip_onedir(onedir_output)
    _report_onedir_size(onedir_output)

    return onedir_output


def build_repair():
    """Build the repair tool as a separate onedir."""
    print("=" * 50)
    print("构建修复工具 (onedir)...")
    print("=" * 50)

    extra_data = [
        f"{STATIC_DIR}{os.pathsep}static",
    ]

    _repair_excludes = [
        "PyQt5",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineQuick",
        "PyQt6.QtBluetooth", "PyQt6.QtDBus", "PyQt6.QtDesigner",
        "PyQt6.QtHelp", "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtNfc", "PyQt6.QtOpenGLWidgets", "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets", "PyQt6.QtPositioning", "PyQt6.QtPrintSupport",
        "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuick3D",
        "PyQt6.QtQuickWidgets", "PyQt6.QtRemoteObjects", "PyQt6.QtSensors",
        "PyQt6.QtSerialPort", "PyQt6.QtSpatialAudio", "PyQt6.QtSql",
        "PyQt6.QtStateMachine", "PyQt6.QtSvg", "PyQt6.QtSvgWidgets",
        "PyQt6.QtTest", "PyQt6.QtTextToSpeech", "PyQt6.QtUiTools",
        "PyQt6.QtWebSockets", "PyQt6.QtXml",
    ]

    cmd = _pyinstaller_cmd(
        REPAIR_SCRIPT, REPAIR_NAME,
        onefile=False,
        hidden_imports=[
            "Crypto", "Crypto.Cipher.AES", "Crypto.Protocol.KDF",
            "Crypto.Hash.SHA256", "tarfile", "gzip", "bz2", "lzma",
            "sounddevice",
            "PyQt6.QtWebChannel",
        ],
        excludes=_repair_excludes,
        extra_data=extra_data,
    )
    print(f"命令: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)

    onedir = os.path.join(DIST_DIR, REPAIR_NAME)
    print(f"\n修复工具构建完成: {onedir}")

    print("正在清理冗余文件...")
    _strip_onedir(onedir)
    _report_onedir_size(onedir)

    return onedir


# ---- Windows installer ----

def _build_setup_from_onedir(main_onedir: str, repair_onedir: str):
    """Package main + repair onedirs -> tar.xz -> setup.exe (Windows only)."""
    tarxz_path = _create_combined_tarxz(main_onedir, repair_onedir)
    setup_script = os.path.join(CODE_DIR, "installer", "setup_tk.py")

    cmd = _pyinstaller_cmd(
        setup_script, SETUP_NAME,
        onefile=True,
        console=False,
        hidden_imports=["installer.shortcut", "tarfile", "lzma", "tkinter"],
        excludes=["PyQt5", "PyQt6"],
        extra_data=[f"{tarxz_path}{os.pathsep}."],
    )
    print(f"命令: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)

    setup_exe = os.path.join(DIST_DIR, f"{SETUP_NAME}.exe")
    setup_size_mb = os.path.getsize(setup_exe) / (1024 * 1024)
    print(f"\n安装程序构建完成: {setup_exe} ({setup_size_mb:.1f} MB)")
    print(f"分发文件: {setup_exe}")
    return setup_exe


def build_setup():
    """Build installer from existing onedirs (Windows only)."""
    if sys.platform != "win32":
        print("setup/installer 构建仅支持 Windows")
        return None

    main_onedir = os.path.join(DIST_DIR, APP_NAME)
    repair_onedir = os.path.join(DIST_DIR, REPAIR_NAME)
    if not os.path.isdir(main_onedir):
        print(f"错误: 未找到主程序 onedir: {main_onedir}")
        print("请先运行 python code/build.py app 构建主程序")
        sys.exit(1)
    if not os.path.isdir(repair_onedir):
        print(f"错误: 未找到修复工具 onedir: {repair_onedir}")
        print("请先运行 python code/build.py repair 构建修复工具")
        sys.exit(1)

    return _build_setup_from_onedir(main_onedir, repair_onedir)


def build_installer():
    """Build the main app + repair tool + installer exe (Windows only)."""
    if sys.platform != "win32":
        print("installer 构建仅支持 Windows")
        return None

    main_onedir = os.path.join(DIST_DIR, APP_NAME)
    repair_onedir = os.path.join(DIST_DIR, REPAIR_NAME)

    if not os.path.isdir(main_onedir):
        main_onedir = build_app()
    if not os.path.isdir(repair_onedir):
        repair_onedir = build_repair()

    return _build_setup_from_onedir(main_onedir, repair_onedir)


# ---- macOS .dmg ----

def build_dmg():
    """Build macOS .dmg from onedir."""
    if sys.platform != "darwin":
        print("DMG 构建仅支持 macOS")
        return None

    onedir = os.path.join(DIST_DIR, APP_NAME)
    if not os.path.isdir(onedir):
        print("未找到 onedir，请先运行 python code/build.py app")
        return None

    dmg_path = os.path.join(DIST_DIR, f"{APP_NAME}.dmg")
    print(f"正在创建 {dmg_path}...")

    # Create a temporary .app bundle
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        app_bundle = os.path.join(tmpdir, f"{APP_NAME}.app")
        os.makedirs(os.path.join(app_bundle, "Contents", "MacOS"), exist_ok=True)
        os.makedirs(os.path.join(app_bundle, "Contents", "Resources"), exist_ok=True)

        # Copy onedir into app bundle
        dest = os.path.join(app_bundle, "Contents", "MacOS", APP_NAME)
        shutil.copytree(onedir, dest, symlinks=True)

        # Create launcher script
        launcher = os.path.join(app_bundle, "Contents", "MacOS", "launch")
        # 显式 utf-8：plist 声明的是 UTF-8，且脚本里含中文应用名；
        # 不写 encoding 时 Windows 上会用 ANSI 代码页，导致 .app 元数据乱码。
        with open(launcher, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n")
            f.write(f'DIR="$(cd "$(dirname "$0")" && pwd)"\n')
            f.write(f'exec "$DIR/{APP_NAME}/{APP_NAME}" "$@"\n')
        os.chmod(launcher, 0o755)

        # Create minimal Info.plist
        plist = os.path.join(app_bundle, "Contents", "Info.plist")
        with open(plist, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n')
            f.write('<plist version="1.0">\n<dict>\n')
            f.write(f'    <key>CFBundleExecutable</key>\n    <string>launch</string>\n')
            f.write(f'    <key>CFBundleName</key>\n    <string>{APP_NAME}</string>\n')
            f.write(f'    <key>CFBundleDisplayName</key>\n    <string>{APP_NAME_ZH}</string>\n')
            bundle_id = f"com.{APP_VENDOR_SLUG.lower()}.{APP_NAME.lower()}"
            f.write(f'    <key>CFBundleIdentifier</key>\n    <string>{bundle_id}</string>\n')
            # 版本号同样单一来源：Finder「显示简介」与「关于本机」读的就是这两项。
            # 以前这里写死 1.0，导致 .app 的版本永远不是 APP_VERSION。
            f.write(f'    <key>CFBundleShortVersionString</key>\n    <string>{APP_VERSION}</string>\n')
            f.write(f'    <key>CFBundleVersion</key>\n    <string>{APP_VERSION}</string>\n')
            f.write('    <key>CFBundlePackageType</key>\n    <string>APPL</string>\n')
            f.write('</dict>\n</plist>\n')

        # Create .dmg
        subprocess.run(["hdiutil", "create", "-volname", APP_NAME,
                        "-srcfolder", tmpdir, "-ov", dmg_path], check=True)

    size_mb = os.path.getsize(dmg_path) / (1024 * 1024)
    print(f"DMG 构建完成: {dmg_path} ({size_mb:.1f} MB)")
    return dmg_path


# ---- Linux portable tarball ----

def build_tarball():
    """Build portable .tar.gz for Linux distribution."""
    onedir = os.path.join(DIST_DIR, APP_NAME)
    if not os.path.isdir(onedir):
        print("未找到 onedir，请先运行 python code/build.py app")
        return None

    tarball = os.path.join(DIST_DIR, f"{APP_NAME}.tar.gz")
    print(f"正在创建 {tarball}...")

    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(onedir, arcname=APP_NAME)

    size_mb = os.path.getsize(tarball) / (1024 * 1024)
    print(f"Tarball 构建完成: {tarball} ({size_mb:.1f} MB)")
    return tarball


# ---- Main ----

def _cmd_all():
    """Handle `python code/build.py all [windows|macos|linux]`."""
    os_target = None
    if len(sys.argv) >= 3:
        os_target = sys.argv[2].lower()
        if os_target not in ("windows", "macos", "linux"):
            print(f"错误: 未知的操作系统 '{sys.argv[2]}'，可选: windows, macos, linux")
            print(__doc__)
            return

    current_os = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")

    # Build app + repair (shared across all platforms)
    build_app()
    build_repair()

    if os_target:
        # User specified a single OS target
        _build_platform_package(os_target, current_os)
    else:
        # No target specified: build all three
        print("\n" + "=" * 60)
        print("  无 OS 参数 — 将尝试构建全部三个平台的安装包")
        print("  PyInstaller 只能为当前操作系统构建，其他平台仅提示")
        print("=" * 60)
        for target in ("windows", "macos", "linux"):
            print()
            _build_platform_package(target, current_os)


def _build_platform_package(target: str, current_os: str):
    """Build the platform-specific package for `target`, warning if cross-build."""
    if target == current_os:
        print(f"\n--- 构建 {target} 包 (当前平台) ---")
        if target == "windows":
            build_installer()
        elif target == "macos":
            build_dmg()
        else:
            build_tarball()
    else:
        print(f"\n--- 跳过 {target} 包 ---")
        print(f"  ⚠ PyInstaller 只能在当前平台 ({current_os}) 构建 {target} 的包。")
        if target == "windows":
            print(f"  请在 Windows 上运行 `python code/build.py all windows` 或 `python code/build.py installer`")
        elif target == "macos":
            print(f"  请在 macOS 上运行 `python code/build.py all macos` 或 `python code/build.py dmg`")
        else:
            print(f"  请在 Linux 上运行 `python code/build.py all linux` 或 `python code/build.py tarball`")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "clean":
        _clean()
    elif cmd == "app":
        build_app()
    elif cmd == "repair":
        build_repair()
    elif cmd == "setup":
        build_setup()
    elif cmd == "installer":
        build_installer()
    elif cmd == "dmg":
        build_dmg()
    elif cmd == "tarball":
        build_tarball()
    elif cmd == "all":
        _cmd_all()
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
