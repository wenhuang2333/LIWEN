"""联机大厅 — 房间创建、发现、加入（纯局域网，无 VPN）

LobbyView: create room form + LAN room list + saved rooms list.
RoomPage: simplified in-room view with peer list and voice.
All communication uses real LAN IP addresses.
"""

import hashlib
import socket
import uuid

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QStackedWidget,
    QSpinBox, QMessageBox, QInputDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal

from ui.room_page import RoomPage
from core.workers import (
    RoomHostWorker, RoomScanWorker, RoomJoinWorker, RoomSessionWorker,
)
from utils.room_store import room_store
from utils.settings import settings
from utils.constants import room_data_port_for_room


class LobbyView(QWidget):
    create_room = pyqtSignal(dict)
    join_room = pyqtSignal(dict)
    scan_requested = pyqtSignal()
    delete_saved = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        # ── Left: Create Room ──
        left = QWidget()
        left.setObjectName("settingsPanel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(20, 16, 20, 16)
        left_layout.setSpacing(10)

        title = QLabel("创建房间")
        title.setObjectName("sectionTitle")
        left_layout.addWidget(title)

        # Room name
        left_layout.addWidget(QLabel("房间名称"))
        self._create_name = QLineEdit()
        self._create_name.setPlaceholderText("输入房间名称")
        left_layout.addWidget(self._create_name)

        # Password toggle
        pwd_header = QHBoxLayout()
        pwd_header.setContentsMargins(0, 0, 0, 0)
        pwd_header.setSpacing(8)
        self._pwd_toggle = QPushButton()
        self._pwd_toggle.setCheckable(True)
        self._pwd_toggle.setChecked(False)
        self._pwd_toggle.setFixedSize(44, 24)
        self._pwd_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pwd_toggle.setStyleSheet(self._toggle_style(False))
        self._pwd_toggle.clicked.connect(self._on_pwd_toggled)
        pwd_header.addWidget(self._pwd_toggle)
        pwd_header.addWidget(QLabel("设置密码"))
        pwd_header.addStretch()
        left_layout.addLayout(pwd_header)

        self._create_password = QLineEdit()
        self._create_password.setEchoMode(QLineEdit.EchoMode.Normal)
        self._create_password.setPlaceholderText("输入房间密码")
        self._create_password.setVisible(False)
        left_layout.addWidget(self._create_password)

        # Port
        left_layout.addWidget(QLabel("通信端口"))
        self._create_port = QSpinBox()
        self._create_port.setRange(1024, 65535)
        self._create_port.setValue(12345)
        left_layout.addWidget(self._create_port)

        # Max players
        left_layout.addWidget(QLabel("最大玩家数"))
        self._create_max_players = QSpinBox()
        self._create_max_players.setRange(2, 32)
        self._create_max_players.setValue(8)
        left_layout.addWidget(self._create_max_players)

        # Hint
        save_hint = QLabel(
            "同一局域网（相同路由器/WiFi/热点）下使用。\n"
            "创建的房间会自动保存，可在右侧列表中快速加入。")
        save_hint.setWordWrap(True)
        save_hint.setStyleSheet("color: #9498a4; font-size: 11px;")
        left_layout.addWidget(save_hint)

        left_layout.addStretch()

        create_btn = QPushButton("创建房间")
        create_btn.setObjectName("actionBtn")
        create_btn.clicked.connect(self._on_create)
        left_layout.addWidget(create_btn)

        root.addWidget(left, 2)

        # ── Right: Room lists ──
        right = QVBoxLayout()
        right.setSpacing(12)

        # Discovered rooms
        disc_label = QLabel("发现的房间")
        disc_label.setObjectName("sectionTitle")
        right.addWidget(disc_label)

        self._discovered_list = QListWidget()
        self._discovered_list.setAlternatingRowColors(True)
        self._discovered_list.setMinimumHeight(140)
        self._discovered_list.setStyleSheet(
            "QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #252536; }")
        self._discovered_list.itemSelectionChanged.connect(self._on_selection_changed)
        right.addWidget(self._discovered_list, 2)

        scan_row = QHBoxLayout()
        self._scan_btn = QPushButton("搜索房间")
        self._scan_btn.setObjectName("secondaryBtn")
        self._scan_btn.clicked.connect(self.scan_requested.emit)
        scan_row.addWidget(self._scan_btn)
        self._scan_status = QLabel("")
        self._scan_status.setStyleSheet("color: #9498a4; font-size: 11px;")
        scan_row.addWidget(self._scan_status)
        scan_row.addStretch()
        right.addLayout(scan_row)

        # Saved rooms
        saved_label = QLabel("已保存的房间")
        saved_label.setObjectName("sectionTitle")
        right.addWidget(saved_label)

        self._saved_list = QListWidget()
        self._saved_list.setAlternatingRowColors(True)
        self._saved_list.setMaximumHeight(140)
        self._saved_list.setStyleSheet(
            "QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #252536; }")
        self._saved_list.itemSelectionChanged.connect(self._on_selection_changed)
        right.addWidget(self._saved_list, 1)

        saved_btn_row = QHBoxLayout()
        self._join_btn = QPushButton("加入房间")
        self._join_btn.setObjectName("actionBtn")
        self._join_btn.setEnabled(False)
        self._join_btn.clicked.connect(self._on_join)
        saved_btn_row.addWidget(self._join_btn)

        self._delete_saved_btn = QPushButton("删除")
        self._delete_saved_btn.setObjectName("dangerBtn")
        self._delete_saved_btn.setEnabled(False)
        self._delete_saved_btn.clicked.connect(self._on_delete_saved)
        saved_btn_row.addWidget(self._delete_saved_btn)
        saved_btn_row.addStretch()
        right.addLayout(saved_btn_row)

        root.addLayout(right, 3)

    @staticmethod
    def _toggle_style(checked: bool) -> str:
        if checked:
            return (
                "QPushButton {"
                "  background-color: #89b4fa;"
                "  border: none;"
                "  border-radius: 11px;"
                "  color: #1e1e2e;"
                "  font-size: 10px;"
                "  font-weight: bold;"
                "}"
            )
        return (
            "QPushButton {"
            "  background-color: #585b70;"
            "  border: none;"
            "  border-radius: 11px;"
            "  color: #cdd6f4;"
            "  font-size: 10px;"
            "}"
        )

    def _on_pwd_toggled(self):
        checked = self._pwd_toggle.isChecked()
        self._pwd_toggle.setText("ON" if checked else "OFF")
        self._pwd_toggle.setStyleSheet(self._toggle_style(checked))
        if checked:
            self._create_password.setVisible(True)
            default_pwd = settings.get("room_default_password") or settings.get("default_password") or ""
            self._create_password.setText(default_pwd)
        else:
            self._create_password.setVisible(False)
            self._create_password.clear()

    def _on_create(self):
        name = self._create_name.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请输入房间名称")
            return

        password = self._create_password.text()
        port = self._create_port.value()
        max_players = self._create_max_players.value()

        # Check discovered rooms for name / port conflicts
        for i in range(self._discovered_list.count()):
            item = self._discovered_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole) if item else None
            if not data:
                continue
            if data.get("room_name", "").strip() == name:
                QMessageBox.warning(self, "提示",
                    f"局域网中已存在同名房间「{name}」。\n请更换房间名称后重试。")
                return
            if data.get("port", 0) == port:
                QMessageBox.warning(self, "提示",
                    f"端口 {port} 已被房间「{data.get('room_name', '?')}」占用。\n请更换端口后重试。")
                return

        room_id = uuid.uuid4().hex[:12]
        password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest() if password else ""

        room_info = {
            "room_id": room_id,
            "room_name": name,
            "password_hash": password_hash,
            "port": port,
            "max_players": max_players,
        }

        room_store.add(dict(room_info))
        self.create_room.emit(room_info)
        self._refresh_saved()

    def _on_join(self):
        selected = self._get_selected_room()
        if not selected:
            return
        if selected.get("from_saved"):
            self.join_room.emit(selected["data"])
        elif selected.get("from_discovered"):
            self.join_room.emit(selected["data"])

    def _on_delete_saved(self):
        item = self._saved_list.currentItem()
        if item:
            data = item.data(Qt.ItemDataRole.UserRole)
            if data:
                rid = data.get("room_id", "")
                self.delete_saved.emit(rid)

    def _on_selection_changed(self):
        sel = self._get_selected_room()
        has_sel = sel is not None
        self._join_btn.setEnabled(has_sel)
        is_saved = has_sel and self._saved_list.currentItem() is not None
        self._delete_saved_btn.setEnabled(is_saved)

    def _get_selected_room(self):
        for lst, key in [(self._saved_list, "from_saved"),
                         (self._discovered_list, "from_discovered")]:
            item = lst.currentItem()
            if item:
                data = item.data(Qt.ItemDataRole.UserRole)
                if data:
                    return {"data": data, key: True}
        return None

    def set_discovered_rooms(self, rooms: dict):
        self._discovered_list.clear()
        for rid, info in rooms.items():
            has_pw = info.get("has_password", False)
            lock = " [加密]" if has_pw else ""
            display = (f"{info.get('room_name', rid)}{lock}\n"
                       f"主机: {info.get('host_name', '?')}  |  "
                       f"IP: {info.get('host_ip', '?')}  |  "
                       f"玩家: {info.get('player_count', 0)}/{info.get('max_players', 8)}")
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, dict(info))
            self._discovered_list.addItem(item)

    def set_saved_rooms(self, rooms: list):
        self._saved_list.clear()
        for info in rooms:
            has_pw = bool(info.get("password_hash", ""))
            lock = " [加密]" if has_pw else ""
            display = (f"{info.get('room_name', '?')}{lock}\n"
                       f"端口: {info.get('port', 0)}  |  "
                       f"最大玩家: {info.get('max_players', 8)}")
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, dict(info))
            self._saved_list.addItem(item)

    def set_scan_status(self, text: str):
        self._scan_status.setText(text)

    def _refresh_saved(self):
        self.set_saved_rooms(room_store.get_all())


