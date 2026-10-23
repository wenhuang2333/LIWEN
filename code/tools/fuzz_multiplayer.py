"""联机协议对抗性测试（跨平台，无需管理员权限、无需音频硬件）。

用法：python code/tools/fuzz_multiplayer.py [A B C ...]   # 省略则跑全部

说明：只读仓库、只写临时目录；只绑 127.0.0.1 与高位端口（>=20000）。
**不要与 verify_multiplayer.py 同时运行** —— 两者都会占用 UDP 12347，
同时跑会因为端口竞争出现"广播发现失败"的假红。


覆盖：
  A 端口推导不变量（穷举 1024..65534）
  B 畸形 TCP 应答 → probe_room / join_room 永不抛异常
  C 畸形 UDP 报文 → scan() 不崩且**依然能发现房间**（循环没死）
  D 畸形 UDP 打到真房主 → 广播线程存活（房间仍可被发现）
  E 畸形心跳名单 → RoomSession 不崩，名单被归一化
  F 人数上限 / 重复加入 / 断线清理
  G ping_host 遇到垃圾包头
  H 资源泄漏（线程数 / fd）
  I _recv_header 边界
"""
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

PASS, FAIL, NOTES = [], [], []
THREAD_ERRORS = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def note(msg):
    NOTES.append(msg)
    print(f"NOTE  {msg}", flush=True)


def _thread_excepthook(args):
    THREAD_ERRORS.append(
        f"{args.thread.name}: {args.exc_type.__name__}: {args.exc_value}")
    print(f"THREAD-ERROR  {args.thread.name}: {args.exc_type.__name__}: "
          f"{args.exc_value}", flush=True)


threading.excepthook = _thread_excepthook


SECTIONS = {}


def section(key):
    """把测试节登记进 SECTIONS，便于按节单独运行。"""
    def deco(fn):
        SECTIONS[key] = fn
        return fn
    return deco

from network.transfer import _send_header, _recv_header, ping_host  # noqa: E402
from network.room_protocol import (  # noqa: E402
    RoomHost, RoomDiscoveryClient, RoomJoinClient, RoomSession, _hash_password)
from utils.constants import (  # noqa: E402
    voice_port_for_room, room_data_port_for_room, ROOM_DISCOVERY_PORT,
    MSG_ROOM_INFO, MSG_ROOM_ANNOUNCE, MSG_ROOM_DISCOVERY, MSG_ROOM_PROBE,
    MSG_ROOM_JOIN_REQUEST, MSG_ROOM_JOIN_CHALLENGE, MSG_ROOM_JOIN_RESPONSE,
    MSG_ROOM_JOIN_ACCEPT, MSG_HEARTBEAT, MSG_HEARTBEAT_ACK)

HOSTILE_VALUES = [
    "[1,2,3]", "5", '"x"', "true", "null", "[]", "{}", "not json at all",
    '{"type": [1]}', '{"type": null}', '{"type": {"a": 1}}',
]
HOSTILE_FIELDS = [
    {"room_id": [1, 2]}, {"room_id": {"a": 1}}, {"room_id": None},
    {"room_id": ""}, {"room_id": "   "}, {"room_id": 123},
    {"room_id": "x" * 10000}, {"room_id": "a\x00b"}, {"room_id": "a\nb"},
    {"port": "abc"}, {"port": -1}, {"port": 70000}, {"port": None},
    {"port": True}, {"port": 10 ** 9}, {"port": [1]},
    {"player_count": "abc"}, {"player_count": None}, {"player_count": -5},
    {"max_players": [1]}, {"max_players": 0}, {"max_players": None},
    {"roster": "x"}, {"roster": 42}, {"roster": None}, {"roster": [1, 2, 3]},
    {"roster": [{"peer_id": [1]}]}, {"roster": [{}]}, {"roster": [{}, {}]},
    {"roster": [{"username": None, "peer_id": None, "ip": None}]},
    {"roster": [{"peer_id": {"a": 1}}]},
    {"roster": [{"peer_id": "p%d" % i} for i in range(100000)]},
    {"session_id": None}, {"session_id": [1]}, {"session_id": "y" * 5000},
    {"data_port": None}, {"data_port": "x"}, {"data_port": 70000},
    {"data_port": -1},
]


