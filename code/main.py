import sys
import os
import ctypes
import traceback

from utils.paths import data_dir
from utils.settings import settings
from utils.logger import setup_logging, info, error as log_error


def _setup_console():
    """Attach to parent console for CLI output in frozen --windowed builds.

    PyInstaller --windowed builds discard stdout/stderr entirely.
    When the user runs 'LIWENTool.exe compress ...' from cmd.exe,
    we attach to that console.  When double-clicked with arguments
    (rare), we allocate a fresh console so output is still visible.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        # ATTACH_PARENT_PROCESS = -1
        if not kernel32.AttachConsole(-1):
            kernel32.AllocConsole()
        sys.stdout = open('CONOUT$', 'w', encoding='utf-8', buffering=1)
        sys.stderr = open('CONOUT$', 'w', encoding='utf-8', buffering=1)
    except Exception:
        # 拿不到控制台不应该影响 CLI 本身（参数解析、退出码仍然有效）
        pass


def main():
    # ── CLI mode: if arguments are provided, run CLI without importing Qt ──
    if len(sys.argv) > 1:
        _setup_console()
        setup_logging()
        info("CLI 模式启动")
        try:
            settings.load()
        except Exception:
            pass
        from cli import run_cli
        exit_code = run_cli(sys.argv[1:])
        from utils.logger import close as log_close
        log_close()
        sys.exit(exit_code)

    # ── GUI mode ──
    if sys.platform == "win32":
        os.environ["QT_QPA_PLATFORM"] = "windows:darkmode=0"
    elif sys.platform == "darwin":
        os.environ["QT_QPA_PLATFORM"] = "cocoa"

    from PyQt6.QtWidgets import QApplication
    from utils.logger import install_crash_handler
    from ui.main_window import MainWindow
    from ui.about_page import APP_NAME

    setup_logging()
    info("应用启动")

    # Load persisted settings before creating the UI
    try:
        settings.load()
        info("设置加载完成")
    except Exception as e:
        log_error(f"设置加载失败: {e}")

    _set_app_user_model_id()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    install_crash_handler()

    # System-level app identity (taskbar, task manager, etc.)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    icon = _app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    info("创建主窗口")
    try:
        window = MainWindow()
    except Exception as e:
        # 主窗口构造失败：给出可读的错误和日志位置，而不是一闪而退
        from PyQt6.QtWidgets import QMessageBox
        tb = traceback.format_exc()
        log_error(f"主窗口创建失败: {e}\n{tb}")
        QMessageBox.critical(
            None, "启动失败",
            f"主窗口初始化失败，程序无法启动。\n\n"
            f"错误类型: {type(e).__name__}\n错误信息: {e}\n\n"
            f"详细日志已保存到:\n{os.path.join(data_dir(), 'log.txt')}")
        return 1
    window.show()

    info("进入事件循环")
    exit_code = app.exec()
    info(f"事件循环结束 (exit={exit_code})")
    # 关闭前清理：停止所有仍在运行的工作线程与网络对象，避免
    # "QThread: Destroyed while thread is still running" 崩溃
    try:
        window.shutdown()
    except Exception as e:
        log_error(f"关闭清理失败: {e}")
    from utils.logger import close as _log_close
    _log_close()
    return exit_code


def _resolve_static_dir():
    """Resolve the static/ directory, supporting both source and PyInstaller frozen runs."""
    from utils.paths import static_dir
    return static_dir()


def _app_icon():
    from PyQt6.QtGui import QIcon
    logo = os.path.join(_resolve_static_dir(), "logo.jpg")
    if os.path.exists(logo):
        return QIcon(logo)
    return QIcon()


def _set_app_user_model_id():
    """Set the Windows AppUserModelID so the taskbar shows the correct icon.

    Must be called BEFORE QApplication is created, otherwise Windows will
    use a default/python icon and ignore the window icon set later.
    """
    if sys.platform != "win32":
        return
    try:
        from utils.constants import APP_VENDOR_SLUG, APP_NAME_SLUG
        # 由常量派生，避免改名后这里残留旧标识；保持纯 ASCII（Windows 要求）
        app_id = f"{APP_VENDOR_SLUG}.{APP_NAME_SLUG}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
