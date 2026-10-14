"""Compatibility wrappers — prefer core.formats.FormatRegistry for new code."""

from core.formats import FormatRegistry


def decompress_zip(archive_path, dest_dir, password=None, progress_callback=None):
    return FormatRegistry.get_by_name("zip").decompress(
        archive_path, dest_dir, password=password,
        progress_callback=progress_callback,
    )


def decompress_7z(archive_path, dest_dir, password=None, progress_callback=None):
    return FormatRegistry.get_by_name("7z").decompress(
        archive_path, dest_dir, password=password,
        progress_callback=progress_callback,
    )


def detect_format(filepath):
    handler = FormatRegistry.detect(filepath)
    return handler.format_name() if handler else "unknown"


def get_supported_extensions():
    return list(FormatRegistry.all_extensions())
