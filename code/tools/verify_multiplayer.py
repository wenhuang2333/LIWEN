"""联机（应用层组网）集成验证脚本。

覆盖：
1. 房间创建 → UDP 广播发现（同一局域网路径）
2. IP:端口 直连探测 → room_probe（跨网段/广播被拦截路径）
3. 挑战-应答加入（正确密码 / 错误密码 / 无密码）
4. 心跳保活 + 房主下发成员名单（两个客户端同时在房间）
5. 会话失效（房主重启后 known=False）
6. 房间内文件传输（复用 network/transfer.py，端口由房间端口推导）
7. 语音后端可用性、关闭时序（不留孤儿 PortAudio 流 → 不段错误）与 UDP 收发
   （无音频设备时优雅降级）

可选（默认不跑）：
8. `--audio` 实测打开音频设备（会触发系统麦克风授权弹窗）
9. `--gui`   离屏 Qt 跑一遍联机大厅的「直连加入」整条链路
   （LobbyView → RoomProbeWorker → RoomJoinWorker → RoomPage → 离开/清理）

用法（在 code/ 下执行）:
    python /path/to/verify_multiplayer.py
    python /path/to/verify_multiplayer.py --gui
"""
import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def _fake_probe_host(port, reply: dict):
    """起一个只回一条畸形探测应答的伪主机（用于验证客户端不崩）。"""
    import socket as _s
    from network.transfer import _send_header, _recv_header
    srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)

    def _serve():
        try:
            srv.settimeout(5)
            conn, _ = srv.accept()
            with conn:
                _recv_header(conn)
                _send_header(conn, reply)
        except Exception:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t


def _host(room_id, name, port, password="", max_players=8):
    from network.room_protocol import RoomHost, _hash_password
    info = {
        "room_id": room_id,
        "room_name": name,
        "port": port,
        "max_players": max_players,
    }
    if password:
        info["password_hash"] = _hash_password(password)
    host = RoomHost(info, username="主机")
    host.start()
    return host, info


def _voice_teardown_checks(room_port: int):
    """语音关闭时序回归（不需要音频硬件，确定性覆盖 exit 139 段错误）。

    根因：``start()`` 只**同步**打开输出流；输入流是在 ``_capture_loop`` 线程里
    **异步**打开的（macOS 首次还要等麦克风授权，``Pa_OpenStream`` 可能几百 ms）。
    如果进程恰好在这段窗口里开始退出，采集线程仍在 libportaudio 内部执行，而
    解释器终结阶段会释放 CFFI 的 dlopen 句柄（``dlclose`` 把代码页 unmap）——
    线程随即跳到未映射内存，``EXC_BAD_ACCESS / Instruction Abort``（exit 139）。

    这个窗口必须靠运气才撞得上（实测未修复时 30 次里崩 8 次），所以这里注入一个
    「打开设备很慢」的假后端，把窗口放大成 400ms 的**确定性**断言：``stop()``
    必须等采集线程真正退出，且在途打开的流必须被关掉（不能留成孤儿流）。
    真后端要开麦克风、会弹系统授权框，所以默认测试不碰硬件。
    """
    import network.voice_protocol as vp
    from utils.constants import voice_port_for_room

    open_delay = 0.4

    class _StubStream:
        """假音频流：只记录自己有没有被关过。"""

        def __init__(self, tag):
            self.tag = tag

        def stop(self):
            pass

        def close(self):
            pass

        def abort(self):
            pass

    class _StubBackend(vp._StreamCloser):
        """假后端：``open_input()`` 故意慢，用来复现「在途打开」的竞态。"""

        name = "stub"

        def __init__(self):
            super().__init__()
            self.opened_inputs = []
            self._input = None
            self._output = None

        def open_output(self):
            self._output = _StubStream("out")
            return self._output

        def open_input(self):
            time.sleep(open_delay)          # 模拟在途的 Pa_OpenStream
            stream = _StubStream("in")
            self._input = stream
            self.opened_inputs.append(stream)
            return stream

        @staticmethod
        def read(stream, frames):
            time.sleep(0.02)                # 20ms 一帧
            return b"\x00" * vp.FRAME_BYTES

        @staticmethod
        def write(stream, data):
            pass

        def terminate(self):
            for stream in (self._input, self._output):
                self.close_stream(stream)
            self._input = None
            self._output = None

    saved_backends = dict(vp._BACKENDS)
    saved_probe = vp._probe_backend
    vp._BACKENDS["stub"] = _StubBackend
    vp._probe_backend = lambda: "stub"
    vc = None
    try:
        vc = vp.VoiceClient("teardown-probe", voice_port_for_room(room_port))
        started = vc.start()
        backend = vc._backend
        time.sleep(0.05)                    # 采集线程此刻正卡在 open_input() 里
        t0 = time.monotonic()
        vc.stop()
        waited = time.monotonic() - t0
        alive = [t.name for t in (vc._capture_thread, vc._playback_thread)
                 if t is not None and t.is_alive()]
        check("语音关闭: 假后端下 start() 成功且 stop() 后无线程残留",
              bool(started) and not alive, f"started={started} alive={alive}")
        check("语音关闭: stop() 等到在途的设备打开完成（不能提前返回）",
              waited >= open_delay * 0.8, f"waited={waited:.3f}s")
        check("语音关闭: 在途打开的输入流被关闭（不留孤儿流→无 exit 139）",
              len(backend.opened_inputs) == 1
              and backend.opened_inputs[0] in backend._closed_streams,
              f"opened={len(backend.opened_inputs)}")
        check("语音关闭: stop() 后后端已释放", vc._backend is None)
        vc.stop()
        check("语音关闭: stop() 可重复调用（幂等）", True)
    except Exception as e:
        check("语音关闭: 时序检查本身不抛异常", False, repr(e))
    finally:
        if vc is not None:
            try:
                vc.stop()
            except Exception:
                pass
        vp._BACKENDS.clear()
        vp._BACKENDS.update(saved_backends)
        vp._probe_backend = saved_probe


