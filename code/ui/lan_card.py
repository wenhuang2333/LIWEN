import os
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
    QLineEdit, QLabel, QTabWidget, QWidget, QComboBox, QFileDialog,
    QAbstractItemView, QMessageBox,
)
from PyQt6.QtCore import Qt, pyqtSignal
from utils.constants import DEFAULT_TCP_PORT


class LanCard(QGroupBox):
    send_requested = pyqtSignal(list, str, int)  # files, host, port

    def __init__(self, parent=None):
        super().__init__("局域网传输", parent)
        self._send_files = []
        self._discovery = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        self.tabs = QTabWidget()
        self._setup_send_tab()
        self._setup_receive_tab()
        layout.addWidget(self.tabs)

        self.speed_label = QLabel("")
        self.speed_label.setObjectName("dropHint")
        self.speed_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.speed_label)

    def _setup_send_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        hint = QLabel("拖拽文件/文件夹到此处加入发送列表")
        hint.setObjectName("dropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self.send_list = QListWidget()
        self.send_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.send_list.setMaximumHeight(100)
        layout.addWidget(self.send_list)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("添加文件/文件夹")
        add_btn.clicked.connect(self._add_to_send)
        remove_btn = QPushButton("移除选中")
        remove_btn.setObjectName("dangerBtn")
        remove_btn.clicked.connect(self._remove_from_send)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("secondaryBtn")
        clear_btn.clicked.connect(self._clear_send)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("目标设备:"))
        self.device_combo = QComboBox()
        self.device_combo.setEditable(True)
        self.device_combo.lineEdit().setPlaceholderText("输入 IP 地址或选择设备...")
        target_row.addWidget(self.device_combo, 1)
        self.scan_btn = QPushButton("扫描设备")
        self.scan_btn.setObjectName("secondaryBtn")
        target_row.addWidget(self.scan_btn)
        layout.addLayout(target_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("端  口:"))
        self.port_edit = QLineEdit(str(DEFAULT_TCP_PORT))
        port_row.addWidget(self.port_edit)
        port_row.addStretch()
        layout.addLayout(port_row)

        self.send_btn = QPushButton("发  送")
        self.send_btn.setMinimumHeight(36)
        self.send_btn.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.send_btn)

        self.tabs.addTab(tab, "发送")

    def _setup_receive_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        hint = QLabel("点击下方按钮开始监听，等待接收文件")
        hint.setObjectName("dropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        info_row = QHBoxLayout()
        info_row.addWidget(QLabel("监听端口:"))
        self.listen_port_edit = QLineEdit(str(DEFAULT_TCP_PORT))
        info_row.addWidget(self.listen_port_edit)
        info_row.addStretch()
        layout.addLayout(info_row)

        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("保存到:"))
        self.save_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "Downloads"))
        save_row.addWidget(self.save_edit)
        save_browse = QPushButton("浏览")
        save_browse.clicked.connect(self._browse_save_dir)
        save_row.addWidget(save_browse)
        layout.addLayout(save_row)

        self.listen_btn = QPushButton("开始监听")
        self.listen_btn.setMinimumHeight(36)
        self.listen_btn.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.listen_btn)

        self.listen_status = QLabel("未监听")
        self.listen_status.setObjectName("statusLabel")
        self.listen_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.listen_status)

        layout.addStretch()
        self.tabs.addTab(tab, "接收")

    def _add_to_send(self):
        paths = []
        files = QFileDialog.getOpenFileNames(self, "选择文件")[0]
        paths.extend(files)
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            paths.append(folder)
        for p in paths:
            if p and p not in self._send_files:
                self._send_files.append(p)
                self.send_list.addItem(p)

    def _remove_from_send(self):
        for item in self.send_list.selectedItems():
            self._send_files.remove(item.text())
            self.send_list.takeItem(self.send_list.row(item))

    def _clear_send(self):
        self._send_files.clear()
        self.send_list.clear()

    def _browse_save_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if folder:
            self.save_edit.setText(folder)

    def add_files(self, paths):
        for p in paths:
            if p not in self._send_files:
                self._send_files.append(p)
                self.send_list.addItem(p)

    def get_send_files(self):
        return list(self._send_files)

    def get_target_host(self):
        text = self.device_combo.currentText().strip()
        if "(" in text and text.endswith(")"):
            text = text.split("(")[-1].rstrip(")")
        return text

    def get_target_port(self):
        try:
            return int(self.port_edit.text())
        except ValueError:
            return DEFAULT_TCP_PORT

    def get_listen_port(self):
        try:
            return int(self.listen_port_edit.text())
        except ValueError:
            return DEFAULT_TCP_PORT

    def get_save_dir(self):
        return self.save_edit.text() or os.path.join(os.path.expanduser("~"), "Downloads")

    def set_status(self, text):
        self.listen_status.setText(text)

    def set_speed(self, text):
        self.speed_label.setText(text)

    def set_devices(self, devices):
        current = self.device_combo.currentText()
        self.device_combo.clear()
        for ip, hostname in devices:
            self.device_combo.addItem(f"{hostname} ({ip})")
        if current:
            idx = self.device_combo.findText(current)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)

    def set_scan_callback(self, callback):
        self.scan_btn.clicked.connect(callback)
