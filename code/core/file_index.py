"""
File Index Engine — SQLite-backed file system index with a parallel scan engine.
Replaces the dict-based cache architecture with a persistent, queryable index.
"""

import os
import sqlite3
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QThread, pyqtSignal

from utils.logger import info, error, warn
from utils.paths import data_dir

# ── 扫描调优参数 ───────────────────────────────────────────────
# 一次 executemany 写入的行数。原实现每 500 行 execute 一次并 commit，
# 65 万文件要跑 1300+ 次事务；批量放大后事务数下降两个数量级。
_DB_BATCH_ROWS = 8000
# 两次 commit 之间的最小间隔（秒）。WAL 的自动 checkpoint 是 commit 开销的
# 主要来源，把 commit 压到每秒一次即可兼顾「扫描中可查询」与写入吞吐。
_DB_COMMIT_INTERVAL = 1.0
# 目录并行扫描线程数。磁盘扫描是 I/O 与 Python 字节码的混合负载：实测在
# SSD 上 4 线程约为单线程的 2 倍，再往上会因 GIL 争用与随机读放大而变慢。
_SCAN_WORKERS = max(1, min(4, os.cpu_count() or 1))

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
        # WAL 默认每 1000 页就自动 checkpoint 一次，批量导入时会把每次 commit
        # 都变成一次落盘。索引是可重建的缓存，放宽到 8000 页换取吞吐。
        self._conn.execute("PRAGMA wal_autocheckpoint=8000")
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
    #
    # 只提供「批量」写入接口。逐行 execute + 频繁 commit 是原扫描引擎最大的
    # 瓶颈：在 65 万文件的真实目录上实测 112 万次 execute 占 3.2s、1500 次
    # commit 又占 3.1s，合计接近总耗时的一半。统一走 executemany + 大事务后，
    # 这部分开销降到十分之一以下。

    def insert_rows(self, rows):
        """批量写入 files 行。

        *rows* 的每一项都是 8 元组
        ``(id, path, parent_id, name, size, is_dir, file_count, dir_count)``。
        调用方显式提供 id，因此目录与文件可以共用同一个自增计数器、不会撞号。
        """
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO files"
                "(id, path, parent_id, name, size, is_dir, file_count, dir_count) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)

    def update_dir_stats(self, rows):
        """批量回写目录的子树统计（自底向上聚合的结果）。

        *rows* 的每一项为 ``(size, file_count, dir_count, id)``，按主键更新，
        因此不需要在内存里保留目录路径。
        """
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE files SET size=?, file_count=?, dir_count=? WHERE id=?", rows)

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


# ── 单目录读取（工作线程内执行，纯函数、无 Qt） ────────────────

def _disk_bytes(st) -> int:
    """文件实际占用的磁盘字节数（而不是 ``st_size`` 的「表观大小」）。

    「空间索引」要回答的是"空间被谁占用了"，所以必须按**已分配块**统计：

    * APFS/HFS+ 的稀疏文件与 clone、Linux 的 sparse file 都会让 ``st_size``
      远大于真实占用。本机实测 ``Docker.raw`` 表观 994.61 GB、实际只占
      14.52 GB；``/Users`` 表观 1657 GB、实际 636 GB。按表观统计时整盘合计
      1896 GB —— 比物理容量（994 GB）还大，数字一眼就是错的，进度条也会在
      真实进度 40% 时就冲到 100%。
    * ``st_blocks`` 以 512 字节为单位，与 ``du``、访达「占用空间」、
      Windows 资源管理器「磁盘上的大小」口径一致。

    Windows 上 Python 不提供 ``st_blocks``（恒为 0），退回 ``st_size``：
    常见文件两者一致，代价是不识别 Windows 的稀疏文件。
    """
    if sys.platform == "win32":
        return st.st_size
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return st.st_size
    return blocks * 512


