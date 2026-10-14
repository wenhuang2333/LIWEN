"""Application-wide file logger.

Writes to ``data/log.txt`` (see :mod:`utils.paths`).  Each startup overwrites
the previous log.  Provides a global exception hook that logs crash details
and shows a popup before the process exits.

稳定性要点
----------
* ``setup_logging()`` **绝不抛异常**：日志文件打不开（目录只读、磁盘满）时
  退化为"只写 stderr"，而不是让程序起不来。
* 所有写入串行化（``threading.Lock``）：多个 QThread 同时写日志时不会交叉错乱。
* 缓冲刷新而非逐行 flush：日志是高频操作，逐行 flush 会在压缩/扫描等
  循环里把 GUI 线程卡在磁盘 I/O 上。现在按"时间间隔 + 级别"刷新，
  且 ``close()`` / 崩溃钩子一定 flush。
* 同时安装 ``threading.excepthook``：原实现只覆盖主线程，
  **工作线程里未捕获的异常会静默杀死线程**，UI 表现为进度条永远不动。
"""
import os
import sys
import threading
import time
import traceback
import atexit
from datetime import datetime
from contextlib import contextmanager

from utils.paths import data_dir

_LOG_PATH: str | None = None
_FILE = None
_LOCK = threading.RLock()
_LAST_FLUSH = 0.0

# 距上次 flush 超过该秒数就落盘；ERROR/FATAL 级别总是立即落盘
_FLUSH_INTERVAL = 0.5


def _app_dir() -> str:
    """可写数据目录（data/），日志即写入此处。"""
    return data_dir()


