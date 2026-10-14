"""Compatibility wrappers — prefer core.formats.FormatRegistry for new code."""

from core.formats import FormatRegistry


def list_archive_contents(filepath):
    handler = FormatRegistry.detect(filepath)
    if handler and handler.can_preview():
        return handler.list_contents(filepath)
    return []


def extract_single_file(archive_path, member_name, dest_dir, password=None):
    handler = FormatRegistry.detect(archive_path)
    if handler:
        return handler.extract_single(archive_path, member_name, dest_dir, password=password)
    raise ValueError(f"不支持的格式: {archive_path}")


def is_encrypted(filepath):
    handler = FormatRegistry.detect(filepath)
    if handler:
        return handler.is_encrypted(filepath)
    return False
