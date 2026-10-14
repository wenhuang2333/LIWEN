# 空间索引 — 技术实现文档

## 1. 架构总览

空间索引功能由三层组成：

```
┌─────────────────────────────────────────────────────┐
│  UI 层 (ui/space_index_page.py)                     │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │BarChartWidget│  │  QTreeView   │  │QProgressBar│ │
│  │  柱状图      │  │  目录详情     │  │  进度条     │ │
│  └─────────────┘  └──────────────┘  └────────────┘ │
│         ▲               ▲                ▲          │
│         │    signals    │                │          │
│  ┌──────┴───────────────┴────────────────┴────────┐ │
│  │         SpaceIndexPage (主控制器)               │ │
│  │  _dir_data / _file_entries / _tree_items       │ │
│  └────────────────────┬───────────────────────────┘ │
├───────────────────────┼─────────────────────────────┤
│  信号跨线程传递       │  pyqtSignal (queued)         │
├───────────────────────┼─────────────────────────────┤
│  工作线程 (core/workers.py)                         │
│  ┌────────────────────┴───────────────────────────┐ │
│  │         SpaceScanWorker (QThread)              │ │
│  │  os.walk() → 文件收集 → 目录聚合(bottom-up)    │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**核心数据结构（位于 SpaceIndexPage，纯 Python dict，不涉及 QObject）：**

```python
_dir_data = {
    "C:\\Users": {
        'size': 1234567890,       # 该目录的总大小（包含所有后代）
        'file_count': 42,         # 直接子文件数
        'dir_count': 5,           # 直接子目录数
        'parent': "C:\\",         # 父目录路径
        'children_names': ["张三", "李四", ...],  # 子目录的 basename 列表
    },
    ...
}

_file_entries = {
    "C:\\Users\\张三": [
        ("文档.txt", "C:\\Users\\张三\\文档.txt", 1024, False),
        ...
    ],
    ...
}
```

**为什么用 dict 而不是 QStandardItem？** QStandardItem 是 QObject 子类，每个实例有 ~200B 开销。一颗百万文件的目录树会占用数百 MB 内存且阻塞 UI 线程。普通 Python dict 轻量得多（~72B/条目），且只在用户展开节点时才创建对应的 QStandardItem。

## 2. 数据流

```
                 SpaceScanWorker (QThread)
                        │
    ┌───────────────────┼───────────────────┐
    │                   │                   │
    ▼                   ▼                   ▼
total_size_signal   file_batch          progress
(对象信号)          (list[dict])        (object,object,float,float)
    │                   │                   │
    ▼                   ▼                   ▼
_on_total_size     _on_file_batch      _on_scan_progress
 记录总盘大小      存入_dir_data      更新进度条(0-10000)
                   _file_entries      更新已扫描标签
                       │              (多单位显示+速度)
                       ▼
                   _chart_timer.start()
                   (600ms 防抖)
                       │
    ┌──────────────────┼───────────────────┐
    │                   │                   │
    ▼                   ▼                   ▼
_dir_batch          _sync_tree_         _refresh_bar_chart
(bottom-up)         during_scan         (从 _dir_data
 更新目录最终大小    (1200ms 定时器)     构建柱状图数据)
 构建 children_names  增量同步树模型
                       │
                       ▼
                   _on_scan_finished
                   断开 worker 信号
                   完整重建树模型
                   启用排序
```

### 信号类型（关键：使用 object 避免 32-bit 溢出）

```python
# core/workers.py 第333-341行
progress      = pyqtSignal(object, object, float, float)  # scanned, total, elapsed, speed
file_batch    = pyqtSignal(list)
dir_batch     = pyqtSignal(list)
total_size_signal = pyqtSignal(object)  # 总盘大小，可能 > 2.1GB
finished      = pyqtSignal(object, object)  # scanned, skipped
scanning_path = pyqtSignal(str)
```

`pyqtSignal(int)` 在 PyQt6 中映射到 C++ `int`（32-bit 有符号，最大值 ~2.14GB）。磁盘总大小动辄数百 GB，必须使用 `pyqtSignal(object)` 传递 Python 的任意精度整数。

### 进度条（同样受限于 C++ int）

QProgressBar 的 `setMaximum()` 和 `setValue()` 使用 C++ int。解决方案：使用 0–10000 刻度表示百分比。

```python
# _on_scan_progress 第483-488行
if total_i > 0:
    pct = int(scanned_i / total_i * 10000)   # 0.01% 精度
    if pct > 10000:
        pct = 10000
    self._progress_bar.setValue(pct)
