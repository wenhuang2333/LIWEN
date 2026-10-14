"""容错的 JSON 持久化工具 —— 全项目小型状态文件的统一读写入口。

为什么需要它
------------
项目里有一批"用户状态文件"（``settings.json``、``rooms.json``、
``http_request_history.json``、``http_variables.json``、``repair_task.json``）。
它们原先各自用 ``open(path, "w") + json.dump`` 直接覆写，存在两个真实缺陷：

1. **写入非原子** —— 写到一半时进程崩溃 / 断电 / 磁盘写满，原文件会被截断成
   半截 JSON；下次启动读取失败，用户的全部设置（含 API Key、已存房间）静默丢失。
   解决办法：先写同目录临时文件，``flush`` + ``fsync`` 后再 ``os.replace``
   原子替换（POSIX 与 Windows 上 ``os.replace`` 都是原子的）。
2. **异常未捕获** —— ``settings.py`` / ``room_store.py`` 的 ``save()`` 没有
   任何 ``try``；一旦应用数据目录不可写（只读漫游配置、OneDrive 重定向的家目录、
   磁盘满、权限受限），**整个程序在 import 阶段就崩溃退出**，连界面都出不来。
   解决办法：读写都只记日志、不向上抛异常。

``os.replace`` 是同目录内的重命名，因此临时文件必须与目标同目录（跨文件系统
的 rename 会退化为非原子的复制）。``tempfile.mkstemp`` 创建的临时文件权限为
0600，比原先直接覆写更安全 —— 状态文件里含 API Key。
"""

import json
import os
import tempfile
import threading

# 写日志需要 logger，但 logger 在极早期启动阶段可能还没初始化；
# 此时 _write_raw 会静默丢弃，不会反过来抛异常，因此这里的依赖是安全的。
from utils.logger import warn as _log_warn, error as _log_error

__all__ = ["load_json", "save_json", "ensure_dir"]

# 保护同一进程内对同一文件的并发写入（例如多个 QThread 同时保存设置）
_write_lock = threading.Lock()


def ensure_dir(path: str) -> bool:
    """确保 ``path``（文件或目录）的父目录存在。失败返回 False，不抛异常。"""
    try:
        directory = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        return True
    except OSError as e:
        _log_error(f"创建目录失败 {path}: {type(e).__name__}: {e}")
        return False


def load_json(path: str, default=None, expect_type=None):
    """读取 JSON。文件缺失 / 损坏 / 类型不符时返回 ``default``，永不抛异常。

    :param expect_type: 期望的顶层类型（如 ``dict`` / ``list``）。不符时视为损坏，
        返回 ``default``，避免调用方拿到意料之外的类型后继续崩。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as e:
        _log_warn(f"读取 JSON 失败，使用默认值 {path}: {type(e).__name__}: {e}")
        return default

    if expect_type is not None and not isinstance(data, expect_type):
        _log_warn(
            f"JSON 顶层类型不符（期望 {expect_type.__name__}，"
            f"实际 {type(data).__name__}），使用默认值: {path}"
        )
        return default
    return data


def save_json(path: str, data, indent: int = 2) -> bool:
    """原子地写入 JSON。成功返回 True，失败只记日志并返回 False，永不抛异常。

    步骤：同目录建临时文件 → 写入 → ``flush`` + ``fsync`` → ``os.replace`` 替换。
    任一步失败都会清理临时文件，原文件保持不变。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path = None
    with _write_lock:
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".tmp-", suffix=".json", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
            return True
        except (OSError, TypeError, ValueError) as e:
            _log_error(
                f"保存 JSON 失败 {path}: {type(e).__name__}: {e} "
                f"（原有文件未被破坏）")
            return False
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
