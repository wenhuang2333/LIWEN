"""
File Index Engine — SQLite-backed file system index with os.scandir scan engine.
Replaces the dict-based cache architecture with a persistent, queryable index.
"""

import os
import sys
import sqlite3
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

from PyQt6.QtCore import QThread, pyqtSignal

from utils.logger import info, error, warn
from utils.paths import data_dir

# ── SQLite schema ──────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    parent_id INTEGER,
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    is_dir INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    dir_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES files(id)
);

CREATE INDEX IF NOT EXISTS idx_parent ON files(parent_id);
CREATE INDEX IF NOT EXISTS idx_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_is_dir ON files(is_dir);

CREATE TABLE IF NOT EXISTS scan_meta (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
);
"""


def _index_db_path(drive_path: str) -> str:
    """Generate the filesystem path for a drive's index database."""
    import re
    safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', drive_path.strip('/\\')).strip('_') or "root"
    return os.path.join(data_dir(), f"space_index_{safe_name}.db")


class IndexDatabase:
    """SQLite database for file index storage."""

    def __init__(self, db_path=":memory:"):
        self._db_path = db_path
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("PRAGMA cache_size=-64000")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    @property
    def conn(self):
        return self._conn

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def clear(self):
        """清空索引表，让下一次扫描得到干净、一致的快照。

        UI 路径（``SpaceIndexPage._start_scan``）会先删掉 .db 文件再重建，所以
        看不到问题；但 CLI 的 ``space-scan`` 是直接打开已有数据库再扫描的，
        旧的、已删除的文件记录会一直留着，目录大小也会被重复累加 —— 同一个
        盘扫描两次，查询结果里就会出现幽灵条目和翻倍的大小。
        """
        with self._lock:
            self._conn.execute("DELETE FROM files")
            self._conn.execute("DELETE FROM scan_meta")
            self._conn.commit()

    @property
    def file_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM files WHERE is_dir = 0").fetchone()
        return int(row[0]) if row else 0

    # ── Metadata persistence ────────────────────────────────────

    def set_meta(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scan_meta(key, value) VALUES (?, ?)",
                (key, value))

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM scan_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def get_all_meta(self):
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM scan_meta").fetchall()
        return dict(rows)

    # ── Write operations (thread-safe via lock) ────────────────

    def insert_file(self, path, parent_id, name, size):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO files(path, parent_id, name, size, is_dir, "
                "file_count, dir_count) VALUES (?,?,?,?,0,0,0)",
                (path, parent_id, name, size))

    def insert_dir_placeholder(self, path, name, parent_id=None):
        """Insert a directory placeholder (size=0). Does nothing if already exists."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO files(path, parent_id, name, size, is_dir, "
                "file_count, dir_count) VALUES (?,?,?,0,1,0,0)",
                (path, parent_id, name))

    def update_dir(self, path, parent_id, name, size, file_count, dir_count):
        """Update an existing directory's metadata after scan."""
        with self._lock:
            self._conn.execute(
                "UPDATE files SET parent_id=?, name=?, size=?, file_count=?, dir_count=? "
                "WHERE path=?", (parent_id, name, size, file_count, dir_count, path))

    def update_dir_size(self, path, size, file_count, dir_count):
        with self._lock:
            self._conn.execute(
                "UPDATE files SET size=?, file_count=?, dir_count=? WHERE path=?",
                (size, file_count, dir_count, path))

    def commit(self):
        with self._lock:
            self._conn.commit()

    # ── Read operations ────────────────────────────────────────

    def get_by_path(self, path):
        with self._lock:
            row = self._conn.execute(
                "SELECT id, path, parent_id, name, size, is_dir, file_count, dir_count "
                "FROM files WHERE path=?", (path,)).fetchone()
        if row:
            return dict(zip(
                ['id', 'path', 'parent_id', 'name', 'size', 'is_dir', 'file_count', 'dir_count'], row))
        return None

    def get_by_id(self, file_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT id, path, parent_id, name, size, is_dir, file_count, dir_count "
                "FROM files WHERE id=?", (file_id,)).fetchone()
        if row:
            return dict(zip(
                ['id', 'path', 'parent_id', 'name', 'size', 'is_dir', 'file_count', 'dir_count'], row))
        return None

    def get_parent_id(self, child_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT parent_id FROM files WHERE id=?", (child_id,)).fetchone()
        return row[0] if row else None

    def get_children(self, parent_id):
        """Return all children of a directory, sorted by size desc."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path, parent_id, name, size, is_dir, file_count, dir_count "
                "FROM files WHERE parent_id=? ORDER BY is_dir DESC, size DESC",
                (parent_id,)).fetchall()
        return [dict(zip(
            ['id', 'path', 'parent_id', 'name', 'size', 'is_dir', 'file_count', 'dir_count'], r))
            for r in rows]

    def get_children_count(self, parent_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM files WHERE parent_id=?", (parent_id,)).fetchone()
        return row[0] if row else 0

    def get_total_scanned(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM files WHERE is_dir=0").fetchone()
        return row[0] if row else 0

    def get_top_level_items(self, root_id):
        """Get top-level children for bar chart display."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, size, path FROM files WHERE parent_id=? "
                "ORDER BY size DESC LIMIT 80", (root_id,)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def get_path_chain(self, dir_id):
        """Return list of (id, name) from root to the given directory, for breadcrumbs."""
        chain = []
        current = dir_id
        while current is not None:
            entry = self.get_by_id(current)
            if entry is None:
                break
            chain.append((entry['id'], entry['name']))
            current = entry.get('parent_id')
        chain.reverse()
        return chain


# ── Pure file-flush helper ──────────────────────────────────────

def _flush_files_db(db: IndexDatabase, batch: list, path_to_id: dict = None):
    """Write a batch of file entries to the database (pure, no Qt)."""
    for fpath, parent_path, fname, size in batch:
        parent_id = path_to_id.get(parent_path) if path_to_id else None
        if parent_id is None:
            parent_row = db.get_by_path(parent_path)
            parent_id = parent_row['id'] if parent_row else None
            if parent_id is not None and path_to_id is not None:
                path_to_id[parent_path] = parent_id
        db.insert_file(fpath, parent_id, fname, size)
    db.commit()


# ── Pure scan function ──────────────────────────────────────────

def scan_directory(
    root_path: str,
    db: IndexDatabase,
    cancel_check=None,
    progress_callback=None,
    scanning_path_callback=None,
    status_callback=None,
    dirs_updated_callback=None,
) -> dict:
    """Scan a directory tree and write results to IndexDatabase (pure, no Qt).

    Args:
        root_path: Root directory to scan.
        db: An IndexDatabase instance (not :memory:).
        cancel_check: callable() -> bool, return True to abort.
        progress_callback: callable(scanned_bytes, total, elapsed, speed).
        scanning_path_callback: callable(current_dir_path).
        status_callback: callable(status_text).
        dirs_updated_callback: callable() — called when new dirs are available.

    Returns:
        {"scanned_bytes": int, "skipped_dirs": int, "skipped_files": int,
         "os_used": int, "dir_count": int, "elapsed": float, "cancelled": bool}
    """
    import shutil
    start_time = time.time()
    skipped_dirs = 0
    skipped_files = 0
    dir_count = 0
    os_used_val = 0
    scanned_bytes = 0
    file_count = 0
    last_progress = 0

    try:
        os_used_val = shutil.disk_usage(root_path).used
    except Exception:
        os_used_val = 0
    total = os_used_val

    def _emit_status(msg):
        if status_callback:
            status_callback(msg)

    def _emit_progress(sb, t, el, sp):
        if progress_callback:
            progress_callback(sb, t, el, sp)

    def _emit_scanning_path(p):
        if scanning_path_callback:
            scanning_path_callback(p)

    def _emit_dirs_updated():
        if dirs_updated_callback:
            dirs_updated_callback()

    _emit_status("正在扫描...")

    # 扫描前清空：保证结果是一份完整快照，而不是与上次扫描的叠加。
    # 未完成（取消/异常）时把 scan_complete 标记为 0，查询方可以据此提示
    # "索引不完整"，而不是把半份数据当成完整的用。
    try:
        db.clear()
    except Exception as e:
        warn(f"scan_directory: 清空旧索引失败: {e}")
    try:
        db.set_meta("scan_complete", "0")
        db.set_meta("scan_started_at", str(time.time()))
        db.commit()
    except Exception as e:
        warn(f"scan_directory: 写入扫描元数据失败: {e}")

    # Phase 1: Collect all directory paths and files via os.scandir
    root_name = os.path.basename(root_path) or root_path
    db.insert_dir_placeholder(root_path, root_name)
    db.commit()
    root_info = db.get_by_path(root_path)
    root_id = root_info['id'] if root_info else None

    all_dir_paths = [root_path]
    dir_queue = deque([root_path])
    dir_info_temp = {}
    file_batch = []
    path_to_id = {root_path: root_id}

    _emit_status("正在扫描目录结构...")

    while dir_queue and not (cancel_check and cancel_check()):
        dirpath = dir_queue.popleft()
        dir_count += 1

        if dir_count % 20 == 0:
            _emit_scanning_path(dirpath)

        try:
            entries = list(os.scandir(dirpath))
        except (PermissionError, OSError, FileNotFoundError):
            skipped_dirs += 1
            continue

        dir_file_total = 0
        dir_file_count = 0
        subdirs = []

        for entry in entries:
            if cancel_check and cancel_check():
                break
            try:
                if entry.is_file(follow_symlinks=False):
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    dir_file_total += size
                    dir_file_count += 1
                    scanned_bytes += size
                    file_count += 1
                    file_batch.append((entry.path, dirpath, entry.name, size))

                    if len(file_batch) >= 500:
                        _flush_files_db(db, file_batch, path_to_id)
                        file_batch.clear()
                        _emit_dirs_updated()

                elif entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                    all_dir_paths.append(entry.path)
                    dir_queue.append(entry.path)
                    parent_db_id = path_to_id.get(dirpath)
                    db.insert_dir_placeholder(entry.path, entry.name, parent_db_id)
                    placeholder = db.get_by_path(entry.path)
                    if placeholder:
                        path_to_id[entry.path] = placeholder['id']

            except (PermissionError, OSError):
                skipped_files += 1

        dir_info_temp[dirpath] = {
            'file_total': dir_file_total,
            'file_count': dir_file_count,
            'subdirs': subdirs,
        }

        now = time.time()
        if now - last_progress > 0.4:
            elapsed = now - start_time
            speed = scanned_bytes / elapsed if elapsed > 0 else 0
            _emit_progress(scanned_bytes, total, elapsed, speed)
            last_progress = now

    if file_batch:
        _flush_files_db(db, file_batch, path_to_id)
        file_batch.clear()
        _emit_dirs_updated()

    path_to_id.clear()

    _emit_status("正在计算目录大小...")
    _emit_dirs_updated()

    cancelled = cancel_check() if cancel_check else False
    if cancelled:
        _emit_status("扫描已取消")
        return {
            "scanned_bytes": scanned_bytes, "skipped_dirs": skipped_dirs,
            "skipped_files": skipped_files, "os_used": os_used_val,
            "dir_count": dir_count, "elapsed": time.time() - start_time,
            "cancelled": True,
        }

    # Phase 2: Build directory sizes bottom-up
    _emit_status("正在计算目录大小...")

    sorted_dirs = sorted(all_dir_paths,
                         key=lambda p: len(p.rstrip(os.sep).split(os.sep)),
                         reverse=True)

    batch_count = 0
    last_dir_update = time.time()
    for dirpath in sorted_dirs:
        if cancel_check and cancel_check():
            cancelled = True
            break

        info = dir_info_temp.get(dirpath)
        if info is None:
            continue

        total_sz = info['file_total']
        fc = info['file_count']
        dc = 0

        for sub_path in info['subdirs']:
            sub_row = db.get_by_path(sub_path)
            if sub_row and sub_row['is_dir']:
                total_sz += sub_row['size']
                fc += sub_row['file_count']
                dc += 1 + sub_row['dir_count']

        parent_path = os.path.dirname(dirpath)
        if parent_path == dirpath:
            parent_id = None
        else:
            parent_row = db.get_by_path(parent_path)
            parent_id = parent_row['id'] if parent_row else None

        name = os.path.basename(dirpath) or dirpath
        db.update_dir(dirpath, parent_id, name, total_sz, fc, dc)

        del dir_info_temp[dirpath]

        batch_count += 1
        if batch_count % 500 == 0:
            db.commit()
            now = time.time()
            if now - last_dir_update >= 30.0:
                _emit_dirs_updated()
                last_dir_update = now

    db.commit()
    _emit_dirs_updated()

    all_dir_paths.clear()

    if cancelled:
        # 取消 = 索引不完整。显式记录，避免查询方把半份数据当成完整快照用。
        try:
            db.set_meta("scan_complete", "0")
            db.commit()
        except Exception as e:
            warn(f"scan_directory: 写入取消标记失败: {e}")
        _emit_status("扫描已取消")
        return {
            "scanned_bytes": scanned_bytes, "skipped_dirs": skipped_dirs,
            "skipped_files": skipped_files, "os_used": os_used_val,
            "dir_count": dir_count, "elapsed": time.time() - start_time,
            "cancelled": True,
        }

    elapsed = time.time() - start_time
    total_skipped = skipped_dirs + skipped_files
    try:
        db.set_meta("scan_complete", "1")
        db.set_meta("scan_finished_at", str(time.time()))
        db.set_meta("scan_root", root_path)
        db.commit()
    except Exception as e:
        warn(f"scan_directory: 写入完成标记失败: {e}")
    _emit_status(f"扫描完成 — {dir_count} 个目录, {total_skipped} 个跳过, 耗时 {elapsed:.1f}s")
    return {
        "scanned_bytes": scanned_bytes, "skipped_dirs": skipped_dirs,
        "skipped_files": skipped_files, "os_used": os_used_val,
        "dir_count": dir_count, "elapsed": elapsed, "cancelled": False,
    }


# ── Scan Engine (QThread wrapper) ───────────────────────────────

class ScanEngine(QThread):
    """QThread wrapper around scan_directory() — bridges callbacks to pyqtSignals."""

    progress = pyqtSignal(object, object, float, float)
    scanning_path = pyqtSignal(str)
    status = pyqtSignal(str)
    finished = pyqtSignal(object, object, object, object)
    error = pyqtSignal(str)
    dirs_updated = pyqtSignal()

    def __init__(self, root_path: str, db: IndexDatabase):
        super().__init__()
        self._root_path = root_path
        self._db = db
        self._cancel = False
        self.os_used = 0
        self._skipped_dirs = 0
        self._skipped_files = 0
        self._dir_count = 0

    def cancel(self):
        self._cancel = True

    @property
    def skipped_count(self):
        return self._skipped_dirs + self._skipped_files

    def run(self):
        result = scan_directory(
            self._root_path, self._db,
            cancel_check=lambda: self._cancel,
            progress_callback=self.progress.emit,
            scanning_path_callback=self.scanning_path.emit,
            status_callback=self.status.emit,
            dirs_updated_callback=self.dirs_updated.emit,
        )
        self.os_used = result.get("os_used", 0)
        self._skipped_dirs = result.get("skipped_dirs", 0)
        self._skipped_files = result.get("skipped_files", 0)
        self._dir_count = result.get("dir_count", 0)
        self.finished.emit(
            result["scanned_bytes"], result["skipped_dirs"],
            result["skipped_files"], result["os_used"])
