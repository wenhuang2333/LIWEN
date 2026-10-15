import os
import string
import sys
import time
import datetime
import subprocess
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTableView, QHeaderView, QProgressBar, QToolTip,
    QMenu, QApplication,
)
from PyQt6.QtCore import (
    Qt, QModelIndex, QSortFilterProxyModel, QRectF, QTimer,
)
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QFont, QFontMetrics, QMouseEvent,
    QAction,
)

from core.file_index import IndexDatabase, _index_db_path
from core.workers import SpaceScanWorker
from ui.file_list_model import FileListModel
from utils.helpers import format_size
from utils.logger import info, warn, error
from ui.about_page import APP_VERSION

# 「占用」而非「大小」：索引按磁盘实际分配块统计（与访达「占用空间」、
# du、资源管理器「磁盘上的大小」同口径），不是文件表观大小。
TREE_COLUMNS = ["名称", "占用", "占比", "项目数"]

# 等待扫描线程退出的最长时间 (毫秒)。扫描线程会持续向 SQLite 写入,
# 未确认其退出前绝不能断开信号、释放引用或关闭数据库连接。
_SCAN_STOP_TIMEOUT_MS = 5000
# 扫描线程健康检查周期 (毫秒): 线程异常结束 (run() 抛异常) 时不会发出
# finished/error, 页面靠该定时器兜底复位 UI, 避免进度条永久卡死。
_SCAN_WATCHDOG_INTERVAL_MS = 700
# 线程启动后的宽限期 (秒), 避免把 "尚未真正启动" 误判为 "已异常结束"。
_SCAN_START_GRACE_SEC = 3.0


def _multipart_size(total_bytes):
    if total_bytes <= 0:
        return "0 K"
    b = int(total_bytes)
    k = b // 1024; b = b % 1024
    m = k // 1024; k = k % 1024
    g = m // 1024; m = m % 1024
    t = g // 1024; g = g % 1024
    parts = []
    if t > 0:
        parts.append(f"{t}T")
    if g > 0:
        parts.append(f"{g}G")
    if m > 0:
        parts.append(f"{m}M")
    if k > 0:
        parts.append(f"{k}K")
    if not parts:
        parts.append("0K")
    return " ".join(parts)


def _format_speed(bytes_per_sec):
    if bytes_per_sec <= 0:
        return ""
    if bytes_per_sec >= 1024 ** 3:
        return f"{bytes_per_sec / 1024 ** 3:.1f} GB/s"
    if bytes_per_sec >= 1024 ** 2:
        return f"{bytes_per_sec / 1024 ** 2:.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


# Linux 上不需要单独列出的伪文件系统 / 容器层
_LINUX_SKIP_FSTYPES = frozenset([
    "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs",
    "cgroup", "cgroup2", "autofs", "ramfs", "debugfs", "tracefs",
    "fusectl", "mqueue", "bpf", "configfs", "pstore", "securityfs",
    "hugetlbfs", "efivarfs", "binfmt_misc", "nsfs",
])


def _get_available_drives():
    """列出可扫描的驱动器 / 挂载点（Windows / macOS / Linux 三平台）。

    Linux 分支原先直接 ``return ["/"]``，外接硬盘和独立挂载的分区在界面上
    完全看不到，用户只能扫根分区。改用 ``psutil.disk_partitions()`` 枚举真实
    挂载点，并过滤掉 snap / tmpfs / overlay 这类伪文件系统。
    """
    drives = []
    if sys.platform == "win32":
        try:
            from ctypes import windll
            bitmask = windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    path = f"{letter}:\\"
                    try:
                        res = windll.kernel32.GetDriveTypeW(path)
                        if res in (2, 3, 4, 5, 6):
                            drives.append(path)
                    except Exception:
                        drives.append(path)
                bitmask >>= 1
        except Exception:
            for letter in string.ascii_uppercase:
                path = f"{letter}:\\"
                if os.path.exists(path):
                    drives.append(path)
    elif sys.platform == "darwin":
        drives.append("/")
        try:
            for name in os.listdir("/Volumes"):
                vol_path = os.path.join("/Volumes", name)
                if os.path.ismount(vol_path) and not name.startswith("."):
                    drives.append(vol_path)
        except Exception:
            pass
    else:
        drives.append("/")
        try:
            import psutil
            for part in psutil.disk_partitions(all=False):
                mountpoint = part.mountpoint
                if not mountpoint or mountpoint == "/":
                    continue
                if (part.fstype or "").lower() in _LINUX_SKIP_FSTYPES:
                    continue
                if mountpoint.startswith(("/snap/", "/var/snap/", "/run/", "/sys/")):
                    continue
                if os.path.isdir(mountpoint) and mountpoint not in drives:
                    drives.append(mountpoint)
        except Exception as e:
            warn(f"_get_available_drives: 枚举 Linux 挂载点失败，仅扫描根目录: {e}")
    return drives


def _get_drive_label(drive_path):
    if drive_path == "/":
        return "根目录 (/)"
    if sys.platform == "darwin":
        if drive_path.startswith("/Volumes/"):
            return os.path.basename(drive_path)
        return drive_path
    label = drive_path.rstrip("\\")
    if sys.platform == "win32":
        try:
            from ctypes import windll, create_unicode_buffer
            buf = create_unicode_buffer(256)
            windll.kernel32.GetVolumeInformationW(
                drive_path, buf, 256, None, None, None, None, 0)
            vol = buf.value.strip()
            if vol:
                label += f" ({vol})"
        except Exception:
            pass
    return label


