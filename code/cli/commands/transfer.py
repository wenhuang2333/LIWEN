"""LAN file transfer CLI commands."""

import sys
import signal
import threading

_running = True


def _on_sigint(signum, frame):
    global _running
    _running = False
    print("\n正在停止...", file=sys.stderr)


def register(subparsers):
    p = subparsers.add_parser("lan-send", help="通过局域网发送文件")
    p.add_argument("files", nargs="+", help="要发送的文件/目录")
    p.add_argument("-H", "--host", required=True, help="目标主机 IP")
    p.add_argument("-p", "--port", type=int, default=12345, help="目标端口 (默认: 12345)")
    p.add_argument("-n", "--name", default="", help="发送者名称")
    p.add_argument("--no-compress", action="store_true", help="不预先压缩")
    p.add_argument("--no-request", action="store_true", help="跳过请求/确认握手")
    p.set_defaults(func=_handle_send)

    p = subparsers.add_parser("lan-receive", help="监听并接收局域网文件 (Ctrl+C 停止)")
    p.add_argument("-p", "--port", type=int, default=12345, help="监听端口 (默认: 12345)")
    p.add_argument("-d", "--dest", default=".", help="保存目录 (默认: 当前目录)")
    p.add_argument("-y", "--yes", action="store_true", help="自动接受所有传输")
    p.set_defaults(func=_handle_receive)

    p = subparsers.add_parser("lan-scan", help="扫描局域网在线设备")
    p.add_argument("-t", "--timeout", type=float, default=3.0, help="扫描超时秒数 (默认: 3.0)")
    p.set_defaults(func=_handle_scan)

    p = subparsers.add_parser("lan-ping", help="测试局域网主机连通性")
    p.add_argument("-H", "--host", required=True, help="目标主机 IP")
    p.add_argument("-p", "--port", type=int, default=12345, help="目标端口 (默认: 12345)")
    p.add_argument("-t", "--timeout", type=int, default=3, help="超时秒数 (默认: 3)")
    p.set_defaults(func=_handle_ping)


def _progress_printer(pct: int):
    bar_len = 40
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r[{bar}] {pct}%", end="", file=sys.stderr, flush=True)


def _handle_send(args):
    from network.transfer import send_files

    def speed_cb(s):
        print(f"\r传输速度: {s}", end="", file=sys.stderr, flush=True)

    def status_cb(s):
        print(f"\n{s}", file=sys.stderr)

    try:
        send_files(
            args.files, args.host, args.port,
            compress_first=not args.no_compress,
            progress_callback=_progress_printer,
            speed_callback=speed_cb,
            status_callback=status_cb,
            send_request=not args.no_request,
            from_name=args.name,
        )
        print("\n发送完成")
        return 0
    except PermissionError as e:
        print(f"\n发送被拒绝: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\n发送失败: {e}", file=sys.stderr)
        return 1


def _handle_receive(args):
    from network.transfer import receive_files

    signal.signal(signal.SIGINT, _on_sigint)

    def request_cb(info, decision, event):
        print(f"\n收到传输请求: {info.get('from_name', '未知')} — {info.get('file_name', '未知')}", file=sys.stderr)
        print("输入 y 接受, n 拒绝: ", end="", file=sys.stderr, flush=True)
        try:
            answer = sys.stdin.readline().strip().lower()
        except EOFError:
            answer = "n"
        if answer == "y":
            decision["accepted"] = True
            event.set()
        else:
            decision["accepted"] = False
            event.set()

    def speed_cb(s):
        print(f"\r传输速度: {s}", end="", file=sys.stderr, flush=True)

    def status_cb(s):
        print(f"\n{s}", file=sys.stderr)

    print(f"正在监听端口 {args.port}... (按 Ctrl+C 停止)", file=sys.stderr)
    try:
        receive_files(
            args.port, args.dest,
            progress_callback=_progress_printer,
            speed_callback=speed_cb,
            status_callback=status_cb,
            request_callback=request_cb if not args.yes else None,
            auto_accept=args.yes,
        )
        return 0
    except Exception as e:
        print(f"\n接收失败: {e}", file=sys.stderr)
        return 1


def _handle_scan(args):
    from network.discovery import DiscoveryService

    svc = DiscoveryService()
    try:
        svc.start()
        print(f"正在扫描局域网 (超时 {args.timeout}s)...", file=sys.stderr)
        devices = svc.scan_now(timeout=args.timeout)
        svc.stop()

        if not devices:
            print("未发现在线设备")
        else:
            print(f"{'设备名称':<20} {'IP 地址':<16} {'端口'}")
            print("-" * 44)
            for device_id, info in devices.items():
                print(f"{info.get('name', device_id):<20} {info.get('ip', '?'):<16} {info.get('port', '?')}")
            print(f"\n共 {len(devices)} 台设备在线")
        return 0
    except Exception as e:
        print(f"扫描失败: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            svc.stop()
        except Exception:
            pass


def _handle_ping(args):
    from network.transfer import ping_host

    print(f"正在测试 {args.host}:{args.port}...")
    ok, msg = ping_host(args.host, args.port, args.timeout)
    if ok:
        print(f"✓ {msg}")
        return 0
    else:
        print(f"✗ {msg}", file=sys.stderr)
        return 1
