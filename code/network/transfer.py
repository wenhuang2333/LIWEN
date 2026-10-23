import os
import json
import shutil
import socket
import struct
import sys
import time
import threading
import zipfile
from pathlib import Path
from utils.helpers import get_temp_archive_path, format_size
from utils.logger import info, warn, error
from utils.constants import (
    BUFFER_SIZE, MSG_TRANSFER_REQUEST, MSG_TRANSFER_ACCEPT,
    MSG_TRANSFER_REJECT, MSG_PING, MSG_PONG,
)

HEADER_ENCODING = "utf-8"
# 包头最大长度：包头只含文件名/大小/类型等短字段，64 KB 已远超正常需求。
# 过大的上限会让未认证的连接用极少的流量占住大量内存（配合并发上限）。
MAX_HEADER_SIZE = 64 * 1024  # 64 KB

# 单个文件声明大小的合理性上限，超出即视为异常元数据直接拒绝
MAX_RECV_BYTES = 64 * 1024 ** 3  # 64 GiB

# 并发处理线程上限：对端未经认证，不能来一个连接就起一个线程
MAX_CONCURRENT_HANDLERS = 8
# 等待并发槽位的时限（秒），超时直接拒绝该连接
HANDLER_SLOT_TIMEOUT = 5.0
# 读取一个包头的硬性总时限（秒）：每次 recv 都按剩余时间设超时，
# 慢速滴流的对端无法靠“每次发 1 字节”无限续命
HEADER_READ_TIMEOUT = 30.0
# 数据传输阶段单次 socket 操作的超时（秒）
DATA_TIMEOUT = 30.0
# 取消监听后等待工作线程收尾的时限（秒）
HANDLER_JOIN_TIMEOUT = 3.0

# 接收归档的大小上限：超过则拒绝自动解压，仅保留归档文件
MAX_ARCHIVE_SIZE = 2 * 1024 ** 3  # 2 GiB
# 允许的最大解压膨胀比（解压后总大小 / 归档大小）
MAX_EXTRACT_RATIO = 100
# 解压前要求预留的磁盘余量
EXTRACT_FREE_SPACE_MARGIN = 64 * 1024 * 1024  # 64 MiB

# Windows 保留设备名（大小写不敏感；带扩展名同样保留，如 CON.txt）
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# 全局并发槽位：所有 receive_files 共用，保证线程数不会随连接数无限增长
_HANDLER_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_HANDLERS)


def _check_name_safety(safe_name: str) -> None:
    """拒绝 Windows 保留设备名与 NTFS 数据流（ADS）形式的文件名。"""
    if ":" in safe_name:
        raise ValueError(f"拒绝写入含冒号（NTFS 数据流）的文件名: {safe_name}")
    if sys.platform == "win32":
        stem = safe_name.split(".")[0].strip().upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"拒绝写入 Windows 保留设备名: {safe_name}")


def _safe_dest(save_dir: str, name: str) -> str:
    """Build a safe destination path that stays within save_dir.

    Strips directory components from *name* and rejects names that would
    escape *save_dir* even after realpath resolution (e.g. symlink tricks).
    额外拒绝 Windows 保留设备名与 NTFS 数据流文件名。
    """
    safe_name = os.path.basename(name) or "unnamed"
    _check_name_safety(safe_name)
    dest = os.path.join(save_dir, safe_name)
    real_dest = os.path.realpath(dest)
    real_save = os.path.realpath(save_dir)
    if os.path.commonpath([real_dest, real_save]) != real_save:
        raise ValueError(f"拒绝写入保存目录之外的位置: {safe_name}")
    return dest


def _unique_dest(save_dir: str, name: str) -> str:
    """为 *name* 生成不会覆盖已有文件的落盘路径（同名则追加序号）。"""
    dest = _safe_dest(save_dir, name)
    if not (os.path.exists(dest) or os.path.exists(dest + ".part")):
        return dest
    stem, ext = os.path.splitext(os.path.basename(dest))
    for index in range(1, 10000):
        candidate = os.path.join(save_dir, f"{stem} ({index}){ext}")
        if not (os.path.exists(candidate) or os.path.exists(candidate + ".part")):
            info(f"接收目标已存在，改用新文件名: {candidate}")
            return candidate
    raise ValueError(f"无法为 {name} 生成不冲突的文件名")


