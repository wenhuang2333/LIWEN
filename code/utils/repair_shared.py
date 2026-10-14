"""Shared serialization contract between main app and repair EXE."""
import os
from datetime import datetime

from utils import jsonstore
from utils.settings import SETTINGS_DIR

REPAIR_TASK_FILE = os.path.join(SETTINGS_DIR, "repair_task.json")
REPAIR_LOCK_FILE = os.path.join(SETTINGS_DIR, "repair.lock")
REPAIR_TASK_VERSION = "1.0"


def serialize_check_results(results: list, main_exe_path: str) -> str:
    """Write repair_task.json. Only includes fail/warning results with a repair_id."""
    payload = {
        "version": REPAIR_TASK_VERSION,
        "timestamp": datetime.now().isoformat(),
        "main_exe_path": main_exe_path,
        "results": [
            {
                "check_id": r.check_id,
                "category": r.category,
                "name": r.name,
                "status": r.status,
                "detail": r.detail,
                "suggestions": r.suggestions,
                "repair_id": r.repair_id,
                "timestamp": r.timestamp,
            }
            for r in results
            if r.status in ("fail", "warning") and r.repair_id
        ],
    }
    # 原子写入：修复进程可能立刻读取该文件，不能读到半截 JSON
    jsonstore.save_json(REPAIR_TASK_FILE, payload)
    return REPAIR_TASK_FILE


def load_check_results() -> dict | None:
    """Read and validate repair_task.json. Returns None if missing, corrupted, or wrong version."""
    data = jsonstore.load_json(REPAIR_TASK_FILE, default=None, expect_type=dict)
    if not data or data.get("version") != REPAIR_TASK_VERSION:
        return None
    return data


def delete_repair_task():
    """Remove the repair task file after repairs are complete."""
    try:
        if os.path.exists(REPAIR_TASK_FILE):
            os.remove(REPAIR_TASK_FILE)
    except OSError:
        pass


def acquire_lock() -> bool:
    """Try to acquire the repair lock. Returns True if lock acquired, False if already locked."""
    if os.path.exists(REPAIR_LOCK_FILE):
        try:
            mtime = os.path.getmtime(REPAIR_LOCK_FILE)
            age = datetime.now().timestamp() - mtime
            if age < 600:  # lock is less than 10 minutes old
                return False
        except OSError:
            pass
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(REPAIR_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def release_lock():
    """Release the repair lock."""
    try:
        if os.path.exists(REPAIR_LOCK_FILE):
            os.remove(REPAIR_LOCK_FILE)
    except OSError:
        pass