class RoomLobbyPage(QWidget):
    room_entered = pyqtSignal(dict)
    room_left = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_room = None
        self._host_worker = None
        self._scan_worker = None
        self._join_worker = None
        self._session_worker = None

        self._setup_ui()
        self._connect_signals()
        self._lobby._refresh_saved()

    def _username(self) -> str:
        """房间内统一使用设置里的昵称。

        原实现走的是 ``socket.gethostname()``，于是房间成员显示成
        ``xxxdeMacBook-Air.local``，而同一个应用的「局域网传输」页用的是
        ``settings.username`` —— 两套称呼、互相对不上。
        """
        return settings.username or socket.gethostname()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(0)

        title = QLabel("联机大厅")
        title.setObjectName("myCardTitle")
        root.addWidget(title)
        root.addSpacing(16)

        self._stack = QStackedWidget()

        self._lobby = LobbyView()
        self._room_view = RoomPage()

        self._stack.addWidget(self._lobby)
        self._stack.addWidget(self._room_view)
        self._stack.setCurrentIndex(0)

        root.addWidget(self._stack, 1)

    def _connect_signals(self):
        self._lobby.create_room.connect(self._on_create_room)
        self._lobby.join_room.connect(self._on_join_room)
        self._lobby.scan_requested.connect(self._on_scan)
        self._lobby.delete_saved.connect(self._on_delete_saved)
        self._room_view.leave_requested.connect(self._on_leave_room)

    # ── Create room ──

    def _on_create_room(self, room_info: dict):
        room_info = dict(room_info)
        room_info.setdefault("peer_id", "host")
        room_info.setdefault("username", self._username())
        # 显式下发文件传输端口，避免两端各自推导后因版本差异对不上
        room_info.setdefault(
            "data_port", room_data_port_for_room(room_info.get("port", 0)))
        self._current_room = room_info

        # Stop any stale session (e.g. re-hosting a saved room)
        self._stop_session()

        # Start broadcasting room on LAN
        self._host_worker = RoomHostWorker(room_info, self._username())
        self._host_worker.error.connect(
            lambda e: self._lobby.set_scan_status(f"房间广播错误: {e}"))
        # 房主自己也要看到谁进来了
        self._host_worker.roster_updated.connect(self._on_roster)
        self._host_worker.start()

        # Configure and show room page
        self._room_view.set_room_context(room_info)
        self._stack.setCurrentIndex(1)
        self.room_entered.emit(room_info)

    # ── Join room ──

    def _on_join_room(self, room_info: dict):
        if self._join_worker and self._join_worker.isRunning():
            return
        room_id = room_info.get("room_id", "")
        has_password = bool(room_info.get("password_hash", "")) or room_info.get("has_password", False)

        # Check if this saved room is also currently discovered (live host)
        host_ip = room_info.get("host_ip", "")
        port = room_info.get("port", 0)
        if not host_ip:
            discovered = self._lobby._discovered_list
            for i in range(discovered.count()):
                item = discovered.item(i)
                data = item.data(Qt.ItemDataRole.UserRole) if item else None
                if data and data.get("room_id") == room_id:
                    host_ip = data.get("host_ip", "")
                    port = data.get("port", 0)
                    if data.get("has_password", False):
                        has_password = True
                    break

        if host_ip and port:
            # Live host found — TCP join with remote password verification
            password = ""
            if has_password:
                password, ok = QInputDialog.getText(
                    self, "需要密码", f"房间「{room_info.get('room_name', '')}」需要密码:",
                    QLineEdit.EchoMode.Password)
                if not ok:
                    return

            self._lobby.set_scan_status("正在加入...")
            self._start_join_worker(host_ip, port, room_id, password)
            return

        # No live host — re-host this saved room locally
        if has_password:
            password, ok = QInputDialog.getText(
                self, "需要密码",
                f"未发现运行中的房间「{room_info.get('room_name', '')}」。\n"
                f"将重新主持此房间，请输入房间密码:",
                QLineEdit.EchoMode.Password)
            if not ok:
                return
            saved_hash = room_info.get("password_hash", "")
            if hashlib.sha256(password.encode("utf-8")).hexdigest() != saved_hash:
                QMessageBox.warning(self, "密码错误", "房间密码不正确，无法重新主持")
                return
        else:
            reply = QMessageBox.question(
                self, "重新主持",
                f"未发现运行中的房间「{room_info.get('room_name', '')}」。\n"
                f"是否重新主持此房间？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Re-host
        if self._host_worker and self._host_worker.isRunning():
            self._host_worker.cancel()
            self._host_worker.wait(3000)
        self._on_create_room(room_info)

    def _start_join_worker(self, host_ip: str, port: int, room_id: str, password: str):
        if self._join_worker and self._join_worker.isRunning():
            try:
                self._join_worker.finished.disconnect(self._on_join_result)
            except TypeError:
                pass
            try:
                self._join_worker.error.disconnect()
            except TypeError:
                pass
        self._join_worker = RoomJoinWorker(
            host_ip, port, room_id, password, username=self._username())
        self._join_worker.finished.connect(self._on_join_result)
        self._join_worker.error.connect(
            lambda e: self._lobby.set_scan_status(f"加入失败: {e}"))
        self._join_worker.start()

    def _on_join_result(self, success: bool, message: str, config: dict):
        self._lobby.set_scan_status(message)
        if success:
            if not config or not config.get("room_id"):
                QMessageBox.warning(self, "加入失败", "房间主机返回了无效的配置")
                return
            if not room_store.exists(config.get("room_id", "")):
                room_store.add(dict(config))
                self._lobby._refresh_saved()
            self._enter_room(config)
        else:
            QMessageBox.warning(self, "加入失败", message)

    def _enter_room(self, room_info: dict):
        self._current_room = room_info
        self._room_view.set_room_context(room_info)
        self._stack.setCurrentIndex(1)
        self._start_session(room_info)
        self.room_entered.emit(room_info)

    # ── Room session (heartbeat + roster) ──

    def _start_session(self, room_config: dict):
        """建立房间会话：心跳保活并接收房主下发的成员名单。

        没有这一步时客户端从不发心跳，房主 10 秒后就把成员全部剔除，
        而且客户端也拿不到任何成员信息（语音和文件传输都没有目标）。
        """
        self._stop_session()
        self._session_worker = RoomSessionWorker(room_config, self._username())
        self._session_worker.roster_updated.connect(self._on_roster)
        self._session_worker.session_lost.connect(self._on_session_lost)
        self._session_worker.error.connect(
            lambda e: self._lobby.set_scan_status(f"房间会话错误: {e}"))
        self._session_worker.start()

    def _stop_session(self):
        worker = self._session_worker
        self._session_worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.cancel()
                worker.wait(3000)
        except Exception:
            pass

    def _on_roster(self, roster: list):
        self._room_view.update_peers(roster)

    def _on_session_lost(self):
        """会话失效：明确告知用户并退回大厅，而不是留在一个死房间里。"""
        self._room_view.on_session_lost()
        QMessageBox.warning(
            self, "会话已失效",
            "与房间主机的连接已中断（房主可能已重启）。\n"
            "请返回大厅后重新加入房间。")
        self._leave_room_ui()

    def _leave_room_ui(self):
        self._stop_session()
        self._room_view.shutdown()
        if self._host_worker and self._host_worker.isRunning():
            self._host_worker.cancel()
            self._host_worker.wait(3000)
        if self._join_worker and self._join_worker.isRunning():
            self._join_worker.cancel()
            self._join_worker.wait(3000)
        self._current_room = None
        self._stack.setCurrentIndex(0)
        self._lobby._refresh_saved()

    # ── Scan rooms ──

    def _on_scan(self):
        self._lobby.set_scan_status("正在扫描...")
        if self._scan_worker and self._scan_worker.isRunning():
            try:
                self._scan_worker.finished.disconnect(self._on_scan_result)
            except TypeError:
                pass
        self._scan_worker = RoomScanWorker(self._username())
        self._scan_worker.finished.connect(self._on_scan_result)
        self._scan_worker.start()

    def _on_scan_result(self, rooms: dict):
        self._lobby.set_discovered_rooms(rooms)
        count = len(rooms)
        if count:
            self._lobby.set_scan_status(f"发现 {count} 个房间")
        else:
            self._lobby.set_scan_status("未发现房间 — 请确认已有房间正在广播中")

    # ── Leave room ──

    def _on_leave_room(self):
        self._leave_room_ui()
        self.room_left.emit()

    # ── Delete saved room ──

    def _on_delete_saved(self, room_id: str):
        room_store.remove(room_id)
        self._lobby._refresh_saved()

    # ── External API ──

    @property
    def room_page(self):
        return self._room_view

    def cleanup(self):
        self._stop_session()
        self._room_view.shutdown()
        if self._current_room:
            self._on_leave_room()