class BarChartWidget(QWidget):
    """Horizontal bar chart — top items sorted by size descending."""

    BAR_COLORS = [
        QColor("#89b4fa"), QColor("#a6e3a1"), QColor("#f9e2af"),
        QColor("#fab387"), QColor("#f38ba8"), QColor("#cba6f7"),
        QColor("#94e2d5"), QColor("#74c7ec"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._hover_idx = -1
        self._max_size = 0
        self.setMouseTracking(True)
        self.setMinimumHeight(140)
        self.setMaximumHeight(220)

    def set_items(self, items):
        self._items = items[:80]
        self._max_size = max((s for _, s, _ in self._items), default=1)
        self._hover_idx = -1
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#1e1e2e"))

        if not self._items:
            painter.setPen(QColor("#9498a4"))
            painter.setFont(QFont("", 10))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                           "扫描后将显示文件大小分布")
            painter.end()
            return

        ml, mb, mt, mr = 6, 22, 6, 4
        chart_w = self.width() - ml - mr
        chart_h = self.height() - mb - mt
        if chart_w <= 0 or chart_h <= 0:
            painter.end()
            return

        n = len(self._items)
        bar_w = max(3, (chart_w - (n - 1) * 1) // n)
        font = QFont("", 7)
        painter.setFont(font)
        fm = QFontMetrics(font)
        max_val = self._max_size

        for i, (name, size, path) in enumerate(self._items):
            x = ml + i * (bar_w + 1)
            frac = size / max_val if max_val > 0 else 0
            bar_h = max(2, int(frac * chart_h))
            y = mt + chart_h - bar_h

            color = self.BAR_COLORS[i % len(self.BAR_COLORS)]
            if i == self._hover_idx:
                color = color.lighter(140)

            painter.setPen(QPen(QColor("#11111b"), 1))
            painter.setBrush(color)
            painter.drawRect(QRectF(x, y, bar_w, bar_h))

            if bar_h > 16 and bar_w > 24:
                label = format_size(size)
                if fm.horizontalAdvance(label) < bar_w - 2:
                    painter.setPen(QColor("#e8e8f0"))
                    painter.drawText(QRectF(x, y - 14, bar_w, 14),
                                     Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(QPen(QColor("#45475a"), 1))
        axis_y = mt + chart_h
        painter.drawLine(ml, axis_y, ml + chart_w, axis_y)
        painter.setPen(QColor("#9498a4"))
        painter.drawText(QRectF(ml, axis_y + 2, chart_w // 2, 14),
                         Qt.AlignmentFlag.AlignLeft, "0")
        painter.drawText(QRectF(ml + chart_w // 2, axis_y + 2, chart_w // 2, 14),
                         Qt.AlignmentFlag.AlignRight, format_size(max_val))
        painter.end()

    def mouseMoveEvent(self, event: QMouseEvent):
        old = self._hover_idx
        self._hover_idx = -1
        if not self._items:
            return
        ml = 6
        n = len(self._items)
        bar_w = max(3, (self.width() - ml - 4 - (n - 1)) // n)
        px = event.position().x()
        idx = int((px - ml) // (bar_w + 1))
        if 0 <= idx < n:
            self._hover_idx = idx
            name, size, path = self._items[idx]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{path}\n{format_size(size)}",
                self)
        else:
            QToolTip.hideText()
        if old != self._hover_idx:
            self.update()

    def leaveEvent(self, event):
        if self._hover_idx != -1:
            self._hover_idx = -1
            QToolTip.hideText()
            self.update()


class DriveUsageBar(QWidget):
    """Drive usage summary — scanned | unscanned (system/permission) | free space."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scanned = 0
        self._os_used = 0
        self._total = 0
        self.setFixedHeight(32)
        self.setMinimumHeight(24)

    def set_usage(self, scanned, os_used, total):
        self._scanned = scanned
        self._os_used = os_used
        self._total = total
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        if w <= 0 or self._total <= 0:
            painter.end()
            return

        # Layout constants
        text_w = 100
        bar_x = text_w + 6
        bar_w = w - bar_x - 8
        bar_h = h - 4
        bar_y = 2

        painter.fillRect(self.rect(), QColor("#1e1e2e"))

        if bar_w <= 20 or self._total <= 0:
            painter.end()
            return

        # Fractions
        frac_scanned = min(self._scanned / self._total, 1.0)
        frac_os = min(self._os_used / self._total, 1.0)
        frac_unscanned = max(0, frac_os - frac_scanned)
        frac_free = 1.0 - frac_os

        scanned_w = int(frac_scanned * bar_w)
        unscanned_w = int(frac_unscanned * bar_w)
        free_w = bar_w - scanned_w - unscanned_w

        # Scanned (green)
        if scanned_w > 0:
            painter.setBrush(QColor("#a6e3a1"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(bar_x, bar_y, scanned_w, bar_h), 2, 2)

        # Unscanned / system-reserved (yellow/orange)
        if unscanned_w > 1:
            painter.setBrush(QColor("#f9e2af"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(bar_x + scanned_w, bar_y, unscanned_w, bar_h), 2, 2)

        # Free space (dark gray)
        if free_w > 1:
            painter.setBrush(QColor("#313244"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(bar_x + scanned_w + unscanned_w, bar_y, free_w, bar_h), 2, 2)

        # Legend text
        font = QFont("", 8)
        painter.setFont(font)
        painter.setPen(QColor("#a6e3a1"))
        painter.drawText(QRectF(0, 0, text_w, h / 2), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         f"已扫描 {format_size(self._scanned)}")

        if frac_unscanned > 0.005:
            unscanned_abs = int(self._os_used - self._scanned)
            painter.setPen(QColor("#f9e2af"))
            painter.drawText(QRectF(0, h / 2, text_w, h / 2), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"系统保留 {format_size(unscanned_abs)}")

        painter.end()


class SpaceIndexPage(QWidget):
    """Disk space analyzer — SQLite-backed index + custom QAbstractItemModel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._db = None
        self._root_path = ""
        self._scanning = False
        self._scanned_bytes = 0
        self._total_drive = 0
        self._phase1_complete = False
        self._scan_started_at = 0.0

        # Navigation state
        self._nav_stack = []  # list of (dir_id, dir_name) for back navigation
        self._current_nav_id = None  # currently displayed directory DB id

        # 扫描线程看门狗: run() 异常逃逸时不会发出任何终止信号, 靠它兜底复位
        self._scan_watchdog = QTimer(self)
        self._scan_watchdog.setInterval(_SCAN_WATCHDOG_INTERVAL_MS)
        self._scan_watchdog.timeout.connect(self._check_scan_health)

        self._setup_ui()

        # Check for cached results after UI is set up
        self._on_drive_changed()
        info("SpaceIndexPage.__init__")

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(8)

        title = QLabel("空间索引")
        title.setObjectName("myCardTitle")
        root.addWidget(title)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        drive_lbl = QLabel("选择驱动器:")
        drive_lbl.setStyleSheet("color: #c8ccd4; font-size: 13px;")
        toolbar.addWidget(drive_lbl)

        self._drive_combo = QComboBox()
        self._drive_combo.setMinimumWidth(200)
        self._drive_combo.setStyleSheet(
            "QComboBox { background: #313244; color: #e8e8f0; border: 1px solid #45475a;"
            "  border-radius: 4px; padding: 6px 12px; font-size: 13px; }"
            "QComboBox:hover { border-color: #89b4fa; }"
            "QComboBox QAbstractItemView { background: #313244; color: #e8e8f0;"
            "  selection-background-color: #45475a; border: 1px solid #45475a; }"
        )
        toolbar.addWidget(self._drive_combo)
        self._populate_drives()
        self._drive_combo.currentIndexChanged.connect(self._on_drive_changed)

        self._scan_btn = QPushButton("扫描")
        self._scan_btn.setObjectName("actionBtn")
        self._scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scan_btn.clicked.connect(self._start_scan)
        toolbar.addWidget(self._scan_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._cancel_scan)
        toolbar.addWidget(self._cancel_btn)

        self._scanned_label = QLabel("")
        self._scanned_label.setStyleSheet(
            "color: #a6e3a1; font-size: 13px; font-weight: 600;")
        toolbar.addWidget(self._scanned_label)

        self._cached_label = QLabel("")
        self._cached_label.setStyleSheet(
            "color: #f9e2af; font-size: 12px;")
        toolbar.addWidget(self._cached_label)

        toolbar.addStretch()
        root.addLayout(toolbar)

        # Bar chart
        self._bar_chart = BarChartWidget()
        root.addWidget(self._bar_chart)

        # Drive usage summary bar (visible after scan)
        self._drive_bar = DriveUsageBar()
        self._drive_bar.setVisible(False)
        root.addWidget(self._drive_bar)

        # ── Breadcrumb + Navigation bar ──
        nav_bar = QHBoxLayout()
        nav_bar.setSpacing(6)

        self._nav_back_btn = QPushButton("◀ 后退")
        self._nav_back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nav_back_btn.setEnabled(False)
        self._nav_back_btn.clicked.connect(self._navigate_back)
        self._nav_back_btn.setStyleSheet(
            "QPushButton { background: #313244; color: #c8ccd4; border: 1px solid #45475a;"
            "  border-radius: 4px; padding: 5px 10px; font-size: 12px; }"
            "QPushButton:hover { background: #45475a; color: #e8e8f0; }"
            "QPushButton:disabled { color: #585b70; }"
        )
        nav_bar.addWidget(self._nav_back_btn)

        self._nav_up_btn = QPushButton("▲ 向上")
        self._nav_up_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nav_up_btn.setEnabled(False)
        self._nav_up_btn.clicked.connect(self._navigate_up)
        self._nav_up_btn.setStyleSheet(self._nav_back_btn.styleSheet())
        nav_bar.addWidget(self._nav_up_btn)

        self._breadcrumb_widget = QWidget()
        self._breadcrumb_layout = QHBoxLayout(self._breadcrumb_widget)
        self._breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self._breadcrumb_layout.setSpacing(2)
        self._breadcrumb_layout.addStretch()
        nav_bar.addWidget(self._breadcrumb_widget, 1)

        root.addLayout(nav_bar)

        # ── Table view (flat directory listing) ──
        self._table_view = QTableView()
        self._table_view.setSortingEnabled(True)
        self._table_view.setShowGrid(False)
        self._table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table_view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table_view.verticalHeader().setVisible(False)
        self._table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table_view.customContextMenuRequested.connect(self._on_table_context_menu)
        self._table_view.doubleClicked.connect(self._on_table_double_click)
        self._table_view.setStyleSheet(
            "QTableView { background: #181825; color: #e8e8f0; border: 1px solid #313244;"
            "  border-radius: 6px; font-size: 13px; outline: none;"
            "  alternate-background-color: #1e1e2e; gridline-color: #252536; }"
            "QTableView::item { padding: 6px 8px; border-bottom: 1px solid #252536; }"
            "QTableView::item:hover { background-color: #313244; }"
            "QTableView::item:selected { background-color: #45475a; }"
            "QTableView QHeaderView::section {"
            "  background-color: #252536; color: #c8ccd4; padding: 6px 8px;"
            "  border: none; border-right: 1px solid #313244; font-weight: 600; font-size: 12px; }"
            "QTableView QHeaderView::section:hover { background-color: #313244; }"
        )

        # Placeholder model; real model set on scan start / cache load
        self._file_model = None
        self._proxy_model = QSortFilterProxyModel()
        self._proxy_model.setSortRole(Qt.ItemDataRole.UserRole)
        self._table_view.setModel(self._proxy_model)

        h_header = self._table_view.horizontalHeader()
        h_header.setStretchLastSection(True)
        h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        h_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        h_header.setMinimumSectionSize(60)
        self._table_view.sortByColumn(1, Qt.SortOrder.DescendingOrder)

        root.addWidget(self._table_view, 1)

        # ── Bottom detail strip ──
        self._detail_strip = QLabel("")
        self._detail_strip.setStyleSheet(
            "color: #9498a4; font-size: 12px; background: #1e1e2e;"
            "border: 1px solid #313244; border-radius: 4px; padding: 6px 12px;"
        )
        self._detail_strip.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._detail_strip.setVisible(False)
        root.addWidget(self._detail_strip)

        # Connect selection to detail strip
        sel_model = self._table_view.selectionModel()
        if sel_model is not None:
            sel_model.selectionChanged.connect(self._on_table_selection_changed)

        # Bottom bar
        bottom = QHBoxLayout()
        bottom.setSpacing(12)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(8)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)
        bottom.addWidget(self._progress_bar, 1)

        self._status_label = QLabel("就绪 — 选择一个驱动器并点击扫描")
        self._status_label.setStyleSheet("color: #9498a4; font-size: 12px;")
        bottom.addWidget(self._status_label)

        root.addLayout(bottom)

    def _populate_drives(self):
        drives = _get_available_drives()
        self._drive_combo.clear()
        for d in drives:
            self._drive_combo.addItem(_get_drive_label(d), d)

    # ── Cache / Persistence ──────────────────────────────────────

    def _check_drive_cache(self, drive_path: str) -> tuple:
        """Return (has_cache: bool, metadata: dict, db_path: str)."""
        db_path = _index_db_path(drive_path)
        if not os.path.exists(db_path):
            return False, {}, db_path
        try:
            cdb = IndexDatabase(db_path)
            meta = cdb.get_all_meta()
            cdb.close()
            return True, meta, db_path
        except Exception:
            return False, {}, db_path

    def _on_drive_changed(self):
        """Handle drive selection change — check cache and update UI state."""
        if self._scanning:
            return
        drive_path = self._drive_combo.currentData()
        if not drive_path:
            return
        has_cache, meta, db_path = self._check_drive_cache(drive_path)
        if has_cache:
            self._load_cached_results(drive_path, meta, db_path)
        else:
            self._show_no_cache_state(drive_path)

    def _load_cached_results(self, drive_path: str, meta: dict, db_path: str):
        """Load and display cached scan results from a file-based DB."""
        # Close previous DB if any
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass

        self._root_path = drive_path
        self._scanning = False
        self._phase1_complete = True
        self._nav_stack.clear()

        self._db = IndexDatabase(db_path)
        self._file_model = FileListModel(self._db)
        self._proxy_model.setSourceModel(self._file_model)

        # Navigate to root
        root_info = self._db.get_by_path(drive_path)
        if root_info:
            self._file_model.set_root_size(root_info['size'])
            self._current_nav_id = root_info['id']
            self._file_model.set_directory(root_info['id'])
        self._update_breadcrumbs()

        self._refresh_bar_chart()

        # Drive usage bar
        try:
            import shutil
            du = shutil.disk_usage(drive_path)
            scanned = int(meta.get('scanned_bytes', 0))
            os_used = int(meta.get('os_used', 0))
            self._drive_bar.set_usage(scanned, os_used, du.total)
        except Exception:
            self._drive_bar.set_usage(0, 0, 1)
        self._drive_bar.setVisible(True)

        # Update UI state
        scan_time = meta.get('scan_time', '未知')
        scanned_str = _multipart_size(int(meta.get('scanned_bytes', 0)))
        self._cached_label.setText(f"上次扫描: {scan_time} | 已扫描: {scanned_str}")
        self._scan_btn.setText("重新扫描")
        self._scanned_label.setText(f"已扫描: {scanned_str}")
        self._status_label.setText(f"已加载缓存 — 上次扫描: {scan_time}")
        self._progress_bar.setVisible(False)
        self._cancel_btn.setVisible(False)

        info(f"SpaceIndexPage._load_cached_results: {drive_path} ({scan_time})")

    def _show_no_cache_state(self, drive_path: str):
        """Show placeholder when no cached scan exists for the drive."""
        self._reset_scan_state()
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._root_path = drive_path
        self._cached_label.setText("")
        self._scan_btn.setText("扫描")
        self._scanned_label.setText("")
        self._status_label.setText("尚未扫描该驱动器 — 点击「扫描」开始分析")
        self._drive_bar.setVisible(False)
        info(f"SpaceIndexPage._show_no_cache_state: {drive_path}")

    # ── Navigation ────────────────────────────────────────────────

    def _update_breadcrumbs(self):
        """Rebuild the breadcrumb bar from the current navigation path."""
        # Clear existing breadcrumb buttons
        while self._breadcrumb_layout.count() > 1:  # keep the trailing stretch
            item = self._breadcrumb_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._current_nav_id is None or self._db is None:
            return

        chain = self._db.get_path_chain(self._current_nav_id)
        for i, (dir_id, name) in enumerate(chain):
            btn = QPushButton(name if name else self._root_path)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFlat(True)
            btn.setStyleSheet(
                "QPushButton { color: #89b4fa; font-size: 12px; padding: 3px 6px;"
                "  border: none; border-radius: 3px; background: transparent; }"
                "QPushButton:hover { background: #313244; text-decoration: underline; }"
            )
            btn.clicked.connect(lambda checked, did=dir_id: self._navigate_to(did))
            self._breadcrumb_layout.insertWidget(i, btn)

            if i < len(chain) - 1:
                sep = QLabel(">")
                sep.setStyleSheet("color: #585b70; font-size: 12px; background: transparent; padding: 0 2px;")
                self._breadcrumb_layout.insertWidget(i * 2 + 1, sep)

        # Update nav button states
        self._nav_back_btn.setEnabled(len(self._nav_stack) > 0)
        # Up is enabled if current directory has a parent
        if self._file_model and self._current_nav_id is not None:
            entry = self._db.get_by_id(self._current_nav_id)
            has_parent = entry and entry.get('parent_id') is not None
            self._nav_up_btn.setEnabled(has_parent)
        else:
            self._nav_up_btn.setEnabled(False)

    def _navigate_to(self, dir_id):
        """Navigate into a directory, pushing current onto the nav stack."""
        if self._file_model is None:
            return
        if self._current_nav_id is not None and self._current_nav_id != dir_id:
            # Don't push if navigating to same directory
            cur_entry = self._db.get_by_id(self._current_nav_id)
            if cur_entry:
                self._nav_stack.append((self._current_nav_id, cur_entry.get('name', '')))
            else:
                self._nav_stack.append((self._current_nav_id, ''))
        self._current_nav_id = dir_id
        self._file_model.set_directory(dir_id)
        root_info = self._db.get_by_path(self._root_path)
        if root_info:
            self._file_model.set_root_size(root_info['size'])
        self._update_breadcrumbs()
        self._detail_strip.setVisible(False)
        info(f"SpaceIndexPage._navigate_to: dir_id={dir_id}")

    def _navigate_back(self):
        """Pop the nav stack and return to the previous directory."""
        if not self._nav_stack:
            return
        prev_id, prev_name = self._nav_stack.pop()
        self._current_nav_id = prev_id
        if self._file_model:
            self._file_model.set_directory(prev_id)
            root_info = self._db.get_by_path(self._root_path)
            if root_info:
                self._file_model.set_root_size(root_info['size'])
        self._update_breadcrumbs()
        self._detail_strip.setVisible(False)

    def _navigate_up(self):
        """Go to the parent of the current directory."""
        if self._current_nav_id is None or self._db is None:
            return
        entry = self._db.get_by_id(self._current_nav_id)
        if entry and entry.get('parent_id') is not None:
            self._navigate_to(entry['parent_id'])

    # ── Table handlers ────────────────────────────────────────────

    def _on_table_double_click(self, index):
        """Double-click a directory to navigate into it."""
        if self._file_model is None:
            return
        proxy_idx = index
        source_idx = self._proxy_model.mapToSource(proxy_idx)
        row = source_idx.row()
        if self._file_model.is_dir(row):
            child_id = self._file_model.get_child_id(row)
            if child_id is not None:
                self._navigate_to(child_id)

    def _on_table_selection_changed(self, selected, deselected):
        """Update the bottom detail strip with info about the selected item."""
        if self._db is None or self._file_model is None:
            return
        indexes = self._table_view.selectionModel().selectedRows()
        if not indexes:
            self._detail_strip.setVisible(False)
            return

        proxy_idx = indexes[0]
        source_idx = self._proxy_model.mapToSource(proxy_idx)
        row = source_idx.row()
        child = self._file_model.get_child_info(row)
        if child is None:
            self._detail_strip.setVisible(False)
            return

        is_dir = bool(child['is_dir'])
        icon = "📁" if is_dir else "📄"
        name = child['name']
        path = child['path']
        size = format_size(child['size'])
        if is_dir:
            type_str = "文件夹"
            items_str = f"{child['file_count'] + child['dir_count']} 项"
        else:
            ext = os.path.splitext(child['name'])[1].lower()
            type_str = ext if ext else "文件"
            items_str = ""

        # Try to get modification time
        try:
            mtime_ts = os.path.getmtime(path)
            mtime_str = datetime.datetime.fromtimestamp(mtime_ts).strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime_str = ""

        parts = [f"{icon} {name}", f"占用: {size}"]
        if type_str:
            parts.append(f"类型: {type_str}")
        if items_str:
            parts.append(items_str)
        if mtime_str:
            parts.append(f"修改: {mtime_str}")
        parts.append(f"路径: {path}")

        self._detail_strip.setText("  |  ".join(parts))
        self._detail_strip.setVisible(True)

    def _on_table_context_menu(self, pos):
        """Right-click context menu on the table view."""
        indexes = self._table_view.selectionModel().selectedRows()
        if not indexes:
            return
        proxy_idx = indexes[0]
        source_idx = self._proxy_model.mapToSource(proxy_idx)
        path = source_idx.data(Qt.ItemDataRole.UserRole)
        if not path:
            return

        menu = QMenu(self._table_view)
        menu.setStyleSheet(
            "QMenu { background: #313244; color: #e8e8f0; border: 1px solid #45475a;"
            "  padding: 4px; }"
            "QMenu::item { padding: 6px 24px; border-radius: 3px; }"
            "QMenu::item:selected { background: #45475a; }"
        )

        open_action = menu.addAction("📂 在资源管理器中打开")
        open_action.triggered.connect(lambda: self._open_in_explorer(path))

        menu.addSeparator()

        copy_action = menu.addAction("📋 复制路径")
        copy_action.triggered.connect(lambda: self._copy_path(path))

        copy_name_action = menu.addAction("📋 复制名称")
        name = source_idx.data(Qt.ItemDataRole.DisplayRole)
        copy_name_action.triggered.connect(lambda n=name: self._copy_text(n))

        menu.exec(self._table_view.viewport().mapToGlobal(pos))

    def _open_in_explorer(self, path):
        from utils.helpers import open_file_or_dir
        try:
            if sys.platform == "win32" and not os.path.isdir(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                open_file_or_dir(path if os.path.isdir(path) else os.path.dirname(path))
        except Exception as e:
            error(f"打开文件管理器失败: {e}")

    def _copy_path(self, text):
        QApplication.clipboard().setText(text)
        self._status_label.setText(f"已复制: {text}")

    def _copy_text(self, text):
        QApplication.clipboard().setText(text)
        self._status_label.setText(f"已复制: {text}")

    # ── Scan lifecycle ─────────────────────────────────────────

    def _start_scan(self):
        idx = self._drive_combo.currentIndex()
        if idx < 0:
            return
        root_path = self._drive_combo.currentData()
        if not root_path or not os.path.exists(root_path):
            return

        # 重入保护: 上一次扫描线程仍在运行时绝不复用/关闭它的数据库连接
        if self._worker is not None and self._worker.isRunning():
            warn("SpaceIndexPage._start_scan: 上一次扫描仍在运行, 已忽略本次启动")
            self._status_label.setText("上一次扫描仍在停止中，请稍候...")
            return
        # 线程已结束但引用未回收: 先安全回收, 再开新扫描
        if self._worker is not None:
            self._release_worker(_SCAN_STOP_TIMEOUT_MS)

        info(f"SpaceIndexPage._start_scan: {root_path}")

        self._reset_scan_state()
        self._root_path = root_path
        self._scanning = True
        self._scan_started_at = time.monotonic()
        self._phase1_complete = False

        # Close previous DB
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass

        # Create fresh file-based database (delete old cache first)
        db_path = _index_db_path(root_path)
        try:
            os.remove(db_path)
        except OSError:
            pass
        self._db = IndexDatabase(db_path)

        # Create model backed by this database
        self._file_model = FileListModel(self._db)
        self._proxy_model.setSourceModel(self._file_model)

        self._detail_strip.setVisible(False)

        self._scan_btn.setVisible(False)
        self._cancel_btn.setVisible(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 10000)
        self._progress_bar.setValue(0)
        self._scanned_label.setText("")
        self._cached_label.setText("")
        self._status_label.setText(f"正在扫描 {root_path}...")
        self._drive_combo.setEnabled(False)

        self._worker = SpaceScanWorker(root_path, self._db)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.scanning_path.connect(self._on_scanning_path)
        self._worker.status.connect(self._status_label.setText)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.error.connect(self._on_scan_error)
        self._worker.dirs_updated.connect(self._on_dirs_updated)
        self._worker.start()
        self._scan_watchdog.start()

    def _reset_scan_state(self):
        self._root_path = ""
        self._scanning = False
        self._phase1_complete = False
        self._scanned_bytes = 0
        self._total_drive = 0
        self._nav_stack.clear()
        self._current_nav_id = None
        self._proxy_model.setSourceModel(None)
        self._file_model = None
        self._bar_chart.set_items([])
        self._progress_bar.setValue(0)
        self._scanned_label.setText("")
        self._cached_label.setText("")
        self._status_label.setText("")
        self._drive_bar.setVisible(False)
        self._detail_strip.setVisible(False)
        self._nav_back_btn.setEnabled(False)
        self._nav_up_btn.setEnabled(False)
        # Clear breadcrumbs
        while self._breadcrumb_layout.count() > 1:
            item = self._breadcrumb_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _check_scan_health(self):
        """看门狗：扫描线程已死但页面仍停在"扫描中"时兜底复位。

        ScanEngine/SpaceScanWorker 正常结束一定会发 finished/error。如果线程
        已经不在运行、却迟迟没有终止信号（例如异常在信号发出前就逃逸了），
        进度条会永久卡住、扫描按钮也不再可用。这里只做状态复位，不动数据。
        """
        if not self._scanning:
            return
        w = self._worker
        if w is not None and w.isRunning():
            return
        if w is None and self._scan_started_at and \
                time.monotonic() - self._scan_started_at < _SCAN_WATCHDOG_INTERVAL_MS / 1000.0:
            # 刚启动、worker 还没来得及赋值，给它一个周期
            return
        warn("SpaceIndexPage._check_scan_health: 扫描线程已结束但没有终止信号，复位界面状态")
        self._scanning = False
        self._scan_watchdog.stop()
        self._scan_btn.setVisible(True)
        self._cancel_btn.setVisible(False)
        self._drive_combo.setEnabled(True)
        self._progress_bar.setVisible(False)
        self._drive_bar.setVisible(False)
        self._scan_btn.setText("扫描")
        self._status_label.setText("扫描已中断（详见日志）")
        if self._worker is not None:
            self._release_worker(1000)

    def _release_worker(self, timeout_ms: int = _SCAN_STOP_TIMEOUT_MS) -> bool:
        """安全回收扫描线程：先取消，再等它真正退出，最后才断开信号。

        返回 True 表示线程已停止、引用已清空；False 表示它仍在运行，调用方
        **必须保留引用**（绝不能在活线程上 disconnect 或置 None，否则会丢掉
        对线程的控制权，也拿不到它的结束信号）。
        """
        w = self._worker
        if w is None:
            return True
        try:
            if w.isRunning():
                w.cancel()
                if not w.wait(timeout_ms):
                    warn(f"SpaceIndexPage: 扫描线程未在 {timeout_ms}ms 内停止，保留引用")
                    return False
            # 逐个断开我们连接过的信号，而不是无参 disconnect()。
            # 无参调用是通配断开，连 QObject.destroyed 一起断，Qt 会在
            # stderr 打印 "wildcard call disconnects from destroyed signal"。
            for sig in ("progress", "scanning_path", "status",
                        "finished", "error", "dirs_updated"):
                s = getattr(w, sig, None)
                if s is not None:
                    try:
                        s.disconnect()
                    except (TypeError, RuntimeError):
                        pass
        except Exception as e:
            warn(f"SpaceIndexPage._release_worker 异常: {e}")
            return False
        self._worker = None
        return True

    def _cancel_scan(self):
        self._scanning = False
        self._phase1_complete = False
        if self._worker:
            # 必须等线程真正退出后再断开信号 / 丢弃引用。
            # 原实现只调用 cancel() 就 disconnect + 置 None，而 cancel() 在
            # ScanEngine 还没构造好时是空操作，线程仍在跑；随后用户可以立刻
            # 重新扫描，_start_scan 会关掉旧连接、删库、建新库，正在写入的老
            # 线程于是抛 sqlite3.ProgrammingError —— 异常逃出 run()，页面永远
            # 收不到 finished/error，进度条卡死。
            if not self._release_worker():
                self._scan_btn.setVisible(True)
                self._cancel_btn.setVisible(False)
                self._status_label.setText("正在停止…")
                return
        self._scan_btn.setVisible(True)
        self._cancel_btn.setVisible(False)
        self._drive_combo.setEnabled(True)
        self._progress_bar.setVisible(False)
        self._drive_bar.setVisible(False)
        self._cached_label.setText("")
        self._scan_btn.setText("扫描")
        self._status_label.setText("扫描已取消")
        info("SpaceIndexPage._cancel_scan")

    # ── Signal handlers ────────────────────────────────────────

    def _on_scanning_path(self, path):
        if not self._scanning:
            return
        try:
            self._status_label.setText(f"正在扫描: {path}")
        except Exception:
            pass

    def _on_dirs_updated(self):
        """Handle incremental directory updates during scanning."""
        if not self._scanning:
            return
        try:
            if self._file_model and not self._phase1_complete:
                # Phase 1 just finished — navigate to root for first time
                root_info = self._db.get_by_path(self._root_path)
                if root_info:
                    self._file_model.set_root_size(root_info['size'])
                    self._current_nav_id = root_info['id']
                    self._file_model.set_directory(root_info['id'])
                    self._update_breadcrumbs()
                self._phase1_complete = True
                self._status_label.setText("正在计算目录大小...")
            elif self._file_model and self._current_nav_id is not None:
                # Phase 2 incremental update — refresh current directory
                self._file_model.set_directory(self._current_nav_id)
                root_info = self._db.get_by_path(self._root_path)
                if root_info:
                    self._file_model.set_root_size(root_info['size'])
                self._update_breadcrumbs()
            self._refresh_bar_chart()
        except Exception as e:
            error(f"SpaceIndexPage._on_dirs_updated 异常: {e}")

    def _on_scan_progress(self, scanned, total, elapsed, speed):
        if not self._scanning:
            return
        try:
            scanned_i = int(scanned)
            total_i = int(total) if total else 0
        except (TypeError, ValueError, SystemError):
            return
        if total_i > 0 and self._total_drive == 0:
            self._total_drive = total_i
        if self._total_drive > 0:
            pct = int(scanned_i / self._total_drive * 10000)
            if pct > 10000:
                pct = 10000
            self._progress_bar.setValue(pct)
        self._scanned_bytes = scanned_i
        try:
            speed_str = _format_speed(int(speed)) if speed > 0 else ""
        except (TypeError, ValueError):
            speed_str = ""
        text = f"已扫描: {_multipart_size(scanned_i)}"
        if speed_str:
            text += f"  |  {speed_str}"
        self._scanned_label.setText(text)

    def _on_scan_finished(self, scanned, skipped_dirs, skipped_files, os_used):
        scanned_i = int(scanned) if scanned else 0
        skipped_d = int(skipped_dirs) if skipped_dirs else 0
        skipped_f = int(skipped_files) if skipped_files else 0
        os_used_i = int(os_used) if os_used else 0
        info(f"SpaceIndexPage._on_scan_finished: scanned={scanned_i}, skipped_dirs={skipped_d}, skipped_files={skipped_f}, os_used={os_used_i}")

        self._scanning = False
        self._scan_watchdog.stop()
        self._scan_btn.setVisible(True)
        self._cancel_btn.setVisible(False)
        self._drive_combo.setEnabled(True)
        self._progress_bar.setVisible(False)
        self._progress_bar.setValue(10000)
        self._scanned_label.setText(f"已扫描: {_multipart_size(scanned_i)}")
        self._scan_btn.setText("重新扫描")

        # 终止信号已到，线程即将（或已经）退出；回收引用，避免下一次扫描
        # 复用一个已死线程对象，也避免 disconnect 后引用悬空。
        self._release_worker(_SCAN_STOP_TIMEOUT_MS)

        # Write scan metadata to DB for persistence
        scan_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            if self._db:
                self._db.set_meta("drive_path", self._root_path)
                self._db.set_meta("scan_time", scan_time)
                self._db.set_meta("scanned_bytes", str(scanned_i))
                self._db.set_meta("os_used", str(os_used_i))
                self._db.set_meta("skipped_dirs", str(skipped_d))
                self._db.set_meta("skipped_files", str(skipped_f))
                self._db.set_meta("app_version", APP_VERSION)
                self._db.commit()
        except Exception as e:
            error(f"写入扫描元数据失败: {e}")

        # Show cached label
        self._cached_label.setText(f"上次扫描: {scan_time} | 已扫描: {_multipart_size(scanned_i)}")

        # Final model refresh — navigate to root
        if self._file_model:
            root_info = self._db.get_by_path(self._root_path)
            if root_info:
                self._file_model.set_root_size(root_info['size'])
                self._current_nav_id = root_info['id']
                self._file_model.set_directory(root_info['id'])
                self._update_breadcrumbs()

        self._refresh_bar_chart()

        # Drive usage summary bar
        try:
            import shutil
            du = shutil.disk_usage(self._root_path)
            self._drive_bar.set_usage(scanned_i, os_used_i, du.total)
        except Exception:
            self._drive_bar.set_usage(scanned_i, os_used_i, max(os_used_i, scanned_i))
        self._drive_bar.setVisible(True)

        # Build detailed summary
        parts = [f"扫描完成 — 已扫描: {_multipart_size(scanned_i)}"]
        if os_used_i > 0 and os_used_i > scanned_i:
            diff = os_used_i - scanned_i
            parts.append(f"系统报告已用: {_multipart_size(os_used_i)}")
            parts.append(f"差异: {_multipart_size(diff)} (系统文件/无权限目录)")
        if skipped_d:
            parts.append(f"跳过 {skipped_d} 个目录")
        if skipped_f:
            parts.append(f"跳过 {skipped_f} 个文件")
        self._status_label.setText("  |  ".join(parts))

    def _on_scan_error(self, msg):
        error(f"SpaceIndexPage scan error: {msg}")
        self._scanning = False
        self._scan_watchdog.stop()
        self._scan_btn.setVisible(True)
        self._cancel_btn.setVisible(False)
        self._drive_combo.setEnabled(True)
        self._progress_bar.setVisible(False)
        self._drive_bar.setVisible(False)
        self._release_worker(_SCAN_STOP_TIMEOUT_MS)
        self._status_label.setText(f"扫描错误: {msg[:120]}")

    # ── Bar chart ──────────────────────────────────────────────

    def _refresh_bar_chart(self):
        if self._db is None or not self._root_path:
            return
        try:
            root_info = self._db.get_by_path(self._root_path)
            if root_info is None:
                return
            items = self._db.get_top_level_items(root_info['id'])
            self._bar_chart.set_items(items)
        except Exception as e:
            error(f"SpaceIndexPage._refresh_bar_chart 异常: {e}")

    # ── Cleanup ────────────────────────────────────────────────

    def cleanup(self):
        self._scanning = False
        try:
            self._scan_watchdog.stop()
        except Exception:
            pass
        # 必须等写线程真正结束再关数据库：否则活着的 ScanEngine 会继续往一个
        # 已关闭的连接里写，抛 sqlite3.ProgrammingError 并让进程退出时报错。
        if not self._release_worker(_SCAN_STOP_TIMEOUT_MS):
            warn("SpaceIndexPage.cleanup: 扫描线程仍在运行，跳过关闭数据库以免连接被并发使用")
            info("SpaceIndexPage.cleanup")
            return
        # Process any pending events so queued signals are drained before DB close
        QApplication.processEvents()
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        info("SpaceIndexPage.cleanup")
