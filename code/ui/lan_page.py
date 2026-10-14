import os
import socket
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
    QLineEdit, QLabel, QFileDialog, QAbstractItemView, QListWidgetItem,
    QStackedWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal
from utils.constants import DEFAULT_TCP_PORT
from utils.settings import settings


class LanPage(QWidget):
    send_requested = pyqtSignal(list, str, int, bool)
    listen_toggled = pyqtSignal(bool, int, str)
    scan_requested = pyqtSignal()
    ping_requested = pyqtSignal(str, int)

    MODE_SEND = 0
    MODE_RECEIVE = 1

    # 本机网络信息的缓存时长（秒）：避免每次设置变更都重新枚举网卡
    _LOCAL_INFO_TTL = 5.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._send_file_settings = {}
        self._listening = False
        self._mode = self.MODE_SEND
        self._devices = {}
        self._send_allowed = False      # enabled only after ping succeeds
        self._confirmed_device = None   # (ip, port) of the ping-confirmed device
        self._setup_ui()

    @property
    def _send_files(self):
        return list(self._send_file_settings.keys())

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # ---- Top row: mode toggle + preview ----
        top_row = QHBoxLayout()

        mode_widget = QWidget()
        mode_widget.setObjectName("modeToggle")
        mode_layout = QHBoxLayout(mode_widget)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(0)

        self.btn_send = QPushButton(" 发送")
        self.btn_send.setCheckable(True)
        self.btn_send.setChecked(True)
        self.btn_send.clicked.connect(lambda: self._set_mode(self.MODE_SEND))

        self.btn_receive = QPushButton(" 接收")
        self.btn_receive.setCheckable(True)
        self.btn_receive.clicked.connect(lambda: self._set_mode(self.MODE_RECEIVE))

        mode_layout.addWidget(self.btn_send)
        mode_layout.addWidget(self.btn_receive)
        top_row.addWidget(mode_widget)
        top_row.addStretch()

        self.preview_btn = QPushButton("预览压缩包")
        self.preview_btn.setObjectName("secondaryBtn")
        top_row.addWidget(self.preview_btn)
        root.addLayout(top_row)

        # ---- Build panels ----
        self._send_panel = self._build_send_panel()
        self._recv_panel = self._build_recv_panel()
        self._device_panel = self._build_device_panel()

        # ---- Content: stacked pages with shared device panel ----
        self.content_stack = QStackedWidget()
        root.addWidget(self.content_stack, 1)

        # Page 0: Send mode — [send_panel | device_panel]
        send_page = QWidget()
        self._send_layout = QHBoxLayout(send_page)
        self._send_layout.setContentsMargins(0, 0, 0, 0)
        self._send_layout.setSpacing(16)
        self._send_layout.addWidget(self._send_panel, 2)
        self.content_stack.addWidget(send_page)

        # Page 1: Receive mode — [device_panel (2) | recv_panel (1)]
        recv_page = QWidget()
        self._recv_layout = QHBoxLayout(recv_page)
        self._recv_layout.setContentsMargins(0, 0, 0, 0)
        self._recv_layout.setSpacing(16)
        self._recv_layout.addWidget(self._recv_panel, 1)
        self.content_stack.addWidget(recv_page)

        self._send_layout.addWidget(self._device_panel, 1)
        self._device_panel_side = self.MODE_SEND

        self._set_mode(self.MODE_SEND)

    # ============================================================
    #  Panel builders
    # ============================================================

    def _build_send_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.settings_title = QLabel("发送设置")
        self.settings_title.setObjectName("sectionTitle")
        layout.addWidget(self.settings_title)

        self._send_widgets = []

        # Drop zone
        self.drop_zone = QWidget()
        self.drop_zone.setObjectName("dropZone")
        dz_layout = QVBoxLayout(self.drop_zone)
        dz_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint = QLabel("拖拽文件或文件夹到此处")
        self.drop_hint.setObjectName("dropZoneHint")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz_layout.addWidget(self.drop_hint)
        self.drop_sub = QLabel("选择要通过局域网发送的内容")
        self.drop_sub.setObjectName("pageHint")
        self.drop_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz_layout.addWidget(self.drop_sub)
        layout.addWidget(self.drop_zone, 1)
        self._send_widgets.append(self.drop_zone)

        # File list
        self.send_list = QListWidget()
        self.send_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.send_list.setMaximumHeight(120)
        layout.addWidget(self.send_list)
        self._send_widgets.append(self.send_list)

        # File buttons
        file_btn_row = QHBoxLayout()
        add_btn = QPushButton("添加文件")
        add_btn.clicked.connect(self._add_to_send)
        add_folder_btn = QPushButton("添加文件夹")
        add_folder_btn.setObjectName("secondaryBtn")
        add_folder_btn.clicked.connect(self._add_folder_to_send)
        remove_btn = QPushButton("移除")
        remove_btn.setObjectName("dangerBtn")
        remove_btn.clicked.connect(self._remove_from_send)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("smallBtn")
        clear_btn.clicked.connect(self._clear_send)
        file_btn_row.addWidget(add_btn)
        file_btn_row.addWidget(add_folder_btn)
        file_btn_row.addWidget(remove_btn)
        file_btn_row.addWidget(clear_btn)
        layout.addLayout(file_btn_row)
        self._send_widgets.extend([add_btn, add_folder_btn, remove_btn, clear_btn])

        # Send hint
        self.send_hint_label = QLabel("请先扫描并选择设备，再进行链路确认")
        self.send_hint_label.setObjectName("statusMuted")
        layout.addWidget(self.send_hint_label)
        self._send_widgets.append(self.send_hint_label)

        layout.addStretch()

        self.send_btn = QPushButton("发  送")
        self.send_btn.setObjectName("actionBtn")
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self._do_send)
        layout.addWidget(self.send_btn)

        return panel

    def _build_recv_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("接收设置")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self._recv_widgets = []

        self.save_label = QLabel("保存到")
        layout.addWidget(self.save_label)
        self._recv_widgets.append(self.save_label)

        save_row = QHBoxLayout()
        save_row.setSpacing(4)
        self.save_edit = QLineEdit(
            settings.get("default_receive_path") or
            os.path.join(os.path.expanduser("~"), "Downloads")
        )
        save_row.addWidget(self.save_edit)
        self._recv_widgets.append(self.save_edit)

        self.save_btn = QPushButton("...")
        self.save_btn.setObjectName("secondaryBtn")
        self.save_btn.setFixedWidth(36)
        self.save_btn.clicked.connect(self._browse_save)
        save_row.addWidget(self.save_btn)
        self._recv_widgets.append(self.save_btn)
        layout.addLayout(save_row)

        self.listen_status_label = QLabel("")
        self.listen_status_label.setStyleSheet("color: #a6e3a1; font-size: 12px;")
        layout.addWidget(self.listen_status_label)
        self._recv_widgets.append(self.listen_status_label)

        self.recv_hint_label = QLabel("开始监听后，其它设备才能向您发送文件")
        self.recv_hint_label.setObjectName("statusMuted")
        layout.addWidget(self.recv_hint_label)
        self._recv_widgets.append(self.recv_hint_label)

        layout.addStretch()

        self.listen_btn = QPushButton("开始监听")
        self.listen_btn.setObjectName("actionBtn")
        self.listen_btn.clicked.connect(self._do_listen_toggle)
        layout.addWidget(self.listen_btn)

        return panel

    def _build_device_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Local info
        local_widget = QWidget()
        local_widget.setObjectName("settingsPanel")
        local_layout = QVBoxLayout(local_widget)
        local_layout.setSpacing(4)

        local_title = QLabel("本机信息")
        local_title.setObjectName("sectionTitle")
        local_layout.addWidget(local_title)

        self.local_ip_label = QLabel(self._get_local_info())
        self.local_ip_label.setStyleSheet(
            "color: #a6e3a1; font-size: 12px; padding: 4px 0;"
        )
        self.local_ip_label.setWordWrap(True)
        local_layout.addWidget(self.local_ip_label)

        layout.addWidget(local_widget)

        # Nearby devices
        dev_widget = QWidget()
        dev_widget.setObjectName("settingsPanel")
        dev_layout = QVBoxLayout(dev_widget)
        dev_layout.setSpacing(6)

        dev_title = QLabel("附近设备")
        dev_title.setObjectName("sectionTitle")
        dev_layout.addWidget(dev_title)

        dev_btn_row = QHBoxLayout()
        self.scan_btn = QPushButton("扫描设备")
        self.scan_btn.setObjectName("secondaryBtn")
        self.scan_btn.clicked.connect(self.scan_requested.emit)
        dev_btn_row.addWidget(self.scan_btn)

        self.ping_btn = QPushButton("链路确认")
        self.ping_btn.setObjectName("secondaryBtn")
        self.ping_btn.clicked.connect(self._do_ping)
        dev_btn_row.addWidget(self.ping_btn)
        dev_btn_row.addStretch()
        dev_layout.addLayout(dev_btn_row)

        self.device_list = QListWidget()
        self.device_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.device_list.setMaximumHeight(180)
        self.device_list.setStyleSheet(
            "QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #252536; }"
        )
        self.device_list.itemSelectionChanged.connect(self._on_device_selection_changed)
        dev_layout.addWidget(self.device_list)

        self.device_status_label = QLabel("")
        self.device_status_label.setObjectName("statusMuted")
        dev_layout.addWidget(self.device_status_label)

        self.ping_result_label = QLabel("")
        self.ping_result_label.setStyleSheet("font-size: 11px;")
        self.ping_result_label.setWordWrap(True)
        dev_layout.addWidget(self.ping_result_label)

        layout.addWidget(dev_widget, 1)

        return panel

    # ============================================================
    #  Mode switching
    # ============================================================

    def _set_mode(self, mode):
        self._mode = mode
        self.btn_send.setChecked(mode == self.MODE_SEND)
        self.btn_receive.setChecked(mode == self.MODE_RECEIVE)

        is_send = mode == self.MODE_SEND

        self.settings_title.setText("发送设置" if is_send else "接收设置")
        self.drop_hint.setText(
            "拖拽文件或文件夹到此处" if is_send else "等待接收文件..."
        )

        for w in self._send_widgets:
            w.setVisible(is_send)
        for w in self._recv_widgets:
            w.setVisible(not is_send)

        # Reparent device panel to the correct layout
        if is_send and self._device_panel_side != self.MODE_SEND:
            self._recv_layout.removeWidget(self._device_panel)
            self._send_layout.addWidget(self._device_panel, 1)
            self._device_panel_side = self.MODE_SEND
        elif not is_send and self._device_panel_side != self.MODE_RECEIVE:
            self._send_layout.removeWidget(self._device_panel)
            self._recv_layout.insertWidget(0, self._device_panel, 2)
            self._device_panel_side = self.MODE_RECEIVE

        self._device_panel.show()
        self.content_stack.setCurrentIndex(0 if is_send else 1)

    # ============================================================
    #  File management
    # ============================================================

    def _add_to_send(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择文件")
        for f in files:
            if f not in self._send_file_settings:
                self._send_file_settings[f] = {}
                self.send_list.addItem(f)
        self._update_send_button()

    def _add_folder_to_send(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder and folder not in self._send_file_settings:
            self._send_file_settings[folder] = {}
            self.send_list.addItem(folder)
        self._update_send_button()

    def _remove_from_send(self):
        for item in self.send_list.selectedItems():
            t = item.text()
            if t in self._send_file_settings:
                del self._send_file_settings[t]
            self.send_list.takeItem(self.send_list.row(item))
        self._update_send_button()

    def _clear_send(self):
        self._send_file_settings.clear()
        self.send_list.clear()
        self._send_allowed = False
        self._confirmed_device = None
        self.ping_result_label.setText("")
        self._update_send_button()

    def _browse_save(self):
        folder = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if folder:
            self.save_edit.setText(folder)

    # ============================================================
    #  Action
    # ============================================================

    def _do_send(self):
        if not self._send_allowed or not self._confirmed_device:
            return
        if not self._send_files:
            return

        host_ip, port = self._confirmed_device
        self.send_requested.emit(list(self._send_files), host_ip, port, True)

    def _do_listen_toggle(self):
        self._listening = not self._listening
        if self._listening:
            self.listen_btn.setText("停止监听")
            self.listen_status_label.setText("● 正在监听...")
            self.listen_status_label.setVisible(True)
            self.recv_hint_label.setText("等待其它设备连接...")
            save_dir = self.save_edit.text()
            self.listen_toggled.emit(True, DEFAULT_TCP_PORT, save_dir)
        else:
            self.listen_btn.setText("开始监听")
            self.listen_status_label.setVisible(False)
            self.listen_status_label.setText("")
            self.recv_hint_label.setText("开始监听后，其它设备才能向您发送文件")
            self.listen_toggled.emit(False, 0, "")

    # ============================================================
    #  Device management
    # ============================================================

    def _get_local_info(self, force: bool = False):
        """本机网络信息（名称 / 主机名 / IP）。

        性能注意：本方法会被 ``_auto_save()`` 间接调用（用户每敲一个字都会
        触发 settings_changed）。原实现调用 ``socket.getaddrinfo(hostname)``，
        这是一次**可能阻塞数秒的 DNS 解析**，且发生在 Qt 主线程上——在
        hosts/DNS 异常（VPN、临时局域网、企业域环境）时表现为输入框卡死。
        现在改为纯本地网卡枚举，并加 TTL 缓存。
        """
        import time as _time
        cached = getattr(self, "_local_info_cache", None)
        if (not force and cached is not None
                and _time.monotonic() - cached[0] < self._LOCAL_INFO_TTL):
            return cached[1]

        lines = []
        try:
            name = settings.username
            hostname = socket.gethostname()
            if name != hostname:
                lines.append(f"名称: {name}")
            lines.append(f"主机: {hostname}")
            # 本地网卡枚举，不做 DNS 解析
            try:
                from network.interfaces import get_lan_interfaces
                ips = []
                for iface in get_lan_interfaces():
                    ip = iface.get("ip")
                    if ip and ip not in ips:
                        ips.append(ip)
            except Exception:
                ips = []
            if not ips:
                # 兜底：只查本机名对应地址（仍可能触发解析，故放在失败分支）
                try:
                    for item in socket.getaddrinfo(hostname, None):
                        ip = item[4][0]
                        if ip not in ips and not ip.startswith("127.") and ":" not in ip:
                            ips.append(ip)
                except Exception:
                    pass
            for ip in ips:
                lines.append(f"IP: {ip}")
        except Exception:
            lines.append("无法获取网络信息")

        text = "\n".join(lines) if lines else "无网络连接"
        self._local_info_cache = (_time.monotonic(), text)
        return text

    def _on_device_selection_changed(self):
        selected = self.device_list.selectedItems()
        if selected:
            self.device_status_label.setText(f"已选择 {len(selected)} 个设备")
        else:
            self.device_status_label.setText("")
        # Reset send permission when selection changes
        self._send_allowed = False
        self._confirmed_device = None
        self._update_send_button()
        self.ping_result_label.setText("")

    def _update_send_button(self):
        if self._mode == self.MODE_SEND:
            has_files = bool(self._send_files)
            self.send_btn.setEnabled(self._send_allowed and has_files)
            if not self._send_allowed:
                self.send_hint_label.setText("请先扫描并选择设备，再进行链路确认")
            elif not has_files:
                self.send_hint_label.setText("请先添加要发送的文件")
            else:
                self.send_hint_label.setText("就绪，点击发送")

    def _do_ping(self):
        host, port = self._get_target_host_port()
        if not host:
            self.ping_result_label.setText("请先选择一个设备")
            self.ping_result_label.setStyleSheet("color: #f38ba8; font-size: 11px;")
            return
        self.ping_result_label.setText("正在检测...")
        self.ping_result_label.setStyleSheet("color: #f9e2af; font-size: 11px;")
        self.ping_requested.emit(host, port)

    def set_ping_result(self, success, msg):
        if success:
            self.ping_result_label.setText(f"✓ {msg}")
            self.ping_result_label.setStyleSheet("color: #a6e3a1; font-size: 11px;")
            # Enable send on successful ping
            host, port = self._get_target_host_port()
            if host:
                self._send_allowed = True
                self._confirmed_device = (host, port)
                self._update_send_button()
        else:
            self.ping_result_label.setText(f"✗ {msg}")
            self.ping_result_label.setStyleSheet("color: #f38ba8; font-size: 11px;")
            self._send_allowed = False
            self._confirmed_device = None
            self._update_send_button()

    def _get_target_host_port(self):
        items = self.device_list.selectedItems()
        if items:
            data = items[0].data(Qt.ItemDataRole.UserRole)
            if data and "ip" in data:
                port = data.get("port", DEFAULT_TCP_PORT)
                return data["ip"], port
        return "", 0

    def _get_selected_devices(self):
        result = {}
        for item in self.device_list.selectedItems():
            data = item.data(Qt.ItemDataRole.UserRole)
            if data and "ip" in data:
                result[data["ip"]] = data
        return result

    def handle_drop(self, paths):
        if self._mode == self.MODE_SEND:
            for p in paths:
                if p not in self._send_file_settings:
                    self._send_file_settings[p] = {}
                    self.send_list.addItem(p)
            self._update_send_button()

    def set_listen_status(self, text):
        if self._listening:
            self.listen_status_label.setText(f"● {text}")

    def set_devices(self, devices):
        current_selected = self._get_selected_devices()
        self._devices = {}
        self.device_list.clear()
        for ip, info in devices:
            self._devices[ip] = info
            name = info.get("username", ip)
            port = info.get("port", DEFAULT_TCP_PORT)
            display = f"{name}\nIP: {ip}  |  端口: {port}"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, {"ip": ip, "name": name, "port": port})
            self.device_list.addItem(item)
            if ip in current_selected:
                item.setSelected(True)

        count = len(self._devices)
        self.device_status_label.setText(
            f"发现 {count} 个设备" if count > 0 else "未发现设备，点击扫描"
        )

    def on_receive_finished(self):
        self._listening = False
        self.listen_btn.setText("开始监听")
        self.listen_status_label.setVisible(False)
        self.recv_hint_label.setText("开始监听后，其它设备才能向您发送文件")

    @property
    def is_listening(self):
        return self._listening

    def set_busy(self, busy):
        self.send_btn.setEnabled(not busy and self._send_allowed)
        self.listen_btn.setEnabled(not busy)
        self.btn_send.setEnabled(not busy)
        self.btn_receive.setEnabled(not busy)
        self.scan_btn.setEnabled(not busy)
        self.ping_btn.setEnabled(not busy)

    def set_scan_busy(self, busy: bool):
        """设备扫描期间禁用按钮，避免重复点击产生并发扫描线程。"""
        self.scan_btn.setEnabled(not busy)
        self.scan_btn.setText("扫描中…" if busy else "扫描设备")

    def set_ping_busy(self, busy: bool):
        """链路确认期间禁用按钮，避免重复点击覆盖仍在运行的线程。"""
        self.ping_btn.setEnabled(not busy)
        self.ping_btn.setText("检测中…" if busy else "链路确认")

    def refresh_local_info(self, force: bool = False):
        self.local_ip_label.setText(self._get_local_info(force=force))