# ── B 的伪主机：只回一条畸形应答 ────────────────────────────
def fake_tcp_host(port, raw_payload=None, reply=None, mode="reply"):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)

    def serve():
        try:
            srv.settimeout(6)
            conn, _ = srv.accept()
            with conn:
                try:
                    _recv_header(conn)
                except Exception:
                    pass
                if mode == "close":
                    return
                if mode == "half":
                    conn.sendall(struct.pack("!I", 200))
                    return
                if mode == "huge":
                    conn.sendall(struct.pack("!I", 2 ** 31))
                    return
                if raw_payload is not None:
                    body = raw_payload.encode("utf-8")
                    conn.sendall(struct.pack("!I", len(body)) + body)
                    return
                _send_header(conn, reply)
        except Exception:
            pass
        finally:
            try:
                srv.close()
            except Exception:
                pass

    t = threading.Thread(target=serve, daemon=True, name=f"fakehost-{port}")
    t.start()
    return srv, t


@section("A")
def a_ports():
    print("\n=== A 端口推导不变量（穷举 1024..65534） ===", flush=True)
    bad = []
    shared_note, shared_plus_note = [], []
    reserved = {12345, 12346, 12347}
    for p in range(1024, 65535):
        v = voice_port_for_room(p)
        d = room_data_port_for_room(p)
        if not 12400 <= v <= 12799:
            bad.append((p, "voice 越界", v))
        if not 12800 <= d <= 13199:
            bad.append((p, "data 越界", d))
        if v == d:
            bad.append((p, "voice==data", v))
        # 语音是 UDP、数据是 TCP，真正要避开的同协议固定端口如下：
        #   UDP: 12346(设备发现) 12347(房间发现) 12345(TCP 默认端口, 一并避开)
        #   TCP: 12345(局域网传输) room_port+1(加入服务) room_port(推导文档声明要避开)
        for r in reserved:
            if v == r or d == r:
                bad.append((p, "撞固定端口", r))
        if d == p or d == p + 1:
            bad.append((p, "data 撞房间端口", d))
        if v == p:
            shared_note.append(p)
        if v == p + 1:
            shared_plus_note.append(p)
        if voice_port_for_room(p) != v or room_data_port_for_room(p) != d:
            bad.append((p, "非纯函数", v))
    check("端口推导全部不变量（0 违例）", not bad,
          f"sample={bad[:3]}" if bad else "12400-12799 / 12800-13199 区间、避固定端口、voice≠data 全部成立")
    if shared_note:
        note(f"voice_port_for_room(p)==p 的房间端口: {shared_note}（room_port 本身在联机路径里"
             f"从不 bind，UDP/TCP 端口空间独立，因此无实际冲突）")
    if shared_plus_note:
        note(f"voice_port_for_room(p)==p+1 的房间端口: {shared_plus_note}（p+1 是 TCP 加入服务，"
             f"语音是 UDP，两者可同时绑定）")
    # 400 个槽位映射 6.5 万个房间端口 → 必然共用，量化清楚
    seen = {}
    for p in range(1, 65535):
        seen.setdefault(voice_port_for_room(p), []).append(p)
    dup = [(v, len(ps)) for v, ps in seen.items() if len(ps) > 1]
    note(f"语音端口仅 400 个槽位：{len(dup)} 个端口被多个房间端口共用（每槽最多 "
         f"{max([c for _, c in dup] or [0])} 个房间端口）；隔离依赖「只单播给本房间名单里的 IP」，"
         f"跨房间串音仅在「同一台机器同时加入两个语音端口相撞的房间」时可能出现（UDP 端口不是隔离边界）")

    weird = [0, -1, 70000, 999999, "abc", None, True, 1.5, [1], 65535, 12345]
    results = []
    for w in weird:
        try:
            v, d = voice_port_for_room(w), room_data_port_for_room(w)
            in_range = (12400 <= v <= 12799) and (12800 <= d <= 13199)
            results.append((w, "ok" if in_range else f"越界 {v}/{d}"))
        except Exception as e:
            results.append((w, f"{type(e).__name__}: {e}"))
    for w, r in results:
        note(f"非法入参 {w!r} → {r}")


