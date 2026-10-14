import os
import struct
import zlib
import io
import time
from pathlib import Path
from typing import Optional, Callable

from core.formats._base import ArchiveHandler, register_handler

try:
    from utils.logger import error as log_error, exception as log_exception
except Exception:  # pragma: no cover - 日志模块不可用时退化为静默
    def log_error(message: str):
        pass

    def log_exception(message: str):
        pass

MAGIC = b"SHS\x01"
HEADER_SIZE = 64
VERSION = 1
FLAG_ENCRYPTED = 0x0001
ENTRY_FLAG_FILE = 0x0000
ENTRY_FLAG_DIR = 0xFFFF
PBKDF2_ITERATIONS = 100_000
COMPRESS_LEVEL = 6
ZLIB_THRESHOLD = 128

# 流式压缩/解压的分块大小（1 MiB）
_STREAM_CHUNK = 1 << 20


def _compute_crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _align4(n: int) -> int:
    return (n + 3) & ~3


def _safe_join(dest_dir: str, name: str) -> str:
    """安全拼接解压输出路径，拒绝任何逃逸出 dest_dir 的条目名。

    os.path.join(dest, "/abs") 会直接返回 "/abs"，"../x" 也会向上逃逸，
    因此对根目录与候选路径都做 realpath 归一化后校验前缀。
    """
    if not name or "\x00" in name:
        raise ValueError(f"压缩包内包含非法路径: {name}")
    root = os.path.realpath(dest_dir)
    candidate = os.path.realpath(os.path.join(root, name))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError(f"压缩包内包含非法路径: {name}")
    return candidate


class _ProgressReporter:
    """进度回调节流器：仅在整数百分比变化且间隔超过 0.1s 时上报。

    大目录逐文件回调会产生成千上万次跨线程 Qt 信号并卡死界面；
    这里按“百分比变化 + 时间间隔”双重节流，并保证结束时一定上报 100%。
    """

    _MIN_INTERVAL = 0.1

    def __init__(self, callback, total: int):
        self._callback = callback
        self._total = total
        self._last_percent = -1
        self._last_time = 0.0

    def update(self, processed: int, force: bool = False):
        """按已处理数量上报进度；重复百分比与高频调用会被丢弃。"""
        if not self._callback or self._total <= 0:
            return
        percent = int(processed / self._total * 100)
        if percent == self._last_percent:
            return
        now = time.monotonic()
        if not force and percent < 100 and \
                (now - self._last_time) < self._MIN_INTERVAL:
            return
        self._last_percent = percent
        self._last_time = now
        self._callback(percent)

    def finish(self):
        """收尾时确保上报一次 100%。"""
        if not self._callback or self._total <= 0:
            return
        if self._last_percent != 100:
            self._last_percent = 100
            self._callback(100)


