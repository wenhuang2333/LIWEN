"""Room hosting, discovery, and session management over LAN.

- RoomHost: UDP announcements + TCP join server with challenge-response auth.
- RoomDiscoveryClient: one-shot UDP room scanner.
- RoomJoinClient: TCP join with challenge-response handshake.
- Session management: session_id, heartbeat (3s), peer state tracking.
"""

import hashlib
import hmac
import json
import os
import socket
import threading
import time
import uuid

from utils.constants import (
    ROOM_DISCOVERY_PORT, MSG_ROOM_ANNOUNCE, MSG_ROOM_DISCOVERY,
    MSG_ROOM_INFO, MSG_ROOM_JOIN_REQUEST, MSG_ROOM_JOIN_CHALLENGE,
    MSG_ROOM_JOIN_RESPONSE, MSG_ROOM_JOIN_ACCEPT, MSG_ROOM_JOIN_REJECT,
    MSG_HEARTBEAT, MSG_HEARTBEAT_ACK, BUFFER_SIZE,
    room_data_port_for_room,
)
from network.transfer import _send_header, _recv_header
# 只导入 warn：本模块里 `info` 被大量用作局部变量名（对端信息字典），
# 模块级导入会被遮蔽。
from utils.logger import warn

HEADER_ENCODING = "utf-8"
HEARTBEAT_INTERVAL = 3   # seconds
HEARTBEAT_TIMEOUT = 10   # seconds — auto-kick after this

# 房主在成员名单里的固定标识（房主没有 session_id）
HOST_PEER_ID = "host"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _challenge_response(password_hash: str, nonce: str) -> str:
    return hashlib.sha256((password_hash + nonce).encode("utf-8")).hexdigest()


def _get_local_ip():
    try:
        from network.interfaces import get_local_ip
        return get_local_ip()
    except ImportError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ""


def _get_broadcast_addrs():
    try:
        from network.interfaces import get_all_broadcast_addresses
        addrs = get_all_broadcast_addresses()
        if addrs:
            return addrs
    except ImportError:
        pass
    return ["255.255.255.255"]