def main():
    from utils.constants import room_data_port_for_room
    from network.transfer import _send_header, _recv_header, ping_host
    from network.room_protocol import (
        RoomHost, RoomDiscoveryClient, RoomJoinClient, RoomSession, _hash_password)
    from utils.constants import MSG_ROOM_INFO, MSG_ROOM_JOIN_REQUEST

    print("=" * 72)
    print("联机（应用层组网）集成验证")
    print("=" * 72)

    BASE_PORT = 20000
    host, info = _host("testroom001", "测试房间", BASE_PORT, password="secret")
    time.sleep(0.6)

    # ── 1. UDP 广播发现 ──
    # 广播是"尽力而为"的：同网段可能有别的程序/别的房间在占用 12347，
    # 单次扫描偶发漏包不代表功能坏了 —— 重试两次再判失败。
    disco = RoomDiscoveryClient("测试客户端")
    rooms, scan_err = {}, None
    try:
        for _ in range(3):
            rooms = disco.scan(timeout=2.0)
            if "testroom001" in rooms:
                break
            time.sleep(0.5)
    except Exception as e:
        scan_err = repr(e)
    check("UDP 广播发现房间", "testroom001" in rooms,
          f"发现 {list(rooms)}" + (f" err={scan_err}" if scan_err else ""))

    # ── 2. 直连探测（不依赖 UDP 广播）──
    ok, msg, probed = RoomJoinClient.probe_room("127.0.0.1", BASE_PORT, timeout=5)
    check("直连探测 room_probe", ok and probed.get("room_id") == "testroom001",
          f"{msg} {probed.get('room_name')}")
    check("探测结果显示需要密码", probed.get("has_password") is True)
    check("探测结果不含 session_id", "session_id" not in probed)
    # 探测只能重复广播里已经公开的东西：字段集合必须是广播公告的子集
    # （data_port 是推导值，加入应答里会给，不该由探测多说）
    announce_keys = set(json.loads(host._build_announce().decode("utf-8")))
    check("探测字段不超出广播公告（不多说一个字）",
          set(probed) <= announce_keys,
          f"probe={sorted(probed)} announce={sorted(announce_keys)}")
    check("探测回填的是用户填的地址（不采信主机自报 IP）",
          probed.get("host_ip") == "127.0.0.1", str(probed.get("host_ip")))

    # ── 2b. 恶意/畸形主机不得让客户端崩溃 ──
    # 伪主机：把 room_id 回成列表。旧实现直接拿它当字典键 → TypeError，
    # 在 Qt 槽里未捕获异常会让进程 abort（LAN 上可触发的 DoS）。
    fake_port = BASE_PORT + 300
    _fake_probe_host(fake_port + 1, {
        "type": MSG_ROOM_INFO, "room_id": [1, 2], "room_name": "坏房间",
        "has_password": False, "host_name": "x", "host_ip": "127.0.0.1",
        "port": fake_port, "player_count": 1, "max_players": 8,
    })
    time.sleep(0.4)
    ok_bad, msg_bad, info_bad = RoomJoinClient.probe_room(
        "127.0.0.1", fake_port, timeout=3)
    check("畸形 room_id（列表）被拒绝且不抛异常",
          not ok_bad and info_bad == {} and "room_id" in msg_bad, msg_bad)

    # ── 2c. 对端 peer_id 为空时，名单里绝不能出现 session_id ──
    # 旧实现用 `info.get("peer_id") or sid` 兜底：只要客户端上报空 peer_id，
    # 房主就把**会话凭证**当 peer_id 广播给全房间 → 可冒名顶替。
    leak_host, leak_info = _host("leakroom001", "凭证泄漏测试房", BASE_PORT + 400)
    time.sleep(0.5)
    raw = socket.create_connection(("127.0.0.1", BASE_PORT + 401), timeout=5)
    try:
        from network.transfer import _send_header as _sh, _recv_header as _rh
        _sh(raw, {"type": MSG_ROOM_JOIN_REQUEST, "room_id": "leakroom001",
                  "username": "空ID玩家", "peer_id": ""})
        accept = _rh(raw) or {}
        sid = accept.get("session_id", "")
        roster = leak_host.build_roster()
        peer_ids = [p.get("peer_id") for p in roster]
        check("空 peer_id 时名单不泄露 session_id",
              bool(sid) and sid not in peer_ids and sid not in json.dumps(roster),
              f"session_id={sid!r} peer_ids={peer_ids}")
    finally:
        raw.close()
        leak_host.stop()

    # 取消回调必须真能打断连接（黑洞地址：旧实现要卡满 5s，cancel 形同虚设）
    t0 = time.time()
    ok_c, msg_c, _ = RoomJoinClient.probe_room(
        "10.255.255.1", 23456, timeout=5.0,
        should_cancel=lambda: time.time() - t0 > 0.4)
    dt = time.time() - t0
    check("探测可被取消（不必等满 timeout）",
          not ok_c and dt < 3.0, f"{msg_c} 用时 {dt:.2f}s")

    # ── 2d. 畸形 UDP 报文不得打垮房主广播循环 ──
    # `msg.get("type")` 之前没有 isinstance 检查时，一个 5 字节的广播包
    # `[1,2,3]`（合法 JSON、但不是对象）就能让 `_broadcast_loop` 线程抛
    # AttributeError 永久退出 —— 房间还在、别人却再也扫不到，且 `_running`
    # 仍为 True，没有任何机制会重启它。
    junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for payload in (b"[1,2,3]", b"5", b'"x"', b"null", b"[]", b"true",
                    b"\x00\x01\xff", b"z" * 4000):
        try:
            junk.sendto(payload, ("127.0.0.1", 12347))
        except OSError:
            pass
    for _ in range(3):
        try:
            junk.sendto(json.dumps({"type": "room_discovery",
                                    "username": "j"}).encode(),
                        ("127.0.0.1", 12347))
        except OSError:
            pass
        time.sleep(0.1)
    junk.close()
    time.sleep(3.0)     # 让广播循环至少再播一轮（每轮 2.5s 监听窗口）
    rooms_after_junk = RoomDiscoveryClient("测试客户端").scan(timeout=2.0)
    check("房主广播循环被畸形 UDP 打过之后仍然存活（房间仍可被发现）",
          "testroom001" in rooms_after_junk, f"发现 {list(rooms_after_junk)}")

    # ── 3a. 错误密码被拒 ──
    ok, msg, _ = RoomJoinClient.join_room(
        "127.0.0.1", BASE_PORT, "testroom001", password="wrong", username="坏人")
    check("错误密码被拒绝", not ok, msg)

    # ── 3b. 无密码加入被提示需要密码 ──
    ok, msg, _ = RoomJoinClient.join_room(
        "127.0.0.1", BASE_PORT, "testroom001", username="无密码者")
    check("缺密码被拒绝", not ok, msg)

    # ── 3c. 正确密码加入 ──
    ok1, msg1, cfg1 = RoomJoinClient.join_room(
        "127.0.0.1", BASE_PORT, "testroom001", password="secret", username="玩家甲")
    check("正确密码加入成功", ok1 and cfg1.get("session_id"), msg1)
    check("加入回包含成员名单", isinstance(cfg1.get("roster"), list)
          and len(cfg1["roster"]) == 2, str(cfg1.get("roster")))

    ok2, msg2, cfg2 = RoomJoinClient.join_room(
        "127.0.0.1", BASE_PORT, "testroom001", password="secret", username="玩家乙")
    check("第二个客户端加入成功", ok2 and cfg2.get("session_id"), msg2)

    # ── 4. 心跳 + 成员名单 (跨平台混连的核心链路) ──
    roster_seen = {"甲": None, "乙": None}

    def _mk_session(cfg, key):
        s = RoomSession(cfg, username=key)
        s.on_roster = lambda r, k=key: roster_seen.__setitem__(k, r)
        s.on_session_lost = lambda k=key: roster_seen.__setitem__(k, "LOST")
        s.start_heartbeat()
        return s

    s1 = _mk_session(cfg1, "甲")
    s2 = _mk_session(cfg2, "乙")
    deadline = time.time() + 8
    while time.time() < deadline:
        if roster_seen["甲"] and roster_seen["乙"]:
            break
        time.sleep(0.2)

    r1 = roster_seen["甲"]
    check("客户端甲收到成员名单", isinstance(r1, list) and len(r1) == 3, str(r1))
    if isinstance(r1, list):
        names = sorted(p.get("username", "") for p in r1)
        check("名单含房主+两个玩家", names == ["主机", "玩家乙", "玩家甲"], str(names))
        check("名单不含 session_id（防冒名）",
              all("session_id" not in p for p in r1))
    check("客户端乙收到成员名单", isinstance(roster_seen["乙"], list)
          and len(roster_seen["乙"]) == 3, str(roster_seen["乙"]))

    # ── 5. 房间内文件传输（UDP 之外的独立 TCP 通道）──
    save_dir = tempfile.mkdtemp(prefix="liwen_room_rx_")
    xfer_port = room_data_port_for_room(BASE_PORT)
    check("房间数据端口在专用区间", 12800 <= xfer_port <= 13199, str(xfer_port))

    recv_result = {}
    recv_stop = {"v": False}

    def _receiver():
        from network.transfer import receive_files
        try:
            receive_files(xfer_port, save_dir,
                          status_callback=lambda m: recv_result.setdefault("status", m),
                          cancel_check=lambda: recv_stop["v"],
                          auto_accept=True)
        except Exception as e:
            recv_result["error"] = repr(e)

    rt = threading.Thread(target=_receiver, daemon=True)
    rt.start()
    time.sleep(0.8)

    src = os.path.join(save_dir, "..", "liwen_payload.txt")
    src = os.path.abspath(src)
    with open(src, "w", encoding="utf-8") as f:
        f.write("LIWEN 联机文件传输验证 " * 200)

    send_err = None
    try:
        from network.transfer import send_files
        send_files([src], "127.0.0.1", xfer_port, compress_first=True,
                   from_name="玩家甲", send_request=True)
    except Exception as e:
        send_err = repr(e)

    time.sleep(2.0)
    received = [f for f in os.listdir(save_dir) if not f.startswith("liwen_room_rx_")]
    check("房间内文件传输送达", send_err is None and received,
          f"send={send_err} received={received}")

    # 收完就停掉监听线程并 join：否则 TCP 端口会一直被这个进程占着
    recv_stop["v"] = True
    rt.join(timeout=6)
    check("接收监听线程已退出（端口释放）", not rt.is_alive(),
          f"recv_result={recv_result}")

    # ── 6. 会话失效检测（房主重启）──
    s1.stop(); s2.stop()
    host.stop()
    time.sleep(0.5)

    host2, _ = _host("testroom001", "测试房间", BASE_PORT, password="secret")
    time.sleep(0.4)
    ok3, msg3, cfg3 = RoomJoinClient.join_room(
        "127.0.0.1", BASE_PORT, "testroom001", password="secret", username="玩家丙")
    lost = {"v": False}
    s3 = RoomSession(cfg3, username="玩家丙")
    s3.on_session_lost = lambda: lost.__setitem__("v", True)
    # 伪造一个房主不认识的 session_id，模拟"房主重启过"
    s3._session_id = "deadbeefdeadbeef"
    s3.start_heartbeat()
    deadline = time.time() + 8
    while time.time() < deadline and not lost["v"]:
        time.sleep(0.2)
    check("会话失效被检出（known=False）", lost["v"])
    s3.stop(); host2.stop()

    # ── 6.5 协议边界：非 JSON 对象 / 畸形报文不得打断循环 ──
    # 包头协议规定载荷永远是 JSON 对象，但对端可以发 `[1,2,3]` / `5` / `"x"`
    # （都是合法 JSON）。`_recv_header` 现在对非对象显式抛 ValueError，
    # 调用点据此如实报错；`probe_room` / `join_room` / `ping_host` 都不许抛栈。
    hostile_port = BASE_PORT + 300
    # 注意：probe_room/join_room 连的是 room_port + 1，伪主机必须绑在 +1 上，
    # 否则拿到的是 Connection refused —— 那是假绿，测不到解析路径。
    t_hostile = _fake_probe_host(hostile_port + 1, [1, 2, 3])
    try:
        ok_nondict, msg_nondict, info_nondict = RoomJoinClient.probe_room(
            "127.0.0.1", hostile_port, timeout=3)
        check("探测遇到非 JSON 对象应答时如实失败（不抛栈）",
              (not ok_nondict) and isinstance(msg_nondict, str) and info_nondict == {}
              and "协议错误" in msg_nondict,
              msg_nondict)
    except Exception as e:
        check("探测遇到非 JSON 对象应答时如实失败（不抛栈）", False, repr(e))
    t_hostile.join(4)

    t_hostile2 = _fake_probe_host(hostile_port + 2, [1, 2, 3])
    try:
        ok_nondict2, msg_nondict2, cfg_nondict2 = RoomJoinClient.join_room(
            "127.0.0.1", hostile_port + 1, "rid")
        check("加入遇到非 JSON 对象应答时如实失败（不抛栈）",
              (not ok_nondict2) and isinstance(msg_nondict2, str) and cfg_nondict2 == {}
              and "协议错误" in msg_nondict2,
              msg_nondict2)
    except Exception as e:
        check("加入遇到非 JSON 对象应答时如实失败（不抛栈）", False, repr(e))
    t_hostile2.join(4)

    t_hostile3 = _fake_probe_host(hostile_port + 2, [1, 2, 3])
    try:
        ok_ping, msg_ping = ping_host("127.0.0.1", hostile_port + 2, timeout=3)
        check("ping_host 遇到非 JSON 对象应答时如实返回失败（不抛栈）",
              ok_ping is False and isinstance(msg_ping, str), msg_ping)
    except Exception as e:
        check("ping_host 遇到非 JSON 对象应答时如实返回失败（不抛栈）", False, repr(e))
    t_hostile3.join(4)

    t_hostile4 = _fake_probe_host(hostile_port + 3, [1, 2, 3])
    try:
        s_raw = socket.socket()
        s_raw.settimeout(4)
        s_raw.connect(("127.0.0.1", hostile_port + 3))
        _send_header(s_raw, {"type": MSG_ROOM_INFO})
        try:
            payload = _recv_header(s_raw)
            check("_recv_header 对非对象包头抛 ValueError（契约：dict 或 None）",
                  False, f"竟返回 {payload!r}")
        except ValueError as e:
            check("_recv_header 对非对象包头抛 ValueError（契约：dict 或 None）",
                  "协议错误" in str(e), str(e))
        except Exception as e:
            check("_recv_header 对非对象包头抛 ValueError（契约：dict 或 None）",
                  False, f"抛的是 {type(e).__name__}: {e}")
        finally:
            s_raw.close()
    except Exception as e:
        check("_recv_header 对非对象包头抛 ValueError（契约：dict 或 None）", False, repr(e))
    t_hostile4.join(4)

    # reply_listener 走同一套解析：房主已停，12347 上只有它一个监听者，
    # 因此畸形包与随后的合法公告投递是确定的 —— 畸形包之后仍能发现房间，
    # 才证明解析循环没被异常打断（只断言"没抛异常"是可能假绿的）。
    listener_result = {}

    def _listen_once():
        try:
            listener_result["rooms"] = RoomDiscoveryClient(
                "监听者").reply_listener(timeout=2.5)
        except Exception as e:
            listener_result["err"] = repr(e)

    lt = threading.Thread(target=_listen_once, daemon=True)
    lt.start()
    time.sleep(0.5)
    junk2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for payload in (b"[1,2,3]", b"7", b'"s"', b"null", b"[]", b"\x00\xff\xfe"):
        try:
            junk2.sendto(payload, ("127.0.0.1", 12347))
        except OSError:
            pass
    time.sleep(0.3)
    try:
        junk2.sendto(json.dumps({
            "type": "room_announce", "room_id": "afterjunk1",
            "room_name": "存活房间", "has_password": False, "host_name": "伪主机",
            "host_ip": "127.0.0.1", "port": 20999, "player_count": 1,
            "max_players": 8,
        }).encode(), ("127.0.0.1", 12347))
    except OSError:
        pass
    junk2.close()
    lt.join(6)
    check("畸形 UDP 报文之后 reply_listener 仍能发现房间（循环未被打断）",
          listener_result.get("err") is None
          and "afterjunk1" in listener_result.get("rooms", {}),
          f"err={listener_result.get('err')} rooms={list(listener_result.get('rooms', {}))}")

    # ── 7. 语音后端 ──
    from network.voice_protocol import (
        is_voice_available, voice_backend_name, VoiceClient, INSTALL_HINT)
    from utils.constants import voice_port_for_room
    backend = voice_backend_name()
    check("音频后端可用（跨平台选型）", is_voice_available(),
          f"backend={backend!r} hint={INSTALL_HINT}")
    # 不能只断言 backend 名字：那已被上一项蕴含（可用 ⇔ 探测到后端）。
    # 这里断言真正能失败的东西：安装提示必须同时给出两条跨平台路径，
    # 并且"不可用时"的返回值必须如实为 False。
    check("安装提示同时给出 sounddevice 与 pyaudio 两条路径",
          "sounddevice" in INSTALL_HINT and "pyaudio" in INSTALL_HINT,
          INSTALL_HINT)
    ok_bad, msg_bad, _ = RoomJoinClient.probe_room("127.0.0.1", 70000)
    check("非法端口探测如实失败（不抛异常）", not ok_bad, msg_bad)

    # 真正打开音频设备会触发系统麦克风授权弹窗，所以默认不测；
    # 加 --audio 才做"设备能不能开"的实测。
    if "--audio" in sys.argv:
        vc = VoiceClient("test-sender", voice_port_for_room(BASE_PORT))
        started = vc.start()
        # 契约：start() 成功 ⇔ 真的解析到了后端；失败必须给出原因而不是假装成功
        check("start() 成功与否与后端可用性一致（不假装成功）",
              bool(started) == bool(vc.backend_name),
              f"started={started} backend={vc.backend_name!r} "
              f"available={is_voice_available()}")
        if started:
            check("语音设备可打开（实测）", True, f"backend={vc.backend_name}")
        else:
            print("NOTE  语音实测未打开设备（无设备/被占用/未授权），"
                  "已确认返回 False 而非假成功")
        vc.stop()
        vc.stop()   # 必须可重复调用（清理路径会调两次）

        # 启动后**从不 stop** 也必须干净退出：PortAudio 的流若留到解释器
        # 线程终结阶段才被 GC，进程会段错误（exit 139）。用子进程验证，
        # 因为段错误会带走整个测试进程。
        probe_code = (
            "import sys; sys.path.insert(0, %r);"
            "from network.voice_protocol import VoiceClient;"
            "from utils.constants import voice_port_for_room;"
            "vc = VoiceClient('no-stop-probe', voice_port_for_room(30600));"
            "vc.start();"
            "print('exiting without stop')\n" % CODE_DIR
        )
        try:
            r = subprocess.run([sys.executable, "-c", probe_code],
                               capture_output=True, text=True, timeout=90)
            check("启动后不 stop() 也干净退出（无段错误）", r.returncode == 0,
                  f"rc={r.returncode} {r.stderr.strip()[-160:]}")
        except Exception as e:
            check("启动后不 stop() 也干净退出（无段错误）", False, repr(e))
    else:
        print("SKIP  语音设备实测（加 --audio 启用，避免触发系统麦克风授权弹窗）")

    # ── 7.5 语音关闭时序（exit 139 段错误回归，不需要音频硬件）──
    _voice_teardown_checks(BASE_PORT + 60)

    # ── 8. 联机大厅「直连加入」GUI 全链路（--gui）──
    # 只做协议层验证是不够的：真正的入口是 LobbyView 的 IP:端口 那一行，
    # 它要经过 RoomProbeWorker → _on_probe_result → RoomJoinWorker →
    # RoomPage 才进得了房间。这里用离屏 Qt 把这条链路整条跑一遍。
    if "--gui" in sys.argv:
        _gui_direct_join_checks(BASE_PORT + 180)
    else:
        print("SKIP  联机大厅 GUI 直连链路（加 --gui 启用，需要离屏 Qt）")

    # ── 清理（atexit 兜底：中途抛异常也不会漏临时文件/目录）──
    def _cleanup():
        shutil.rmtree(save_dir, ignore_errors=True)
        try:
            os.remove(src)
        except OSError:
            pass

    atexit.register(_cleanup)
    _cleanup()

    print("=" * 72)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