@section("B")
def b_hostile_tcp():
    print("\n=== B 畸形 TCP 应答（probe_room / join_room 必须永不抛异常） ===", flush=True)
    base = 21000
    port = base
    problems = []

    # 非 JSON / 非对象正文
    for raw in HOSTILE_VALUES:
        port += 1
        srv, _t = fake_tcp_host(port, raw_payload=raw)
        for fn, args in ((RoomJoinClient.probe_room, ("127.0.0.1", port)),
                         (RoomJoinClient.join_room, ("127.0.0.1", port, "rid1"))):
            try:
                res = fn(*args, timeout=3) if fn is RoomJoinClient.probe_room else fn(*args)
                if not isinstance(res, tuple) or len(res) != 3 or not isinstance(res[0], bool):
                    problems.append(f"{fn.__name__} 返回结构异常 {raw!r}: {res!r}")
            except Exception as e:
                problems.append(f"{fn.__name__} raw={raw!r} 抛 {type(e).__name__}: {e}")
        srv.close()

    # 合法 JSON 对象，但字段畸形
    for extra in HOSTILE_FIELDS:
        port += 1
        reply = {"type": MSG_ROOM_INFO, "room_name": "x", "port": 21000,
                 "has_password": False, "host_name": "h", "player_count": 1,
                 "max_players": 8, "room_id": "rid1", "session_id": "sid1",
                 "data_port": 1}
        reply.update(extra)
        srv, _t = fake_tcp_host(port, reply=reply)
        try:
            res = RoomJoinClient.probe_room("127.0.0.1", port, timeout=3)
            if not isinstance(res, tuple) or len(res) != 3:
                problems.append(f"probe 返回结构异常 {extra}: {res!r}")
            elif res[0]:
                info = res[2]
                if not isinstance(info.get("room_id"), str):
                    problems.append(f"probe 成功但 room_id 非 str {extra}: {info!r}")
        except Exception as e:
            problems.append(f"probe 字段畸形 {extra} 抛 {type(e).__name__}: {e}")
        srv.close()

        port += 1
        reply2 = dict(reply)
        reply2["type"] = MSG_ROOM_JOIN_ACCEPT
        srv, _t = fake_tcp_host(port, reply=reply2)
        try:
            ok, msg, cfg = RoomJoinClient.join_room("127.0.0.1", port, "rid1")
            if ok:
                if not isinstance(cfg.get("room_id"), str):
                    problems.append(f"join 成功但 room_id 非 str {extra}: {cfg!r}")
                if not isinstance(cfg.get("data_port"), int) or not 1 <= cfg["data_port"] <= 65535:
                    problems.append(f"join 成功但 data_port 非法 {extra}: {cfg!r}")
                if any(not isinstance(p.get("peer_id"), str) for p in cfg.get("roster", [])):
                    problems.append(f"join 成功后名单含非 str peer_id {extra}: {cfg.get('roster')!r}")
            elif not isinstance(msg, str):
                problems.append(f"join 失败但 message 非 str {extra}: {msg!r}")
        except Exception as e:
            problems.append(f"join 字段畸形 {extra} 抛 {type(e).__name__}: {e}")
        srv.close()

    # 连接后立刻关闭 / 只回一半 / 声明巨大长度
    for mode in ("close", "half", "huge"):
        port += 1
        srv, _t = fake_tcp_host(port, mode=mode)
        for fn in (RoomJoinClient.probe_room,):
            try:
                res = fn("127.0.0.1", port, timeout=3)
                if res[0]:
                    problems.append(f"probe {mode} 竟然成功: {res!r}")
            except Exception as e:
                problems.append(f"probe {mode} 抛 {type(e).__name__}: {e}")
        srv.close()
        port += 1
        srv2, _t2 = fake_tcp_host(port, mode=mode)
        try:
            res = RoomJoinClient.join_room("127.0.0.1", port, "rid1")
            if res[0]:
                problems.append(f"join {mode} 竟然成功: {res!r}")
        except Exception as e:
            problems.append(f"join {mode} 抛 {type(e).__name__}: {e}")
        srv2.close()

    # 真房主 + 畸形加入请求（房主侧不能崩、名单不能泄露 session_id）
    hport = base + 400
    host = RoomHost({"room_id": "hostfuzz", "room_name": "模糊房", "port": hport,
                     "max_players": 8}, username="主机")
    host.start()
    time.sleep(0.5)
    for extra in ({"username": [1]}, {"username": None}, {"username": "z" * 100000},
                  {"username": "a\x00b"}, {"peer_id": [1]}, {"peer_id": None},
                  {"peer_id": "q" * 100000}, {"username": "", "peer_id": ""}):
        payload = {"type": MSG_ROOM_JOIN_REQUEST, "room_id": "hostfuzz",
                   "username": "attacker", "peer_id": "ap"}
        payload.update(extra)
        try:
            s = socket.socket()
            s.settimeout(3)
            s.connect(("127.0.0.1", hport + 1))
            _send_header(s, payload)
            try:
                _recv_header(s)
            except Exception:
                pass
            if extra.get("username") in ([1], None) or not extra.get("username"):
                pass
            s.close()
        except Exception as e:
            problems.append(f"畸形加入请求 {extra} 处理异常 {type(e).__name__}: {e}")
    try:
        roster = host.build_roster()
        if any(not isinstance(p.get("peer_id"), str) for p in roster):
            problems.append(f"房主名单含非 str peer_id: {roster!r}")
        blob = json.dumps(roster)
        for peer in host.get_peers().keys():
            if peer in blob:
                problems.append(f"房主名单泄露了 session_id {peer!r}")
    except Exception as e:
        problems.append(f"build_roster 异常 {type(e).__name__}: {e}")
    host.stop()
    check("畸形 TCP 应答 / 加入请求全程不抛异常且字段类型干净", not problems,
          f"{len(problems)} 个问题: {problems[:4]}" if problems else
          f"覆盖 {len(HOSTILE_VALUES) + len(HOSTILE_FIELDS) * 2 + 6 + 8} 组样例")