@register_handler
class ShishanHandler(ArchiveHandler):

    @classmethod
    def format_name(cls) -> str:
        return "shishan"

    @classmethod
    def extensions(cls) -> list[str]:
        return [".shishan"]

    @classmethod
    def can_compress(cls) -> bool:
        return False  # Replaced by CustomHandler; keep for decompression only

    @classmethod
    def supports_password(cls) -> bool:
        return True

    @classmethod
    def detect(cls, filepath: str) -> bool:
        if Path(filepath).suffix.lower() == ".shishan":
            return True
        try:
            with open(filepath, "rb") as f:
                return f.read(4) == MAGIC
        except OSError:
            return False

    # ===================================================================
    # Internal helpers
    # ===================================================================

    @staticmethod
    def _walk_files(file_paths):
        entries = []
        for root_path in file_paths:
            root = Path(root_path)
            if root.is_file():
                entries.append((root.name, root, root.stat().st_size))
            elif root.is_dir():
                for dirpath, _, filenames in os.walk(root):
                    for fn in filenames:
                        fp = Path(dirpath) / fn
                        arcname = str(fp.relative_to(root.parent)).replace("\\", "/")
                        entries.append((arcname, fp, fp.stat().st_size))
        return entries

    @staticmethod
    def _build_entry_table(file_paths) -> tuple[bytes, list[dict]]:
        entries = ShishanHandler._walk_files(file_paths)
        buf = io.BytesIO()
        meta_list = []

        # Add directory entries for all distinct parent paths
        dirs_seen = set()
        for arcname, fp, size in entries:
            parent = os.path.dirname(arcname)
            if parent and parent not in dirs_seen:
                dirs_seen.add(parent)
                parts = parent.split("/")
                for i in range(1, len(parts) + 1):
                    sub = "/".join(parts[:i])
                    if sub not in dirs_seen:
                        dirs_seen.add(sub)
                        path_bytes = sub.encode("utf-8")
                        buf.write(struct.pack("<I", len(path_bytes)))
                        buf.write(path_bytes)
                        pad = _align4(len(path_bytes)) - len(path_bytes)
                        if pad:
                            buf.write(b"\x00" * pad)
                        buf.write(struct.pack("<QQ", 0, 0))
                        buf.write(struct.pack("<I", 0))
                        buf.write(struct.pack("<H", ENTRY_FLAG_DIR))
                        meta_list.append({
                            "name": sub, "size": 0, "compressed_size": 0, "is_dir": True
                        })

        # Add file entries
        for arcname, fp, size in entries:
            path_bytes = arcname.encode("utf-8")
            buf.write(struct.pack("<I", len(path_bytes)))
            buf.write(path_bytes)
            pad = _align4(len(path_bytes)) - len(path_bytes)
            if pad:
                buf.write(b"\x00" * pad)
            buf.write(struct.pack("<Q", size))       # OrigSize placeholder
            buf.write(struct.pack("<Q", 0))           # CompSize placeholder
            buf.write(struct.pack("<I", 0))           # CRC32 placeholder
            buf.write(struct.pack("<H", ENTRY_FLAG_FILE))
            meta_list.append({
                "name": arcname, "full_path": str(fp), "size": size,
                "compressed_size": 0, "is_dir": False,
                "data_offset": None,
            })

        return buf.getvalue(), meta_list

    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        from Crypto.Protocol.KDF import PBKDF2
        from Crypto.Hash import SHA256
        return PBKDF2(password, salt, dkLen=32, count=PBKDF2_ITERATIONS, hmac_hash_module=SHA256)

    @staticmethod
    def _encrypt_aes_gcm(plaintext: bytes, password: str) -> tuple[bytes, bytes, bytes]:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
        salt = get_random_bytes(16)
        nonce = get_random_bytes(12)
        key = ShishanHandler._derive_key(password, salt)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return nonce, salt, ciphertext + tag

    @staticmethod
    def _decrypt_aes_gcm(nonce: bytes, salt: bytes, ciphertext_with_tag: bytes, password: str):
        from Crypto.Cipher import AES
        key = ShishanHandler._derive_key(password, salt)
        tag = ciphertext_with_tag[-16:]
        ciphertext = ciphertext_with_tag[:-16]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext

    @staticmethod
    def _compress_block(data: bytes) -> tuple[bytes, int, bool]:
        if len(data) < ZLIB_THRESHOLD:
            return data, len(data), False
        compressed = zlib.compress(data, COMPRESS_LEVEL)
        if len(compressed) < len(data):
            return compressed, len(compressed), True
        return data, len(data), False

    @staticmethod
    def _decompress_block(data: bytes, size_hint: int) -> bytes:
        """解压一个数据块；返回原始 data 表示该块未被压缩。

        用 decompressobj 增量解压并按声明大小设硬上限：绝不能先
        zlib.decompress 出一大块再校验，否则构造的压缩包可撑爆内存。
        """
        if len(data) >= size_hint and size_hint != 0:
            return data
        try:
            decompressor = zlib.decompressobj(15)
            # deflate 理论最大膨胀比约 1032:1，用它兜住 size_hint 溢出
            max_possible = len(data) * 1032 + 1032
            cap = min(size_hint, max_possible) + 1
            raw = decompressor.decompress(data, cap)
            if len(raw) > size_hint:
                raise ValueError("解压数据超出声明大小，压缩包可能被构造")
            # 输出未触顶说明输入已耗尽，flush 不会再有数据；否则直接拒绝
            if not decompressor.unconsumed_tail:
                raw += decompressor.flush()
            if len(raw) > size_hint:
                raise ValueError("解压数据超出声明大小，压缩包可能被构造")
            return raw
        except zlib.error:
            return data

    @staticmethod
    def _parse_entry_table(entry_table_bytes: bytes) -> list[dict]:
        """解析目录表；越界或损坏时抛出干净的中文 ValueError。

        struct.unpack_from / decode 不能直接作用在未校验的偏移上，
        否则损坏的压缩包会把 struct.error、UnicodeDecodeError 抛给用户。
        """
        entries = []
        pos = 0
        total = len(entry_table_bytes)
        while pos < total:
            if pos + 4 > total:
                raise ValueError("压缩包目录表不完整或已损坏")
            path_len = struct.unpack_from("<I", entry_table_bytes, pos)[0]
            pos += 4
            if path_len == 0 or path_len > total - pos:
                raise ValueError("压缩包目录表不完整或已损坏")
            # surrogateescape 保证坏字节不会抛 UnicodeDecodeError
            arcname = entry_table_bytes[pos:pos + path_len].decode(
                "utf-8", errors="surrogateescape")
            pos += _align4(path_len)
            if pos + 16 > total:
                raise ValueError("压缩包目录表不完整或已损坏")
            orig_size, comp_size = struct.unpack_from(
                "<QQ", entry_table_bytes, pos)
            pos += 16
            if pos + 4 > total:
                raise ValueError("压缩包目录表不完整或已损坏")
            crc32_val = struct.unpack_from("<I", entry_table_bytes, pos)[0]
            pos += 4
            if pos + 2 > total:
                raise ValueError("压缩包目录表不完整或已损坏")
            entry_flags = struct.unpack_from("<H", entry_table_bytes, pos)[0]
            pos += 2
            entries.append({
                "name": arcname,
                "size": orig_size,
                "compressed_size": comp_size,
                "crc32": crc32_val,
                "is_dir": entry_flags == ENTRY_FLAG_DIR,
            })
        return entries

    @staticmethod
    def _render_entry_table(meta_list: list[dict]) -> bytes:
        """用最终值渲染目录表；字段宽度固定，因此长度与占位表一致。"""
        entry_table_buf = io.BytesIO()
        dirs_seen = set()
        for entry in meta_list:
            arcname = entry["name"]
            if entry["is_dir"]:
                if arcname in dirs_seen:
                    continue
                dirs_seen.add(arcname)
                path_bytes = arcname.encode("utf-8")
                entry_table_buf.write(struct.pack("<I", len(path_bytes)))
                entry_table_buf.write(path_bytes)
                pad = _align4(len(path_bytes)) - len(path_bytes)
                if pad:
                    entry_table_buf.write(b"\x00" * pad)
                entry_table_buf.write(struct.pack("<QQ", 0, 0))
                entry_table_buf.write(struct.pack("<I", 0))
                entry_table_buf.write(struct.pack("<H", ENTRY_FLAG_DIR))
            else:
                path_bytes = arcname.encode("utf-8")
                entry_table_buf.write(struct.pack("<I", len(path_bytes)))
                entry_table_buf.write(path_bytes)
                pad = _align4(len(path_bytes)) - len(path_bytes)
                if pad:
                    entry_table_buf.write(b"\x00" * pad)
                entry_table_buf.write(struct.pack("<Q", entry["size"]))
                entry_table_buf.write(struct.pack("<Q", entry["compressed_size"]))
                entry_table_buf.write(struct.pack("<I", entry["original_crc"]))
                entry_table_buf.write(struct.pack("<H", ENTRY_FLAG_FILE))
        return entry_table_buf.getvalue()

    @staticmethod
    def _copy_raw_stream(out_file, src_path: str, start: int) -> tuple[int, int]:
        """把文件原样拷贝到 out_file 的 start 处，返回 (size, crc32)。"""
        crc = 0
        total = 0
        with open(src_path, "rb") as src:
            while True:
                chunk = src.read(_STREAM_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                crc = zlib.crc32(chunk, crc)
                out_file.write(chunk)
        out_file.seek(start + total)
        return total, crc & 0xFFFFFFFF

    @staticmethod
    def _write_block_stream(out_file, src_path: str,
                            declared_size: int) -> tuple[int, int]:
        """把文件数据流式写入 out_file（压缩或原样），返回 (comp_size, crc32)。

        分块压缩与一次性压缩的 deflate 输出完全一致；若压缩后并未变小，
        则回退到当前块起点重写原始数据，保证与旧实现逐字节相同。
        """
        start = out_file.tell()
        if declared_size < ZLIB_THRESHOLD:
            return ShishanHandler._copy_raw_stream(out_file, src_path, start)
        compressor = zlib.compressobj(COMPRESS_LEVEL)
        crc = 0
        total = 0
        with open(src_path, "rb") as src:
            while True:
                chunk = src.read(_STREAM_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                crc = zlib.crc32(chunk, crc)
                piece = compressor.compress(chunk)
                if piece:
                    out_file.write(piece)
        tail = compressor.flush()
        if tail:
            out_file.write(tail)
        comp_size = out_file.tell() - start
        if comp_size < total:
            return comp_size, crc & 0xFFFFFFFF
        # 压缩后反而更大：回退为原样存储
        out_file.seek(start)
        out_file.truncate(start)
        return ShishanHandler._copy_raw_stream(out_file, src_path, start)

    @staticmethod
    def _copy_block_to_file(src_read, offset: int, size: int,
                            out_path: str) -> tuple[int, int]:
        """把原样存储的数据块流式写入 out_path，返回 (字节数, CRC32)。

        src_read(offset, n) 每次最多返回 n 字节，避免整块读入内存。
        """
        crc = 0
        written = 0
        with open(out_path, "wb") as out:
            while written < size:
                chunk = src_read(offset + written,
                                 min(_STREAM_CHUNK, size - written))
                if not chunk:
                    break
                written += len(chunk)
                crc = zlib.crc32(chunk, crc)
                out.write(chunk)
        return written, crc & 0xFFFFFFFF

    @staticmethod
    def _decompress_block_to_file(data: bytes, size_hint: int,
                                  out_path: str) -> tuple[int, int]:
        """增量解压到 out_path，返回 (字节数, CRC32)。

        分块解压并按声明大小设硬上限，绝不先整块解压再校验；超出即抛错。
        """
        if len(data) >= size_hint and size_hint != 0:
            with open(out_path, "wb") as out:
                out.write(data)
            return len(data), _compute_crc32(data)
        try:
            decompressor = zlib.decompressobj(15)
            total = 0
            crc = 0
            with open(out_path, "wb") as out:
                pending = data
                while pending:
                    chunk = decompressor.decompress(pending, _STREAM_CHUNK)
                    pending = decompressor.unconsumed_tail
                    if chunk:
                        total += len(chunk)
                        if total > size_hint:
                            raise ValueError(
                                "解压数据超出声明大小，压缩包可能被构造")
                        crc = zlib.crc32(chunk, crc)
                        out.write(chunk)
                if not decompressor.unconsumed_tail:
                    tail = decompressor.flush()
                    if tail:
                        total += len(tail)
                        if total > size_hint:
                            raise ValueError(
                                "解压数据超出声明大小，压缩包可能被构造")
                        crc = zlib.crc32(tail, crc)
                        out.write(tail)
            return total, crc & 0xFFFFFFFF
        except zlib.error:
            # 不是合法的 zlib 流：按原样存储处理（与旧行为一致）
            with open(out_path, "wb") as out:
                out.write(data)
            return len(data), _compute_crc32(data)

    # ===================================================================
    # Public API
    # ===================================================================

    @staticmethod
    def compress(
        file_paths, output_path,
        password=None, volume_size=None, progress_callback=None,
        seed_code=None, compress_level=0,
    ):
        if not file_paths:
            return {"path": output_path, "size": 0, "file_count": 0}

        entry_table_bytes, meta_list = ShishanHandler._build_entry_table(file_paths)
        file_entries = [m for m in meta_list if not m["is_dir"]]
        total_files = len(file_entries)
        entry_count = len(meta_list)
        entry_table_size = len(entry_table_bytes)

        reporter = _ProgressReporter(progress_callback, total_files)
        # 先写同目录临时文件，全部成功后再原子替换；失败时原压缩包保持不变
        tmp_path = f"{output_path}.{os.getpid()}.part"

        def build_header(flags: int, table_size: int) -> bytes:
            header = struct.pack(
                "<4sHHII", MAGIC, VERSION, flags, entry_count, table_size)
            header += struct.pack("<Q", HEADER_SIZE + table_size)
            header += b"\x00" * 36  # reserved (bytes 24-59, CRC at 60-63)
            header += struct.pack("<I", _compute_crc32(header[:60]))
            return header

        try:
            if password:
                # AES-GCM 需要整体加密，此路径仍按内存缓冲处理
                data_buf = io.BytesIO()
                for i, entry in enumerate(file_entries):
                    with open(entry["full_path"], "rb") as f:
                        raw = f.read()
                    entry["original_crc"] = _compute_crc32(raw)
                    compressed, comp_size, _ = ShishanHandler._compress_block(raw)
                    entry["compressed_size"] = comp_size
                    data_buf.write(compressed)
                    reporter.update(i + 1)
                reporter.finish()
                final_entry_table = ShishanHandler._render_entry_table(meta_list)
                payload = final_entry_table + data_buf.getvalue()
                nonce, salt, encrypted = ShishanHandler._encrypt_aes_gcm(
                    payload, password)
                header = build_header(FLAG_ENCRYPTED, len(final_entry_table))
                with open(tmp_path, "wb") as f:
                    f.write(header)
                    f.write(nonce)
                    f.write(salt)
                    f.write(encrypted)
            else:
                # 流式写入：先占位目录表，数据直接落盘，最后回填目录表
                with open(tmp_path, "wb") as f:
                    f.write(build_header(0, entry_table_size))
                    f.write(entry_table_bytes)
                    for i, entry in enumerate(file_entries):
                        comp_size, crc = ShishanHandler._write_block_stream(
                            f, entry["full_path"], entry["size"])
                        entry["compressed_size"] = comp_size
                        entry["original_crc"] = crc
                        reporter.update(i + 1)
                    reporter.finish()
                    final_entry_table = ShishanHandler._render_entry_table(
                        meta_list)
                    if len(final_entry_table) != entry_table_size:
                        raise ValueError("内部错误：目录表长度不一致")
                    f.seek(HEADER_SIZE)
                    f.write(final_entry_table)
            os.replace(tmp_path, output_path)
        except Exception:
            log_exception(f"压缩失败，已保留原文件: {output_path}")
            raise
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    log_error(f"临时文件清理失败: {tmp_path}")

        return {
            "path": output_path,
            "size": os.path.getsize(output_path),
            "file_count": total_files,
        }

    @staticmethod
    def decompress(
        archive_path, dest_dir,
        password=None, progress_callback=None,
    ):
        with open(archive_path, "rb") as f:
            file_size = os.fstat(f.fileno()).st_size
            header = f.read(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                raise ValueError("文件损坏：头部不完整")
            magic, version, flags = struct.unpack_from("<4sHH", header, 0)
            if magic != MAGIC:
                raise ValueError("不是有效的 .shishan 文件")
            entry_count, entry_table_size = struct.unpack_from("<II", header, 8)
            data_offset = struct.unpack_from("<Q", header, 16)[0]
            stored_crc = struct.unpack_from("<I", header, 60)[0]
            if _compute_crc32(header[:60]) != stored_crc:
                raise ValueError("文件头部校验失败，文件可能已损坏")

            encrypted = bool(flags & FLAG_ENCRYPTED)
            data_bytes = None
            data_base = 0
            if encrypted:
                nonce = f.read(12)
                salt = f.read(16)
                remaining = f.read()
                if not password:
                    raise ValueError("此压缩包已加密，需要密码")
                try:
                    payload = ShishanHandler._decrypt_aes_gcm(nonce, salt, remaining, password)
                except Exception:
                    raise ValueError("密码错误或文件已损坏")
                if entry_table_size > len(payload):
                    raise ValueError("压缩包目录表不完整或已损坏")
                entry_table_bytes = payload[:entry_table_size]
                data_bytes = payload[entry_table_size:]
            else:
                # 先按剩余字节数校验目录表大小，避免被伪造的超大值拖爆内存
                remaining_bytes = file_size - HEADER_SIZE
                if entry_table_size > remaining_bytes:
                    raise ValueError("压缩包目录表不完整或已损坏")
                entry_table_bytes = f.read(entry_table_size)
                # 数据区留在文件里逐条目 seek 读取，不再整段读入内存
                data_base = HEADER_SIZE + entry_table_size

            # Parse entry table
            entries = ShishanHandler._parse_entry_table(entry_table_bytes)

            file_count = sum(1 for e in entries if not e["is_dir"])
            data_pos = 0
            processed = 0
            reporter = _ProgressReporter(progress_callback, file_count)

            def read_block(offset: int, size: int) -> bytes:
                """读取一个压缩块；加密路径走内存切片，普通路径只读所需字节。"""
                if size <= 0:
                    return b""
                if data_bytes is not None:
                    return data_bytes[offset:offset + size]
                start = data_base + offset
                if start >= file_size:
                    return b""
                size = min(size, file_size - start)
                f.seek(start)
                return f.read(size)

            def read_chunk(offset: int, size: int) -> bytes:
                """按块读取数据区，供原样存储的大文件流式落盘。"""
                if size <= 0:
                    return b""
                if data_bytes is not None:
                    view = memoryview(data_bytes)
                    return bytes(view[offset:offset + size])
                start = data_base + offset
                if start >= file_size:
                    return b""
                size = min(size, file_size - start)
                f.seek(start)
                return f.read(size)

            for entry in entries:
                name = entry["name"]
                if entry["is_dir"]:
                    os.makedirs(_safe_join(dest_dir, name), exist_ok=True)
                    continue

                out_path = _safe_join(dest_dir, name)
                if out_path == os.path.realpath(dest_dir):
                    raise ValueError(f"压缩包内包含非法路径: {name}")
                parent = os.path.dirname(out_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)

                block_offset = data_pos
                data_pos += entry["compressed_size"]
                block_size = entry["compressed_size"]
                # 逐条目写 .part，校验通过后再替换，失败不破坏已有文件
                part_path = f"{out_path}.{os.getpid()}.part"
                try:
                    # 与旧实现一致：块不小于声明大小即视为原样存储
                    if entry["size"] != 0 and block_size >= entry["size"]:
                        _, crc = ShishanHandler._copy_block_to_file(
                            read_chunk, block_offset, block_size, part_path)
                    else:
                        compressed = read_block(block_offset, block_size)
                        _, crc = ShishanHandler._decompress_block_to_file(
                            compressed, entry["size"], part_path)
                    if crc != entry["crc32"]:
                        raise ValueError(
                            f"文件校验失败: {name}, 文件可能已损坏")
                    os.replace(part_path, out_path)
                except BaseException:
                    if os.path.exists(part_path):
                        try:
                            os.remove(part_path)
                        except OSError:
                            log_error(f"临时文件清理失败: {part_path}")
                    raise

                processed += 1
                reporter.update(processed)

            reporter.finish()
            return {"dest": dest_dir, "file_count": file_count}

    @staticmethod
    def list_contents(archive_path: str) -> list[dict]:
        try:
            with open(archive_path, "rb") as f:
                header = f.read(HEADER_SIZE)
                if len(header) < HEADER_SIZE:
                    return []
                magic = struct.unpack_from("<4s", header, 0)[0]
                if magic != MAGIC:
                    return []
                _, flags = struct.unpack_from("<HH", header, 4)
                entry_count, entry_table_size = struct.unpack_from("<II", header, 8)

                encrypted = bool(flags & 0x0001)
                if encrypted:
                    return [{
                        "name": "[加密压缩包]",
                        "size": 0,
                        "compressed_size": 0,
                        "modified": None,
                        "is_dir": False,
                    }]

                # 目录表大小先按文件剩余字节数校验，防止伪造超大值
                remaining_bytes = os.fstat(f.fileno()).st_size - HEADER_SIZE
                if entry_table_size > remaining_bytes:
                    raise ValueError("压缩包目录表不完整或已损坏")
                entry_table_bytes = f.read(entry_table_size)

            entries = ShishanHandler._parse_entry_table(entry_table_bytes)
            return [{
                "name": e["name"],
                "size": e["size"],
                "compressed_size": e["compressed_size"],
                "modified": None,
                "is_dir": e["is_dir"],
            } for e in entries]
        except Exception:
            return []

    @staticmethod
    def extract_single(archive_path, member_name, dest_dir, password=None):
        import tempfile
        import shutil
        tmp_dir = tempfile.mkdtemp(prefix="shishan_extract_")
        try:
            ShishanHandler.decompress(archive_path, tmp_dir, password=password)
            # member_name 来自调用方/压缩包，读写两侧都必须校验
            src = _safe_join(tmp_dir, member_name)
            if not os.path.isfile(src):
                raise FileNotFoundError(f"压缩包中未找到: {member_name}")
            dest = _safe_join(dest_dir, member_name)
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(src, dest)
            return dest
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def is_encrypted(archive_path: str) -> bool:
        try:
            with open(archive_path, "rb") as f:
                f.read(4)  # magic
                version, flags = struct.unpack("<HH", f.read(4))
            return bool(flags & FLAG_ENCRYPTED)
        except Exception:
            return False
