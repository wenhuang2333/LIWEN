"""LAN room CLI commands."""

import sys
import signal
import threading
import time

_running = True


def _on_sigint(signum, frame):
    global _running
    _running = False


def register(subparsers):
    p = subparsers.add_parser("room-scan", help="扫描局域网联机房间")
    p.add_argument("-t", "--timeout", type=float, default=3.0, help="扫描超时秒数 (默认: 3.0)")
    p.set_defaults(func=_handle_room_scan)

    p = subparsers.add_parser("room-create", help="创建联机房间 (Ctrl+C 停止)")
    p.add_argument("-n", "--name", required=True, help="房间名称")
    p.add_argument("--password", default="", help="房间密码 (留空则无密码)")
    p.add_argument("-p", "--port", type=int, default=12345, help="游戏端口 (默认: 12345)")
    p.add_argument("--max-players", type=int, default=8, help="最大玩家数 (默认: 8)")
    p.set_defaults(func=_handle_room_create)

    p = subparsers.add_parser("room-probe", help="按 IP:端口 探测房间（无需 UDP 广播）")
    p.add_argument("-H", "--host", required=True, help="房间主机 IP")
    p.add_argument("-p", "--port", type=int, default=12345, help="房间端口 (默认: 12345)")
    p.set_defaults(func=_handle_room_probe)

    p = subparsers.add_parser("room-join", help="加入联机房间")
    p.add_argument("-H", "--host", required=True, help="房间主机 IP")
    p.add_argument("-p", "--port", type=int, default=12345, help="房间端口 (默认: 12345)")
    p.add_argument("--room-id", default="",
                   help="房间 ID (留空则先探测主机，自动获取)")
    p.add_argument("--password", default="", help="房间密码")
    p.set_defaults(func=_handle_room_join)


def _handle_room_scan(args):
    from network.room_protocol import RoomDiscoveryClient

    print(f"正在扫描局域网房间 (超时 {args.timeout}s)...", file=sys.stderr)
    client = RoomDiscoveryClient()
    rooms = client.scan(timeout=args.timeout)

    if not rooms:
        print("未发现联机房间")
    else:
        print(f"{'房间名称':<20} {'房间 ID':<38} {'主机 IP':<16} {'玩家数'}")
        print("-" * 80)
        for rid, info in rooms.items():
            name = info.get("room_name", "?")
            host_ip = info.get("host_ip", "?")
            players = info.get("player_count", 0)
            max_p = info.get("max_players", "?")
            print(f"{name:<20} {rid:<38} {host_ip:<16} {players}/{max_p}")
        print(f"\n共 {len(rooms)} 个房间")
    return 0


def _handle_room_create(args):
    import uuid
    from network.room_protocol import RoomHost
    from network.room_protocol import _hash_password, _get_local_ip

    if not 1024 <= args.port <= 65534:
        print(f"✗ 房间端口无效: {args.port}（需 1024-65534，加入服务使用 端口+1）",
              file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, _on_sigint)

    room_info = {
        "room_id": str(uuid.uuid4())[:8],
        "room_name": args.name,
        "port": args.port,
        "max_players": args.max_players,
    }
    if args.password:
        room_info["password_hash"] = _hash_password(args.password)

    host = RoomHost(room_info)
    host.start()

    print(f"房间已创建:")
    print(f"  名称: {args.name}")
    print(f"  ID: {room_info['room_id']}")
    print(f"  端口: {args.port}")
    print(f"  密码: {'有' if args.password else '无'}")
    local_ip = _get_local_ip()
    print(f"  本机 IP: {local_ip}")
    print(f"\n其他成员加入方式:")
    print(f"  局域网广播: python code/main.py room-scan")
    print(f"  直连(跨网段): python code/main.py room-join -H {local_ip} -p {args.port}")
    print(f"\n按 Ctrl+C 停止广播...")

    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        host.stop()
        print("\n房间已关闭")
    return 0


def _handle_room_probe(args):
    from network.room_protocol import RoomJoinClient

    print(f"正在探测 {args.host}:{args.port} ...", file=sys.stderr)
    ok, msg, info = RoomJoinClient.probe_room(args.host, args.port)
    if not ok:
        print(f"✗ 探测失败: {msg}", file=sys.stderr)
        return 1
    print(f"✓ 发现房间:")
    print(f"  名称: {info.get('room_name', '?')}")
    print(f"  ID: {info.get('room_id', '?')}")
    print(f"  主机: {info.get('host_name', '?')} ({info.get('host_ip', '?')}:{info.get('port', '?')})")
    print(f"  密码: {'有' if info.get('has_password') else '无'}")
    print(f"  玩家: {info.get('player_count', 0)}/{info.get('max_players', 8)}")
    return 0


def _handle_room_join(args):
    from network.room_protocol import RoomJoinClient

    room_id = args.room_id
    if not room_id:
        # 未给 room_id 时先探测：这正是"应用层直连"的用法——
        # 不依赖 UDP 广播，只要主机 IP:端口可达即可加入。
        print(f"未指定房间 ID，正在探测 {args.host}:{args.port} ...", file=sys.stderr)
        ok, msg, info = RoomJoinClient.probe_room(args.host, args.port)
        if not ok:
            print(f"✗ 探测失败: {msg}", file=sys.stderr)
            return 1
        room_id = info.get("room_id", "")
        if info.get("has_password") and not args.password:
            print("✗ 该房间需要密码，请用 --password 指定", file=sys.stderr)
            return 1

    print(f"正在加入房间 {room_id} 于 {args.host}:{args.port}...", file=sys.stderr)
    ok, msg, config = RoomJoinClient.join_room(
        args.host, args.port, room_id, args.password)

    if ok:
        print(f"✓ 加入成功!")
        print(f"  房间名: {config.get('room_name', '?')}")
        print(f"  Session ID: {config.get('session_id', '?')}")
        print(f"  主机: {config.get('host_ip', '?')}")
        return 0
    else:
        print(f"✗ 加入失败: {msg}", file=sys.stderr)
        return 1
