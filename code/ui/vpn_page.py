"""
局域网联机页面 — 集成 Rust lan_vpn 核心，支持局域网直连和信令服务器两种模式
"""
import json
import os
import re
import subprocess
import sys
from ui.styles import MONO_FONT_FAMILY
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QSpinBox, QListWidget, QListWidgetItem, QTextEdit,
    QGroupBox, QFormLayout, QStackedWidget, QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QTextCursor

from utils.constants import voice_port_for_room
from utils.settings import settings, SETTINGS_DIR
from utils.paths import project_root
from utils.helpers import format_size
from core.workers import VoiceWorker


def _is_admin() -> bool:
    """Check if the current process has administrator privileges (Windows only)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


AUTO_JOIN_FILE = os.path.join(SETTINGS_DIR, "auto_join.json")


def _request_admin_relaunch(room_context: dict = None) -> bool:
    """Re-launch the app with admin privileges via UAC. Saves room context
    so the elevated instance can auto-restore the room. Returns True on
    success (caller should quit), False if the user denied or an error occurred."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        # Save room context for the elevated process to restore
        if room_context:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(AUTO_JOIN_FILE, "w", encoding="utf-8") as f:
                json.dump(room_context, f, ensure_ascii=False)
        # Choose executable: pythonw.exe avoids a persistent console window.
        # pythonw.exe is the GUI subsystem build of Python — same interpreter,
        # same libraries, no console attached. Fall back to python.exe if missing.
        if getattr(sys, "frozen", False):
            exe = sys.executable
        else:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            exe = pythonw if os.path.exists(pythonw) else sys.executable

        # Build relaunch args. lpFile already specifies the executable —
        # args must NOT include sys.executable, only the script + flags.
        if getattr(sys, "frozen", False):
            args = list(sys.argv[1:])
        else:
            args = [os.path.abspath(sys.argv[0])]
            args.extend(sys.argv[1:])
        args.append("--auto-join")
        cmd = subprocess.list2cmdline(args)
        # Use the project root as working directory so wintun.dll can be
        # found by the Rust core regardless of UAC's lpDirectory behavior.
        cwd = project_root()
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, cmd, cwd, 1)
        return ret > 32
    except Exception:
        return False

# --- Conditional import of Rust module ---
_VPN_AVAILABLE = False
_VpnCore = None
_PeerInfo = None
try:
    import lan_vpn
    _VpnCore = lan_vpn.VpnCore
    _PeerInfo = lan_vpn.PeerInfo
    _VPN_AVAILABLE = True
except ImportError:
    pass

# --- Conditional check for voice support ---
_VOICE_AVAILABLE = False
try:
    from network.voice_protocol import is_voice_available
    _VOICE_AVAILABLE = is_voice_available()
except Exception:
    pass

MODE_LAN = 0
MODE_SERVER = 1

_IP_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_SUBNET_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d{1,2})$")


def _validate_ip(ip: str) -> bool:
    m = _IP_RE.match(ip)
    if not m:
        return False
    return all(0 <= int(g) <= 255 for g in m.groups())


def _validate_subnet(subnet: str) -> bool:
    m = _SUBNET_RE.match(subnet)
    if not m:
        return False
    octets_ok = all(0 <= int(g) <= 255 for g in m.groups()[:4])
    prefix = int(m.group(5))
    return octets_ok and 8 <= prefix <= 30


def _validate_ws_url(url: str) -> bool:
    return url.startswith("ws://") or url.startswith("wss://")



def _console_encoding() -> str:
    """子进程控制台输出的解码编码（跨平台）。

    项目规范禁止硬编码 ``encoding="gbk"``：中文 Windows 的 OEM 代码页通常是
    cp936（等价于 gbk），但非中文 Windows 是 cp437/cp850，Linux/macOS 是
    UTF-8。硬编码 gbk 在非中文系统上会把正常输出解成乱码。
    """
    import locale
    try:
        enc = locale.getpreferredencoding(False) or ""
    except Exception:
        enc = ""
    if not enc or enc.lower() in ("ascii", "us-ascii", "ansi_x3.4-1968"):
        if sys.platform == "win32":
            try:
                import ctypes
                cp = ctypes.windll.kernel32.GetOEMCP()
                if cp:
                    return f"cp{cp}"
            except Exception:
                pass
        return "utf-8"
    return enc


