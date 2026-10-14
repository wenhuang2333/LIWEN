import os
import sys
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QProgressBar, QLabel, QMessageBox, QPushButton, QStackedWidget,
    QSizePolicy, QSizeGrip,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPixmap, QAction

from ui.styles import get_main_style
from ui.compress_page import CompressPage
from ui.lan_page import LanPage
from ui.room_lobby_page import RoomLobbyPage
from ui.crawler_page import CrawlerPage
from ui.my_page import MyPage
from ui.about_page import AboutPage
from ui.diagnostic_page import DiagnosticPage
from ui.space_index_page import SpaceIndexPage
from ui.http_request_page import HttpRequestPage
from ui.ai_page import AiPage
from ui.archive_preview_dialog import ArchivePreviewDialog
from core.workers import CompressWorker, DecompressWorker, SendWorker, ReceiveWorker, PingWorker
from utils.constants import DEFAULT_TCP_PORT
from utils.helpers import format_size
from utils.logger import error as log_error, warn as log_warn
from utils.paths import static_path
from utils.settings import settings

BORDER_WIDTH = 6


def _static_path(filename: str) -> str:
    return static_path(filename)


class TitleBar(QWidget):
    """Custom title bar with logo, app name, and window controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._start_pos = None
        self.setFixedHeight(40)
        self.setObjectName("titleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # — Left: logo + app name —
        left = QHBoxLayout()
        left.setSpacing(8)
        left.setContentsMargins(10, 4, 0, 4)

        logo_path = _static_path("logo.jpg")
        if os.path.exists(logo_path):
            logo_lbl = QLabel()
            pix = QPixmap(logo_path).scaled(24, 24,
                                            Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation)
            logo_lbl.setPixmap(pix)
            logo_lbl.setFixedSize(24, 24)
            left.addWidget(logo_lbl)

        title_lbl = QLabel("帽兜工具")
        title_lbl.setStyleSheet("color: #e0e0e8; font-size: 13px; font-weight: 600;")
        left.addWidget(title_lbl)
        layout.addLayout(left)
        layout.addStretch()

        # — Right: window controls —
        btn_style = (
            "QPushButton { background: transparent; border: none; padding: 0 14px;"
            "  font-size: 15px; color: #c8ccd4; min-height: 40px; }"
            "QPushButton:hover { background-color: #313244; }"
        )
        close_hover = (
            "QPushButton { background: transparent; border: none; padding: 0 16px;"
            "  font-size: 15px; color: #c8ccd4; min-height: 40px; }"
            "QPushButton:hover { background-color: #e64553; color: #ffffff; }"
        )

        self._min_btn = QPushButton("─")
        self._min_btn.setStyleSheet(btn_style)
        self._min_btn.clicked.connect(self._on_minimize)
        layout.addWidget(self._min_btn)

        self._max_btn = QPushButton("□")
        self._max_btn.setStyleSheet(btn_style)
        self._max_btn.clicked.connect(self._on_maximize)
        layout.addWidget(self._max_btn)

        self._close_btn = QPushButton("✕")
        self._close_btn.setStyleSheet(close_hover)
        self._close_btn.clicked.connect(self._on_close)
        layout.addWidget(self._close_btn)

    def _on_minimize(self):
        self.window().showMinimized()

    def _on_maximize(self):
        win = self.window()
        if win.isMaximized():
            win.showNormal()
        else:
            win.showMaximized()

    def _on_close(self):
        self.window().close()

    def update_max_button(self, maximized: bool):
        self._max_btn.setText("❐" if maximized else "□")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._start_pos is not None and event.buttons() == Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._start_pos
            win = self.window()
            if win.isMaximized():
                win.showNormal()
            win.move(win.pos() + delta)
            self._start_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self._start_pos = None

    def mouseDoubleClickEvent(self, event):
        self._on_maximize()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("帽兜工具")
        self.setMinimumSize(1020, 680)
        self.resize(1120, 760)
        self.setAcceptDrops(True)
        self._prev_accent = settings.accent_color

        # Frameless window: native title bar on macOS, custom on Windows/Linux
        if sys.platform == "darwin":
            self.setWindowFlags(
                Qt.WindowType.Window |
                Qt.WindowType.WindowSystemMenuHint |
                Qt.WindowType.WindowMinMaxButtonsHint |
                Qt.WindowType.WindowCloseButtonHint
            )
            self._custom_title_bar = False
        else:
            self.setWindowFlags(
                Qt.WindowType.Window |
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.WindowSystemMenuHint |
                Qt.WindowType.WindowMinMaxButtonsHint
            )
            self._custom_title_bar = True

        # App icon
        icon_path = _static_path("logo.jpg")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self._compress_worker = None
        self._decompress_worker = None
        self._send_worker = None
        self._receive_worker = None
        self._discovery_timer = None
        self._discovery_obj = None
        self._discovery_reply_thread = None
        self._ping_worker = None
        self._scan_thread = None
        self._pending_send_queue = []

        # Category / sub-page tracking
        self._current_category = 0
        self._current_sub_pages = {}  # cat_idx -> sub_idx
        self._cat_sub_stacks = {}     # cat_idx -> QStackedWidget
        self._cat_sub_btns = {}       # cat_idx -> [QPushButton, ...]

        self.setStyleSheet(get_main_style(settings.accent_color))
        self._setup_ui()
        self._connect_signals()
        self._apply_settings()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---- Custom Title Bar (Windows/Linux only) ----
        if self._custom_title_bar:
            self._title_bar = TitleBar(self)
            main_layout.addWidget(self._title_bar)
        else:
            self._title_bar = None

        # ---- Content area: sidebar + right panel ----
        content_wrapper = QWidget()
        content_wrapper.setObjectName("contentWrapper")
        wrapper_layout = QHBoxLayout(content_wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)

        # -- Left Sidebar --
        sidebar = QWidget()
        sidebar.setObjectName("sideBar")
        sidebar.setFixedWidth(140)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 12, 0, 0)
        sidebar_layout.setSpacing(2)

        self.cat_btns = []
        categories = ["本机", "局域网", "互联网", "设置", "关于"]
        for i, name in enumerate(categories):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, idx=i: self._switch_category(idx))
            sidebar_layout.addWidget(btn)
            self.cat_btns.append(btn)

        self.cat_btns[0].setChecked(True)
        sidebar_layout.addStretch()
        wrapper_layout.addWidget(sidebar)

        # -- Right Panel (main_stack + bottom bar) --
        self._right_panel = QWidget()
        right_layout = QVBoxLayout(self._right_panel)
        right_layout.setContentsMargins(0, 0, BORDER_WIDTH, BORDER_WIDTH)
        right_layout.setSpacing(0)

        # Create all pages
        self.compress_page = CompressPage()
        self.lan_page = LanPage()
        self.room_lobby_page = RoomLobbyPage()
        self.crawler_page = CrawlerPage()
        self.my_page = MyPage()
        self.about_page = AboutPage()
        self.diagnostic_page = DiagnosticPage()
        self.space_index_page = SpaceIndexPage()
        self.http_request_page = HttpRequestPage()
        self.ai_page = AiPage()

        # Main stack — 5 category pages
        self.main_stack = QStackedWidget()
        self.main_stack.setObjectName("pageContainer")

        # Cat 0: 本机 — sub-tabs: 压缩解压, 空间索引
        self.main_stack.addWidget(self._make_category_with_sub_tabs(
            0, ["压缩解压", "空间索引"],
            [self.compress_page, self.space_index_page]))

        # Cat 1: 局域网 — sub-tabs: 局域网传输, 联机大厅
        self.main_stack.addWidget(self._make_category_with_sub_tabs(
            1, ["局域网传输", "联机大厅"],
            [self.lan_page, self.room_lobby_page]))

        # Cat 2: 互联网 — sub-tabs: 网页爬虫, 请求发送, 生成式AI
        self.main_stack.addWidget(self._make_category_with_sub_tabs(
            2, ["网页爬虫", "请求发送", "生成式AI"],
            [self.crawler_page, self.http_request_page, self.ai_page]))

        # Cat 3: 设置 — sub-tabs: 设置, 故障检测
        self.main_stack.addWidget(self._make_category_with_sub_tabs(
            3, ["设置", "故障检测"],
            [self.my_page, self.diagnostic_page]))

        # Cat 4: 关于 — AboutPage directly
        self.main_stack.addWidget(self.about_page)

        right_layout.addWidget(self.main_stack, 1)

        # -- Bottom Bar --
        bottom = QWidget()
        bottom.setObjectName("bottomBar")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(20, 8, 20, 8)
        bottom_layout.setSpacing(16)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("")
        bottom_layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        bottom_layout.addWidget(self.progress_bar, 1)

        self.speed_label = QLabel("")
        bottom_layout.addWidget(self.speed_label)

        bottom_layout.addStretch()
        grip = QSizeGrip(bottom)
        bottom_layout.addWidget(grip)

        right_layout.addWidget(bottom)
        wrapper_layout.addWidget(self._right_panel, 1)
        main_layout.addWidget(content_wrapper, 1)

    def _make_category_with_sub_tabs(self, cat_idx, tab_names, pages):
        """Create a container widget with sub-tab bar + QStackedWidget."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        tab_bar = QWidget()
        tab_bar.setObjectName("subTabBar")
        tab_layout = QHBoxLayout(tab_bar)
        tab_layout.setContentsMargins(20, 0, 20, 0)
        tab_layout.setSpacing(0)

        btns = []
        for i, name in enumerate(tab_names):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, ci=cat_idx, si=i: self._switch_sub_page(ci, si))
            tab_layout.addWidget(btn)
            btns.append(btn)

        btns[0].setChecked(True)
        tab_layout.addStretch()
        layout.addWidget(tab_bar)

        stack = QStackedWidget()
        for page in pages:
            stack.addWidget(page)
        layout.addWidget(stack, 1)

        self._cat_sub_stacks[cat_idx] = stack
        self._cat_sub_btns[cat_idx] = btns
        self._current_sub_pages[cat_idx] = 0

        return container

    def _switch_category(self, index):
        for i, btn in enumerate(self.cat_btns):
            btn.setChecked(i == index)
        self._current_category = index
        self.main_stack.setCurrentIndex(index)

    def _switch_sub_page(self, cat_idx, sub_idx):
        btns = self._cat_sub_btns.get(cat_idx)
        if btns:
            for i, btn in enumerate(btns):
                btn.setChecked(i == sub_idx)
        stack = self._cat_sub_stacks.get(cat_idx)
        if stack:
            stack.setCurrentIndex(sub_idx)
            self._current_sub_pages[cat_idx] = sub_idx

    def _connect_signals(self):
        self.compress_page.compress_requested.connect(self._start_compress)
        self.compress_page.decompress_requested.connect(self._start_decompress)
        self.compress_page.preview_btn.clicked.connect(self._open_preview)
        self.lan_page.send_requested.connect(self._start_send)
        self.lan_page.listen_toggled.connect(self._toggle_receive)
        self.lan_page.scan_requested.connect(self._scan_devices)
        self.lan_page.ping_requested.connect(self._start_ping)
        self.lan_page.preview_btn.clicked.connect(self._open_preview)
        self.my_page.settings_changed.connect(self._on_settings_changed)

    def _apply_settings(self):
        self.lan_page.refresh_local_info()
        save_path = settings.get("default_receive_path")
        if save_path and os.path.isdir(save_path):
            self.lan_page.save_edit.setText(save_path)

    def _on_settings_changed(self):
        current = settings.accent_color
        if current != self._prev_accent:
            self.setStyleSheet(get_main_style(current))
            self._prev_accent = current
        self.lan_page.refresh_local_info()
        save_path = settings.get("default_receive_path")
        if save_path and os.path.isdir(save_path):
            self.lan_page.save_edit.setText(save_path)
        if self._discovery_obj:
            self._discovery_obj.set_username(settings.username)

    # ---- native window resize ----
    # WM_NCHITTEST handling is currently disabled pending PyQt6 6.7+
    # compatibility testing on the user's Windows 11 configuration.
    # Frameless resize borders can be re-enabled once verified.

    # ---- drag & drop ----

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        cat = self._current_category
        sub = self._current_sub_pages.get(cat, 0)
        if cat == 0:
            if sub == 0:
                self.compress_page.handle_drop(paths)
        elif cat == 1:
            if sub == 0:
                self.lan_page.handle_drop(paths)

    # ---- window state ----

    def changeEvent(self, event):
        if event.type() == event.Type.WindowStateChange:
            if self._title_bar is not None:
                self._title_bar.update_max_button(self.isMaximized())
            if self.isMaximized():
                self._right_panel.layout().setContentsMargins(0, 0, 0, 0)
            else:
                self._right_panel.layout().setContentsMargins(
                    0, 0, BORDER_WIDTH, BORDER_WIDTH)
        super().changeEvent(event)

    # ---- compress ----

    def _start_compress(self, file_paths, output, fmt, password, volume_size,
                        seed_code="", compress_level=0):
        if not file_paths:
            QMessageBox.warning(self, "提示", "请先添加要压缩的文件或文件夹")
            return
        if not output:
            from utils.helpers import get_output_archive_name
            name = get_output_archive_name(file_paths, fmt)
            output = os.path.join(os.path.expanduser("~"), "Desktop", name)
        if not password:
            password = settings.get("default_password") or None
        self._compress_worker = CompressWorker(
            file_paths, output, fmt, password, volume_size,
            seed_code=seed_code if seed_code else None,
            compress_level=compress_level)
        self._compress_worker.progress.connect(self._on_progress)
        self._compress_worker.status.connect(self._on_status)
        self._compress_worker.finished.connect(self._on_compress_done)
        self._compress_worker.error.connect(self._on_error)
        self._compress_worker.start()
        self._set_busy(True)
        self._log(f"开始压缩 ({fmt})...")

    def _on_compress_done(self, result):
        self._set_busy(False)
        self.progress_bar.setValue(100)
        size_str = format_size(result.get("size", 0))
        count = result.get("file_count", 0)
        QMessageBox.information(
            self, "压缩完成",
            f"压缩完成!\n文件: {result['path']}\n大小: {size_str}\n文件数: {count}"
        )
        self._log(f"压缩完成 — {size_str}")

    # ---- decompress ----

    def _start_decompress(self, archive_path, dest, password, seed_code=""):
        if not archive_path:
            QMessageBox.warning(self, "提示", "请先选择要解压的压缩包")
            return
        if not dest:
            dest = os.path.dirname(archive_path)
        if not password:
            password = settings.get("default_password") or None
        self._decompress_worker = DecompressWorker(
            archive_path, dest, password,
            seed_code=seed_code if seed_code else None)
        self._decompress_worker.progress.connect(self._on_progress)
        self._decompress_worker.status.connect(self._on_status)
        self._decompress_worker.finished.connect(self._on_decompress_done)
        self._decompress_worker.error.connect(self._on_error)
        self._decompress_worker.start()
        self._set_busy(True)
        self._log(f"开始解压: {os.path.basename(archive_path)}")

    def _on_decompress_done(self, result):
        self._set_busy(False)
        self.progress_bar.setValue(100)
        QMessageBox.information(
            self, "解压完成",
            f"解压完成!\n目标: {result['dest']}\n文件数: {result.get('file_count', 0)}"
        )
        self._log(f"解压完成 — {result['dest']}")

    # ---- LAN send ----

    def _start_send(self, file_paths, host, port, compress_first):
        if not file_paths:
            QMessageBox.warning(self, "提示", "请先添加要发送的文件")
            return
        if not host:
            QMessageBox.warning(self, "提示", "请选择目标设备")
            return
        from_name = settings.username
        self._send_worker = SendWorker(
            file_paths, host, port,
            compress_first=compress_first,
            from_name=from_name,
            send_request=True,
        )
        self._send_worker.progress.connect(self._on_progress)
        self._send_worker.status.connect(self._on_status)
        self._send_worker.speed.connect(self.speed_label.setText)
        self._send_worker.finished.connect(self._on_send_done)
        self._send_worker.error.connect(self._on_error)
        self._send_worker.start()
        self._set_busy(True)
        self._log(f"正在向 {host}:{port} 发送传输请求...")

    def _on_send_done(self, result):
        self._set_busy(False)
        self.progress_bar.setValue(100)
        QMessageBox.information(self, "发送完成",
                                f"文件已成功发送到 {result.get('host', '')}")
        self._log("发送完成")

    # ---- LAN receive ----

    def _toggle_receive(self, start, port, save_dir):
        if not start:
            if self._receive_worker and self._receive_worker.isRunning():
                self._receive_worker.cancel()
            self._stop_discovery()
            self._set_busy(False)
            self._log("已停止监听")
            return
        auto_accept = settings.get("auto_accept")
        self._receive_worker = ReceiveWorker(port, save_dir, auto_accept=auto_accept)
        self._receive_worker.progress.connect(self._on_progress)
        self._receive_worker.status.connect(self.lan_page.set_listen_status)
        self._receive_worker.speed.connect(self.speed_label.setText)
        self._receive_worker.finished.connect(self._on_receive_done)
        self._receive_worker.error.connect(self._on_error)
        self._receive_worker.request_decision.connect(self._on_receive_request)
        self._receive_worker.start()
        self._start_discovery(listen_port=port)
        self._log(f"开始监听端口 {port}...")

    def _on_receive_request(self, info, decision, event):
        if event is None:
            reply = QMessageBox.question(
                self, "收到传输请求",
                f"来自: {info.get('from_name', info.get('from', ''))}\n"
                f"文件: {info['name']}\n"
                f"大小: {format_size(info['size'])}\n\n是否接受?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._receive_worker.cancel()
            return
        if settings.get("auto_accept"):
            decision["accepted"] = True
            event.set()
            return
        reply = QMessageBox.question(
            self, "收到传输请求",
            f"来自: {info.get('from_name', info.get('from', ''))}\n"
            f"文件: {info['name']}\n"
            f"大小: {format_size(info['size'])}\n\n是否接受?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        decision["accepted"] = (reply == QMessageBox.StandardButton.Yes)
        event.set()

    def _on_receive_done(self, result):
        self._set_busy(False)
        self.lan_page.on_receive_finished()
        self.progress_bar.setValue(100)
        self._stop_discovery()
        self._log("接收完成")

    # ---- discovery ----

    def _start_ping(self, host, port):
        # 重复点击会覆盖 self._ping_worker 并让上一个线程失去引用，这里直接拒绝
        if self._ping_worker is not None and self._ping_worker.isRunning():
            self._log("上一次链路检测尚未结束")
            return
        self._log(f"正在检测 {host}:{port}...")
        self.lan_page.set_ping_busy(True)
        self._ping_worker = PingWorker(host, port)
        self._ping_worker.finished.connect(self._on_ping_result)
        self._ping_worker.error.connect(self._on_ping_error)
        self._ping_worker.start()

    def _on_ping_result(self, ok, msg):
        self.lan_page.set_ping_busy(False)
        self.lan_page.set_ping_result(ok, msg)
        self._log(msg)

    def _on_ping_error(self, msg):
        self.lan_page.set_ping_busy(False)
        self.lan_page.set_ping_result(False, str(msg))

    def _scan_devices(self):
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._log("上一次扫描尚未结束")
            return
        self._log("正在扫描局域网设备...")
        self.lan_page.set_scan_busy(True)
        self._scan_thread = _DiscoveryScanThread(settings.username)
        self._scan_thread.finished.connect(self._on_scan_finished)
        self._scan_thread.start()

    def _on_scan_finished(self, devices):
        self.lan_page.set_scan_busy(False)
        self.lan_page.set_devices(devices)

    def _start_discovery(self, listen_port=None):
        from network.discovery import get_discovery_service
        # 重复"开始监听"时先停掉旧定时器，否则每次都会泄漏一个仍在触发的 QTimer
        if self._discovery_timer is not None:
            self._discovery_timer.stop()
            self._discovery_timer = None
        self._discovery_obj = get_discovery_service()
        self._discovery_obj.set_username(settings.username)
        self._listen_port = listen_port or settings.get(
            "lan_default_port", DEFAULT_TCP_PORT)
        # 如实上报本机接收端口，否则对方会去连默认的 12345
        self._discovery_obj.set_listen_port(self._listen_port)
        self._discovery_timer = QTimer(self)
        self._discovery_timer.timeout.connect(self._on_discovery_tick)
        self._discovery_timer.start(4000)

    def _on_discovery_tick(self):
        if not self._discovery_obj:
            return
        # reply_listener() 是阻塞调用（默认 3 秒）。原先直接在 QTimer 回调里执行，
        # 导致"开始监听"期间每 4 秒有 3 秒界面完全无响应。改为工作线程执行。
        if self._discovery_reply_thread is not None and self._discovery_reply_thread.isRunning():
            return
        self._discovery_reply_thread = _ReplyListenerThread(
            self._discovery_obj, self._listen_port)
        self._discovery_reply_thread.done.connect(self._on_discovery_replied)
        self._discovery_reply_thread.start()

    def _on_discovery_replied(self, devices):
        if not devices:
            return
        self.lan_page.set_devices(
            [(info.get("ip", did), info) for did, info in devices])

    def _stop_discovery(self):
        if self._discovery_timer:
            self._discovery_timer.stop()
            self._discovery_timer = None
        if self._discovery_reply_thread and self._discovery_reply_thread.isRunning():
            self._discovery_reply_thread.wait(4000)
        self._discovery_reply_thread = None
        if self._discovery_obj:
            # 单例服务由进程共享，这里只停止本窗口的使用，不做销毁
            self._discovery_obj.unregister_callback(self._on_discovery_event)
            self._discovery_obj = None

    def _on_discovery_event(self, event_type, data):
        pass

    # ---- preview ----

    def _open_preview(self):
        from PyQt6.QtWidgets import QFileDialog
        from core.formats import FormatRegistry
        filepath, _ = QFileDialog.getOpenFileName(
            self, "选择压缩包预览", "",
            FormatRegistry.file_filter_string()
        )
        if filepath:
            # 构造/显示预览对话框时任何异常都不该冒到 sys.excepthook 变成
            # "程序异常崩溃"弹窗，这里兜底成普通错误提示。
            try:
                dialog = ArchivePreviewDialog(filepath, self)
                dialog.exec()
            except Exception as e:
                log_error(f"预览压缩包失败: {e}")
                QMessageBox.critical(self, "预览失败", f"无法预览该压缩包:\n{e}")

    # ---- helpers ----

    def _on_progress(self, value):
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(value)

    def _on_status(self, text):
        self.status_label.setText(text)
        self.speed_label.setText("")

    def _on_error(self, msg):
        self._set_busy(False)
        self.progress_bar.setVisible(False)
        # 接收失败（例如端口被占用）时，若不收尾就会留下：
        # 仍在每 4 秒触发的发现定时器 + 按钮仍显示"停止监听"。
        self._stop_discovery()
        try:
            self.lan_page.on_receive_finished()
        except Exception:
            pass
        log_error(f"操作失败: {msg}")
        QMessageBox.critical(self, "错误", msg)
        self._log(f"错误: {msg}")

    def _set_busy(self, busy):
        self.compress_page.set_busy(busy)
        self.lan_page.set_busy(busy)
        if not busy:
            self.progress_bar.setVisible(False)

    def _log(self, msg):
        self.status_label.setText(msg)

    def shutdown(self):
        """停止所有后台活动。幂等——``closeEvent`` 与 ``main()`` 退出路径都会调用。

        必须在窗口关闭前把 QThread / socket / QTimer 全部收掉，否则解释器退出时
        会出现 "QThread: Destroyed while thread is still running" 或
        "libpng 段错误" 这类退出崩溃。
        """
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True

        # 把设置页防抖窗口里挂起的改动落盘，避免"改完设置立刻关窗口"丢失
        try:
            self.my_page.flush_pending_save()
        except Exception:
            pass

        for cleanup in (self._stop_discovery,
                        self.room_lobby_page.cleanup,
                        self.crawler_page.cleanup,
                        self.http_request_page.cleanup,
                        self.diagnostic_page.cleanup,
                        self.space_index_page.cleanup,
                        self.ai_page.cleanup):
            try:
                cleanup()
            except Exception as e:
                log_error(f"关闭清理失败 ({getattr(cleanup, '__qualname__', cleanup)}): {e}")

        workers = [self._compress_worker, self._decompress_worker,
                   self._send_worker, self._receive_worker,
                   self._ping_worker, self._scan_thread,
                   self._discovery_reply_thread]

        # Cancel workers that support cancellation
        for w in workers:
            try:
                if w and w.isRunning() and hasattr(w, "cancel"):
                    w.cancel()
            except RuntimeError:
                # 底层 C++ 对象已被销毁
                pass

        # Wait for workers to finish gracefully
        for w in workers:
            try:
                if w and w.isRunning():
                    w.wait(3000)
            except RuntimeError:
                pass

        # 仍不退出：QThread.terminate() 对跑着 Python 字节码的线程是不安全的
        # （可能持着 GIL / 停在 socket 调用中间），因此只记录，不再强杀。
        for w in workers:
            try:
                if w and w.isRunning():
                    log_warn(f"退出时仍有线程未停止: {type(w).__name__}")
            except RuntimeError:
                pass

    def closeEvent(self, event):
        self.shutdown()
        event.accept()


class _ReplyListenerThread(QThread):
    """在工作线程里执行阻塞的 UDP 监听（reply_listener）。

    放进线程是为了把 3 秒的阻塞从 Qt 主线程移走；主线程只接收结果。
    """

    done = pyqtSignal(dict)

    def __init__(self, discovery, listen_port=None, timeout=3.0):
        super().__init__()
        self._discovery = discovery
        self._timeout = timeout
        self._listen_port = listen_port

    def run(self):
        try:
            if self._listen_port:
                self._discovery.set_listen_port(self._listen_port)
            self._discovery.reply_listener(self._timeout)
            self.done.emit(self._discovery.get_devices())
        except Exception as e:
            log_error(f"设备发现监听失败: {e}")
            self.done.emit({})


class _DiscoveryScanThread(QThread):
    finished = pyqtSignal(list)

    def __init__(self, username="", timeout=3.0):
        super().__init__()
        self._username = username
        self._timeout = timeout

    def run(self):
        try:
            from network.discovery import get_discovery_service
            d = get_discovery_service()
            d.set_username(self._username)
            # 一次性主动扫描：原先误用了只监听不回发广播的 reply_listener()，
            # 因此"扫描设备"永远返回 0 个结果。
            found = d.scan_now(self._timeout)
            self.finished.emit(
                [(info.get("ip", did), info) for did, info in found.items()])
        except Exception as e:
            log_error(f"设备扫描失败: {e}")
            self.finished.emit([])