def _send_header(sock, metadata):
    data = json.dumps(metadata, ensure_ascii=False).encode(HEADER_ENCODING)
    sock.sendall(struct.pack("!I", len(data)))
    sock.sendall(data)


def _recv_header(sock, deadline=None):
    """读取并解析一个包头；对端正常关闭时返回 ``None``。

    *deadline* 是 ``time.monotonic()`` 基准的绝对截止时间。给定时每次
    ``recv`` 都按剩余时间重设 socket 超时，因此读取包头有一个硬性总时限，
    对端无法靠慢速滴流无限延长它。读取结束后恢复原超时设置。
    """
    prev_timeout = sock.gettimeout()

    def _apply_deadline():
        if deadline is None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("读取包头超时")
        sock.settimeout(remaining)

    try:
        # 长度前缀必须循环读满 4 字节：TCP 可能把它拆到多个分段里
        raw_len = b""
        while len(raw_len) < 4:
            _apply_deadline()
            chunk = sock.recv(4 - len(raw_len))
            if not chunk:
                return None
            raw_len += chunk

        data_len = struct.unpack("!I", raw_len)[0]
        if data_len > MAX_HEADER_SIZE:
            raise ValueError(f"协议错误: 包头过大 ({data_len} > {MAX_HEADER_SIZE})")
        data = b""
        while len(data) < data_len:
            _apply_deadline()
            chunk = sock.recv(data_len - len(data))
            if not chunk:
                return None
            data += chunk
    finally:
        sock.settimeout(prev_timeout)

    try:
        payload = json.loads(data.decode(HEADER_ENCODING))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"协议错误: 包头不是合法 JSON — {e}") from e
    # 包头在整套协议里**永远是 JSON 对象**。对端可以发 `[1]`、`5`、`"x"`
    # （都是合法 JSON），调用方却一律按 dict 用 —— 非对象必须在这里拦掉，
    # 否则 `.get()` 抛的是 AttributeError，多数调用点只捕获 ValueError/OSError，
    # 异常会一路逃到线程顶层，让监听线程静默死掉（局域网内 5 字节即可触发）。
    if not isinstance(payload, dict):
        raise ValueError(
            f"协议错误: 包头不是 JSON 对象（{type(payload).__name__}）")
    return payload


def _extract_transfer_fields(metadata):
    """校验包头里的名称与大小字段，返回 ``(name, size)``。

    元数据来自不可信的对端，不能直接索引：缺字段、类型错误或声明超大小时
    统一抛出 ``ValueError``，由调用方记录日志并关闭连接。
    """
    if not isinstance(metadata, dict):
        raise ValueError(f"协议错误: 包头不是 JSON 对象 ({type(metadata).__name__})")
    name = metadata.get("name") or "unnamed"
    if not isinstance(name, str):
        raise ValueError(f"协议错误: 文件名类型非法 ({type(name).__name__})")
    size = metadata.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError(f"协议错误: 文件大小类型非法 ({size!r})")
    if not 0 <= size <= MAX_RECV_BYTES:
        raise ValueError(f"协议错误: 文件大小超出允许范围 ({size} > {MAX_RECV_BYTES})")
    return name, size


def _estimate_extract_size(archive_path):
    """估算归档解压后的总字节数；无法识别格式时返回 ``None``。"""
    try:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as zf:
                return sum(item.file_size for item in zf.infolist())
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        warn(f"读取 ZIP 目录失败，跳过精确估算: {e}")
    try:
        import tarfile
        with tarfile.open(archive_path, "r:*") as tf:
            return sum(m.size for m in tf.getmembers() if m.isfile())
    except Exception as e:
        warn(f"无法估算归档解压后大小 ({type(e).__name__}: {e})")
        return None