def _gui_direct_join_checks(port: int):
    """离屏 Qt：房主真实开房 → 大厅直连探测 → 加入 → 名单 → 离开 → 清理。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        import tempfile as _tf

        from PyQt6.QtCore import QEventLoop
        from PyQt6.QtWidgets import QApplication

        import utils.room_store as rs
        from ui.room_lobby_page import RoomLobbyPage
        from core.workers import RoomHostWorker
    except Exception as e:  # PyQt6 缺失 / 无离屏平台
        print(f"SKIP  联机大厅 GUI 直连链路（Qt 不可用: {e}）")
        return

    app = QApplication.instance() or QApplication(["verify_multiplayer"])

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.02)

    # 不要污染用户真实的 rooms.json
    tmpdir = _tf.mkdtemp(prefix="liw_verify_rooms_")
    saved_file, saved_rooms = rs.ROOMS_FILE, dict(rs.room_store._rooms)
    rs.ROOMS_FILE = os.path.join(tmpdir, "rooms.json")
    rs.room_store._rooms = {}

    room_id = "verifygui01"
    page = RoomLobbyPage()
    host = RoomHostWorker({
        "room_id": room_id, "room_name": "GUI验证房",
        "port": port, "host_ip": "127.0.0.1",
    }, username="房主")
    try:
        page.show()
        host.start()
        pump(1.0)
        check("GUI: 房主房间已启动", host.isRunning())

        page._on_direct_join("127.0.0.1", port)
        deadline = time.time() + 15
        while time.time() < deadline and page._stack.currentIndex() != 1:
            pump(0.2)
        check("GUI: 直连探测 → 加入 → 进入房间视图",
              page._stack.currentIndex() == 1,
              f"status={page._lobby._scan_status.text()!r}")
        cfg = page._current_room or {}
        check("GUI: 加入配置含 room_id / session_id / data_port",
              cfg.get("room_id") == room_id and bool(cfg.get("session_id"))
              and cfg.get("data_port") not in (None, 0),
              f"data_port={cfg.get('data_port')}")
        check("GUI: 房间内未泄露 session_id 到名单",
              all("session_id" not in p for p in (cfg.get("roster") or [])))

        rows = 0
        deadline = time.time() + 8
        while time.time() < deadline and rows < 2:
            pump(0.3)
            rows = page._room_view._peer_list.count()
        check("GUI: 成员名单含房主与自己（心跳下发）", rows >= 2, f"rows={rows}")

        page._on_leave_room()
        pump(1.5)
        check("GUI: 离开后回到大厅", page._stack.currentIndex() == 0)
        page.cleanup()
        pump(1.0)
    finally:
        host.cancel()
        host.wait(3000)
        check("GUI: 房主线程已停止", not host.isRunning())
        page.deleteLater()
        pump(0.3)
        rs.ROOMS_FILE, rs.room_store._rooms = saved_file, saved_rooms
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