def setup_logging() -> str | None:
    """Initialize the log file (overwrites previous).

    Returns the log path, or ``None`` if the log file could not be opened —
    调用方不需要（也不能）为此处理异常。
    """
    global _LOG_PATH, _FILE, _LAST_FLUSH
    try:
        _LOG_PATH = os.path.join(data_dir(create=True), "log.txt")
        _FILE = open(_LOG_PATH, "w", encoding="utf-8")
    except OSError as e:
        # 关键：日志失败不能影响主流程
        _FILE = None
        print(f"[logger] 无法打开日志文件，日志将被丢弃: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None

    _LAST_FLUSH = time.monotonic()
    _write_raw(f"=== 帽兜工具 启动日志 {datetime.now().isoformat()} ===\n")
    _write_raw(f"Python: {sys.version}\n")
    _write_raw(f"可执行文件: {sys.executable}\n")
    _write_raw(f"工作目录: {os.getcwd()}\n")
    _write_raw(f"平台: {sys.platform}\n")
    _write_raw(f"Frozen: {getattr(sys, 'frozen', False)}\n\n")
    _install_faulthandler()
    return _LOG_PATH


def _install_faulthandler() -> None:
    """把原生崩溃（Qt / lxml 等 C 扩展的段错误）也写进日志。

    纯 Python 的 ``sys.excepthook`` 捕获不到段错误；没有 faulthandler 时，
    这类崩溃表现为"程序毫无征兆地消失"，日志里什么都没有。
    """
    try:
        import faulthandler
        if _FILE is not None and not faulthandler.is_enabled():
            faulthandler.enable(file=_FILE, all_threads=True)
    except Exception:
        pass


def _write_raw(text: str, force: bool = False):
    """线程安全地写入日志。任何失败都静默忽略——日志绝不能影响主流程。"""
    global _LAST_FLUSH
    if _FILE is None:
        return
    with _LOCK:
        try:
            _FILE.write(text)
            now = time.monotonic()
            if force or now - _LAST_FLUSH >= _FLUSH_INTERVAL:
                _FILE.flush()
                _LAST_FLUSH = now
        except Exception:
            pass


def log(level: str, message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    _write_raw(f"[{ts}] [{level}] {message}\n",
               force=level in ("ERROR", "FATAL"))


def info(message: str):
    log("INFO", message)


def warn(message: str):
    log("WARN", message)


def error(message: str):
    log("ERROR", message)


def exception(message: str):
    """记录当前异常的完整堆栈。用于 except 块内。"""
    log("ERROR", f"{message}\n{traceback.format_exc()}")


@contextmanager
def function_scope(name: str):
    """Context manager that logs function entry and exit."""
    log("ENTER", name)
    try:
        yield
        log("EXIT ", name)
    except Exception:
        log("ERROR", f"{name} 异常退出\n{traceback.format_exc()}")
        raise


def close():
    global _FILE
    with _LOCK:
        if _FILE:
            try:
                _write_raw(f"\n=== 日志结束 {datetime.now().isoformat()} ===\n",
                           force=True)
                _FILE.close()
            except Exception:
                pass
            _FILE = None


def _show_crash_dialog(title: str, text: str, detail: str):
    """尽力弹出崩溃提示；任何失败都不影响退出流程。"""
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance()
        if not app:
            return
        msg = QMessageBox()
        msg.setWindowTitle(title)
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setText(text)
        if detail:
            msg.setDetailedText(detail[:4000])
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
    except Exception:
        pass


# 崩溃提示必须回到 GUI 线程弹窗：``sys.excepthook`` 在工作线程里被调用时
# 直接 ``QMessageBox.exec()`` 是在非 GUI 线程上创建/运行控件，属于未定义行为
# （实测会卡死或直接崩溃）。因此用一个活在主线程的 QObject 做信号中转。
_crash_emitter = None


def install_crash_handler():
    """Install the global exception hooks. Call after QApplication is created."""
    global _crash_emitter
    sys.excepthook = _crash_excepthook
    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass
    try:
        from PyQt6.QtCore import QObject, pyqtSignal, Qt

        class _CrashEmitter(QObject):
            # title, text, detail
            show = pyqtSignal(str, str, str)

        emitter = _CrashEmitter()
        emitter.show.connect(_show_crash_dialog,
                             Qt.ConnectionType.QueuedConnection)
        _crash_emitter = emitter
    except Exception:
        _crash_emitter = None


def _dispatch_crash_dialog(title: str, text: str, detail: str):
    """把崩溃弹窗投递到 GUI 线程；没有 GUI 时退化为 stderr 输出。"""
    emitter = _crash_emitter
    if emitter is not None:
        try:
            emitter.show.emit(title, text, detail)
            return
        except Exception:
            pass
    try:
        print(f"[crash] {title}\n{text}\n{detail}", file=sys.stderr)
    except Exception:
        pass


def _crash_excepthook(exc_type, exc_value, exc_tb):
    """Global exception hook — logs crash and shows popup before exit."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log("FATAL", f"未处理的异常:\n{tb_text}")
    _dispatch_crash_dialog(
        "程序异常崩溃",
        (f"程序遇到严重错误即将关闭。\n\n"
         f"错误类型: {exc_type.__name__}\n"
         f"错误信息: {exc_value}\n\n"
         f"详细日志已保存到:\n{_LOG_PATH or '(未知)'}"),
        tb_text,
    )
    close()
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    """``threading.excepthook`` —— 捕获工作线程里未处理的异常。

    原实现只装 ``sys.excepthook``，而它**只对主线程生效**：QThread /
    threading.Thread 里的异常会直接杀死该线程且不留痕迹，用户看到的是
    进度条卡死、按钮无响应。这里至少把它完整记进日志。
    """
    if args.exc_type is SystemExit:
        return
    tb_text = "".join(traceback.format_exception(
        args.exc_type, args.exc_value, args.exc_traceback))
    thread_name = args.thread.name if args.thread else "?"
    log("FATAL", f"线程 [{thread_name}] 未处理的异常:\n{tb_text}")


def install_crash_handler():
    """Install the global exception hooks. Call after QApplication is created."""
    sys.excepthook = _crash_excepthook
    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass


# Close the log file on normal exit
atexit.register(close)