@section("C")
def c_hostile_udp_scan():
    print("\n=== C 畸形 UDP 报文：scan()/reply_listener() 不崩且循环仍能工作 ===", flush=True)
    # 伪主机：第 1 个探测请求回畸形数组，之后回合法公告 —— 同一个 scan() 调用
    # 里既要有畸形包、又要能发现房间，才能证明"循环没被异常打断"。
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", ROOM_DISCOVERY_PORT))
    srv.settimeout(0.4)
    state = {"n": 0}

    def responder():
        end = time.time() + 8
        while time.time() < end:
            try:
                data, addr = srv.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            state["n"] += 1
            if state["n"] <= 2:
                # 前两个探测一律回畸形包（合法 JSON 但不是对象）
                payload = b"[1,2,3]" if state["n"] == 1 else b'"just a string"'
                # scan() 只广播 2 次，第二次之后不会再收到新探测，
                # 因此畸形应答之后**主动补发**合法公告（同一 addr、同一扫描窗口内），
                # 否则这条检查会因探测次数而非产品缺陷而假红。
                def late(addr=addr):
                    for delay in (0.25, 0.8, 1.3):
                        time.sleep(delay)
                        try:
                            srv.sendto(json.dumps({
                                "type": MSG_ROOM_ANNOUNCE, "room_id": "fakerid01",
                                "room_name": "伪房间", "has_password": False,
                                "host_name": "伪主机", "host_ip": "127.0.0.1",
                                "port": 21400, "player_count": 1, "max_players": 8,
                            }).encode("utf-8"), addr)
                        except OSError:
                            return
                threading.Thread(target=late, daemon=True, name="udp-late-announce").start()
            else:
                payload = json.dumps({
                    "type": MSG_ROOM_ANNOUNCE, "room_id": "fakerid01",
                    "room_name": "伪房间", "has_password": False,
                    "host_name": "伪主机", "host_ip": "127.0.0.1",
                    "port": 21400, "player_count": 1, "max_players": 8,
                }).encode("utf-8")
            try:
                srv.sendto(payload, addr)
            except OSError:
                pass

    rt = threading.Thread(target=responder, daemon=True, name="udp-responder")
    rt.start()
    time.sleep(0.2)

    problems = []
    try:
        rooms = RoomDiscoveryClient("扫描者").scan(timeout=2.0)
        if "fakerid01" not in rooms:
            problems.append(f"畸形包之后仍应发现房间，实际 rooms={list(rooms)}")
        for rid, info in rooms.items():
            if not isinstance(rid, str):
                problems.append(f"房间键非 str: {rid!r}")
            if set(info) != {"room_id", "room_name", "has_password", "host_name",
                             "host_ip", "port", "player_count", "max_players"}:
                problems.append(f"字段集不符: {sorted(info)}")
    except Exception as e:
        problems.append(f"scan() 抛 {type(e).__name__}: {e} "
                        f"| 循环存活={rt.is_alive()}")

    # reply_listener 走同一个解析模式（它 bind 12347，畸形包直接打到它）
    srv.close()
    time.sleep(0.3)
    listener_result = {}
    def _listen():
        try:
            listener_result["rooms"] = RoomDiscoveryClient("监听者").reply_listener(timeout=2.0)
        except Exception as e:
            listener_result["err"] = f"{type(e).__name__}: {e}"

    lt = threading.Thread(target=_listen, daemon=True, name="reply-listener")
    lt.start()
    time.sleep(0.4)
    junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for payload in (b"[1,2,3]", b"7", b'"s"', b"null", b"[]",
                    b"\x00\x01\x02\xff", b"x" * 60000):
        try:
            junk.sendto(payload, ("127.0.0.1", ROOM_DISCOVERY_PORT))
        except OSError:
            pass
    junk.close()
    lt.join(6)
    if "err" in listener_result:
        problems.append(f"reply_listener 抛 {listener_result['err']}")

    check("畸形 UDP 报文不打断扫描/监听循环（且畸形包之后仍能发现房间）",
          not problems, f"{problems[:3]}" if problems else
          f"responder 收到 {state['n']} 次查询，监听者返回 {len(listener_result.get('rooms', {}))} 个房间")