def _guard_archive_extraction(archive_path, save_dir):
    """解压前的安全闸门：归档大小、膨胀比与磁盘剩余空间。"""
    archive_size = os.path.getsize(archive_path)
    if archive_size > MAX_ARCHIVE_SIZE:
        raise ValueError(
            f"归档过大，拒绝自动解压 ({format_size(archive_size)} > "
            f"{format_size(MAX_ARCHIVE_SIZE)})")
    declared = _estimate_extract_size(archive_path)
    if declared is None:
        # 未知格式无法估算膨胀，退化为按归档本身大小校验
        declared = archive_size
    if archive_size > 0 and declared > archive_size * MAX_EXTRACT_RATIO:
        raise ValueError(
            f"解压膨胀比异常，拒绝自动解压 ({declared / archive_size:.1f}x > "
            f"{MAX_EXTRACT_RATIO}x)")
    os.makedirs(save_dir, exist_ok=True)
    free = shutil.disk_usage(save_dir).free
    if declared + EXTRACT_FREE_SPACE_MARGIN > free:
        raise ValueError(
            f"磁盘剩余空间不足，拒绝自动解压 (需要 {format_size(declared)} + "
            f"{format_size(EXTRACT_FREE_SPACE_MARGIN)} 余量, 剩余 {format_size(free)})")
    info(f"归档解压校验通过: {format_size(archive_size)} -> {format_size(declared)}")


def _extract_received_archive(dest, save_dir, status_callback=None):
    """解压收到的归档；违反安全限制时保留归档文件并报告错误。"""
    try:
        _guard_archive_extraction(dest, save_dir)
    except ValueError as e:
        error(f"拒绝解压 {dest}: {e} — 归档文件已保留")
        if status_callback:
            status_callback(f"解压已阻止（归档已保留）: {e}")
        return False

    from core.formats import FormatRegistry
    handler = FormatRegistry.detect(dest)
    if not handler or not handler.can_decompress():
        warn(f"无法识别归档格式，保留文件不删除: {dest}")
        if status_callback:
            status_callback("无法识别归档格式，已保留归档文件")
        return False
    handler.decompress(dest, save_dir)
    os.remove(dest)
    return True


def _send_file_data(sock, filepath, size, progress_callback, speed_callback, cancel_check):
    """发送文件内容，返回前校验实际写出的字节数（L7）。"""
    sent = 0
    start_time = time.time()
    with open(filepath, "rb") as f:
        # Try sendfile for zero-copy transfer on supported platforms
        if hasattr(sock, "sendfile") and hasattr(f, "fileno"):
            try:
                offset = 0
                while offset < size:
                    if cancel_check and cancel_check():
                        raise InterruptedError("传输已取消")
                    sent_chunk = sock.sendfile(f, offset, size - offset)
                    if sent_chunk == 0:
                        break
                    offset += sent_chunk
                    sent = offset
                    elapsed = max(time.time() - start_time, 0.001)
                    speed = sent / elapsed
                    if progress_callback and size > 0:
                        progress_callback(int(sent / size * 100))
                    if speed_callback:
                        speed_callback(f"{format_size(int(speed))}/s")
                if sent >= size:
                    return
            except (AttributeError, OSError) as e:
                if cancel_check and cancel_check():
                    raise InterruptedError("传输已取消")
                if sent:
                    # 已经写出部分数据：回退会从头重发，导致内容错乱，直接报错
                    raise ConnectionError(f"发送中断: 已发送 {sent}/{size} 字节 — {e}")
                warn(f"sendfile 不可用，回退到 read/write 循环: {e}")
            if sent:
                raise ConnectionError(f"发送不完整: {sent}/{size} 字节")

        while True:
            if cancel_check and cancel_check():
                raise InterruptedError("传输已取消")
            chunk = f.read(BUFFER_SIZE)
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            elapsed = max(time.time() - start_time, 0.001)
            speed = sent / elapsed
            if progress_callback and size > 0:
                progress_callback(int(sent / size * 100))
            if speed_callback:
                speed_callback(f"{format_size(int(speed))}/s")

    if sent != size:
        raise ConnectionError(f"发送不完整: {sent}/{size} 字节")


