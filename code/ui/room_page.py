"""局域网房间页面 — 房间内视图（纯应用层组网）

显示房间信息、在线成员、语音通话、房间内文件传输和连接状态。
所有通信基于真实局域网 IP，不创建虚拟网卡、不需要管理员权限，
因此 Windows / macOS / Linux 使用同一套代码即可互相联机。

房间内文件传输直接复用「局域网传输」那套已经加固过的 TCP 协议
（``network/transfer.py``：压缩、请求/接受、原子落盘、路径穿越防护），
只是把端口换成由房间端口推导出来的 ``room_data_port_for_room()``。
"""

import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QTextEdit, QMessageBox, QComboBox,
    QProgressBar, QFileDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor

from utils.constants import voice_port_for_room, room_data_port_for_room
from utils.helpers import format_size
from utils.logger import info, warn
from utils.settings import settings
from ui.styles import MONO_FONT_FAMILY
from core.workers import VoiceWorker, SendWorker, ReceiveWorker

_VOICE_AVAILABLE = False
_VOICE_BACKEND = ""
_VOICE_HINT = "pip install sounddevice"
try:
    from network.voice_protocol import (
        is_voice_available, voice_backend_name, INSTALL_HINT)
    _VOICE_AVAILABLE = is_voice_available()
    _VOICE_BACKEND = voice_backend_name()
    _VOICE_HINT = INSTALL_HINT
except Exception:
    pass

# 「发送给全部成员」在目标下拉框里的哨兵值
ALL_TARGETS = "__all__"