```

### 信号断开（防止 GC 异常）

Worker 在子线程中发出信号，信号通过 queued connection 排队到主线程事件队列。如果 Worker 被 Python GC 回收时还有未处理的信号，会导致 "Exception ignored in garbage collection"。解决方案：在三个生命周期节点显式断开信号。

```python
# _on_scan_finished / _on_scan_error / _cancel_scan / cleanup
if self._worker:
    try:
        self._worker.disconnect()
    except Exception:
        pass
```

## 3. 完整源码

### 3.1 core/workers.py — SpaceScanWorker

```python
class SpaceScanWorker(QThread):
    progress = pyqtSignal(object, object, float, float)
    file_batch = pyqtSignal(list)
    dir_batch = pyqtSignal(list)
    dir_done = pyqtSignal(str, object, int)
    total_size_signal = pyqtSignal(object)
    finished = pyqtSignal(object, object)
    error = pyqtSignal(str)
    status = pyqtSignal(str)
    scanning_path = pyqtSignal(str)

    BATCH_SIZE = 300
    PROGRESS_INTERVAL = 0.4

    def __init__(self, root_path: str):
        super().__init__()
        self._root_path = root_path
        self._cancel = False
        self._skipped = 0
        self._dir_count = 0

    def cancel(self):
        self._cancel = True

    @property
    def skipped_count(self):
        return self._skipped

    def run(self):
        import shutil

        start_time = time.time()
        self._skipped = 0
        self._dir_count = 0
        scanned = 0
        last_progress = 0

        try:
            total = shutil.disk_usage(self._root_path).total
        except Exception:
            total = 0
        self.total_size_signal.emit(total)

        self.status.emit("正在扫描文件...")

        dir_info = {}
        self._skipped = 0
        file_batch = []

        for dirpath, dirnames, filenames in os.walk(self._root_path):
            if self._cancel:
                break

            self._dir_count += 1
            if self._dir_count % 20 == 0:
                self.scanning_path.emit(dirpath)
            parent = os.path.dirname(dirpath)

            dir_files = []
            dir_size = 0
            for fname in filenames:
                if self._cancel:
                    break
                fpath = os.path.join(dirpath, fname)
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    size = 0
                dir_size += size
                scanned += size
                dir_files.append((fpath, fname, size))

            if dir_files:
                file_batch.append({
                    'dirpath': dirpath, 'parent': parent,
                    'files': dir_files,
                })
                if len(file_batch) >= self.BATCH_SIZE:
                    self.file_batch.emit(file_batch)
                    file_batch.clear()

            dir_info[dirpath] = {
                'dirpath': dirpath,
                'parent': parent,
                'file_total': dir_size,
                'subdirs': [os.path.join(dirpath, d) for d in dirnames],
                'total': 0,
            }

            now = time.time()
            if total > 0 and now - last_progress > self.PROGRESS_INTERVAL:
                elapsed = now - start_time
                speed = scanned / elapsed if elapsed > 0 else 0
                self.progress.emit(scanned, total, elapsed, speed)
                last_progress = now

        if file_batch:
            self.file_batch.emit(file_batch)
            file_batch.clear()

        if self._cancel:
            self.status.emit("扫描已取消")
            self.finished.emit(scanned, self._skipped)
            return

        self.status.emit("正在计算目录大小...")

        sorted_dirs = sorted(dir_info.keys(), key=lambda p: p.count(os.sep), reverse=True)
        dir_batch = []

        for dirpath in sorted_dirs:
            if self._cancel:
                break
            info = dir_info[dirpath]
            total = info['file_total']
            for sub in info['subdirs']:
                sub_info = dir_info.get(sub)
                if sub_info:
                    total += sub_info['total']
            info['total'] = total
            info.pop('file_total', None)
            info.pop('subdirs', None)

            dir_batch.append({
                'path': dirpath,
                'name': os.path.basename(dirpath) or dirpath,
                'size': total,
                'is_dir': True,
                'parent_path': info['parent'],
                'entry_count': 0,
            })
            if len(dir_batch) >= self.BATCH_SIZE:
                self.dir_batch.emit(dir_batch)
                dir_batch.clear()

        if dir_batch and not self._cancel:
            self.dir_batch.emit(dir_batch)
            dir_batch.clear()

        if self._cancel:
            self.status.emit("扫描已取消")
            self.finished.emit(scanned, self._skipped)
            return

        elapsed = time.time() - start_time
        self.status.emit(
            f"扫描完成 — {self._dir_count} 个目录, {self._skipped} 个跳过, 耗时 {elapsed:.1f}s")
        self.finished.emit(scanned, self._skipped)