def _recv_file_data(sock, dest, size, progress_callback, speed_callback, cancel_check):
    """接收文件内容到 ``dest + '.part'``，字节数校验通过后再原子改名。

    任何失败（EOF 截断、取消、写盘错误）都会删除 ``.part`` 临时文件，
    避免留下一个顶着正式文件名的残缺文件。
    """
    received = 0
    start_time = time.time()
    tmp_dest = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    try:
        with open(tmp_dest, "wb") as f:
            while received < size:
                if cancel_check and cancel_check():
                    raise InterruptedError("接收已取消")
                chunk = sock.recv(min(BUFFER_SIZE, size - received))
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
                elapsed = max(time.time() - start_time, 0.001)
                speed = received / elapsed
                if progress_callback and size > 0:
                    progress_callback(int(received / size * 100))
                if speed_callback:
                    speed_callback(f"{format_size(int(speed))}/s")

        if received != size:
            raise ConnectionError(f"传输不完整: 收到 {received}/{size} 字节")
        os.replace(tmp_dest, dest)
    except BaseException:
        if os.path.exists(tmp_dest):
            try:
                os.remove(tmp_dest)
            except OSError as e:
                warn(f"清理临时文件失败: {tmp_dest} — {e}")
        raise
    info(f"接收完成: {dest} ({format_size(received)})")


def _dir_size(dirpath):
    total = 0
    for dirpath, _, filenames in os.walk(dirpath):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            total += os.path.getsize(fp)
    return total


def send_files(file_paths, host, port, compress_first=True,
               progress_callback=None, speed_callback=None,
               status_callback=None, cancel_check=None,
               send_request=True, from_name=""):
    has_dirs = any(os.path.isdir(p) for p in file_paths)
    if has_dirs and not compress_first:
        compress_first = True

    if compress_first:
        if status_callback:
            status_callback("正在压缩为临时文件...")
        tmp_zip = get_temp_archive_path()
        from core.formats import FormatRegistry
        FormatRegistry.get_by_name("zip").compress(file_paths, tmp_zip)
        actual_paths = [tmp_zip]
        is_archive = True
    else:
        actual_paths = list(file_paths)
        is_archive = False

    total_size = sum(os.path.getsize(p) if os.path.isfile(p) else _dir_size(p) for p in actual_paths)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30)
    try:
        sock.connect((host, port))
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        raise ConnectionError(f"无法连接到 {host}:{port} — {e}")

    try:
        for fp in actual_paths:
            path = Path(fp)
            is_file = path.is_file()
            metadata = {
                "name": path.name,
                "size": os.path.getsize(fp) if is_file else 0,
                "is_archive": is_archive,
                "is_dir": path.is_dir(),
            }
            if not is_file:
                # 目录在 compress_first=False 时会被强制压缩，正常走不到这里；
                # 真走到这里时也不能发与 size 不符的占位数据（旧实现发 b"DONE"
                # 会让接收端一直读到超时），统一按 0 字节处理。
                warn(f"未压缩的目录项按 0 字节处理: {fp}")

            if send_request:
                metadata["type"] = MSG_TRANSFER_REQUEST
                metadata["from_name"] = from_name
                metadata["from_ip"] = _get_local_ip()
                _send_header(sock, metadata)

                if status_callback:
                    status_callback("等待对方确认...")
                response = _recv_header(sock)
                if response is None:
                    raise ConnectionError("对方未响应请求")
                resp_type = response.get("type", "")
                if resp_type == MSG_TRANSFER_REJECT:
                    raise PermissionError("对方拒绝了传输请求")
                elif resp_type != MSG_TRANSFER_ACCEPT:
                    raise ConnectionError(f"未知响应: {resp_type}")
                if status_callback:
                    status_callback("对方已接受，正在传输...")
            else:
                _send_header(sock, metadata)

            if is_file:
                _send_file_data(sock, str(fp), metadata["size"],
                                progress_callback, speed_callback, cancel_check)

    finally:
        sock.close()
        if compress_first and os.path.exists(tmp_zip):
            os.remove(tmp_zip)
        info(f"发送结束: {len(actual_paths)} 个文件 ({format_size(total_size)}) -> {host}:{port}")