class RoomHost:
    """Hosts a room on the LAN: periodic UDP announcements + TCP join server
    with challenge-response auth + heartbeat monitoring + session IDs."""

    def __init__(self, room_info: dict, username: str = ""):
        self._room = dict(room_info)
        self._username = username or socket.gethostname()
        self._password_hash = room_info.get("password_hash", "")
        self._running = False
        self._thread = None
        self._udp_sock = None
        self._tcp_server = None
        self._join_thread = None
        self._heartbeat_thread = None
        self._player_count = 1

        # session_id → {username, ip, last_heartbeat, peer_id}
        self._peers: dict[str, dict] = {}
        self._peers_lock = threading.Lock()

    @property
    def player_count(self) -> int:
        return self._player_count

    def get_peers(self) -> dict:
        with self._peers_lock:
            return dict(self._peers)

    def kick_peer(self, session_id: str):
        with self._peers_lock:
            if session_id in self._peers:
                del self._peers[session_id]
                self._player_count = 1 + len(self._peers)

    def build_roster(self) -> list:
        """当前房间成员名单（房主在前）。

        原协议从不把成员名单发出去：客户端加入后只能看到房主一个人，
        "在线玩家"列表永远只有一行。名单随心跳回包下发，每个心跳只多几十
        字节。**名单里刻意不含 session_id** —— 它是房主签发的会话凭证，
        泄露给同房间其他成员会让别人可以冒名顶替。
        """
        roster = [{
            "peer_id": HOST_PEER_ID,
            "username": self._username,
            "ip": _get_local_ip(),
            "is_host": True,
        }]
        with self._peers_lock:
            for sid, info in self._peers.items():
                roster.append({
                    "peer_id": info.get("peer_id") or sid,
                    "username": info.get("username", ""),
                    "ip": info.get("ip", ""),
                    "is_host": False,
                })
        return roster

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._broadcast_loop, daemon=True)
        self._thread.start()
        self._join_thread = threading.Thread(target=self._join_loop, daemon=True)
        self._join_thread.start()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def stop(self):
        self._running = False
        for sock in [self._udp_sock, self._tcp_server]:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _build_announce(self) -> bytes:
        return json.dumps({
            "type": MSG_ROOM_ANNOUNCE,
            "room_id": self._room["room_id"],
            "room_name": self._room["room_name"],
            "has_password": bool(self._password_hash),
            "host_name": self._username,
            "host_ip": _get_local_ip(),
            "port": self._room["port"],
            "player_count": self._player_count,
            "max_players": self._room.get("max_players", 8),
        }).encode("utf-8")

    def _broadcast_loop(self):
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._udp_sock.settimeout(1)

        _bound = False
        try:
            self._udp_sock.bind(("0.0.0.0", ROOM_DISCOVERY_PORT))
            _bound = True
        except OSError:
            pass

        while self._running:
            try:
                announce = self._build_announce()
                broadcasts = _get_broadcast_addrs()
                for bcast in broadcasts:
                    try:
                        self._udp_sock.sendto(announce, (bcast, ROOM_DISCOVERY_PORT))
                    except OSError:
                        pass
            except OSError:
                pass

            # Listen for discovery queries for 2.5s
            deadline = time.time() + 2.5
            while time.time() < deadline and self._running:
                try:
                    data, addr = self._udp_sock.recvfrom(4096)
                    msg = json.loads(data.decode("utf-8"))
                    if msg.get("type") == MSG_ROOM_DISCOVERY:
                        info = self._build_announce()
                        info_dict = json.loads(info.decode("utf-8"))
                        info_dict["type"] = MSG_ROOM_INFO
                        self._udp_sock.sendto(
                            json.dumps(info_dict).encode("utf-8"), addr)
                except socket.timeout:
                    continue
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue

    def _join_loop(self):
        join_port = self._room["port"] + 1
        # 端口越界时 bind() 抛的是 OverflowError 而不是 OSError，原实现只捕获
        # OSError，会让整个 join 线程静默死掉：房间照常广播，但谁也进不来。
        if not 1 <= join_port <= 65535:
            raise ValueError(f"房间端口 {self._room['port']} 无法用于加入服务（需 1-65534）")
        self._tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp_server.settimeout(2)
        try:
            self._tcp_server.bind(("0.0.0.0", join_port))
            self._tcp_server.listen(5)
        except OSError:
            return

        while self._running:
            try:
                conn, addr = self._tcp_server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            t = threading.Thread(target=self._handle_join,
                                 args=(conn, addr), daemon=True)
            t.start()

    def _handle_join(self, conn, addr):
        session_id = uuid.uuid4().hex[:16]
        try:
            conn.settimeout(15)
            metadata = _recv_header(conn)
            if metadata is None:
                conn.close()
                return

            # 心跳：加入后对端会周期性发 MSG_HEARTBEAT。原实现只认
            # MSG_ROOM_JOIN_REQUEST，于是心跳全部被丢弃 → 每个成员在
            # HEARTBEAT_TIMEOUT(10s) 后都被当成离线剔除，大厅人数永远掉回 1。
            if metadata.get("type") == MSG_HEARTBEAT:
                sid = metadata.get("session_id", "")
                acked = False
                with self._peers_lock:
                    info = self._peers.get(sid)
                    if info is not None:
                        info["last_heartbeat"] = time.time()
                        acked = True
                # 回包里带上成员名单，客户端据此刷新"在线玩家"。
                # known=False 表示房主不认识这个会话（房主重启过，或成员因网络
                # 中断超时被剔除）——客户端必须显式告诉用户"会话已失效"，
                # 而不是继续对着空气发心跳、显示一份永远不会更新的名单。
                _send_header(conn, {
                    "type": MSG_HEARTBEAT_ACK,
                    "session_id": sid,
                    "known": acked,
                    "roster": self.build_roster() if acked else [],
                })
                return

            if metadata.get("type") != MSG_ROOM_JOIN_REQUEST:
                conn.close()
                return

            room_id = metadata.get("room_id", "")
            if room_id != self._room["room_id"]:
                _send_header(conn, {"type": MSG_ROOM_JOIN_REJECT,
                             "reason": "room_not_found"})
                conn.close()
                return

            if self._password_hash:
                nonce = os.urandom(16).hex()
                _send_header(conn, {"type": MSG_ROOM_JOIN_CHALLENGE,
                             "nonce": nonce})
                response = _recv_header(conn)
                if response is None or response.get("type") != MSG_ROOM_JOIN_RESPONSE:
                    conn.close()
                    return
                expected = _challenge_response(self._password_hash, nonce)
                # 常量时间比较，避免通过响应时间侧信道逐字节猜测口令
                if not hmac.compare_digest(str(response.get("response", "")), expected):
                    _send_header(conn, {"type": MSG_ROOM_JOIN_REJECT,
                                 "reason": "wrong_password"})
                    conn.close()
                    return

            peer_name = metadata.get("username", addr[0])

            # Check for duplicate join
            with self._peers_lock:
                for sid, info in self._peers.items():
                    if info["ip"] == addr[0] and info["username"] == peer_name:
                        _send_header(conn, {"type": MSG_ROOM_JOIN_REJECT,
                                     "reason": "already_joined"})
                        conn.close()
                        return

                # 房主自己占 1 个位置，其余为成员
                max_players = int(self._room.get("max_players") or 8)
                if len(self._peers) + 1 >= max_players:
                    _send_header(conn, {"type": MSG_ROOM_JOIN_REJECT,
                                 "reason": "room_full"})
                    conn.close()
                    return

                self._player_count = 1 + len(self._peers)
                self._peers[session_id] = {
                    "username": peer_name,
                    "ip": addr[0],
                    "last_heartbeat": time.time(),
                    "peer_id": metadata.get("peer_id", addr[0]),
                }
                self._player_count = 1 + len(self._peers)

            _send_header(conn, {
                "type": MSG_ROOM_JOIN_ACCEPT,
                "room_id": self._room["room_id"],
                "room_name": self._room["room_name"],
                "port": self._room["port"],
                "session_id": session_id,
                "host_ip": _get_local_ip(),
                "host_username": self._username,
                # 加入即回一份名单，客户端不必等第一个心跳就能画出成员列表
                "roster": self.build_roster(),
                "data_port": self._room.get("data_port", 0),
            })
        except (socket.timeout, ConnectionError, OSError, ValueError,
                json.JSONDecodeError, UnicodeDecodeError):
            pass
        finally:
            conn.close()

    def _heartbeat_loop(self):
        """Monitor peer heartbeats and clean up stale connections."""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            if not self._running:
                break
            now = time.time()
            with self._peers_lock:
                expired = [sid for sid, info in self._peers.items()
                          if now - info["last_heartbeat"] > HEARTBEAT_TIMEOUT]
                for sid in expired:
                    del self._peers[sid]
                if expired:
                    self._player_count = 1 + len(self._peers)


