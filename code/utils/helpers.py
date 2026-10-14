import os
import sys
import hashlib
import subprocess
import tempfile
import uuid
from pathlib import Path


def open_file_or_dir(path: str):
    """Cross-platform open a file or directory with the OS default handler."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def spawn_detached(args, cwd=None):
    """Launch a detached/independent subprocess. Returns the Popen object."""
    if sys.platform == "win32":
        kwargs = dict(
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | 0x00000008,
            close_fds=True,
        )
    else:
        kwargs = dict(
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if cwd:
        kwargs["cwd"] = cwd
    return subprocess.Popen(args, **kwargs)


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_speed(bytes_per_sec):
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    elif bytes_per_sec < 1024 * 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    else:
        return f"{bytes_per_sec / (1024 * 1024 * 1024):.2f} GB/s"


def hash_file(filepath, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_common_parent(paths):
    if not paths:
        return ""
    if len(paths) == 1:
        p = Path(paths[0])
        return str(p.parent) if p.is_file() else str(p)
    parents = [Path(p).parent if Path(p).is_file() else Path(p) for p in paths]
    try:
        common = Path(os.path.commonpath([str(p) for p in parents]))
        return str(common)
    except ValueError:
        return str(Path(paths[0]).parent)


def get_temp_archive_path(prefix="lan_transfer"):
    return os.path.join(tempfile.gettempdir(), f"{prefix}_{uuid.uuid4().hex}.zip")


def is_archive_file(filepath):
    from core.formats import FormatRegistry
    return FormatRegistry.detect(filepath) is not None


def get_output_archive_name(files, fmt):
    ext = "zhans" if fmt == "custom" else fmt
    if not files:
        return f"archive.{ext}"
    if len(files) == 1:
        base = Path(files[0]).stem
    else:
        base = Path(get_common_parent(files)).name or "archive"
    return f"{base}.{ext}"


def count_files(paths):
    count = 0
    for p in paths:
        p = Path(p)
        if p.is_file():
            count += 1
        elif p.is_dir():
            for _, _, filenames in os.walk(p):
                count += len(filenames)
    return count