def receive_files(port, save_dir, progress_callback=None, speed_callback=None,
                  status_callback=None, request_callback=None, cancel_check=None,
                  auto_accept=False):
    """监听 *port* 接收文件，直到 ``cancel_check`` 返回 True。"""
    info(f"开始监听 TCP {port}，保存目录: {save_dir}")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.settimeout(2)
    server.bind(("0.0.0.0", port))
    server.listen(5)

    if status_callback:
        # 不能用 gethostbyname(gethostname())：主机名不解析的主机会抛 gaierror
        try:
            from network.interfaces import get_local_ip
            local_ip = get_local_ip()
        except Exception as e:
            warn(f"获取本机 IP 失败，回退到 socket 探测: {e}")
            local_ip = _get_local_ip()
        status_callback(f"监听中: {local_ip}:{port}")

    handlers = []
    try:
        while True:
            if cancel_check and cancel_check():
                break
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue

            # 并发上限：拿不到槽位就拒绝连接，避免线程/内存被无限占用
            if not _HANDLER_SEMAPHORE.acquire(timeout=HANDLER_SLOT_TIMEOUT):
                error(f"并发接收已达上限 ({MAX_CONCURRENT_HANDLERS})，拒绝 {addr[0]}")
                if status_callback:
                    status_callback(f"并发连接已达上限，已拒绝 {addr[0]}")
                conn.close()
                continue

            # Handle each client in its own thread for concurrent receives
            t = threading.Thread(
                target=_run_handler, daemon=True,
                args=(conn, addr, save_dir, progress_callback,
                      speed_callback, status_callback, request_callback,
                      cancel_check, auto_accept))
            handlers = [h for h in handlers if h.is_alive()]
            handlers.append(t)
            t.start()
    finally:
        server.close()
        # 短时限内等待工作线程收尾，让上层"接收完成"的状态是可信的
        for t in handlers:
            t.join(timeout=HANDLER_JOIN_TIMEOUT)
        alive = [t for t in handlers if t.is_alive()]
        if alive:
            warn(f"{len(alive)} 个接收线程未在 {HANDLER_JOIN_TIMEOUT}s 内结束")
        info(f"停止监听 TCP {port}")


def _run_handler(conn, addr, save_dir, progress_callback, speed_callback,
                 status_callback, request_callback, cancel_check, auto_accept):
    """工作线程入口：保证并发槽位一定会被释放。"""
    try:
        _handle_client(conn, addr, save_dir, progress_callback, speed_callback,
                       status_callback, request_callback, cancel_check, auto_accept)
    finally:
        _HANDLER_SEMAPHORE.release()