@section("D")
def d_hostile_udp_to_host():
    print("\n=== D 畸形 UDP 打到真房主：广播线程必须存活 ===", flush=True)
    port = 21500
    host = RoomHost({"room_id": "hostlivetest", "room_name": "存活房",
                     "port": port, "max_players": 8}, username="主机")
    host.start()
    time.sleep(1.0)

    junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for payload in (b"[1,2,3]", b"9", b'"s"', b"null", b"[]", b"true",
                    b"{}", b"\x00\xff\xfe" * 3, b"y" * 50000):
        try:
            junk.sendto(payload, ("127.0.0.1", ROOM_DISCOVERY_PORT))
        except OSError:
            pass
    for _ in range(5):
        try:
            junk.sendto(json.dumps({"type": MSG_ROOM_DISCOVERY,
                                    "username": "j"}).encode(), ("127.0.0.1", ROOM_DISCOVERY_PORT))
        except OSError:
            pass
        time.sleep(0.1)
    junk.close()
    time.sleep(3.0)   # 让广播循环至少再播一轮

    rooms = {}
    try:
        rooms = RoomDiscoveryClient("扫描者").scan(timeout=2.0)
    except Exception as e:
        note(f"scan 异常 {e!r}")
    check("房主广播线程被畸形报文打过后仍然存活（房间仍可被发现）",
          "hostlivetest" in rooms, f"发现 {list(rooms)}")
    host.stop()


