import os
import struct
import zlib
import io
import time
import shutil
import hashlib
import hmac
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

MAGIC = b"ZHA\x01"
HEADER_SIZE = 64
VERSION = 1
FLAG_ENCRYPTED = 0x0001
FLAG_DEDUP = 0x0002
ENTRY_FLAG_FILE = 0x0000
ENTRY_FLAG_DIR = 0xFFFF
PBKDF2_ITERATIONS = 100_000
DEFAULT_COMPRESS_LEVEL = 6
DEFAULT_COMPRESS_THRESHOLD = 128
ZLIB_STRATEGIES = [
    zlib.Z_DEFAULT_STRATEGY,
    zlib.Z_FILTERED,
    zlib.Z_HUFFMAN_ONLY,
    zlib.Z_RLE,
    zlib.Z_FIXED,
]

# 流式压缩/解压的分块大小（1 MiB）
_STREAM_CHUNK = 1 << 20

# Header field layout (offsets within 64-byte header)
HDR_MAGIC = 0
HDR_VERSION = 4
HDR_FLAGS = 6
HDR_ENTRY_COUNT = 8
HDR_ENTRY_TABLE_SIZE = 12
HDR_DATA_OFFSET = 16
HDR_SEED_VERIFIER = 24
HDR_DERIVED_MAGIC = 32
HDR_COMPRESS_LEVEL = 36
HDR_COMPRESS_STRATEGY = 37
HDR_COMPRESS_THRESHOLD = 38
HDR_USER_COMPRESS_LEVEL = 40
HDR_ACTUAL_ZLIB_LEVEL = 41
HDR_WRAPPED_FORMAT = 42
HDR_WRAPPED_FORMAT_LEN = 14
HDR_RESERVED_END = 56
HDR_CRC32 = 60


def _compute_crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _compute_sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


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


def _derive_from_seed(seed_code: str) -> dict:
    """Derive all seed-dependent format parameters from an ASCII seed code."""
    seed_bytes = seed_code.encode("utf-8")
    params_hash = hashlib.sha256(b"params:" + seed_bytes).digest()
    return {
        "seed_verifier": hashlib.sha256(b"verify:" + seed_bytes).digest()[:8],
        "derived_magic": hashlib.sha256(b"magic:" + seed_bytes).digest()[:4],
        "compress_level": 1 + (params_hash[0] % 9),
        "compress_strategy": params_hash[1] % len(ZLIB_STRATEGIES),
        "compress_threshold": 64 + (params_hash[2] % 192),
    }


def _verify_seed(seed_code: str, expected_verifier: bytes) -> bool:
    """Check whether seed_code matches the stored verifier (constant-time)."""
    actual = hashlib.sha256(b"verify:" + seed_code.encode("utf-8")).digest()[:8]
    return hmac.compare_digest(actual, expected_verifier)


def _level_params(user_level: int) -> dict:
    """Map user compression level (0-10) to zlib parameters.

    Level 0 uses the default balanced profile. Higher levels increase
    zlib level from 6→9 and lower the threshold so smaller files also
    get compressed. Level 1 is the entry to proprietary compression.
    """
    if user_level <= 0:
        return {"zlib_level": DEFAULT_COMPRESS_LEVEL, "strategy": 0,
                "threshold": DEFAULT_COMPRESS_THRESHOLD}
    # user_level 1-10: zlib_level stays at 6 for Lv1, then ramps to 9
    zlib_level = min(9, DEFAULT_COMPRESS_LEVEL + max(0, user_level - 1))
    threshold = max(16, DEFAULT_COMPRESS_THRESHOLD - user_level * 12)
    return {"zlib_level": zlib_level, "strategy": 0, "threshold": threshold}


