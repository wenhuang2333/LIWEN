"""Path resolution between main app and repair EXE onedirs."""
import os
import sys

from utils.paths import dist_dir

REPAIR_ONEDIR_NAME = "ArchiveTool_Repair"
MAIN_ONEDIR_NAME = "ArchiveTool"

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
REPAIR_EXE_NAME = f"ArchiveTool_Repair{_EXE_SUFFIX}"
MAIN_EXE_NAME = f"ArchiveTool{_EXE_SUFFIX}"


def _find_executable(dir_path: str, base_name: str) -> str:
    """Find executable in dir_path, handling platform-specific suffix."""
    candidates = []
    if sys.platform == "win32":
        candidates = [f"{base_name}.exe"]
    else:
        candidates = [base_name, f"{base_name}.exe"]
    for c in candidates:
        p = os.path.join(dir_path, c)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return os.path.join(dir_path, candidates[0])


def find_repair_exe() -> str:
    """Return the absolute path to the repair executable."""
    if getattr(sys, "frozen", False):
        install_base = os.path.dirname(os.path.dirname(sys.executable))
        return _find_executable(os.path.join(install_base, REPAIR_ONEDIR_NAME),
                                "ArchiveTool_Repair")
    else:
        return _find_executable(os.path.join(dist_dir(), REPAIR_ONEDIR_NAME),
                                "ArchiveTool_Repair")


def find_main_exe() -> str:
    """Return the absolute path to the main executable."""
    if getattr(sys, "frozen", False):
        install_base = os.path.dirname(os.path.dirname(sys.executable))
        return _find_executable(os.path.join(install_base, MAIN_ONEDIR_NAME),
                                "ArchiveTool")
    else:
        return _find_executable(os.path.join(dist_dir(), MAIN_ONEDIR_NAME),
                                "ArchiveTool")
