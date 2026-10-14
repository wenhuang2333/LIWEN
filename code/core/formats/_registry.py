from typing import Type, Optional


class FormatRegistry:
    """Central registry of all ArchiveHandler subclasses.

    Handlers auto-register via the @register_handler decorator when their module
    is imported. Importing ``core.formats`` triggers all registrations.
    """

    _handlers: list[Type] = []

    @classmethod
    def register(cls, handler_cls: Type) -> None:
        if handler_cls not in cls._handlers:
            cls._handlers.append(handler_cls)

    @classmethod
    def detect(cls, filepath: str) -> Optional[Type]:
        """Auto-detect the format of an existing file.

        Prioritizes proprietary CustomHandler detection so that enhanced-
        compression files (which use the standard extension but our magic
        bytes) are correctly identified even when they have .zip etc.
        """
        from core.formats.custom_handler import CustomHandler
        if CustomHandler.detect(filepath):
            return CustomHandler
        for handler_cls in cls._handlers:
            if handler_cls is CustomHandler:
                continue
            if handler_cls.detect(filepath):
                return handler_cls
        return None

    @classmethod
    def get_by_name(cls, name: str) -> Optional[Type]:
        for handler_cls in cls._handlers:
            if handler_cls.format_name() == name:
                return handler_cls
        return None

    @classmethod
    def compressible_formats(cls) -> list[str]:
        return [h.format_name() for h in cls._handlers if h.can_compress()]

    @classmethod
    def all_extensions(cls) -> set[str]:
        result = set()
        for h in cls._handlers:
            for ext in h.extensions():
                result.add(ext)
        return result

    @classmethod
    def volume_formats(cls) -> list[str]:
        return [h.format_name() for h in cls._handlers if h.supports_volumes()]

    @classmethod
    def file_filter_string(cls) -> str:
        """Build a Qt file dialog filter string for all supported formats."""
        parts = []
        all_exts = []
        for h in cls._handlers:
            exts_str = " ".join(f"*{e}" for e in h.extensions())
            parts.append(f"{h.format_name().upper()} 文件 ({exts_str})")
            all_exts.extend(f"*{e}" for e in h.extensions())
        parts.insert(0, f"所有压缩文件 ({' '.join(sorted(all_exts))})")
        parts.append("所有文件 (*.*)")
        return ";;".join(parts)