def _read_wrapped_format(header: bytes) -> str:
    """Read the wrapped format name from header, trimming null bytes."""
    raw = header[HDR_WRAPPED_FORMAT:HDR_WRAPPED_FORMAT + HDR_WRAPPED_FORMAT_LEN]
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def _is_v2_header(header: bytes) -> bool:
    """Return True if the header has V2-specific fields (seed or level)."""
    verifier = header[HDR_SEED_VERIFIER:HDR_SEED_VERIFIER + 8]
    if verifier != b"\x00" * 8:
        return True
    if header[HDR_USER_COMPRESS_LEVEL] != 0:
        return True
    if header[HDR_WRAPPED_FORMAT] != 0:  # first byte non-zero = has wrapped format
        return True
    return False


@register_handler
class CustomHandler(ArchiveHandler):

    @classmethod
    def format_name(cls) -> str:
        return "custom"

    @classmethod
    def extensions(cls) -> list[str]:
        return [".zhans"]

    @classmethod
    def supports_password(cls) -> bool:
        return True

    @classmethod
    def detect(cls, filepath: str) -> bool:
        """Detect by magic bytes — checks for ZHANS application signature."""
        try:
            with open(filepath, "rb") as f:
                return f.read(4) == MAGIC
        except OSError:
            return False

    @classmethod
    def get_origin_info(cls, filepath: str) -> dict:
        """Return structured origin information for UI display.

        Returns dict with keys:
            from_this_app, version, has_seed, seed_verifier,
            derived_magic, compress_level, compress_strategy,
            can_decompress, format_display
        """
        try:
            with open(filepath, "rb") as f:
                header = f.read(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                return {"from_this_app": False, "can_decompress": False,
                        "format_display": "文件损坏（头部不完整）"}
            magic = header[HDR_MAGIC:HDR_MAGIC + 4]
            if magic != MAGIC:
                return {"from_this_app": False, "can_decompress": False,
                        "format_display": "非本程序格式"}
            v2 = _is_v2_header(header)
            wrapped = _read_wrapped_format(header) if v2 else ""
            user_level = header[HDR_USER_COMPRESS_LEVEL] if v2 else 0
            info = {
                "from_this_app": True,
                "can_decompress": True,
                "version": 2 if v2 else 1,
                "has_seed": bool(header[HDR_SEED_VERIFIER:HDR_SEED_VERIFIER + 8] != b"\x00" * 8),
                "user_compress_level": user_level,
                "wrapped_format": wrapped,
            }
            if wrapped:
                info["format_display"] = f"{wrapped.upper()} 格式 (专有压缩 Lv.{user_level})"
            elif v2:
                info["format_display"] = f"自定义格式 V2 (压缩 Lv.{user_level})"
            else:
                info["format_display"] = "自定义格式 V1"
            if v2:
                info["seed_verifier"] = header[HDR_SEED_VERIFIER:HDR_SEED_VERIFIER + 8]
                info["derived_magic"] = header[HDR_DERIVED_MAGIC:HDR_DERIVED_MAGIC + 4]
                info["compress_level"] = header[HDR_COMPRESS_LEVEL]
                info["compress_strategy"] = header[HDR_COMPRESS_STRATEGY]
                info["compress_threshold"] = struct.unpack_from(
                    "<H", header, HDR_COMPRESS_THRESHOLD)[0]
            return info
        except Exception:
            return {"from_this_app": False, "can_decompress": False,
                    "format_display": "无法读取文件"}

    @classmethod
    def verify_seed(cls, filepath: str, seed_code: str) -> bool:
        """Check if seed_code matches the archive's stored verifier."""
        try:
            with open(filepath, "rb") as f:
                header = f.read(HEADER_SIZE)
            if not _is_v2_header(header):
                return False
            expected = header[HDR_SEED_VERIFIER:HDR_SEED_VERIFIER + 8]
            return _verify_seed(seed_code, expected)
        except Exception:
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
    def _build_entry_table(file_paths, include_v2_fields=False):
        """Build entry table bytes and meta list.

        When include_v2_fields=True, each entry gets content_sha256 and
        data_offset fields (40 extra bytes per file entry).
        """
        entries = CustomHandler._walk_files(file_paths)
        buf = io.BytesIO()
        meta_list = []

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
                        if include_v2_fields:
                            buf.write(b"\x00" * 32)   # content_sha256
                            buf.write(struct.pack("<Q", 0))  # data_offset
                        buf.write(struct.pack("<H", ENTRY_FLAG_DIR))
                        meta_list.append({
                            "name": sub, "size": 0, "compressed_size": 0,
                            "is_dir": True,
                        })

        for arcname, fp, size in entries:
            path_bytes = arcname.encode("utf-8")
            buf.write(struct.pack("<I", len(path_bytes)))
            buf.write(path_bytes)
            pad = _align4(len(path_bytes)) - len(path_bytes)
            if pad:
                buf.write(b"\x00" * pad)
            buf.write(struct.pack("<Q", size))
            buf.write(struct.pack("<Q", 0))
            buf.write(struct.pack("<I", 0))
            if include_v2_fields:
                buf.write(b"\x00" * 32)   # content_sha256 placeholder
                buf.write(struct.pack("<Q", 0))  # data_offset placeholder
            buf.write(struct.pack("<H", ENTRY_FLAG_FILE))
            meta_list.append({
                "name": arcname, "full_path": str(fp), "size": size,
                "compressed_size": 0, "is_dir": False,
                "content_sha256": None, "data_offset": None,
            })

        return buf.getvalue(), meta_list

    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        from Crypto.Protocol.KDF import PBKDF2
        from Crypto.Hash import SHA256
        return PBKDF2(password, salt, dkLen=32, count=PBKDF2_ITERATIONS,
                       hmac_hash_module=SHA256)

    @staticmethod
    def _encrypt_aes_gcm(plaintext: bytes, password: str) -> tuple[bytes, bytes, bytes]:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
        salt = get_random_bytes(16)
        nonce = get_random_bytes(12)
        key = CustomHandler._derive_key(password, salt)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return nonce, salt, ciphertext + tag

    @staticmethod
    def _decrypt_aes_gcm(nonce: bytes, salt: bytes, ciphertext_with_tag: bytes,
                          password: str):
        from Crypto.Cipher import AES
        key = CustomHandler._derive_key(password, salt)
        tag = ciphertext_with_tag[-16:]
        ciphertext = ciphertext_with_tag[:-16]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext

    @staticmethod
    def _compress_block(data: bytes, level: int = DEFAULT_COMPRESS_LEVEL,
                        strategy: int = 0,
                        threshold: int = DEFAULT_COMPRESS_THRESHOLD,
                        raw_deflate: bool = False,
                        ) -> tuple[bytes, int, bool]:
        if len(data) < threshold:
            return data, len(data), False
        if raw_deflate:
            strategy_idx = min(strategy, len(ZLIB_STRATEGIES) - 1)
            zlib_strategy = ZLIB_STRATEGIES[strategy_idx]
            compressor = zlib.compressobj(level, zlib.DEFLATED, -15, 9, zlib_strategy)
            compressed = compressor.compress(data) + compressor.flush()
        else:
            compressed = zlib.compress(data, level)
        if len(compressed) < len(data):
            return compressed, len(compressed), True
        return data, len(data), False

    @staticmethod
    def _decompress_block(data: bytes, size_hint: int,
                          raw_deflate: bool = False) -> bytes:
        """解压一个数据块；返回原始 data 表示该块未被压缩。

        用 decompressobj 增量解压并按声明大小设硬上限：绝不能先
        zlib.decompress 出一大块再校验，否则构造的压缩包可撑爆内存。
        """
        if len(data) >= size_hint and size_hint != 0:
            return data
        try:
            decompressor = zlib.decompressobj(-15 if raw_deflate else 15)
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
    def _parse_entry_table(entry_table_bytes: bytes, use_v2: bool) -> list[dict]:
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
            content_sha256 = None
            data_offset_val = None
            if use_v2:
                if pos + 32 > total:
                    raise ValueError("压缩包目录表不完整或已损坏")
                content_sha256 = entry_table_bytes[pos:pos + 32]
                pos += 32
                if pos + 8 > total:
                    raise ValueError("压缩包目录表不完整或已损坏")
                data_offset_val = struct.unpack_from(
                    "<Q", entry_table_bytes, pos)[0]
                pos += 8
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
                "content_sha256": content_sha256,
                "data_offset": data_offset_val,
            })
        return entries

    @staticmethod
    def _render_entry_table(meta_list: list[dict], use_v2: bool) -> bytes:
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
                if use_v2:
                    entry_table_buf.write(b"\x00" * 32)
                    entry_table_buf.write(struct.pack("<Q", 0))
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
                if use_v2:
                    entry_table_buf.write(
                        entry.get("content_sha256") or b"\x00" * 32)
                    entry_table_buf.write(
                        struct.pack("<Q", entry.get("data_offset") or 0))
                entry_table_buf.write(struct.pack("<H", ENTRY_FLAG_FILE))
        return entry_table_buf.getvalue()

    @staticmethod
    def _hash_file(src_path: str) -> tuple[bytes, int]:
        """分块计算文件的 SHA-256 与 CRC32，避免整文件读入内存。"""
        hasher = hashlib.sha256()
        crc = 0
        with open(src_path, "rb") as src:
            while True:
                chunk = src.read(_STREAM_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                crc = zlib.crc32(chunk, crc)
        return hasher.digest(), crc & 0xFFFFFFFF

    @staticmethod
    def _write_block_stream(out_file, src_path: str, level: int, strategy: int,
                            threshold: int, raw_deflate: bool,
                            declared_size: int) -> tuple[int, int]:
        """把文件数据流式写入 out_file（压缩或原样），返回 (comp_size, crc32)。

        分块压缩与一次性压缩的 deflate 输出完全一致；若压缩后并未变小，
        则回退到当前块起点重写原始数据，保证与旧实现逐字节相同。
        """
        start = out_file.tell()
        if declared_size < threshold:
            return CustomHandler._copy_raw_stream(out_file, src_path, start)
        if raw_deflate:
            strategy_idx = min(strategy, len(ZLIB_STRATEGIES) - 1)
            compressor = zlib.compressobj(level, zlib.DEFLATED, -15, 9,
                                          ZLIB_STRATEGIES[strategy_idx])
        else:
            compressor = zlib.compressobj(level)
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
        return CustomHandler._copy_raw_stream(out_file, src_path, start)

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
    def _decompress_block_to_file(data: bytes, size_hint: int, raw_deflate: bool,
                                  out_path: str) -> tuple[int, int]:
        """增量解压到 out_path，返回 (字节数, CRC32)。

        分块解压并按声明大小设硬上限，绝不先整块解压再校验；超出即抛错。
        """
        if len(data) >= size_hint and size_hint != 0:
            with open(out_path, "wb") as out:
                out.write(data)
            return len(data), _compute_crc32(data)
        try:
            decompressor = zlib.decompressobj(-15 if raw_deflate else 15)
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
            # 不是合法的 deflate 流：按原样存储处理（与旧行为一致）
            with open(out_path, "wb") as out:
                out.write(data)
            return len(data), _compute_crc32(data)

    @staticmethod
    def _build_header(magic, flags, entry_count, entry_table_size, data_offset,
                      seed_verifier=b"", derived_magic=b"",
                      compress_level=DEFAULT_COMPRESS_LEVEL,
                      compress_strategy=0, compress_threshold=DEFAULT_COMPRESS_THRESHOLD,
                      user_compress_level=0, wrapped_format="",
                      ) -> bytes:
        """Build a 64-byte header with seed-derived fields in the reserved area."""
        header = bytearray(HEADER_SIZE)
        struct.pack_into("<4sHHII", header, HDR_MAGIC,
                         magic, VERSION, flags, entry_count, entry_table_size)
        struct.pack_into("<Q", header, HDR_DATA_OFFSET, data_offset)
        # Seed-derived fields in reserved area
        if seed_verifier:
            header[HDR_SEED_VERIFIER:HDR_SEED_VERIFIER + 8] = seed_verifier
        if derived_magic:
            header[HDR_DERIVED_MAGIC:HDR_DERIVED_MAGIC + 4] = derived_magic
        header[HDR_COMPRESS_LEVEL] = compress_level & 0xFF
        header[HDR_COMPRESS_STRATEGY] = compress_strategy & 0xFF
        struct.pack_into("<H", header, HDR_COMPRESS_THRESHOLD, compress_threshold)
        # User compression level and wrapped format (offset 40-55)
        header[HDR_USER_COMPRESS_LEVEL] = user_compress_level & 0xFF
        header[HDR_ACTUAL_ZLIB_LEVEL] = compress_level & 0xFF
        wf_bytes = wrapped_format.encode("utf-8")[:HDR_WRAPPED_FORMAT_LEN]
        header[HDR_WRAPPED_FORMAT:HDR_WRAPPED_FORMAT + len(wf_bytes)] = wf_bytes
        # Compute CRC32
        crc = _compute_crc32(bytes(header[:HDR_CRC32]))
        struct.pack_into("<I", header, HDR_CRC32, crc)
        return bytes(header)

    # ===================================================================
    # Public API
    # ===================================================================

    @staticmethod
    def compress(
        file_paths, output_path,
        password=None, volume_size=None, progress_callback=None,
        seed_code=None, compress_level=0, wrapped_format="",
    ):
        if not file_paths:
            return {"path": output_path, "size": 0, "file_count": 0}

        # Map user compress level (0-10) to zlib params
        lp = _level_params(compress_level)
        use_v2 = compress_level > 0 or bool(wrapped_format)

        if seed_code:
            seed_params = _derive_from_seed(seed_code)
            # Seed params override base, but user level can make it more aggressive
            zlib_level = max(seed_params["compress_level"], lp["zlib_level"])
            zlib_strategy = max(seed_params["compress_strategy"], lp["strategy"])
            zlib_threshold = min(seed_params["compress_threshold"],
                                 lp["threshold"])
            seed_verifier = seed_params["seed_verifier"]
            derived_magic = seed_params["derived_magic"]
            use_v2 = True
        else:
            zlib_level = lp["zlib_level"]
            zlib_strategy = lp["strategy"]
            zlib_threshold = lp["threshold"]
            seed_verifier = b""
            derived_magic = b""

        entry_table_bytes, meta_list = CustomHandler._build_entry_table(
            file_paths, include_v2_fields=use_v2)
        file_entries = [m for m in meta_list if not m["is_dir"]]
        total_files = len(file_entries)
        entry_count = len(meta_list)
        flags = FLAG_DEDUP if use_v2 else 0

        header_kwargs = dict(
            seed_verifier=seed_verifier,
            derived_magic=derived_magic,
            compress_level=zlib_level,
            compress_strategy=zlib_strategy,
            compress_threshold=zlib_threshold,
            user_compress_level=compress_level,
            wrapped_format=wrapped_format,
        )

        reporter = _ProgressReporter(progress_callback, total_files)
        # 先写同目录临时文件，全部成功后再原子替换；失败时原压缩包保持不变
        tmp_path = f"{output_path}.{os.getpid()}.part"
        try:
            if password:
                # AES-GCM 需要整体加密，此路径仍按内存缓冲处理
                data_buf = io.BytesIO()
                dedup_map = {}
                for i, entry in enumerate(file_entries):
                    with open(entry["full_path"], "rb") as f:
                        raw = f.read()
                    entry["original_crc"] = _compute_crc32(raw)
                    if use_v2:
                        file_hash = _compute_sha256(raw)
                        entry["content_sha256"] = file_hash
                        if file_hash in dedup_map:
                            offset, comp_size = dedup_map[file_hash]
                            entry["compressed_size"] = comp_size
                            entry["data_offset"] = offset
                            reporter.update(i + 1)
                            continue
                    compressed, comp_size, _ = CustomHandler._compress_block(
                        raw, zlib_level, zlib_strategy, zlib_threshold,
                        raw_deflate=use_v2)
                    offset = data_buf.tell()
                    data_buf.write(compressed)
                    entry["compressed_size"] = comp_size
                    if use_v2:
                        entry["data_offset"] = offset
                        dedup_map[file_hash] = (offset, comp_size)
                    reporter.update(i + 1)
                reporter.finish()
                final_entry_table = CustomHandler._render_entry_table(
                    meta_list, use_v2)
                data_offset = HEADER_SIZE + len(final_entry_table)
                flags |= FLAG_ENCRYPTED
                payload = final_entry_table + data_buf.getvalue()
                nonce, salt, encrypted = CustomHandler._encrypt_aes_gcm(
                    payload, password)
                header = CustomHandler._build_header(
                    MAGIC, flags, entry_count, len(final_entry_table),
                    data_offset, **header_kwargs)
                with open(tmp_path, "wb") as f:
                    f.write(header)
                    f.write(nonce)
                    f.write(salt)
                    f.write(encrypted)
            else:
                # 流式写入：先占位目录表，数据直接落盘，最后回填目录表
                entry_table_size = len(entry_table_bytes)
                data_offset = HEADER_SIZE + entry_table_size
                header = CustomHandler._build_header(
                    MAGIC, flags, entry_count, entry_table_size,
                    data_offset, **header_kwargs)
                dedup_map = {}  # sha256 -> (offset, compressed_size)
                with open(tmp_path, "wb") as f:
                    f.write(header)
                    f.write(entry_table_bytes)
                    for i, entry in enumerate(file_entries):
                        if use_v2:
                            file_hash, file_crc = CustomHandler._hash_file(
                                entry["full_path"])
                            entry["content_sha256"] = file_hash
                            entry["original_crc"] = file_crc
                            if file_hash in dedup_map:
                                offset, comp_size = dedup_map[file_hash]
                                entry["compressed_size"] = comp_size
                                entry["data_offset"] = offset
                                reporter.update(i + 1)
                                continue
                        offset = f.tell() - data_offset
                        comp_size, crc = CustomHandler._write_block_stream(
                            f, entry["full_path"], zlib_level, zlib_strategy,
                            zlib_threshold, use_v2, entry["size"])
                        entry["compressed_size"] = comp_size
                        if not use_v2:
                            entry["original_crc"] = crc
                        if use_v2:
                            entry["data_offset"] = offset
                            dedup_map[file_hash] = (offset, comp_size)
                        reporter.update(i + 1)
                    reporter.finish()
                    final_entry_table = CustomHandler._render_entry_table(
                        meta_list, use_v2)
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
        seed_code=None,
    ):
        with open(archive_path, "rb") as f:
            file_size = os.fstat(f.fileno()).st_size
            header = f.read(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                raise ValueError("文件损坏：头部不完整")
            magic = header[HDR_MAGIC:HDR_MAGIC + 4]
            if magic != MAGIC:
                raise ValueError("不是有效的自定义压缩包")
            version, flags = struct.unpack_from("<HH", header, HDR_VERSION)
            entry_count, entry_table_size = struct.unpack_from(
                "<II", header, HDR_ENTRY_COUNT)
            data_offset = struct.unpack_from("<Q", header, HDR_DATA_OFFSET)[0]
            stored_crc = struct.unpack_from("<I", header, HDR_CRC32)[0]
            if _compute_crc32(header[:HDR_CRC32]) != stored_crc:
                raise ValueError("文件头部校验失败，文件可能已损坏")

            use_v2 = _is_v2_header(header)
            if use_v2:
                seed_verifier = header[HDR_SEED_VERIFIER:HDR_SEED_VERIFIER + 8]
                compress_level = header[HDR_COMPRESS_LEVEL]
                compress_strategy = header[HDR_COMPRESS_STRATEGY]
                compress_threshold = struct.unpack_from(
                    "<H", header, HDR_COMPRESS_THRESHOLD)[0]
                if seed_code:
                    if not _verify_seed(seed_code, seed_verifier):
                        raise ValueError(
                            "种子代码不匹配！该压缩包使用了不同的种子代码创建。"
                            "请检查种子代码是否正确。")
            else:
                compress_level = DEFAULT_COMPRESS_LEVEL
                compress_strategy = 0
                compress_threshold = DEFAULT_COMPRESS_THRESHOLD

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
                    payload = CustomHandler._decrypt_aes_gcm(
                        nonce, salt, remaining, password)
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

            # — Parse entry table (V1 or V2) —
            entries = CustomHandler._parse_entry_table(entry_table_bytes, use_v2)

            file_count = sum(1 for e in entries if not e["is_dir"])
            dedup_cache = {}  # (data_offset, comp_size) -> (out_path, crc32)
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

                use_dedup = use_v2 and entry["data_offset"] is not None
                cached = None
                if use_dedup:
                    cached = dedup_cache.get((entry["data_offset"],
                                              entry["compressed_size"]))
                # 逐条目写 .part，校验通过后再替换，失败不破坏已有文件
                part_path = f"{out_path}.{os.getpid()}.part"
                try:
                    if cached is not None:
                        src_path, crc = cached
                        if crc != entry["crc32"]:
                            raise ValueError(
                                f"文件校验失败: {name}, 文件可能已损坏")
                        shutil.copyfile(src_path, part_path)
                    else:
                        if use_dedup:
                            block_offset = entry["data_offset"]
                        else:
                            block_offset = data_pos
                            data_pos += entry["compressed_size"]
                        block_size = entry["compressed_size"]
                        # 与旧实现一致：块不小于声明大小即视为原样存储
                        if entry["size"] != 0 and block_size >= entry["size"]:
                            _, crc = CustomHandler._copy_block_to_file(
                                read_chunk, block_offset, block_size, part_path)
                        else:
                            compressed = read_block(block_offset, block_size)
                            _, crc = CustomHandler._decompress_block_to_file(
                                compressed, entry["size"], use_v2, part_path)
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
                if use_dedup:
                    dedup_cache[(entry["data_offset"],
                                 entry["compressed_size"])] = (out_path, crc)

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
                magic = struct.unpack_from("<4s", header, HDR_MAGIC)[0]
                if magic != MAGIC:
                    return []
                _, flags = struct.unpack_from("<HH", header, HDR_VERSION)
                entry_count, entry_table_size = struct.unpack_from(
                    "<II", header, HDR_ENTRY_COUNT)
                use_v2 = _is_v2_header(header)

                encrypted = bool(flags & FLAG_ENCRYPTED)
                if encrypted:
                    vlabel = "V2" if use_v2 else "V1"
                    return [{
                        "name": f"[加密压缩包 — 自定义格式 {vlabel}]",
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

            entries = CustomHandler._parse_entry_table(entry_table_bytes, use_v2)
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
    def extract_single(archive_path, member_name, dest_dir,
                       password=None, seed_code=None):
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="zhans_extract_")
        try:
            CustomHandler.decompress(
                archive_path, tmp_dir, password=password, seed_code=seed_code)
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
                f.read(4)
                version, flags = struct.unpack("<HH", f.read(4))
            return bool(flags & FLAG_ENCRYPTED)
        except Exception:
            return False