@section("E")
def e_hostile_heartbeat():
    print("\n=== E 畸形心跳名单：RoomSession 不崩且名单被归一化 ===", flush=True)
    port = 21600
    # 顺序有讲究：**先喂带内容的畸形名单**，否则前几个空名单会让
    # "字段类型已归一化"的断言在没有任何条目时"通过"（假绿）。
    hostile_rosters = [
        # 500 条（约 30KB，能过 64KB 包头上限）→ 应被截断到 64 条
        [{"peer_id": "p%d" % i, "username": "u%d" % i, "ip": "127.0.0.1"}
         for i in range(500)],
        [{"peer_id": "ok1", "username": "甲", "ip": 7}],             # ip 非 str
        [{"peer_id": 42, "username": None, "ip": None}],             # 全非 str
        [{"peer_id": [1]}, {"peer_id": {"a": 1}}],                   # 不可哈希/嵌套
        [None, "x", 5, {}],                                          # 非 dict 条目
        "x", 42, None, [1, 2, 3], [{}], [{}, {}],
    ]
    rstate = {"i": 0}
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port + 1))
    srv.listen(2)
    received = {"heartbeats": 0}

    def serve():
        srv.settimeout(20)
        while True:
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                return
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()

    def _handle(conn):
        """按首包头类型分流：心跳是**每条连接一个包**，首包就是 MSG_HEARTBEAT。"""
        with conn:
            try:
                first = True
                while True:
                    req = _recv_header(conn)
                    if req is None:
                        return
                    if not isinstance(req, dict):
                        continue
                    rtype = req.get("type")
                    if rtype == MSG_HEARTBEAT:
                        received["heartbeats"] += 1
                        roster = hostile_rosters[rstate["i"] % len(hostile_rosters)]
                        rstate["i"] += 1
                        _send_header(conn, {"type": MSG_HEARTBEAT_ACK,
                                            "known": True, "roster": roster})
                        return
                    if first and rtype == MSG_ROOM_JOIN_REQUEST:
                        _send_header(conn, {
                            "type": MSG_ROOM_JOIN_ACCEPT, "room_id": "heartfuzz",
                            "room_name": "心跳房", "port": port, "session_id": "sidfuzz",
                            "host_username": "主机", "host_ip": "127.0.0.1",
                            "roster": [{"peer_id": "host", "username": "主机",
                                        "ip": "127.0.0.1", "is_host": True}],
                            "data_port": room_data_port_for_room(port),
                        })
                    first = False
            except Exception:
                return

    threading.Thread(target=serve, daemon=True, name="fake-heartbeat-host").start()
    time.sleep(0.3)

    ok, msg, cfg = RoomJoinClient.join_room("127.0.0.1", port, "heartfuzz",
                                            username="玩家")
    seen = []
    lost = []
    problems = []
    if not ok:
        problems.append(f"加入伪主机失败: {msg}")
    else:
        sess = RoomSession(cfg, username="玩家")
        sess.on_roster = lambda peers: seen.append(peers)
        sess.on_session_lost = lambda: lost.append(time.time())
        try:
            sess.start_heartbeat()
            deadline = time.time() + 12
            target = 4
            while time.time() < deadline:
                if len(sess.get_peers()) > 0 and received["heartbeats"] >= target:
                    break
                time.sleep(0.2)
        except Exception as e:
            problems.append(f"RoomSession 抛 {type(e).__name__}: {e}")
        finally:
            sess.stop()
        for peers in seen:
            if not isinstance(peers, list):
                problems.append(f"on_roster 收到非 list: {peers!r}")
                continue
            for p in peers:
                if not isinstance(p, dict):
                    problems.append(f"名单条目非 dict: {p!r}")
                    continue
                for k in ("peer_id", "username", "ip"):
                    if not isinstance(p.get(k), str):
                        problems.append(f"名单字段 {k} 非 str: {p!r}")
                if not isinstance(p.get("is_host"), bool):
                    problems.append(f"is_host 非 bool: {p!r}")
        if received["heartbeats"] < 2:
            problems.append(f"心跳次数过少（{received['heartbeats']}），未真正覆盖畸形名单")
        if lost:
            problems.append("畸形名单导致会话被判为失效")
        # 必须真的看到过非空名单，否则上面的类型断言是空转（假绿）
        nonempty = [x for x in seen if x]
        if not nonempty:
            problems.append("所有回调都是空名单 —— 类型归一化断言未真正覆盖任何条目")
        if nonempty and max(len(x) for x in nonempty) > 64:
            problems.append(f"名单未按上限截断: {max(len(x) for x in nonempty)} 条")
        if not any(len(x) >= 2 for x in nonempty):
            problems.append(f"500 条名单未产生多条成员（实际最大 "
                            f"{max([len(x) for x in nonempty] or [0])}），截断路径未覆盖")

    srv.close()
    check("畸形心跳名单不打断会话，名单类型全部归一化（且确实见过非空名单）", not problems,
          f"{problems[:3]}" if problems else
          f"心跳 {received['heartbeats']} 次，回调 {len(seen)} 次，"
          f"非空回调 {len([x for x in seen if x])} 次，最多 {max([len(x) for x in seen] or [0])} 名成员")