def _handle_client(conn, addr, save_dir, progress_callback, speed_callback,
                   status_callback, request_callback, cancel_check, auto_accept):
    """处理一个 TCP 连接上的全部消息，直到对端关闭。

    旧发送端每个连接只发一个文件，因此"循环读取包头直到 EOF"既兼容旧发送端，
    也让新发送端能在同一连接上连续发送多个文件。
    """
    if status_callback:
        status_callback(f"收到来自 {addr[0]} 的连接")
    info(f"处理来自 {addr[0]}:{addr[1]} 的连接")

    try:
        conn.settimeout(DATA_TIMEOUT)
        while True:
            metadata = _recv_header(conn, deadline=time.monotonic() + HEADER_READ_TIMEOUT)
            if metadata is None:
                break  # 对端正常关闭
            if not isinstance(metadata, dict):
                # _recv_header 已保证是 dict；这里留一道防线，防止将来放松
                raise ValueError(f"协议错误: 包头不是 JSON 对象 ({type(metadata).__name__})")
            msg_type = metadata.get("type", "")

            # Handle ping
            if msg_type == MSG_PING:
                _send_header(conn, {"type": MSG_PONG, "hostname": socket.gethostname()})
                break

            # Handle transfer request
            if msg_type == MSG_TRANSFER_REQUEST:
                try:
                    name, size = _extract_transfer_fields(metadata)
                except ValueError as e:
                    error(f"来自 {addr[0]} 的包头非法: {e}")
                    if status_callback:
                        status_callback(f"已拒绝非法传输请求: {e}")
                    break

                accepted = False
                if auto_accept:
                    accepted = True
                    if status_callback:
                        status_callback("自动接受传输请求")
                elif request_callback:
                    decision = {"accepted": False}
                    event = threading.Event()
                    request_callback({
                        "from": f"{metadata.get('from_name', '')} ({addr[0]})",
                        "from_name": metadata.get("from_name", addr[0]),
                        "from_ip": addr[0],
                        "name": name,
                        "size": size,
                        "is_archive": metadata.get("is_archive", False),
                    }, decision, event)
                    event.wait(timeout=120)
                    accepted = decision.get("accepted", False)

                if not accepted:
                    _send_header(conn, {"type": MSG_TRANSFER_REJECT})
                    if status_callback:
                        status_callback("已拒绝传输请求")
                    break

                try:
                    # 先确定落盘路径再回复接受，避免接受后才发现文件名非法
                    dest = _unique_dest(save_dir, name)
                except ValueError as e:
                    error(f"无法确定保存路径: {e}")
                    _send_header(conn, {"type": MSG_TRANSFER_REJECT})
                    if status_callback:
                        status_callback(f"保存路径非法，已拒绝: {e}")
                    break

                _send_header(conn, {"type": MSG_TRANSFER_ACCEPT})
                _recv_file_data(conn, dest, size,
                                progress_callback, speed_callback, cancel_check)

                if metadata.get("is_archive"):
                    if status_callback:
                        status_callback("正在解压接收的文件...")
                    _extract_received_archive(dest, save_dir, status_callback)

                if status_callback:
                    status_callback("接收完成")
                continue

            # Legacy / direct file transfer (no request type)
            try:
                name, size = _extract_transfer_fields(metadata)
            except ValueError as e:
                error(f"来自 {addr[0]} 的包头非法: {e}")
                if status_callback:
                    status_callback(f"已拒绝非法传输请求: {e}")
                break

            if request_callback and not msg_type:
                request_callback({
                    "from": addr[0],
                    "from_name": addr[0],
                    "from_ip": addr[0],
                    "name": name,
                    "size": size,
                    "is_archive": metadata.get("is_archive", False),
                }, None, None)

            dest = _unique_dest(save_dir, name)
            _recv_file_data(conn, dest, size,
                            progress_callback, speed_callback, cancel_check)

            if metadata.get("is_archive"):
                if status_callback:
                    status_callback("正在解压接收的文件...")
                _extract_received_archive(dest, save_dir, status_callback)

            if status_callback:
                status_callback("接收完成")

    except (socket.timeout, ConnectionError, OSError, ValueError,
            KeyError, TypeError, AttributeError) as e:
        error(f"处理 {addr[0]} 的连接时出错: {type(e).__name__}: {e}")
        if status_callback:
            status_callback(f"连接错误: {e}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def ping_host(host, port, timeout=3):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        _send_header(sock, {"type": MSG_PING, "hostname": socket.gethostname()})
        response = _recv_header(sock)
        if response and response.get("type") == MSG_PONG:
            return True, f"链路通畅 — {response.get('hostname', host)}:{port}"
        return False, "目标未响应正确的握手"
    except socket.timeout:
        return False, f"连接超时 ({timeout}s)"
    except ConnectionRefusedError:
        return False, "目标端口拒绝连接"
    except ValueError as e:
        # 对方端口上跑的不是本程序（或包头被破坏）：如实报告，不要抛栈
        return False, str(e)
    except OSError as e:
        return False, f"网络错误: {e}"
    finally:
        sock.close()


def _get_local_ip():
    # Try the "connect a UDP socket" trick to find the preferred local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Fall back to getaddrinfo
    try:
        hostname = socket.gethostname()
        info = socket.getaddrinfo(hostname, None)
        for item in info:
            ip = item[4][0]
            if not ip.startswith("127.") and ":" not in ip:
                return ip
    except Exception:
        pass

    # Last resort
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ""