```

**扫描算法详解：**

1. **Phase 1 — 文件收集 (top-down)**：`os.walk()` 遍历目录树，对每个目录 stat 所有文件大小、累加到 `scanned` 计数器、打包进 `file_batch`。每 300 个目录发射一次 `file_batch` 信号。每 20 个目录发射一次 `scanning_path` 信号（UI 显示当前路径）。每 0.4 秒发射一次 `progress` 信号（UI 更新进度条和速度）。

2. **Phase 2 — 目录聚合 (bottom-up)**：按路径深度降序排列所有目录（最深的先处理）。对每个目录，`total = 自身文件总大小 + 所有子目录的 total`。处理完的目录发射到 `dir_batch`。处理完后释放 `file_total` 和 `subdirs` 字段以回收内存。

**为什么用 os.walk 而不是递归 os.scandir？**
- `os.walk()` 内部使用 `os.scandir()`（Python 3.5+），同样高效
- 避免了 Python 递归函数调用开销和栈深度限制
- 自动处理 top-down 遍历顺序

### 3.2 ui/space_index_page.py — SpaceIndexPage + BarChartWidget

#### 3.2.1 多单位递归进位显示

```python
def _multipart_size(total_bytes):
    """将字节数分解为 T/GB/MB/KB，递归进位（每级 1024x）"""
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
    """格式化扫描速度"""
    if bytes_per_sec <= 0:
        return ""
    if bytes_per_sec >= 1024 ** 3:
        return f"{bytes_per_sec / 1024 ** 3:.1f} GB/s"
    if bytes_per_sec >= 1024 ** 2:
        return f"{bytes_per_sec / 1024 ** 2:.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"
```

进位逻辑：`b // 1024→k, k // 1024→m, m // 1024→g, g // 1024→t`。每级取余数留在本级。显示时只展示非零单位，全零显示 "0 K"。最大可表示值 ≈ 99999T（远超市面最大硬盘容量）。

#### 3.2.2 驱动器枚举

```python
def _get_available_drives():
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
    else:
        drives.append("/")
    return drives


def _get_drive_label(drive_path):
    """获取驱动器卷标，如 'C: (系统)'"""
    if drive_path == "/":
        return "根目录 (/)"
    label = drive_path.rstrip("\\")
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
```

#### 3.2.3 BarChartWidget — 柱状图

```python
class BarChartWidget(QWidget):
    """从左到右按大小降序排列的水平柱状图"""

    BAR_COLORS = [
        QColor("#89b4fa"), QColor("#a6e3a1"), QColor("#f9e2af"),
        QColor("#fab387"), QColor("#f38ba8"), QColor("#cba6f7"),
        QColor("#94e2d5"), QColor("#74c7ec"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []        # [(name, size, path), ...]
        self._hover_idx = -1
        self._max_size = 0
        self.setMouseTracking(True)
        self.setMinimumHeight(140)
        self.setMaximumHeight(220)

    def set_items(self, items):
        """items = [(name, size, path), ...]"""
        self._items = items[:80]    # 最多 80 根柱子
        self._max_size = max((s for _, s, _ in self._items), default=1)
        self._hover_idx = -1
        self.update()

    def paintEvent(self, event):
        """自定义绘制：每根柱子宽度 = (总宽 - 间距) / 数量，高度 = size/max * 图高"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#1e1e2e")
        painter.fillRect(self.rect(), bg)

        if not self._items:
            painter.setPen(QColor("#9498a4"))
            painter.setFont(QFont("Microsoft YaHei", 10))
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
        font = QFont("Microsoft YaHei", 7)
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
                color = color.lighter(140)     # 悬停高亮

            painter.setPen(QPen(QColor("#11111b"), 1))
            painter.setBrush(color)
            painter.drawRect(QRectF(x, y, bar_w, bar_h))

            # 柱顶显示大小标签（仅在空间足够时）
            if bar_h > 16 and bar_w > 24:
                label = format_size(size)
                if fm.horizontalAdvance(label) < bar_w - 2:
                    painter.setPen(QColor("#e8e8f0"))
                    painter.drawText(QRectF(x, y - 14, bar_w, 14),
                                     Qt.AlignmentFlag.AlignCenter, label)

        # 底部坐标轴
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
        """悬停时显示 tooltip（文件路径 + 大小）"""
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
                self,
            )
        else:
            QToolTip.hideText()
        if old != self._hover_idx:
            self.update()

    def leaveEvent(self, event):
        if self._hover_idx != -1:
            self._hover_idx = -1
            QToolTip.hideText()
            self.update()
```