class VpnPage(QWidget):
    """Page for LAN multiplayer VPN tool."""

    leave_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = MODE_LAN
        self._running = False
        self._transitioning = False
        self._core = None
        self._room_context = None
        self._voice_worker = None
        self._voice_on = False
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.setInterval(200)  # 5 Hz — enough for UI updates

        self._setup_ui()

        if _VPN_AVAILABLE:
            self._init_core()
        else:
            self._show_unavailable()

    # ── UI setup ──────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Mode toggle row
        top = QHBoxLayout()

        mode_widget = QWidget()
        mode_widget.setObjectName("modeToggle")
        mode_layout = QHBoxLayout(mode_widget)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(0)

        self._btn_lan = QPushButton(" 局域网直连")
        self._btn_lan.setCheckable(True)
        self._btn_lan.setChecked(True)
        self._btn_lan.clicked.connect(lambda: self._set_mode(MODE_LAN))

        self._btn_server = QPushButton(" 信令服务器")
        self._btn_server.setCheckable(True)
        self._btn_server.clicked.connect(lambda: self._set_mode(MODE_SERVER))

        mode_layout.addWidget(self._btn_lan)
        mode_layout.addWidget(self._btn_server)
        top.addWidget(mode_widget)
        top.addStretch()

        self._room_name_label = QLabel("")
        self._room_name_label.setStyleSheet("color: #c8ccd4; font-weight: bold; font-size: 13px;")
        self._room_name_label.setVisible(False)
        top.addWidget(self._room_name_label)

        self._status_label = QLabel("未启动")
        self._status_label.setStyleSheet("color: #f38ba8; font-weight: bold;")
        top.addWidget(self._status_label)

        root.addLayout(top)

        # Config stack — switches between LAN and Server config
        self._config_stack = QStackedWidget()

        self._lan_cfg = self._make_lan_config()
        self._server_cfg = self._make_server_config()

        self._config_stack.addWidget(self._lan_cfg)
        self._config_stack.addWidget(self._server_cfg)
        root.addWidget(self._config_stack)

        # Peer list + log side by side
        mid = QHBoxLayout()

        self._peer_list = QListWidget()
        self._peer_list.setAlternatingRowColors(True)
        self._peer_list.setMinimumWidth(200)
        mid.addWidget(self._peer_list, 2)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(120)
        self._log_view.setStyleSheet(
            f"font-size: 11px; font-family: {MONO_FONT_FAMILY};"
        )
        mid.addWidget(self._log_view, 3)

        root.addLayout(mid, 1)

        # Traffic info
        self._traffic_label = QLabel("TX: 0 B  |  RX: 0 B")
        self._traffic_label.setStyleSheet("color: #9498a4; font-size: 11px;")
        root.addWidget(self._traffic_label)

        # Start / Stop buttons
        ctrl = QHBoxLayout()
        ctrl.addStretch()

        self._start_btn = QPushButton("启动联机")
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #a6e3a1; color: #1e1e2e;"
            "  font-weight: bold; font-size: 13px; padding: 9px 36px;"
            "  border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #94e2d5; }"
        )
        self._start_btn.clicked.connect(self._start)

        self._stop_btn = QPushButton("停止")
        self._stop_btn.setObjectName("dangerBtn")
        self._stop_btn.clicked.connect(self._stop)
        self._stop_btn.setEnabled(False)

        self._leave_btn = QPushButton("离开房间")
        self._leave_btn.setObjectName("secondaryBtn")
        self._leave_btn.clicked.connect(self._request_leave)
        self._leave_btn.setVisible(False)

        self._voice_btn = QPushButton("语音通话")
        self._voice_btn.setCheckable(True)
        self._voice_btn.clicked.connect(self._toggle_voice)
        self._voice_btn.setVisible(False)
        if not _VOICE_AVAILABLE:
            self._voice_btn.setToolTip("需要安装 PyAudio: pip install pyaudio")
            self._voice_btn.setEnabled(False)
            self._voice_btn.setStyleSheet(
                "QPushButton { background-color: #45475a; color: #9498a4;"
                "  font-size: 13px; padding: 9px 18px;"
                "  border: none; border-radius: 6px; }"
            )

        ctrl.addWidget(self._start_btn)
        ctrl.addWidget(self._stop_btn)
        ctrl.addWidget(self._leave_btn)
        ctrl.addWidget(self._voice_btn)
        ctrl.addStretch()
        root.addLayout(ctrl)

    def _make_lan_config(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        grp = QGroupBox("局域网直连设置")
        form = QFormLayout(grp)

        self._lan_ip = QLineEdit("10.99.1.10")
        self._lan_ip.setPlaceholderText("10.99.x.x")
        self._lan_ip.setToolTip("每个人需要不同的虚拟 IP，在同一子网 10.99.0.0/16 内")
        form.addRow("虚拟 IP 地址:", self._lan_ip)

        self._lan_port = QSpinBox()
        self._lan_port.setRange(1024, 65535)
        self._lan_port.setValue(12345)
        self._lan_port.setToolTip("所有玩家需使用相同端口")
        form.addRow("通信端口:", self._lan_port)

        layout.addWidget(grp)

        help_lbl = QLabel(
            "同一局域网（相同路由器/WiFi）下使用。\n"
            "每人设置不同虚拟 IP（如 10.99.1.10、10.99.1.20），相同端口。\n"
            "启动后自动发现好友，然后在游戏中选局域网模式即可。"
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet("color: #9498a4; font-size: 11px; padding: 4px 0;")
        layout.addWidget(help_lbl)

        return w

    def _make_server_config(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        grp = QGroupBox("信令服务器设置")
        form = QFormLayout(grp)

        self._srv_ip = QLineEdit("10.99.1.10")
        self._srv_ip.setPlaceholderText("10.99.x.x")
        form.addRow("虚拟 IP 地址:", self._srv_ip)

        self._srv_subnet = QLineEdit("10.99.0.0/16")
        form.addRow("虚拟子网:", self._srv_subnet)

        self._srv_url = QLineEdit("ws://127.0.0.1:9000")
        self._srv_url.setPlaceholderText("ws://服务器地址:端口")
        form.addRow("信令服务器:", self._srv_url)

        self._srv_room = QLineEdit("hoi4_room")
        self._srv_room.setPlaceholderText("房间名（好友需一致）")
        form.addRow("房间名:", self._srv_room)

        self._srv_port = QSpinBox()
        self._srv_port.setRange(1024, 65535)
        self._srv_port.setValue(12345)
        form.addRow("通信端口:", self._srv_port)

        layout.addWidget(grp)

        help_lbl = QLabel(
            "需要先部署信令服务器（signal_server.py），所有玩家需能连接该服务器。\n"
            "每人不同虚拟 IP、相同房间名和端口。服务器仅协调连接，游戏流量走 P2P。"
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet("color: #9498a4; font-size: 11px; padding: 4px 0;")
        layout.addWidget(help_lbl)

        return w

    def _show_unavailable(self):
        """Show info when VPN module is not available."""
        for i in range(self._config_stack.count()):
            self._config_stack.widget(i).setEnabled(False)
        self._start_btn.setEnabled(False)
        self._log("")
        if sys.platform != "win32":
            self._log("[!] VPN 隧道联机功能仅在 Windows 上可用")
            self._log("")
            self._log("在 macOS/Linux 上，请使用局域网直连模式（房间页面）。")
        else:
            self._log("[!] VPN 核心模块 (lan_vpn.pyd) 未加载")
            self._log("")
            self._log("此功能需要 VPN 网络适配器支持，")
            self._log("当前版本未包含 VPN 核心组件。")
            self._log("")
            self._log("如需使用联机功能，请联系开发者获取完整版本。")

    # ── Mode switching ────────────────────────────────────────

    def _set_mode(self, mode):
        if self._running:
            return
        self._mode = mode
        self._btn_lan.setChecked(mode == MODE_LAN)
        self._btn_server.setChecked(mode == MODE_SERVER)
        self._config_stack.setCurrentIndex(mode)

    # ── Core lifecycle ────────────────────────────────────────

    def _init_core(self):
        try:
            self._core = _VpnCore()
            pubkey = self._core.get_public_key_hex()
            peer_id = self._core.get_peer_id()
            self._log(f"Rust 核心就绪 — 公钥: {pubkey[:16]}...  PeerID: {peer_id[:16] if peer_id else '(空)'}...")
        except Exception as e:
            self._log(f"[错误] 初始化 Rust 核心失败: {e}")
            self._core = None

    def _start(self):
        if self._core is None:
            QMessageBox.critical(self, "错误", "Rust 核心未加载，无法启动")
            return
        if self._running or self._transitioning:
            return

        # ── pre-start diagnostics ──
        is_admin = _is_admin()
        self._log(f"[诊断] 管理员权限: {'是' if is_admin else '否'}")
        self._log(f"[诊断] 工作目录: {os.getcwd()}")

        # Check wintun.dll presence (PyInstaller: sys._MEIPASS is the unpack dir)
        search_dirs = [os.getcwd()]
        search_dirs.append(os.path.dirname(sys.executable))
        if getattr(sys, "frozen", False):
            search_dirs.append(sys._MEIPASS)
        # 源码运行时 wintun.dll 位于项目根目录
        search_dirs.append(project_root())
        for search_dir in search_dirs:
            candidate = os.path.join(search_dir, "wintun.dll")
            if os.path.exists(candidate):
                self._log(f"[诊断] wintun.dll 位置: {candidate}")
                break
        else:
            self._log("[诊断] ⚠️ 未找到 wintun.dll！")

        # Check for existing wintun adapters
        if sys.platform == "win32":
            try:
                import subprocess
                result = subprocess.run(
                    ['netsh', 'interface', 'show', 'interface'],
                    capture_output=True, text=True, timeout=5,
                    encoding=_console_encoding(), errors="replace",
                )
                for line in result.stdout.splitlines():
                    if 'wintun' in line.lower() or 'TUN' in line:
                        self._log(f"[诊断] 现有网卡: {line.strip()}")
            except Exception:
                pass

        if not is_admin:
            self._log("[诊断] 需要管理员权限，正在请求提权...")
            if _request_admin_relaunch(self._room_context):
                QApplication.instance().quit()
                return
            self._log("[诊断] 用户取消了 UAC 提权，VPN 需要管理员权限才能运行")
            return

        try:
            if self._mode == MODE_LAN:
                ip = self._lan_ip.text().strip()
                port = self._lan_port.value()
                if not ip:
                    QMessageBox.warning(self, "提示", "请输入虚拟 IP 地址")
                    return
                if not _validate_ip(ip):
                    QMessageBox.warning(self, "提示", f"无效的 IP 地址: {ip}")
                    return
                self._log(f"[诊断] 调用 start_lan(ip={ip}, port={port})")
                self._transitioning = True
                self._start_btn.setEnabled(False)
                self._stop_btn.setEnabled(False)
                self._start_btn.setText("启动中…")
                self._core.start_lan(ip, port)
                self._log(f"LAN 模式启动 — IP: {ip}, 端口: {port}")
            else:
                ip = self._srv_ip.text().strip()
                subnet = self._srv_subnet.text().strip()
                url = self._srv_url.text().strip()
                room = self._srv_room.text().strip()
                port = self._srv_port.value()
                if not ip or not url:
                    QMessageBox.warning(self, "提示", "请填写完整的连接配置")
                    return
                if not _validate_ip(ip):
                    QMessageBox.warning(self, "提示", f"无效的 IP 地址: {ip}")
                    return
                if not _validate_subnet(subnet):
                    QMessageBox.warning(self, "提示", f"无效的子网格式: {subnet}\n需要 CIDR 格式，如 10.99.0.0/16")
                    return
                if not _validate_ws_url(url):
                    QMessageBox.warning(self, "提示", f"无效的信令服务器地址: {url}\n需要 ws:// 或 wss:// 开头")
                    return
                self._log(f"[诊断] 调用 start_with_server(ip={ip}, subnet={subnet}, url={url}, room={room}, port={port})")
                self._transitioning = True
                self._start_btn.setEnabled(False)
                self._stop_btn.setEnabled(False)
                self._start_btn.setText("启动中…")
                self._core.start_with_server(ip, subnet, url, room, port)
                self._log(f"服务器模式启动 — 房间: {room}, IP: {ip}")

        except Exception as e:
            err_msg = str(e)
            self._log(f"[诊断] start_xxx 抛出异常: {err_msg}")
            if "already running" in err_msg.lower():
                self._log(f"[警告] 核心未完全停止，正在强制重置...")
                try:
                    self._core.stop()
                except Exception:
                    pass
                self._init_core()
                QMessageBox.warning(self, "提示",
                    "检测到上次会话未完全关闭，已自动重置。\n请再次点击「启动联机」。")
            else:
                QMessageBox.critical(self, "启动失败", err_msg)
                self._log(f"[错误] 启动失败: {err_msg}")
            self._transitioning = False
            self._start_btn.setEnabled(True)
            self._start_btn.setText("启动联机")
            self._start_btn.setStyleSheet(
                "QPushButton { background-color: #a6e3a1; color: #1e1e2e;"
                "  font-weight: bold; font-size: 13px; padding: 9px 36px;"
                "  border: none; border-radius: 6px; }"
                "QPushButton:hover { background-color: #94e2d5; }"
            )
            return

        # Verify the core actually started
        try:
            if not self._core.is_running():
                self._log("[诊断] ⚠️ start_xxx 返回成功但 is_running()=False！")
        except Exception as e:
            self._log(f"[诊断] is_running() 检查失败: {e}")

        self._running = True
        self._transitioning = False
        self._btn_lan.setEnabled(False)
        self._btn_server.setEnabled(False)
        self._poll_timer.start()
        self._start_btn.setText("联机进行中")
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #45475a; color: #cdd6f4;"
            "  font-weight: bold; font-size: 13px; padding: 9px 36px;"
            "  border: none; border-radius: 6px; }"
        )
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_label.setText("运行中")
        self._status_label.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        self._log("等待好友加入...")

    def _stop(self):
        if self._transitioning:
            return
        if not self._running:
            return

        self._transitioning = True
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText("停止中…")

        if self._voice_on:
            self._stop_voice()
        if self._core:
            try:
                self._log("[诊断] 调用 core.stop()...")
                self._core.stop()
                self._log("[诊断] stop() 返回")
            except Exception as e:
                self._log(f"[错误] 停止核心失败: {e}")

        # Always recreate the core after stopping. Without this, the old
        # VpnCore instance is reused and drain_events() will emit residual
        # "VPN core stopped" / TUN-teardown messages from the prior session.
        self._init_core()

        self._running = False
        self._poll_timer.stop()
        self._btn_lan.setEnabled(True)
        self._btn_server.setEnabled(True)
        self._stop_btn.setText("停止")
        self._status_label.setText("已停止")
        self._status_label.setStyleSheet("color: #f38ba8; font-weight: bold;")
        self._log("已停止")

        # Cooldown: keep start button locked for 1.5s so Windows can
        # finish tearing down the TUN adapter asynchronously. Without
        # this, a rapid stop→start cycle creates a new adapter while
        # the old one is still being destroyed — corrupting Rust state.
        self._start_btn.setText("冷却中…")
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #45475a; color: #6c7086;"
            "  font-weight: bold; font-size: 13px; padding: 9px 36px;"
            "  border: none; border-radius: 6px; }"
        )
        QTimer.singleShot(1500, self._on_stop_cooldown_done)

    def _on_stop_cooldown_done(self):
        """Re-enable start button after the TUN-adapter teardown cooldown."""
        self._transitioning = False
        self._start_btn.setText("启动联机")
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #a6e3a1; color: #1e1e2e;"
            "  font-weight: bold; font-size: 13px; padding: 9px 36px;"
            "  border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #94e2d5; }"
        )
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    # ── Polling ───────────────────────────────────────────────

    # TUN-related error patterns that indicate a fatal startup failure
    _FATAL_TUN_ERRORS = ["Failed to create TUN device", "Failed to create second TUN device"]

    def _poll(self):
        if self._core is None:
            return

        try:
            events = self._core.drain_events()
        except Exception as e:
            self._log(f"[诊断] drain_events() 异常: {e}")
            return

        for evt in events:
            etype = evt.get("type", "")
            if etype == "log":
                self._log(str(evt.get("message", "")))
            elif etype == "error":
                msg = str(evt.get("message", ""))
                # Log the FULL event dict for diagnostic purposes
                self._log(f"[错误] {msg}")
                self._log(f"[诊断] 完整错误事件: {evt}")
                # Auto-stop on fatal TUN errors to prevent broken state
                if any(pattern in msg for pattern in self._FATAL_TUN_ERRORS):
                    self._log("[诊断] 检测到致命 TUN 错误，自动停止...")
                    self._stop()
                    QMessageBox.warning(
                        self, "TUN 设备创建失败",
                        f"虚拟网卡创建失败，已自动停止。\n\n错误详情:\n{msg}\n\n"
                        "可能原因：\n"
                        "1. wintun.dll 版本不兼容（当前 0.14.1）\n"
                        "2. 系统中存在残留的 wintun 虚拟网卡\n"
                        "3. Radmin VPN / TAP 适配器冲突\n\n"
                        "建议：\n"
                        "1. 在设备管理器中检查并删除残留的 'Wintun' 网卡\n"
                        "2. 尝试升级 wintun.dll: https://www.wintun.net/\n"
                        "3. 临时禁用 Radmin VPN 后重试"
                    )
                    return
            elif etype == "peer_joined":
                pid = str(evt.get("peer_id", ""))
                vip = str(evt.get("virtual_ip", ""))
                self._update_peer_item(pid, vip, True)
                self._log(f"好友上线: {pid} @ {vip}")
            elif etype == "peer_left":
                pid = str(evt.get("peer_id", ""))
                self._remove_peer_item(pid)
                self._log(f"好友下线: {pid}")
            elif etype == "p2p_success":
                self._log(f"P2P 连接成功: {evt.get('peer_id', '')} — 延迟 {evt.get('latency_ms', 0)}ms")
            else:
                # Log any unknown event types for diagnostics
                self._log(f"[诊断] 未知事件类型: {evt}")

        # Traffic stats
        try:
            tx, rx = self._core.traffic_stats()
            self._traffic_label.setText(
                f"TX: {format_size(tx)}  |  RX: {format_size(rx)}"
            )
        except Exception:
            pass

        # Refresh peer list from routing table
        try:
            peers = self._core.peers()
            for p in peers:
                self._update_peer_item(p.peer_id, p.virtual_ip, p.connected)
        except Exception:
            pass

    # ── Peer list helpers ─────────────────────────────────────

    def _update_peer_item(self, peer_id, virtual_ip, connected):
        status = "● 在线" if connected else "○ 离线"
        for i in range(self._peer_list.count()):
            item = self._peer_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == peer_id:
                item.setText(f"{status}  {virtual_ip}")
                return

        item = QListWidgetItem(f"{status}  {virtual_ip}")
        item.setData(Qt.ItemDataRole.UserRole, peer_id)
        font = QFont()
        font.setPointSize(11)
        item.setFont(font)
        self._peer_list.addItem(item)

    def _remove_peer_item(self, peer_id):
        for i in range(self._peer_list.count()):
            item = self._peer_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == peer_id:
                self._peer_list.takeItem(i)
                break

    def _log(self, msg):
        self._log_view.append(msg)
        doc = self._log_view.document()
        while doc.blockCount() > 300:
            block = doc.findBlockByNumber(0)
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    # ── Room context ──────────────────────────────────────────

    def set_room_context(self, room_info: dict):
        """Pre-fill VPN config from room data and show room UI elements."""
        self._room_context = room_info
        mode_str = room_info.get("mode", "lan")
        self._set_mode(MODE_SERVER if mode_str == "server" else MODE_LAN)

        self._lan_ip.setText(room_info.get("virtual_ip", "10.99.1.10"))
        self._lan_port.setValue(int(room_info.get("port", 12345)))

        if mode_str == "server":
            self._srv_ip.setText(room_info.get("virtual_ip", "10.99.1.10"))
            self._srv_subnet.setText(room_info.get("subnet", "10.99.0.0/16"))
            self._srv_url.setText(room_info.get("server_url", "ws://127.0.0.1:9000"))
            self._srv_room.setText(room_info.get("room_name", ""))
            self._srv_port.setValue(int(room_info.get("port", 12345)))

        room_name = room_info.get("room_name", "")
        if room_name:
            self._room_name_label.setText(f"房间: {room_name}")
            self._room_name_label.setVisible(True)

        self._leave_btn.setVisible(True)
        self._voice_btn.setVisible(True)

        self._log(f"[房间] 已配置房间: {room_name or '(未命名)'}")

    def _request_leave(self):
        self.shutdown()
        self._room_context = None
        self._room_name_label.setVisible(False)
        self._leave_btn.setVisible(False)
        self._voice_btn.setVisible(False)
        self._voice_btn.setChecked(False)
        self._btn_lan.setEnabled(True)
        self._btn_server.setEnabled(True)
        self._start_btn.setText("启动联机")
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #a6e3a1; color: #1e1e2e;"
            "  font-weight: bold; font-size: 13px; padding: 9px 36px;"
            "  border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #94e2d5; }"
        )
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText("未启动")
        self._status_label.setStyleSheet("color: #f38ba8; font-weight: bold;")
        self._log("[房间] 已离开房间")
        self.leave_requested.emit()

    # ── Voice call ───────────────────────────────────────────

    def _toggle_voice(self):
        if self._voice_on:
            self._stop_voice()
        else:
            self._start_voice()

    def _start_voice(self):
        if not _VOICE_AVAILABLE:
            QMessageBox.warning(self, "提示", "语音通话需要 PyAudio 库。\n请运行: pip install pyaudio")
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
            "QPushButton:hover { background-color: #94e2d5; }"
        )
        self._log("[语音] 语音通话已开启")

    def _on_voice_finished(self, ok: bool):
        if not ok and self._voice_on:
            self._voice_on = False
            self._voice_btn.setText("语音通话")
            self._voice_btn.setStyleSheet("")
            self._voice_btn.setObjectName("secondaryBtn")
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
        self._voice_btn.setObjectName("secondaryBtn")
        self._voice_btn.setChecked(False)
        self._log("[语音] 语音通话已关闭")

    # ── External API (called by MainWindow) ────────────────────

    def set_busy(self, busy):
        """Disable config changes while running."""
        if not busy:
            return
        self._lan_cfg.setEnabled(not busy)
        self._server_cfg.setEnabled(not busy)

    def shutdown(self):
        """Stop VPN core + voice without UI side-effects. For cleanup/leave."""
        if self._voice_on:
            self._stop_voice()
        if self._running:
            self._stop_core()

    def _stop_core(self):
        """Stop the Rust VPN core only, without touching UI state."""
        if self._core:
            try:
                self._core.stop()
            except Exception:
                pass
            try:
                self._init_core()
            except Exception:
                self._core = None
        self._running = False
        if self._poll_timer.isActive():
            self._poll_timer.stop()

    def cleanup(self):
        """Called on app shutdown."""
        self.shutdown()