@section("F")
def f_capacity():
    print("\n=== F 人数上限 / 重复加入 / 断线清理 ===", flush=True)
    port = 21700
    host = RoomHost({"room_id": "caproom", "room_name": "上限房", "port": port,
                     "max_players": 4}, username="主机")
    host.start()
    time.sleep(0.5)

    results = []
    socks = []
    for i in range(6):
        ok, msg, cfg = RoomJoinClient.join_room("127.0.0.1", port, "caproom",
                                                username=f"玩家{i}")
        results.append((ok, msg))
        if ok:
            socks.append((i, cfg))
    joined = sum(1 for ok, _ in results if ok)
    full_rejected = [m for ok, m in results if not ok and "room_full" in m]
    check("max_players=4 时最多接入 3 个客户端", joined == 3,
          f"成功 {joined} 个，结果={results}")
    check("超员的客户端被明确拒绝（room_full）", len(full_rejected) == 3,
          f"{full_rejected}")
    check("房主计数与名单长度一致（1 + 成员数）",
          host.player_count == len(host.build_roster()),
          f"player_count={host.player_count} roster={len(host.build_roster())}")

    ok2, msg2, _ = RoomJoinClient.join_room("127.0.0.1", port, "caproom",
                                            username="玩家0")
    check("同名同 IP 重复加入被拒（already_joined）", (not ok2) and "already_joined" in msg2,
          msg2)

    before = len(host.get_peers())
    t0 = time.time()
    cleaned = False
    deadline = time.time() + 20
    while time.time() < deadline:
        if len(host.get_peers()) < before:
            cleaned = True
            break
        time.sleep(0.5)
    took = time.time() - t0
    check("加入后从不发心跳的成员会被清理（不留幽灵）", cleaned,
          f"{before} → {len(host.get_peers())}，耗时 {took:.1f}s")
    host.stop()


@section("G")
def g_ping_hostile():
    print("\n=== G ping_host 遇到垃圾包头 ===", flush=True)
    port = 21800
    problems = []
    for raw in ("[1,2,3]", "5", '"x"', "null", "garbage", ""):
        p = port
        port += 1
        srv, _t = fake_tcp_host(p, raw_payload=raw if raw else None,
                                mode="reply" if raw else "close")
        try:
            res = ping_host("127.0.0.1", p, timeout=3)
            if not isinstance(res, tuple) or res[0] is not False:
                problems.append(f"raw={raw!r} 返回 {res!r}")
        except Exception as e:
            problems.append(f"raw={raw!r} 抛 {type(e).__name__}: {e}")
        srv.close()
    check("ping_host 对垃圾包头如实返回失败（不抛栈）", not problems,
          f"{problems[:3]}" if problems else "6 组样例全部返回 (False, 消息)")