class RoomPage(QWidget):
    """In-room page — application-layer networking only.

    Shows room info, online members, voice controls, file transfer, and status.
    """

    leave_requested = pyqtSignal()
    peer_list_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._room_context: dict | None = None
        self._roster: list = []
        self._voice_worker: VoiceWorker | None = None
        self._voice_on = False
        self._peer_ips: list[str] = []
        self._data_port = 0
        self._local_ip = ""
        self._recv_worker: ReceiveWorker | None = None
        self._send_worker: SendWorker | None = None
        self._send_queue: list = []
        self._send_paths: list = []
        self._send_total = 0

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
        peer_header = QLabel("在线成员")
        peer_header.setObjectName("sectionTitle")
        root.addWidget(peer_header)

        self._peer_list = QListWidget()
        self._peer_list.setAlternatingRowColors(True)
        self._peer_list.setMinimumHeight(140)
        self._peer_list.setStyleSheet(
            "QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #252536; }")
        root.addWidget(self._peer_list, 1)

        # ── File transfer row ──
        send_row = QHBoxLayout()
        send_row.setSpacing(8)

        send_row.addWidget(QLabel("发送给"))
        self._target_combo = QComboBox()
        self._target_combo.setMinimumWidth(180)
        send_row.addWidget(self._target_combo)

        self._send_btn = QPushButton("发送文件")
        self._send_btn.setObjectName("secondaryBtn")
        self._send_btn.clicked.connect(self._on_send_files)
        self._send_btn.setEnabled(False)
        send_row.addWidget(self._send_btn)

        self._xfer_label = QLabel("")
        self._xfer_label.setStyleSheet("color: #9498a4; font-size: 11px;")
        send_row.addWidget(self._xfer_label)
        send_row.addStretch()
        root.addLayout(send_row)

        self._xfer_bar = QProgressBar()
        self._xfer_bar.setRange(0, 100)
        self._xfer_bar.setValue(0)
        self._xfer_bar.setVisible(False)
        root.addWidget(self._xfer_bar)

        # ── Log area ──
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumHeight(140)
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
            self._voice_btn.setToolTip(f"需要音频后端: {_VOICE_HINT}")
            self._voice_btn.setEnabled(False)
            self._voice_btn.setStyleSheet(
                "QPushButton { background-color: #45475a; color: #9498a4;"
                "  font-size: 13px; padding: 9px 18px;"
                "  border: none; border-radius: 6px; }")
        else:
            self._voice_btn.setToolTip(
                f"使用音频后端: {_VOICE_BACKEND or '自动'}")
        ctrl.addWidget(self._voice_btn)

        self._leave_btn = QPushButton("离开房间")
        self._leave_btn.setObjectName("dangerBtn")
        self._leave_btn.clicked.connect(self._request_leave)
        ctrl.addWidget(self._leave_btn)

        ctrl.addStretch()
        root.addLayout(ctrl)

    # ── Room context ─────────────────────────────────────────

    def set_room_context(self, room_info: dict):
        """Configure room display from room data and start the receive listener."""
        self._room_context = dict(room_info)
        self._roster = []

        room_name = room_info.get("room_name", "未命名")
        session_id = room_info.get("session_id", "")
        is_host = not session_id
        try:
            port = int(room_info.get("port", 0))
        except (TypeError, ValueError):
            port = 0

        self._local_ip = self._detect_local_ip()
        host_ip = room_info.get("host_ip") or self._local_ip or "?"
        self._data_port = self._resolve_data_port(room_info, port)

        self._room_name_label.setText(f"房间: {room_name}")
        self._info_label.setText(
            f"{'本机为主机' if is_host else f'主机: {host_ip}:{port}'}\n"
            f"会话: {session_id[:12] + '…' if session_id else '（房主）'}\n"
            f"模式: 应用层直连（真实 IP，无需虚拟网卡/管理员）  |  "
            f"文件端口: {self._data_port}")
        self._log(f"[房间] 已进入房间: {room_name}")
        self._log(f"[房间] 文件传输端口: {self._data_port}")

        self._status_label.setText("已连接")
        self._status_label.setStyleSheet(
            "color: #a6e3a1; font-weight: bold; font-size: 12px;")

        # 房主进入房间后，本机就是成员名单的来源（由 RoomHostWorker 推送）；
        # 加入者则先用房主在 JOIN_ACCEPT 里回的名单画一次，之后靠心跳刷新。
        roster = room_info.get("roster") or []
        if roster:
            self.update_peers(roster)
        elif is_host:
            self.update_peers([{
                "peer_id": room_info.get("peer_id", "host"),
                "username": room_info.get("username") or settings.username or "房主",
                "ip": self._local_ip,
                "is_host": True,
            }])
        else:
            self.update_peers([{
                "peer_id": "host",
                "username": room_info.get("host_username", "房主"),
                "ip": host_ip,
                "is_host": True,
            }])

        self._start_receiver()

    @staticmethod
    def _detect_local_ip() -> str:
        try:
            from network.interfaces import get_local_ip
            return get_local_ip() or ""
        except Exception:
            return ""

    def _resolve_data_port(self, room_info: dict, port: int) -> int:
        """房间内文件传输端口：优先用房主协商好的值，否则本地推导。"""
        try:
            value = int(room_info.get("data_port") or 0)
        except (TypeError, ValueError):
            value = 0
        if 1 <= value <= 65535:
            return value
        return room_data_port_for_room(port)

    def update_peers(self, roster: list):
        """用房主下发的成员名单刷新成员列表、发送目标与语音目标。

        参数是名单列表（``[{peer_id, username, ip, is_host}]``）。原实现收的是
        ``{ip: {...}}`` 字典，但**从来没有被调用过**，所以"在线玩家"永远只显示
        房主一行。
        """
        self._roster = [dict(item) for item in roster if isinstance(item, dict)]
        self._peer_list.clear()
        self._peer_ips.clear()

        others = []
        for item in self._roster:
            name = item.get("username") or item.get("ip") or "未知"
            ip = item.get("ip", "")
            is_host = bool(item.get("is_host"))
            mine = self._is_self(item)
            self._add_peer(name, ip, is_host=is_host, is_self=mine)
            if not mine and ip:
                others.append({"name": name, "ip": ip})

        self._sync_targets(others)
        self._sync_voice_targets()

        self.peer_list_changed.emit(list(self._roster))

    def _is_self(self, entry: dict) -> bool:
        my_id = (self._room_context or {}).get("peer_id", "")
        if my_id and entry.get("peer_id") == my_id:
            return True
        # 房主在名单里没有 session_id，只能靠 is_host + 本机 IP 判断
        return bool(entry.get("is_host")) and bool(self._local_ip) \
            and entry.get("ip") == self._local_ip

    def _sync_targets(self, others: list):
        """刷新「发送给」下拉框；没有其他成员时禁用发送按钮。"""
        current = self._target_combo.currentData()
        self._target_combo.blockSignals(True)
        self._target_combo.clear()
        if others:
            self._target_combo.addItem(f"全部成员（{len(others)}）", ALL_TARGETS)
            for item in others:
                self._target_combo.addItem(f"{item['name']} ({item['ip']})", item["ip"])
        self._target_combo.blockSignals(False)
        if current:
            idx = self._target_combo.findData(current)
            if idx >= 0:
                self._target_combo.setCurrentIndex(idx)
        self._send_btn.setEnabled(bool(others))
        self._send_btn.setToolTip("" if others else "房间内还没有其他成员")

    def _sync_voice_targets(self):
        """把成员 IP 交给语音线程 —— 否则房间语音永远发不出去。"""
        if not (self._voice_worker and self._voice_on):
            return
        try:
            self._voice_worker.set_peers(self._peer_ips)
        except Exception as e:
            warn(f"RoomPage 同步语音目标失败: {e}")

    def _add_peer(self, name: str, ip: str, is_host: bool = False,
                  is_self: bool = False):
        tags = []
        if is_host:
            tags.append("房主")
        if is_self:
            tags.append("本机")
        tag = f" [{'/'.join(tags)}]" if tags else ""
        display = f"{name}{tag}\nIP: {ip or '—'}"
        item = QListWidgetItem(display)
        item.setData(Qt.ItemDataRole.UserRole, ip)
        self._peer_list.addItem(item)
        if ip and ip not in self._peer_ips:
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
                "语音通话需要音频后端库。\n请运行: " + _VOICE_HINT)
            self._voice_btn.setChecked(False)
            return
        if not self._room_context:
            self._voice_btn.setChecked(False)
            return
        if self._voice_worker and self._voice_worker.isRunning():
            self._voice_btn.setChecked(True)
            return

        room_id = self._room_context.get("room_id", "unknown")
        port = int(self._room_context.get("port", 12345) or 12345)
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
        # 必须在 start() 之前把目标塞进去：语音线程起来后再补一次也行，
        # 但先设置可以避免开头几百毫秒的音频发给空列表。
        self._voice_worker.set_peers(self._peer_ips)
        self._voice_worker.start()

        self._voice_on = True
        self._voice_btn.setText("语音通话中")
        self._voice_btn.setStyleSheet(
            "QPushButton { background-color: #a6e3a1; color: #1e1e2e;"
            "  font-weight: bold; font-size: 13px; padding: 9px 18px;"
            "  border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #94e2d5; }")
        self._log(f"[语音] 语音通话已开启（{len(self._peer_ips)} 个目标）")

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

    # ── 房间内文件传输 ───────────────────────────────────────

    def _save_dir(self) -> str:
        path = settings.get("default_receive_path") or \
            os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            warn(f"房间接收目录不可用，回退到用户目录: {e}")
            path = os.path.expanduser("~")
        return path

    def _start_receiver(self):
        """进入房间即开始监听房间文件端口，接收其他成员发来的文件。"""
        self._stop_receiver()
        if not self._data_port:
            return
        try:
            worker = ReceiveWorker(
                self._data_port, self._save_dir(),
                auto_accept=bool(settings.get("auto_accept")))
            worker.progress.connect(self._on_xfer_progress)
            worker.status.connect(self._on_recv_status)
            worker.error.connect(lambda msg: self._log(f"[接收错误] {msg}"))
            worker.request_decision.connect(self._on_receive_request)
            worker.start()
            self._recv_worker = worker
            info(f"RoomPage: 房间文件接收已启动，端口 {self._data_port}")
        except Exception as e:
            warn(f"RoomPage: 房间文件接收启动失败: {e}")
            self._log(f"[接收] 启动失败: {e}")

    def _stop_receiver(self):
        worker = self._recv_worker
        self._recv_worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.cancel()
                if not worker.wait(3000):
                    warn("RoomPage: 房间接收线程未在 3s 内退出")
        except Exception as e:
            warn(f"RoomPage: 停止房间接收异常: {e}")

    def _on_recv_status(self, msg: str):
        self._log(f"[接收] {msg}")

    def _on_receive_request(self, info: dict, decision: dict, event):
        """其他成员发来文件时的确认对话框（与局域网传输页行为一致）。"""
        if event is None:
            return
        try:
            if settings.get("auto_accept"):
                decision["accepted"] = True
                event.set()
                return
            reply = QMessageBox.question(
                self, "收到传输请求",
                f"来自: {info.get('from_name', info.get('from', '未知'))}\n"
                f"文件: {info.get('name', '?')}\n"
                f"大小: {format_size(info.get('size', 0))}\n\n是否接受?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            decision["accepted"] = (reply == QMessageBox.StandardButton.Yes)
        finally:
            event.set()

    def _on_send_files(self):
        if not self._room_context or not self._data_port:
            return
        if self._send_worker is not None and self._send_worker.isRunning():
            self._log("[发送] 上一次发送尚未结束")
            return

        targets = self._resolve_send_targets()
        if not targets:
            QMessageBox.information(self, "提示", "房间内还没有其他成员")
            return

        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        if not paths:
            return

        self._send_queue = list(targets)
        self._send_total = len(paths)
        self._send_paths = list(paths)
        self._xfer_bar.setVisible(True)
        self._xfer_bar.setValue(0)
        self._start_next_send()

    def _resolve_send_targets(self) -> list:
        selected = self._target_combo.currentData()
        others = [{"ip": item["ip"], "name": item.get("username") or item["ip"]}
                  for item in self._roster
                  if not self._is_self(item) and item.get("ip")]
        if selected == ALL_TARGETS or selected is None:
            return others
        return [t for t in others if t["ip"] == selected]

    def _start_next_send(self):
        if not self._send_queue:
            self._send_worker = None
            self._xfer_bar.setVisible(False)
            self._xfer_label.setText("")
            self._log("[发送] 全部完成")
            return
        target = self._send_queue.pop(0)
        remaining = len(self._send_queue)
        suffix = f"（还有 {remaining} 个目标）" if remaining else ""
        self._log(f"[发送] 正在发送 {self._send_total} 个文件 → "
                  f"{target['name']} ({target['ip']}:{self._data_port}) {suffix}")
        self._xfer_label.setText(f"发送至 {target['name']}")

        worker = SendWorker(
            self._send_paths, target["ip"], self._data_port,
            compress_first=True,
            from_name=settings.username,
            send_request=True)
        worker.progress.connect(self._on_xfer_progress)
        worker.status.connect(lambda msg: self._log(f"[发送] {msg}"))
        worker.error.connect(self._on_send_error)
        worker.finished.connect(self._on_send_done)
        worker.start()
        self._send_worker = worker

    def _on_send_done(self, result):
        self._xfer_bar.setValue(100)
        self._log("[发送] 完成")
        self._start_next_send()

    def _on_send_error(self, msg: str):
        self._log(f"[发送错误] {msg}")
        # 单个目标失败不应该中断其余目标
        self._start_next_send()

    def _on_xfer_progress(self, pct):
        try:
            self._xfer_bar.setVisible(True)
            self._xfer_bar.setValue(int(pct))
        except (TypeError, ValueError):
            pass

    # ── Session state ────────────────────────────────────────

    def on_session_lost(self):
        """房主不认识本机的会话（房主重启，或本机超时被剔除）。"""
        self._status_label.setText("会话已失效")
        self._status_label.setStyleSheet(
            "color: #f38ba8; font-weight: bold; font-size: 12px;")
        self._log("[房间] 与房主的会话已失效（房主可能已重启），请重新加入房间")
        self._stop_receiver()
        self._stop_voice()

    # ── Leave ────────────────────────────────────────────────

    def _request_leave(self):
        self.shutdown()
        self._room_context = None
        self._roster = []
        self._peer_list.clear()
        self._peer_ips.clear()
        self._target_combo.clear()
        self._send_btn.setEnabled(False)
        self._xfer_bar.setVisible(False)
        self._xfer_label.setText("")
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
        self._stop_sender()
        self._stop_receiver()
        if self._voice_on:
            self._stop_voice()

    def cleanup(self):
        self.shutdown()

    def _stop_sender(self):
        worker = self._send_worker
        self._send_queue = []
        self._send_worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.cancel()
                if not worker.wait(3000):
                    warn("RoomPage: 发送线程未在 3s 内退出")
        except Exception as e:
            warn(f"RoomPage: 停止发送异常: {e}")

    def _log(self, msg: str):
        self._log_view.append(msg)
        doc = self._log_view.document()
        while doc.blockCount() > 300:
            block = doc.findBlockByNumber(0)
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
