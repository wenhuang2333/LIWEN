from abc import ABC, abstractmethod
from typing import Optional, Callable


class ArchiveHandler(ABC):
    """Abstract base class for all archive format handlers.

    To add a new format, subclass this and decorate with @register_handler.
    """

    # ---- metadata (override in subclass) ----

    @classmethod
    @abstractmethod
    def format_name(cls) -> str:
        """Short internal name: 'zip', '7z', 'shishan', etc."""

    @classmethod
    @abstractmethod
    def extensions(cls) -> list[str]:
        """File extensions including dot: ['.zip'], ['.tar.gz', '.tgz'], etc."""

    @classmethod
    def can_compress(cls) -> bool:
        return True

    @classmethod
    def can_decompress(cls) -> bool:
        return True

    @classmethod
    def can_preview(cls) -> bool:
        return True

    @classmethod
    def supports_volumes(cls) -> bool:
        return False

    @classmethod
    def supports_password(cls) -> bool:
        return True

    # ---- detection ----

    @classmethod
    @abstractmethod
    def detect(cls, filepath: str) -> bool:
        """Return True if filepath matches this format (extension + magic bytes)."""

    # ---- operations ----

    @staticmethod
    @abstractmethod
    def compress(
        file_paths: list[str],
        output_path: str,
        password: Optional[str] = None,
        volume_size: Optional[int] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
        seed_code: Optional[str] = None,
        compress_level: int = 0,
    ) -> dict:
        """Compress files into an archive. Returns {'path': str, 'size': int, 'file_count': int}."""

    @staticmethod
    @abstractmethod
    def decompress(
        archive_path: str,
        dest_dir: str,
        password: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> dict:
        """Decompress an archive. Returns {'dest': str, 'file_count': int}."""

    @staticmethod
    @abstractmethod
    def list_contents(archive_path: str) -> list[dict]:
        """Return [{'name': str, 'size': int, 'compressed_size': int, 'modified': Any, 'is_dir': bool}, ...]."""

    @staticmethod
    @abstractmethod
    def extract_single(
        archive_path: str,
        member_name: str,
        dest_dir: str,
        password: Optional[str] = None,
    ) -> str:
        """Extract a single member by name. Returns the output file path."""

    @staticmethod
    @abstractmethod
    def is_encrypted(archive_path: str) -> bool:
        """Return True if the archive is password-protected."""


def register_handler(cls):
    """Class decorator that auto-registers the handler with FormatRegistry."""
    from core.formats._registry import FormatRegistry
    FormatRegistry.register(cls)
    return cls


# ── tar 系列共用：安全解包 ────────────────────────────────────────

import os as _os
import re as _re
import sys as _sys

# Windows 盘符（含 UNC 前缀）——os.path.splitdrive 在 POSIX 上识别不出 "C:/x"，
# 而 tar 包可能是在 Windows 上打包的，因此必须用平台无关的写法。
_DRIVE_RE = _re.compile(r"^(?:[A-Za-z]:|//|\\\\)")


def _is_unsafe_member_name(name: str) -> bool:
    """判断 tar 成员名是否会逃出目标目录（跨平台，不依赖当前 OS）。"""
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(name):
        return True
    if _os.path.isabs(normalized):
        return True
    return ".." in normalized.split("/")


def tar_extract(tar, member, dest_dir: str):
    """安全地从 tar 中解出成员（兼容 Python 3.10/3.11）。

    ``tarfile.extract(..., filter="data")`` 只有 Python 3.12+ 才有；
    在 3.10/3.11 上传该参数会直接抛
    ``TypeError: extract() got an unexpected keyword argument 'filter'``,
    导致 tar / tar.gz / tar.bz2 / tar.xz 的**解压功能完全不可用**。
    这里按版本分支，并在老版本上手工做同等强度的校验（拒绝绝对路径、
    盘符和 ``..`` 穿越），而不是简单地丢掉防护。
    """
    name = member.name if hasattr(member, "name") else str(member)
    if _sys.version_info >= (3, 12):
        return tar.extract(member, dest_dir, filter="data")
    if _is_unsafe_member_name(name):
        raise ValueError(f"压缩包内包含非法路径: {name}")
    return tar.extract(member, dest_dir)