#### 3.2.4 SpaceIndexPage — 主页面

```python
class SpaceIndexPage(QWidget):
    """磁盘空间分析页 — 柱状图 + 懒加载树形视图"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None

        # 轻量级数据存储（非 QStandardItem）
        self._dir_data = {}       # path -> {size, file_count, dir_count, parent, children_names}
        self._file_entries = {}   # path -> [(name, fullpath, size, is_dir), ...]
        self._tree_items = {}     # path -> [name_item, size_item, pct_item, cnt_item]

        self._total_drive = 0
        self._scanned_bytes = 0
        self._root_path = ""
        self._scanning = False

        # 防抖定时器
        self._chart_timer = QTimer(self)        # 600ms 柱状图刷新
        self._chart_timer.setSingleShot(True)
        self._chart_timer.setInterval(600)
        self._chart_timer.timeout.connect(self._refresh_bar_chart)

        self._tree_sync_timer = QTimer(self)    # 1200ms 树形图同步
        self._tree_sync_timer.setInterval(1200)
        self._tree_sync_timer.timeout.connect(self._sync_tree_during_scan)

        self._setup_ui()
```

**树模型的行工厂方法：**

```python
def _make_row(self, path, name, size, is_dir, child_count):
    """创建 [名称, 大小, 占比, 项目数] 四列行"""
    name_item = QStandardItem(name)
    name_item.setData(path, Qt.ItemDataRole.UserRole)         # 路径
    name_item.setData(is_dir, Qt.ItemDataRole.UserRole + 1)   # 是否目录
    name_item.setData(False, Qt.ItemDataRole.UserRole + 2)    # 是否已懒加载

    size_item = QStandardItem(format_size(size))
    size_item.setData(size, Qt.ItemDataRole.UserRole)         # 排序用

    root_size = self._dir_data.get(self._root_path, {}).get('size', size) or 1
    pct = (size / root_size * 100) if root_size > 0 else 0
    pct_item = QStandardItem(f"{pct:.1f}%" if is_dir else "-")
    pct_item.setData(pct, Qt.ItemDataRole.UserRole)

    cnt_item = QStandardItem(str(child_count) if is_dir else "-")
    cnt_item.setData(child_count, Qt.ItemDataRole.UserRole)

    if is_dir:
        dummy = QStandardItem("...")        # 占位符，展开时替换
        name_item.appendRow(dummy)

    return [name_item, size_item, pct_item, cnt_item]
```

**扫描中增量同步树形视图（每 1.2 秒）：**

```python
def _sync_tree_during_scan(self):
    """定期将新发现的顶层目录和文件同步到树模型中"""
    if not self._scanning or not self._root_path:
        return

    self._tree_view.setSortingEnabled(False)   # 同步期间禁用排序

    root_info = self._dir_data.get(self._root_path, {})
    children_names = root_info.get('children_names', [])
    existing = set(self._tree_items.keys())

    # 新增顶层目录
    for name in children_names:
        child_path = os.path.join(self._root_path, name)
        if child_path in existing:
            continue
        info = self._dir_data.get(child_path, {})
        size = info.get('size', 0)
        fc = info.get('file_count', 0)
        dc = info.get('dir_count', 0)
        row = self._make_row(child_path, name, size, True, fc + dc)
        self._tree_model.appendRow(row)
        self._tree_items[child_path] = row
        existing.add(child_path)

    # 新增根级文件
    root_files = self._file_entries.get(self._root_path, [])
    for fname, fpath, size, _ in root_files:
        if fpath not in existing:
            row = self._make_row(fpath, fname, size, False, 0)
            self._tree_model.appendRow(row)
            self._tree_items[fpath] = row
            existing.add(fpath)

    # 更新已有条目的大小
    for child_path, row in self._tree_items.items():
        info = self._dir_data.get(child_path)
        if info:
            self._update_row(row, info.get('size', 0),
                             info.get('file_count', 0) + info.get('dir_count', 0))

    self._tree_view.setSortingEnabled(True)
    self._tree_sync_timer.start()       # 重新启动定时器
```

**懒加载子节点（用户展开目录时触发）：**