@section("H")
def h_leaks():
    print("\n=== H 资源泄漏（20 轮 probe+join+session） ===", flush=True)
    port = 21900
    # 上限故意开大：20 轮会话在 10s 心跳超时窗口内会同时留在房主名单里，
    # 用 8 人上限会把"测试自己的累积"误判成 room_full
    host = RoomHost({"room_id": "leakroom", "room_name": "泄漏房", "port": port,
                     "max_players": 64}, username="主机")
    host.start()
    time.sleep(0.5)

    def fd_count():
        try:
            return len(os.listdir("/dev/fd"))
        except OSError:
            return -1

    t_before, fd_before = threading.active_count(), fd_count()
    problems = []
    for i in range(20):
        ok, msg, info = RoomJoinClient.probe_room("127.0.0.1", port, timeout=3)
        if not ok:
            problems.append(f"第 {i} 轮探测失败: {msg}")
        ok, msg, cfg = RoomJoinClient.join_room("127.0.0.1", port, "leakroom",
                                                username=f"lb{i}")
        if not ok:
            problems.append(f"第 {i} 轮加入失败: {msg}")
            continue
        sess = RoomSession(cfg, username=f"lb{i}")
        sess.start_heartbeat()
        time.sleep(0.05)
        sess.stop()
    time.sleep(1.5)
    t_after, fd_after = threading.active_count(), fd_count()
    host.stop()
    time.sleep(0.5)
    check("20 轮会话结束后无线程残留",
          t_after - t_before <= 2, f"线程 {t_before} → {t_after}")
    check("20 轮会话结束后无 fd 泄漏（≤ +4）",
          fd_after - fd_before <= 4, f"fd {fd_before} → {fd_after}")
    check("20 轮全部成功", not problems, f"{problems[:3]}" if problems else "20/20")


@section("I")
def i_recv_header():
    print("\n=== I _recv_header 边界 ===", flush=True)
    port = 22000
    cases = [("dict", {"type": "x"}, True), ("list", [1], False),
             ("int", 5, False), ("str", "x", False), ("null", None, None),
             ("empty", None, None)]
    problems = []
    for name, payload, expect_dict in cases:
        p = port
        port += 1
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", p))
        srv.listen(1)

        def serve(_p=p, _payload=payload, _name=name):
            try:
                srv.settimeout(5)
                conn, _ = srv.accept()
                with conn:
                    _recv_header(conn)
                    if _name == "empty":
                        return
                    body = json.dumps(_payload).encode()
                    conn.sendall(struct.pack("!I", len(body)) + body)
            except Exception:
                pass
            finally:
                srv.close()

        threading.Thread(target=serve, daemon=True).start()
        s = socket.socket()
        s.settimeout(4)
        try:
            s.connect(("127.0.0.1", p))
            _send_header(s, {"type": MSG_ROOM_PROBE})
            try:
                got = _recv_header(s)
                if expect_dict is True and not isinstance(got, dict):
                    problems.append(f"{name}: 期望 dict，得到 {got!r}")
                if expect_dict is False and isinstance(got, dict):
                    problems.append(f"{name}: 非对象竟被当作 dict 返回")
            except ValueError:
                if expect_dict is True:
                    problems.append(f"{name}: 合法 dict 竟抛 ValueError")
            except Exception as e:
                problems.append(f"{name}: 抛了非 ValueError 的 {type(e).__name__}: {e}")
        finally:
            s.close()
    check("_recv_header 只返回 dict 或 None，非对象抛 ValueError", not problems,
          f"{problems[:3]}" if problems else "6 组边界全部符合契约")


def main():
    t0 = time.time()
    all_keys = "ABCDEFGHI"
    raw = [a.upper() for a in sys.argv[1:]]
    unknown = [a for a in raw if a not in all_keys]
    if unknown:
        # 打错节名却"0 项通过"是假绿：必须明确报错
        print(f"未知的节名 {unknown}；可用节：{' '.join(all_keys)}"
              f"（例：python code/tools/fuzz_multiplayer.py A B D）")
        return 2
    wanted = raw or list(all_keys)
    for key in all_keys:
        if key not in wanted:
            continue
        try:
            SECTIONS[key]()
        except Exception as e:
            check(f"{key} 节本身抛异常", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()

    print("\n" + "=" * 72)
    if THREAD_ERRORS:
        print(f"线程内未捕获异常 {len(THREAD_ERRORS)} 处（会让监听线程静默死掉）：")
        for e in THREAD_ERRORS:
            print(f"  - {e}")
    else:
        print("线程内未捕获异常：0（无静默死亡的线程）")
    print("=" * 72)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，耗时 {time.time() - t0:.1f}s")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    if THREAD_ERRORS:
        print("线程异常：")
        for e in THREAD_ERRORS:
            print(f"  - {e}")
    return 1 if FAIL or THREAD_ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