def _scan_one_dir(dirpath, cancel_check):
    """读取一个目录，返回 ``(subdirs, files, own_bytes, skipped)``。

    目录无法打开时返回 ``None``（调用方计为 skipped_dirs）。

    * ``subdirs`` —— ``[(path, st_dev, st_ino)]``。**inode 去重故意留给主线程**：
      跨目录去重必须在确定性顺序下进行，否则 macOS 上 ``/Users`` 与
      ``/System/Volumes/Data/Users``（firmlink，同一 inode）谁被保留会随线程
      调度变化，目录树会时有时无。
    * ``files`` —— ``[(path, name, 占用字节数)]``。
    """
    subdirs = []
    files = []
    own_bytes = 0
    skipped = 0
    try:
        with os.scandir(dirpath) as it:
            for entry in it:
                if cancel_check and cancel_check():
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        subdirs.append((entry.path, st.st_dev, st.st_ino))
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        size = _disk_bytes(st)
                        files.append((entry.path, entry.name, size))
                        own_bytes += size
                except OSError:
                    skipped += 1
    except (PermissionError, OSError):
        return None
    return subdirs, files, own_bytes, skipped


# ── 扫描主函数（纯 Python + SQLite，无 Qt） ────────────────────

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

    算法（相较旧实现的三点改进）：

    1. **目录统计全部在内存里做**。旧实现在第一阶段每发现一个子目录就要
       ``INSERT`` 一次再 ``SELECT`` 一次拿主键，第二阶段又对每个子目录和父目录
       各查一次数据库 —— 9 万个目录要发 27 万次查询。现在目录主键由本函数自己
       分配，聚合退化成纯字典/列表运算，SQLite 只承担批量导入。
    2. **自底向上聚合不再排序**。BFS 保证父目录下标一定小于子目录下标，因此
       倒序迭代天然就是「先子后父」；旧实现按路径深度做了一次 O(n log n) 的
       ``sorted()``（还要先 split 每个路径）。
    3. **目录并行扫描 + 目录 inode 去重**。前者在 SSD 上带来约 2 倍加速；后者
       修掉 macOS firmlink 导致的整盘重复计数（见 ``seen_dir_inodes`` 注释）。

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
         "os_used": int, "dir_count": int, "elapsed": float, "cancelled": bool,
         "deduped_dirs": int}
    """
    import shutil

    start_time = time.time()
    skipped_dirs = 0
    skipped_files = 0
    deduped_dirs = 0
    dir_count = 0
    scanned_bytes = 0
    cancelled = False

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

    def _is_cancelled():
        return bool(cancel_check and cancel_check())

    info(f"scan_directory: 开始扫描 {root_path} (并行度 {_SCAN_WORKERS})")

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

    # ── Phase 1: 广度优先遍历 + 内存聚合 ──────────────────────
    _emit_status("正在扫描目录结构...")

    # 目录的四组统计量按「广度优先序号」存在并行列表里，序号即下标。
    dir_parent = []    # 父目录下标，根为 -1
    dir_id = []        # 数据库主键
    dir_size = []      # 先放直属文件字节数，Phase 2 变成子树总字节数
    dir_fcount = []    # 同上：直属文件数 → 子树文件数
    dir_dcount = []    # 子树目录数

    root_name = os.path.basename(root_path.rstrip(os.sep)) or root_path
    next_id = 1
    dir_parent.append(-1)
    dir_id.append(next_id)
    dir_size.append(0)
    dir_fcount.append(0)
    dir_dcount.append(0)
    next_id += 1

    # 目录 inode 去重表。macOS 的 firmlink 会把同一个目录同时挂在 ``/`` 与
    # ``/System/Volumes/Data`` 下（st_dev/st_ino 完全相同），Windows 的目录
    # junction / Linux 的 bind mount 同理。不去重时扫描根目录会把
    # Applications、Users、Library 这些整份数据算两遍 —— 实测 70 万文件里有
    # 16.8 万次重复访问，磁盘用量虚高近一倍。
    # 因为遍历是「逐层」的，浅路径先于深路径入表，所以保留下来的总是
    # ``/Users`` 这种更自然的路径，而不是 ``/System/Volumes/Data/Users``。
    seen_dir_inodes = set()
    try:
        _st = os.stat(root_path, follow_symlinks=False)
        if _st.st_ino:
            seen_dir_inodes.add((_st.st_dev, _st.st_ino))
    except OSError:
        pass

    dir_rows = [(dir_id[0], root_path, None, root_name, 0, 1, 0, 0)]
    file_rows = []
    level = [(root_path, 0)]
    last_commit = time.time()
    last_progress = 0.0
    last_path_emit = 0.0

    def _flush(force=False):
        """把缓冲区交给 SQLite，并按时间节流提交与 UI 刷新。"""
        nonlocal last_commit
        if dir_rows:
            db.insert_rows(dir_rows)
            dir_rows.clear()
        if file_rows:
            db.insert_rows(file_rows)
            file_rows.clear()
        now = time.time()
        if force or now - last_commit >= _DB_COMMIT_INTERVAL:
            db.commit()
            last_commit = now
            _emit_dirs_updated()

    with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
        while level and not cancelled:
            if _is_cancelled():
                cancelled = True
                break

            paths = [p for p, _ in level]
            # chunksize 自适应：默认 map() 会为每一项都建一个 Future，
            # 单层十万目录时会凭空吃掉几十 MB；分块后 Future 数量降到千级。
            chunk = max(1, len(paths) // (_SCAN_WORKERS * 4))
            results = pool.map(lambda p: _scan_one_dir(p, cancel_check),
                               paths, chunksize=chunk)

            next_level = []
            for (dirpath, gidx), res in zip(level, results):
                dir_count += 1
                if res is None:
                    skipped_dirs += 1
                    continue

                subdirs, files, own_bytes, skipped = res
                skipped_files += skipped
                dir_size[gidx] += own_bytes
                dir_fcount[gidx] += len(files)
                scanned_bytes += own_bytes

                parent_db_id = dir_id[gidx]
                for fpath, fname, fsize in files:
                    file_rows.append(
                        (next_id, fpath, parent_db_id, fname, fsize, 0, 0, 0))
                    next_id += 1

                for spath, sdev, sino in subdirs:
                    key = (sdev, sino)
                    if sino and key in seen_dir_inodes:
                        deduped_dirs += 1
                        continue
                    if sino:
                        seen_dir_inodes.add(key)
                    child = len(dir_id)
                    dir_parent.append(gidx)
                    dir_id.append(next_id)
                    next_id += 1
                    dir_size.append(0)
                    dir_fcount.append(0)
                    dir_dcount.append(0)
                    dir_rows.append(
                        (dir_id[child], spath, parent_db_id,
                         os.path.basename(spath) or spath, 0, 1, 0, 0))
                    next_level.append((spath, child))

                if len(file_rows) + len(dir_rows) >= _DB_BATCH_ROWS:
                    _flush()

            level = next_level

            now = time.time()
            if now - last_progress > 0.4:
                elapsed = now - start_time
                speed = scanned_bytes / elapsed if elapsed > 0 else 0
                _emit_progress(scanned_bytes, total, elapsed, speed)
                last_progress = now
            if now - last_path_emit > 0.2:
                _emit_scanning_path(level[-1][0] if level else root_path)
                last_path_emit = now

    # 收尾写盘：取消时也要写，界面才能看到已经扫到的部分
    try:
        _flush(force=True)
    except Exception as e:
        warn(f"scan_directory: 收尾写入失败: {e}")

    # ── Phase 2: 自底向上聚合目录大小 ─────────────────────────
    # 倒序遍历即「先子后父」（BFS 性质保证父下标 < 子下标）。
    _emit_status("正在计算目录大小...")
    for i in range(len(dir_id) - 1, 0, -1):
        if _is_cancelled():
            cancelled = True
            break
        p = dir_parent[i]
        if p < 0:
            continue
        dir_size[p] += dir_size[i]
        dir_fcount[p] += dir_fcount[i]
        dir_dcount[p] += 1 + dir_dcount[i]

    try:
        db.update_dir_stats(
            [(dir_size[i], dir_fcount[i], dir_dcount[i], dir_id[i])
             for i in range(len(dir_id))])
        db.commit()
    except Exception as e:
        warn(f"scan_directory: 回写目录统计失败: {e}")
    _emit_dirs_updated()

    elapsed = time.time() - start_time
    total_skipped = skipped_dirs + skipped_files

    if cancelled:
        # 取消 = 索引不完整。显式记录，避免查询方把半份数据当成完整快照用。
        try:
            db.set_meta("scan_complete", "0")
            db.commit()
        except Exception as e:
            warn(f"scan_directory: 写入取消标记失败: {e}")
        _emit_status("扫描已取消")
        info(f"scan_directory: 已取消 — {dir_count} 个目录, 耗时 {elapsed:.1f}s")
        return {
            "scanned_bytes": scanned_bytes, "skipped_dirs": skipped_dirs,
            "skipped_files": skipped_files, "os_used": os_used_val,
            "dir_count": dir_count, "elapsed": elapsed, "cancelled": True,
            "deduped_dirs": deduped_dirs,
        }

    try:
        db.set_meta("scan_complete", "1")
        db.set_meta("scan_finished_at", str(time.time()))
        db.set_meta("scan_root", root_path)
        db.commit()
    except Exception as e:
        warn(f"scan_directory: 写入完成标记失败: {e}")

    _emit_progress(scanned_bytes, total, elapsed,
                   scanned_bytes / elapsed if elapsed > 0 else 0)
    dedup_note = f", {deduped_dirs} 个重复目录已去重" if deduped_dirs else ""
    _emit_status(f"扫描完成 — {dir_count} 个目录, {total_skipped} 个跳过"
                 f"{dedup_note}, 耗时 {elapsed:.1f}s")
    info(f"scan_directory: 完成 — {dir_count} 个目录, {total_skipped} 个跳过, "
         f"{deduped_dirs} 个重复目录已去重, 耗时 {elapsed:.1f}s")
    return {
        "scanned_bytes": scanned_bytes, "skipped_dirs": skipped_dirs,
        "skipped_files": skipped_files, "os_used": os_used_val,
        "dir_count": dir_count, "elapsed": elapsed, "cancelled": False,
        "deduped_dirs": deduped_dirs,
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
        self.deduped_dirs = 0

    def cancel(self):
        self._cancel = True

    @property
    def skipped_count(self):
        return self._skipped_dirs + self._skipped_files

    def run(self):
        # run() 里逃逸的异常会让页面永远收不到终止信号（进度条卡死），
        # 所以这里必须兜底并显式发 error。
        try:
            result = scan_directory(
                self._root_path, self._db,
                cancel_check=lambda: self._cancel,
                progress_callback=self.progress.emit,
                scanning_path_callback=self.scanning_path.emit,
                status_callback=self.status.emit,
                dirs_updated_callback=self.dirs_updated.emit,
            )
        except Exception as e:
            tb = traceback.format_exc()
            error(f"ScanEngine.run 异常: {e}\n{tb}")
            self.error.emit(f"扫描失败: {e}")
            return
        self.os_used = result.get("os_used", 0)
        self._skipped_dirs = result.get("skipped_dirs", 0)
        self._skipped_files = result.get("skipped_files", 0)
        self._dir_count = result.get("dir_count", 0)
        self.deduped_dirs = result.get("deduped_dirs", 0)
        self.finished.emit(
            result["scanned_bytes"], result["skipped_dirs"],
            result["skipped_files"], result["os_used"])
