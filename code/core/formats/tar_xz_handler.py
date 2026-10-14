import os
import tarfile
from pathlib import Path
from typing import Optional, Callable

from core.formats._base import ArchiveHandler, register_handler, tar_extract


@register_handler
class TarXzHandler(ArchiveHandler):

    @classmethod
    def format_name(cls) -> str:
        return "tar.xz"

    @classmethod
    def extensions(cls) -> list[str]:
        return [".tar.xz", ".txz"]

    @classmethod
    def can_compress(cls) -> bool:
        return False

    @classmethod
    def supports_password(cls) -> bool:
        return False

    @classmethod
    def detect(cls, filepath: str) -> bool:
        ext = Path(filepath).suffix.lower()
        double = "".join(Path(filepath).suffixes[-2:]).lower()
        return ext == ".txz" or double == ".tar.xz"

    @staticmethod
    def compress(file_paths, output_path, password=None, volume_size=None,
                 progress_callback=None, seed_code=None, compress_level=0):
        if compress_level > 0:
            from core.formats.custom_handler import CustomHandler
            return CustomHandler.compress(
                file_paths, output_path,
                password=password, volume_size=volume_size,
                progress_callback=progress_callback,
                compress_level=compress_level,
                wrapped_format=TarXzHandler.format_name(),
            )
        raise NotImplementedError("TAR.XZ 不支持创建，请使用 TAR.GZ 或 TAR.BZ2")

    @staticmethod
    def decompress(archive_path, dest_dir, password=None, progress_callback=None):
        with tarfile.open(archive_path, "r:xz") as tar:
            members = tar.getmembers()
            total = len(members)
            for i, member in enumerate(members):
                tar_extract(tar, member, dest_dir)
                if progress_callback and total > 0:
                    progress_callback(int((i + 1) / total * 100))
        return {"dest": dest_dir, "file_count": total}

    @staticmethod
    def list_contents(archive_path: str) -> list[dict]:
        try:
            with tarfile.open(archive_path, "r:xz") as tar:
                return [
                    {
                        "name": info.name,
                        "size": info.size,
                        "compressed_size": info.size,
                        "modified": info.mtime,
                        "is_dir": info.isdir(),
                    }
                    for info in tar.getmembers()
                ]
        except Exception:
            return []

    @staticmethod
    def extract_single(archive_path, member_name, dest_dir, password=None):
        with tarfile.open(archive_path, "r:xz") as tar:
            tar_extract(tar, member_name, dest_dir)
        return os.path.join(dest_dir, member_name)

    @staticmethod
    def is_encrypted(archive_path: str) -> bool:
        return False