class RoomDiscoveryClient:
    """Discovers rooms on the LAN via directed UDP broadcast."""

    def __init__(self, username: str = ""):
        self._username = username or socket.gethostname()
        self.rooms = {}

    def scan(self, timeout: float = 3.0) -> dict:
        self.rooms = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)

        discovery = json.dumps({
            "type": MSG_ROOM_DISCOVERY,
            "username": self._username,
        }).encode("utf-8")

        broadcasts = _get_broadcast_addrs()
        for _ in range(2):
            for bcast in broadcasts:
                try:
                    sock.sendto(discovery, (bcast, ROOM_DISCOVERY_PORT))
                except OSError:
                    pass
            time.sleep(0.25)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("type") in (MSG_ROOM_INFO, MSG_ROOM_ANNOUNCE):
                    rid = msg.get("room_id", "")
                    if rid and rid not in self.rooms:
                        self.rooms[rid] = {
                            "room_id": rid,
                            "room_name": msg.get("room_name", ""),
                            "has_password": msg.get("has_password", False),
                            "host_name": msg.get("host_name", ""),
                            "host_ip": msg.get("host_ip", addr[0]),
                            "port": msg.get("port", 0),
                            "player_count": msg.get("player_count", 0),
                            "max_players": msg.get("max_players", 8),
                        }
            except socket.timeout:
                continue
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue

        sock.close()
        return self.rooms

    def reply_listener(self, timeout: float = 3.0) -> dict:
        """Listen for room announcements without sending a query first."""
        self.rooms = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", ROOM_DISCOVERY_PORT))
        except OSError:
            sock.close()
            return self.rooms
        sock.settimeout(0.5)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("type") == MSG_ROOM_ANNOUNCE:
                    rid = msg.get("room_id", "")
                    if rid and rid not in self.rooms:
                        self.rooms[rid] = {
                            "room_id": rid,
                            "room_name": msg.get("room_name", ""),
                            "has_password": msg.get("has_password", False),
                            "host_name": msg.get("host_name", ""),
                            "host_ip": msg.get("host_ip", addr[0]),
                            "port": msg.get("port", 0),
                            "player_count": msg.get("player_count", 0),
                            "max_players": msg.get("max_players", 8),
                        }
            except socket.timeout:
                continue
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue

        sock.close()
        return self.rooms


