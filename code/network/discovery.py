"""Singleton DiscoveryService for LAN device and room discovery.

Uses per-interface directed subnet broadcasts (not 255.255.255.255) for
stability on multi-NIC machines and mobile hotspots.  The UI observes
results exclusively through pyqtSignal connections — it never touches
the socket or threads directly.
"""

import json
import socket
import threading
import time
import uuid

from utils.constants import DISCOVERY_PORT, MSG_DISCOVERY, MSG_HERE
from utils.constants import ROOM_DISCOVERY_PORT, MSG_ROOM_ANNOUNCE, MSG_ROOM_DISCOVERY, MSG_ROOM_INFO

_DEVICE_TIMEOUT = 15   # seconds before a device is pruned
_BROADCAST_INTERVAL = 2.5  # seconds between broadcasts
_PRUNE_INTERVAL = 5.0      # seconds between timeout scans


class DiscoveryService:
    """Thread-safe singleton that discovers LAN devices and rooms.

    Uses per-interface directed broadcasts for reliability.  A single
    UDP socket handles both sending and receiving.  Device cache is
    protected by a lock and cleaned up automatically.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_init_done"):
            return
        self._init_done = True

        self._running = False
        self._username = socket.gethostname()
        self._hostname = socket.gethostname()
        self._device_id = uuid.uuid4().hex[:12]

        # 本机实际监听的 TCP 端口：回包时如实上报，否则对方会连到 12345
        from utils.constants import DEFAULT_TCP_PORT
        self._listen_port = DEFAULT_TCP_PORT

        # device_id → {username, hostname, ip, port, last_seen}
        self._devices: dict[str, dict] = {}
        self._devices_lock = threading.Lock()

        # room_id → room_info
        self._rooms: dict[str, dict] = {}
        self._rooms_lock = threading.Lock()

        self._sock: socket.socket | None = None
        self._broadcast_thread: threading.Thread | None = None
        self._listen_thread: threading.Thread | None = None
        self._prune_thread: threading.Thread | None = None

        # PyQt signal callbacks — callable(command, data)
        self._callbacks: list[callable] = []
        self._callbacks_lock = threading.Lock()

    # ── public API ──────────────────────────────────────────

    def set_username(self, name: str):
        self._username = name or self._hostname

    def set_listen_port(self, port: int):
        """设置本机对外公布的接收端口（回 MSG_HERE 时使用）。"""
        try:
            port = int(port)
        except (TypeError, ValueError):
            return
        if 1 <= port <= 65535:
            self._listen_port = port

    @property
    def username(self) -> str:
        return self._username

    @property
    def device_id(self) -> str:
        return self._device_id

    def get_devices(self) -> dict:
        with self._devices_lock:
            return dict(self._devices)

    def get_rooms(self) -> dict:
        with self._rooms_lock:
            return dict(self._rooms)

    def register_callback(self, cb: callable):
        """Register a signal-emitting callback: cb(event_type, data)."""
        with self._callbacks_lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def unregister_callback(self, cb: callable):
        with self._callbacks_lock:
            if cb in self._callbacks:
                self._callbacks.remove(cb)

    def start(self):
        if self._running:
            return
        self._running = True

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.settimeout(0.5)

        self._broadcast_thread = threading.Thread(
            target=self._broadcast_loop, daemon=True)
        self._broadcast_thread.start()

        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True)
        self._listen_thread.start()

        self._prune_thread = threading.Thread(
            target=self._prune_loop, daemon=True)
        self._prune_thread.start()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def scan_now(self, timeout: float = 3.0) -> dict:
        """One-shot scan: send discovery broadcasts, collect replies."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)

        discovery_msg = json.dumps({
            "type": MSG_DISCOVERY,
            "username": self._username,
            "hostname": self._hostname,
            "device_id": self._device_id,
        }).encode("utf-8")

        broadcasts = _get_broadcast_addrs()
        for _ in range(2):
            for bcast in broadcasts:
                try:
                    sock.sendto(discovery_msg, (bcast, DISCOVERY_PORT))
                except OSError:
                    pass
            time.sleep(0.25)

        found: dict[str, dict] = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                msg = _parse_msg(data)
                if msg and msg.get("type") == MSG_HERE:
                    did = msg.get("device_id", addr[0])
                    found[did] = {
                        "device_id": did,
                        "username": msg.get("username", addr[0]),
                        "hostname": msg.get("hostname", addr[0]),
                        "ip": addr[0],
                        "port": msg.get("port", 0),
                    }
            except socket.timeout:
                continue
            except OSError:
                break

        sock.close()

        # Merge into cache
        with self._devices_lock:
            now = time.time()
            for did, info in found.items():
                info["last_seen"] = now
                self._devices[did] = info

        return found

    def reply_listener(self, timeout: float = 3.0):
        """Listen for MSG_DISCOVERY broadcasts and reply with MSG_HERE.

        Used when the app is in receive mode so senders can discover it.

        .. note:: 这是**阻塞**调用（最长 ``timeout`` 秒）。绝不能直接在 Qt
           主线程 / QTimer 回调里调用，否则界面会整段卡死——必须放到工作线程。
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError:
            sock.close()
            return
        sock.settimeout(0.5)

        here_msg = json.dumps({
            "type": MSG_HERE,
            "username": self._username,
            "hostname": self._hostname,
            "device_id": self._device_id,
            "port": self._listen_port,
        }).encode("utf-8")

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                msg = _parse_msg(data)
                if msg and msg.get("type") == MSG_DISCOVERY:
                    sock.sendto(here_msg, addr)
                    did = msg.get("device_id", addr[0])
                    with self._devices_lock:
                        self._devices[did] = {
                            "device_id": did,
                            "username": msg.get("username", addr[0]),
                            "hostname": msg.get("hostname", addr[0]),
                            "ip": addr[0],
                            "port": self._listen_port,
                            "last_seen": time.time(),
                        }
            except socket.timeout:
                continue
            except OSError:
                break

        sock.close()

    # ── broadcast loop ─────────────────────────────────────

    def _broadcast_loop(self):
        discovery_msg = json.dumps({
            "type": MSG_DISCOVERY,
            "username": self._username,
            "hostname": self._hostname,
            "device_id": self._device_id,
        }).encode("utf-8")

        while self._running:
            broadcasts = _get_broadcast_addrs()
            for bcast in broadcasts:
                try:
                    self._sock.sendto(discovery_msg, (bcast, DISCOVERY_PORT))
                except OSError:
                    pass

            # Also broadcast room discovery queries
            room_discovery = json.dumps({
                "type": MSG_ROOM_DISCOVERY,
                "username": self._username,
            }).encode("utf-8")
            for bcast in broadcasts:
                try:
                    self._sock.sendto(room_discovery, (bcast, ROOM_DISCOVERY_PORT))
                except OSError:
                    pass

            time.sleep(_BROADCAST_INTERVAL)

    # ── listen loop ────────────────────────────────────────

    def _listen_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
                msg = _parse_msg(data)
                if msg is None:
                    continue

                msg_type = msg.get("type", "")
                now = time.time()

                if msg_type == MSG_HERE:
                    did = msg.get("device_id", addr[0])
                    with self._devices_lock:
                        self._devices[did] = {
                            "device_id": did,
                            "username": msg.get("username", addr[0]),
                            "hostname": msg.get("hostname", addr[0]),
                            "ip": addr[0],
                            "port": msg.get("port", 0),
                            "last_seen": now,
                        }
                    self._notify("devices_updated", self.get_devices())

                elif msg_type == MSG_DISCOVERY:
                    # Reply with HERE
                    here_reply = json.dumps({
                        "type": MSG_HERE,
                        "username": self._username,
                        "hostname": self._hostname,
                        "device_id": self._device_id,
                        "port": self._listen_port,
                    }).encode("utf-8")
                    try:
                        self._sock.sendto(here_reply, addr)
                    except OSError:
                        pass
                    # Also record the discovering device
                    did = msg.get("device_id", addr[0])
                    with self._devices_lock:
                        self._devices[did] = {
                            "device_id": did,
                            "username": msg.get("username", addr[0]),
                            "hostname": msg.get("hostname", addr[0]),
                            "ip": addr[0],
                            "port": self._listen_port,
                            "last_seen": now,
                        }

                elif msg_type in (MSG_ROOM_INFO, MSG_ROOM_ANNOUNCE):
                    rid = msg.get("room_id", "")
                    if rid:
                        with self._rooms_lock:
                            self._rooms[rid] = {
                                "room_id": rid,
                                "room_name": msg.get("room_name", ""),
                                "has_password": msg.get("has_password", False),
                                "host_name": msg.get("host_name", ""),
                                "host_ip": msg.get("host_ip", addr[0]),
                                "port": msg.get("port", 0),
                                "player_count": msg.get("player_count", 0),
                                "last_seen": now,
                            }
                        self._notify("rooms_updated", self.get_rooms())

                elif msg_type == MSG_ROOM_DISCOVERY:
                    pass  # Only RoomHost responds to these

            except socket.timeout:
                continue
            except OSError:
                break

    # ── prune loop ─────────────────────────────────────────

    def _prune_loop(self):
        while self._running:
            time.sleep(_PRUNE_INTERVAL)
            if not self._running:
                break
            now = time.time()
            changed = False

            with self._devices_lock:
                expired = [did for did, info in self._devices.items()
                          if now - info.get("last_seen", 0) > _DEVICE_TIMEOUT]
                for did in expired:
                    del self._devices[did]
                    changed = True

            with self._rooms_lock:
                expired_rooms = [rid for rid, info in self._rooms.items()
                                if now - info.get("last_seen", 0) > _DEVICE_TIMEOUT]
                for rid in expired_rooms:
                    del self._rooms[rid]
                    changed = True

            if changed:
                self._notify("devices_updated", self.get_devices())
                self._notify("rooms_updated", self.get_rooms())

    # ── helpers ────────────────────────────────────────────

    def _notify(self, event_type: str, data):
        # 快照遍历：广播/接收线程可能与 UI 线程并发注册或注销回调
        with self._callbacks_lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(event_type, data)
            except Exception:
                pass


# ── module-level helpers ──────────────────────────────────

def _get_broadcast_addrs() -> list[str]:
    """Collect directed broadcast addresses from LAN interfaces."""
    try:
        from network.interfaces import get_all_broadcast_addresses
        addrs = get_all_broadcast_addresses()
        if addrs:
            return addrs
    except ImportError:
        pass
    return ["255.255.255.255"]


def _parse_msg(data: bytes) -> dict | None:
    """解析 UDP 报文，**只返回 dict 或 None**。

    对端可以发任意合法 JSON（``[1]``、``5``、``"x"`` 都不是对象），而调用方
    一律按 dict 用（``msg.get(...)``）。若原样返回，``.get()`` 的 AttributeError
    会逃出监听循环 —— 局域网内任意主机发一个 5 字节包就能让设备发现监听线程
    静默死掉（``_running`` 还是 True，只是再也不会发现任何设备）。
    """
    try:
        msg = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


# ── convenience: get or create the singleton ──────────────

def get_discovery_service() -> DiscoveryService:
    return DiscoveryService()
