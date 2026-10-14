"""局域网房间页面 — 简化的房间内视图（无 VPN）

显示房间信息、在线玩家列表、语音通话控制和连接状态。
所有通信基于真实局域网 IP，不创建虚拟网卡。
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QTextEdit, QMessageBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QTextCursor

from utils.constants import voice_port_for_room
from utils.settings import settings
from ui.styles import MONO_FONT_FAMILY
from core.workers import VoiceWorker

_VOICE_AVAILABLE = False
try:
    from network.voice_protocol import is_voice_available
    _VOICE_AVAILABLE = is_voice_available()
except Exception:
    pass


class RoomPage(QWidget):
    """Simple in-room page — no VPN, no virtual adapter.

    Shows room info, online peers, voice controls, and connection status.
    Communicates over real LAN IP addresses.
    """

    leave_requested = pyqtSignal()
    peer_list_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._room_context: dict | None = None
        self._voice_worker: VoiceWorker | None = None
        self._voice_on = False
        self._peer_ips: list[str] = []

        self._setup_ui()

    # ── UI setup ─────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # ── Top bar: room info + status ──
        top = QHBoxLayout()

        self._room_name_label = QLabel("")
        self._room_name_label.setStyleSheet(
            "color: #c8ccd4; font-weight: bold; font-size: 14px;")
        top.addWidget(self._room_name_label)

        top.addStretch()

        self._status_label = QLabel("已连接")
        self._status_label.setStyleSheet(
            "color: #a6e3a1; font-weight: bold; font-size: 12px;")
        top.addWidget(self._status_label)

        root.addLayout(top)

        # ── Room info summary ──
        info_widget = QWidget()
        info_widget.setObjectName("settingsPanel")
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(16, 12, 16, 12)
        info_layout.setSpacing(4)

        self._info_label = QLabel("")
        self._info_label.setStyleSheet(
            "color: #9498a4; font-size: 12px;")
        self._info_label.setWordWrap(True)
        info_layout.addWidget(self._info_label)

        root.addWidget(info_widget)

        # ── Peer list ──
        peer_header = QLabel("在线玩家")
        peer_header.setObjectName("sectionTitle")
        root.addWidget(peer_header)

        self._peer_list = QListWidget()
        self._peer_list.setAlternatingRowColors(True)
        self._peer_list.setMinimumHeight(160)
        self._peer_list.setStyleSheet(
            "QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #252536; }")
        root.addWidget(self._peer_list, 1)

        # ── Log area ──
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumHeight(150)
        self._log_view.setStyleSheet(
            "QTextEdit {"
            "  background-color: #1e1e2e; color: #c8ccd4;"
            f"  font-family: {MONO_FONT_FAMILY}; font-size: 11px;"
            "  border: 1px solid #313244; border-radius: 6px; padding: 10px; }")
        root.addWidget(self._log_view)

        # ── Control buttons ──
        ctrl = QHBoxLayout()
        ctrl.addStretch()

        self._voice_btn = QPushButton("语音通话")
        self._voice_btn.setCheckable(True)
        self._voice_btn.clicked.connect(self._toggle_voice)
        if not _VOICE_AVAILABLE:
            self._voice_btn.setToolTip("需要安装 PyAudio: pip install pyaudio")
            self._voice_btn.setEnabled(False)
            self._voice_btn.setStyleSheet(
                "QPushButton { background-color: #45475a; color: #9498a4;"
                "  font-size: 13px; padding: 9px 18px;"
                "  border: none; border-radius: 6px; }")
        ctrl.addWidget(self._voice_btn)

        self._leave_btn = QPushButton("离开房间")
        self._leave_btn.setObjectName("dangerBtn")
        self._leave_btn.clicked.connect(self._request_leave)
        ctrl.addWidget(self._leave_btn)

        ctrl.addStretch()
        root.addLayout(ctrl)

    # ── Room context ─────────────────────────────────────────

    def set_room_context(self, room_info: dict):
        """Configure room display from room data."""
        self._room_context = room_info
        room_name = room_info.get("room_name", "未命名")
        host_ip = room_info.get("host_ip", "?")
        port = room_info.get("port", 0)
        session_id = room_info.get("session_id", "")

        self._room_name_label.setText(f"房间: {room_name}")
        self._info_label.setText(
            f"主机: {host_ip}:{port}\n"
            f"会话: {session_id[:12] if session_id else '无'}…\n"
            f"模式: 局域网直连")
        self._log(f"[房间] 已进入房间: {room_name}")
        self._log(f"[房间] 主机: {host_ip}:{port}")

        # Add host to peer list
        self._add_peer(
            room_info.get("host_username", "房主"),
            host_ip, is_host=True)

    def update_peers(self, peers: dict):
        """Update the peer list. peers: {ip: {username, ...}}"""
        self._peer_list.clear()
        self._peer_ips.clear()

        # Host is always first
        if self._room_context:
            host_ip = self._room_context.get("host_ip", "")
            host_name = self._room_context.get("host_username", "房主")
            self._add_peer(host_name, host_ip, is_host=True)

        for ip, info in peers.items():
            if ip != self._room_context.get("host_ip", ""):
                self._add_peer(
                    info.get("username", ip), ip, is_host=False)

        # Update voice targets
        if self._voice_worker and self._voice_on:
            from network.voice_protocol import VoiceClient
            # The voice client's set_peers will be called via the worker
            pass

    def _add_peer(self, name: str, ip: str, is_host: bool = False):
        tag = " [房主]" if is_host else ""
        display = f"{name}{tag}\nIP: {ip}"
        item = QListWidgetItem(display)
        item.setData(Qt.ItemDataRole.UserRole, ip)
        self._peer_list.addItem(item)
        if ip not in self._peer_ips:
            self._peer_ips.append(ip)

    # ── Voice ────────────────────────────────────────────────

    def _toggle_voice(self):
        if self._voice_on:
            self._stop_voice()
        else:
            self._start_voice()

    def _start_voice(self):
        if not _VOICE_AVAILABLE:
            QMessageBox.warning(self, "提示",
                "语音通话需要 PyAudio 库。\n请运行: pip install pyaudio")
            self._voice_btn.setChecked(False)
            return
        if not self._room_context:
            self._voice_btn.setChecked(False)
            return
        if self._voice_worker and self._voice_worker.isRunning():
            self._voice_btn.setChecked(True)
            return

        room_id = self._room_context.get("room_id", "unknown")
        port = int(self._room_context.get("port", 12345))
        # 语音端口不能直接用 room_port + 2：默认端口下会等于房间发现端口 12347，
        # 而房主已占用该端口，导致房主侧语音永远起不来。
        voice_port = voice_port_for_room(port)
        sender_id = f"{room_id}:{settings.username}"

        self._voice_worker = VoiceWorker(sender_id, voice_port)
        self._voice_worker.status.connect(
            lambda msg: self._log(f"[语音] {msg}"))
        self._voice_worker.error.connect(
            lambda msg: self._log(f"[语音错误] {msg}"))
        self._voice_worker.finished.connect(self._on_voice_finished)
        self._voice_worker.start()

        self._voice_on = True
        self._voice_btn.setText("语音通话中")
        self._voice_btn.setStyleSheet(
            "QPushButton { background-color: #a6e3a1; color: #1e1e2e;"
            "  font-weight: bold; font-size: 13px; padding: 9px 18px;"
            "  border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #94e2d5; }")
        self._log("[语音] 语音通话已开启")

    def _on_voice_finished(self, ok: bool):
        if not ok and self._voice_on:
            self._voice_on = False
            self._voice_btn.setText("语音通话")
            self._voice_btn.setStyleSheet("")
            self._voice_btn.setChecked(False)
            self._voice_worker = None

    def _stop_voice(self):
        if self._voice_worker and self._voice_worker.isRunning():
            self._voice_worker.cancel()
            self._voice_worker.wait(2000)
        self._voice_worker = None
        self._voice_on = False
        self._voice_btn.setText("语音通话")
        self._voice_btn.setStyleSheet("")
        self._voice_btn.setChecked(False)
        self._log("[语音] 语音通话已关闭")

    # ── Leave ────────────────────────────────────────────────

    def _request_leave(self):
        self.shutdown()
        self._room_context = None
        self._peer_list.clear()
        self._peer_ips.clear()
        self._room_name_label.setText("")
        self._info_label.setText("")
        self._status_label.setText("未连接")
        self._status_label.setStyleSheet(
            "color: #f38ba8; font-weight: bold; font-size: 12px;")
        self._voice_btn.setChecked(False)
        self._log("[房间] 已离开房间")
        self.leave_requested.emit()

    # ── External API ─────────────────────────────────────────

    def shutdown(self):
        """Clean up without UI side-effects."""
        if self._voice_on:
            self._stop_voice()

    def cleanup(self):
        self.shutdown()

    def _log(self, msg: str):
        self._log_view.append(msg)
        doc = self._log_view.document()
        while doc.blockCount() > 300:
            block = doc.findBlockByNumber(0)
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