class RoomJoinClient:
    """Connects to a room host via TCP to request joining."""

    @staticmethod
    def join_room(host_ip: str, port: int, room_id: str,
                  password: str = "", username: str = "",
                  peer_id: str = "") -> tuple:
        """Returns (success: bool, message: str, room_config: dict).

        *username* 是用户在设置里填的昵称。原实现硬编码 ``socket.gethostname()``，
        于是联机大厅显示的是机器名（``xxxdeMacBook-Air.local``），而同为局域网
        功能的「局域网传输」用的是 ``settings.username`` —— 同一个应用里两套
        称呼，房间里也认不出谁是谁。
        """
        # port 来自局域网广播的房间信息（可被伪造）。越界端口会让 connect()
        # 抛 OverflowError——它不属于下面捕获的异常，会直接冒泡到界面。
        try:
            port = int(port)
        except (TypeError, ValueError):
            return False, "房间端口无效", {}
        if not 1 <= port <= 65534:
            return False, f"房间端口无效: {port}", {}
        join_port = port + 1
        peer_id = peer_id or uuid.uuid4().hex[:12]
        username = username or socket.gethostname()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        try:
            sock.connect((host_ip, join_port))
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            sock.close()
            return False, f"无法连接到房间主机 {host_ip}:{join_port} — {e}", {}

        try:
            _send_header(sock, {
                "type": MSG_ROOM_JOIN_REQUEST,
                "room_id": room_id,
                "username": username,
                "peer_id": peer_id,
            })

            response = _recv_header(sock)
            if response is None:
                return False, "房间主机未响应", {}

            resp_type = response.get("type", "")

            if resp_type == MSG_ROOM_JOIN_REJECT:
                reason = response.get("reason", "unknown")
                return False, f"加入被拒绝: {reason}", {}

            if resp_type == MSG_ROOM_JOIN_CHALLENGE:
                if not password:
                    return False, "此房间需要密码", {}
                nonce = response.get("nonce", "")
                pw_hash = _hash_password(password)
                cr = _challenge_response(pw_hash, nonce)
                _send_header(sock, {
                    "type": MSG_ROOM_JOIN_RESPONSE,
                    "room_id": room_id,
                    "response": cr,
                })
                response = _recv_header(sock)
                if response is None:
                    return False, "验证后主机未响应", {}
                resp_type = response.get("type", "")
                if resp_type == MSG_ROOM_JOIN_REJECT:
                    return False, "密码错误", {}

            if resp_type == MSG_ROOM_JOIN_ACCEPT:
                config = {
                    "room_id": response.get("room_id", room_id),
                    "room_name": response.get("room_name", ""),
                    "port": response.get("port", port),
                    "session_id": response.get("session_id", ""),
                    "host_ip": host_ip,
                    "host_username": response.get("host_username", ""),
                    "peer_id": peer_id,
                    "username": username,
                    "roster": response.get("roster") or [],
                    "data_port": response.get("data_port") or
                                 room_data_port_for_room(response.get("port", port)),
                }
                return True, "加入成功", config

            return False, f"未知响应: {resp_type}", {}
        except (socket.timeout, ConnectionError, OSError) as e:
            return False, f"连接错误: {e}", {}
        except ValueError as e:
            return False, f"协议错误: {e}", {}
        finally:
            sock.close()