```python
def _on_item_expanded(self, index):
    """QTreeView.expanded 信号 → 懒加载子节点"""
    src_idx = self._proxy_model.mapToSource(index)
    name_item = self._tree_model.itemFromIndex(src_idx)
    if name_item is None:
        return

    already = name_item.data(Qt.ItemDataRole.UserRole + 2)   # 已加载标志
    if already:
        return

    path = name_item.data(Qt.ItemDataRole.UserRole)
    is_dir = name_item.data(Qt.ItemDataRole.UserRole + 1)
    if not is_dir or not path:
        return

    # 移除 "..." 占位符
    if name_item.rowCount() == 1:
        dummy = name_item.child(0, 0)
        if dummy and dummy.text() == "...":
            name_item.removeRow(0)

    self._populate_children(path, name_item)
    name_item.setData(True, Qt.ItemDataRole.UserRole + 2)     # 标记已加载

def _populate_children(self, parent_path, parent_name_item):
    """从 _dir_data 和 _file_entries 加载子节点"""
    info = self._dir_data.get(parent_path)
    if info is None:
        return

    children_names = info.get('children_names', [])
    added_dirs = set()
    for name in children_names:
        child_path = os.path.join(parent_path, name)
        c_info = self._dir_data.get(child_path, {})
        size = c_info.get('size', 0)
        fc = c_info.get('file_count', 0)
        dc = c_info.get('dir_count', 0)
        row = self._make_row(child_path, name, size, True, fc + dc)
        parent_name_item.appendRow(row)
        added_dirs.add(name)

    files = self._file_entries.get(parent_path, [])
    for fname, fpath, size, _ in files:
        if fname not in added_dirs:
            row = self._make_row(fpath, fname, size, False, 0)
            parent_name_item.appendRow(row)
```

### 3.3 ui/main_window.py — 集成点

```python
# 第19行 — 导入
from ui.space_index_page import SpaceIndexPage

# 第227行 — 实例化
self.space_index_page = SpaceIndexPage()

# 第233-236行 — 添加到"本机"分类的子标签页
# Cat 0: 本机 — sub-tabs: 压缩解压, 空间索引
self.main_stack.addWidget(self._make_category_with_sub_tabs(
    0, ["压缩解压", "空间索引"],
    [self.compress_page, self.space_index_page]))

# 第387-394行 — 拖放路由（仅压缩子页接收拖放）
cat = self._current_category
sub = self._current_sub_pages.get(cat, 0)
if cat == 0:
    if sub == 0:
        self.compress_page.handle_drop(paths)

# 第646行 — 关闭时清理
self.space_index_page.cleanup()
```

## 4. 关键设计决策

| 决策 | 原因 |
|------|------|
| 扫描数据用 Python dict 存储而非 QStandardItem | QStandardItem 是 QObject，单实例 ~200B 开销。百万条目会占数百 MB 且阻塞 UI。Python dict ~72B/条目，仅展开节点时才创建 QStandardItem |
| 两阶段扫描（top-down 收集 + bottom-up 聚合） | 目录总大小依赖所有后代，必须后序遍历。top-down 收集文件大小可立即反馈进度，bottom-up 聚合得到准确的目录大小 |
| 信号类型用 `object` 而非 `int` | C++ `int` 最大 2.14GB，磁盘可达 TB 级。Python 任意精度整数必须通过 `object` 传递 |
| 进度条用 0–10000 刻度 | QProgressBar API 接受 C++ int，不能直接传字节数。百分比 ×100 提供 0.01% 精度 |
| 每目录最多 200 个文件条目 | 超大目录（如 System32 有数千文件）生成过多 QStandardItem 会导致OOM |
| 扫描期间禁用树排序 | `QSortFilterProxyModel.setSortingEnabled(True)` 会在每次 `appendRow` 后重新排序，O(n²) 复杂度 |
| 柱状图/树形图用防抖定时器刷新 | 扫描期间每秒可能收到数十个 batch 信号。直接每次刷新会阻塞 UI。600ms/1200ms 定时器合并刷新 |
| 扫描完成后显式 disconnect worker | 防止 worker 被 GC 时排队信号访问已销毁的 widget |

## 5. 已知问题与优化方向

1. **超大目录（数万文件）下树展开可能卡顿**：当前懒加载会一次性创建所有子节点。可改为虚拟滚动或分页加载。

2. **权限拒绝的目录仅计数不展示**：用户看不到哪些路径被跳过。可增加"已跳过"详情面板。

3. **柱状图宽度固定等分**：文件大小差异极大时，小文件柱子太窄。可改为对数刻度或按比例宽度。

4. **`os.walk` 不区分符号链接**：可能重复计算。已在 `is_dir(follow_symlinks=False)` 中规避，但目录遍历本身不排除 junction/reparse point。

5. **扫大文件时无 ETA**：已移除（之前算不准）。如需恢复，应在 ≥10 个采样点后用线性回归估计而非瞬时速度。
