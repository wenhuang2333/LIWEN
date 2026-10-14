"""Compatibility wrappers — prefer core.formats.FormatRegistry for new code."""

from core.formats import FormatRegistry


def compress_zip(file_paths, output_path, password=None, progress_callback=None):
    return FormatRegistry.get_by_name("zip").compress(
        file_paths, output_path, password=password,
        progress_callback=progress_callback,
    )


def compress_7z(file_paths, output_path, password=None, volume_size=None, progress_callback=None):
    return FormatRegistry.get_by_name("7z").compress(
        file_paths, output_path, password=password,
        volume_size=volume_size, progress_callback=progress_callback,
    )