class RoomSession:
    """Client-side session state for a joined room.

    Manages heartbeat sending and peer list tracking.
    """

    def __init__(self, room_config: dict, username: str = ""):
        self._config = dict(room_config)
        self._username = username or socket.gethostname()
        self._session_id = room_config.get("session_id", "")
        self._peer_id = room_config.get("peer_id", "")
        self._running = False
        self._heartbeat_thread = None

        # 成员名单（房主下发）：peer_id → {username, ip, is_host, peer_id}
        self._peers: dict[str, dict] = {}
        self._peers_lock = threading.Lock()

        # 回调：on_roster(list[dict]) / on_session_lost()
        self.on_roster = None
        self.on_session_lost = None

    @property
    def peer_id(self) -> str:
        return self._peer_id

    @property
    def data_port(self) -> int:
        """房间内文件传输端口（每个成员监听同一个值）。"""
        port = self._config.get("data_port")
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        return port or room_data_port_for_room(self.port)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def room_id(self) -> str:
        return self._config.get("room_id", "")

    @property
    def room_name(self) -> str:
        return self._config.get("room_name", "")

    @property
    def host_ip(self) -> str:
        return self._config.get("host_ip", "")

    @property
    def port(self) -> int:
        return self._config.get("port", 0)

    def get_peers(self) -> dict:
        with self._peers_lock:
            return dict(self._peers)

    def get_peer_ips(self) -> list[str]:
        """除自己以外的成员 IP（语音的发送目标）。"""
        with self._peers_lock:
            peers = list(self._peers.values())
        my_ip = _get_local_ip()
        out = []
        for info in peers:
            ip = info.get("ip", "")
            if not ip or ip == my_ip or ip in out:
                continue
            out.append(ip)
        return out

    def get_send_targets(self) -> list[dict]:
        """除自己以外的成员（供房间内文件传输选择目标）。"""
        with self._peers_lock:
            peers = list(self._peers.values())
        my_ip = _get_local_ip()
        return [p for p in peers
                if p.get("peer_id") != self._peer_id and p.get("ip")
                and not (p.get("is_host") and p.get("ip") == my_ip)]

    def start_heartbeat(self):
        """启动心跳线程：既用于保活，也是获取成员名单的唯一途径。"""
        if self._running:
            return
        self._running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def stop(self):
        self._running = False

    def _heartbeat_loop(self):
        while self._running:
            self._send_heartbeat_once()
            # 分片睡眠：stop() 后最多 0.1s 退出，不用等满一个心跳周期
            for _ in range(int(HEARTBEAT_INTERVAL * 10)):
                if not self._running:
                    break
                time.sleep(0.1)

    def _send_heartbeat_once(self):
        """发一次心跳并处理回包（成员名单 / 会话失效）。"""
        host_ip = self._config.get("host_ip", "")
        try:
            join_port = int(self._config.get("port", 0)) + 1
        except (TypeError, ValueError):
            return
        if not host_ip or not 1 <= join_port <= 65535:
            return

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host_ip, join_port))
            _send_header(sock, {
                "type": MSG_HEARTBEAT,
                "session_id": self._session_id,
                "username": self._username,
                "peer_id": self._peer_id,
            })
            ack = _recv_header(sock)
        except Exception:
            # 单次心跳失败（房主忙、瞬时丢包）不致命，下一轮会重试
            return
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        if not isinstance(ack, dict) or ack.get("type") != MSG_HEARTBEAT_ACK:
            return

        if not ack.get("known", True):
            # 房主不认识这个会话：房主重启过，或本机因网络中断超时被剔除。
            # 必须让界面知道，而不是继续对着空气发心跳、显示一份永不更新的名单。
            self._running = False
            if self.on_session_lost:
                self.on_session_lost()
            return

        roster = ack.get("roster")
        if isinstance(roster, list):
            self._update_roster(roster)

    def _update_roster(self, roster):
        peers = {}
        for item in roster:
            if not isinstance(item, dict):
                continue
            pid = item.get("peer_id") or item.get("ip") or ""
            if not pid:
                continue
            peers[pid] = {
                "peer_id": pid,
                "username": item.get("username", ""),
                "ip": item.get("ip", ""),
                "is_host": bool(item.get("is_host")),
            }
        with self._peers_lock:
            self._peers = peers
        if self.on_roster:
            try:
                self.on_roster(list(peers.values()))
            except Exception as e:
                warn(f"RoomSession.on_roster 回调异常: {e}")
