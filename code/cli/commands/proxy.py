"""反向代理 CLI 命令。

命令组：

* ``proxy-list``   —— 列出已保存的规则（``--json`` 输出原始 JSON）
* ``proxy-add``    —— 新增/覆盖一条规则（``--listen`` / ``--target`` 必填）
* ``proxy-rm``     —— 按 ``--id`` 或 ``--name`` 删除规则
* ``proxy-serve``  —— 前台运行反向代理（打印访问日志，Ctrl+C 停止）

设计规格见 ``obj/开发文档.md`` §19。
"""

import json
import sys
import time


def register(subparsers):
    p = subparsers.add_parser("proxy-list", help="列出反向代理规则")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=_handle_list)

    p = subparsers.add_parser("proxy-add", help="新增或覆盖一条反向代理规则")
    p.add_argument("--name", default="", help="规则名称")
    p.add_argument("--listen", required=True, help="监听地址，如 127.0.0.1:8888")
    p.add_argument("--target", required=True, help="上游目标，如 https://api.example.com")
    p.add_argument("--strip", default="", help="剥离的入口路径前缀，如 /api")
    p.add_argument("--add", default="", dest="add_prefix", help="转发前追加的路径前缀")
    p.add_argument("--host", default="", dest="host_header", help="覆盖 Host 头")
    p.add_argument("--match-host", default="", dest="match_host",
                   help="按 Host 头分发（支持 *.example.com）；留空 = 该端口的兜底规则")
    p.add_argument("--insecure", action="store_true", help="不校验上游 TLS 证书")
    p.add_argument("--tls-cert", default="", help="入口 TLS 证书（PEM）")
    p.add_argument("--tls-key", default="", help="入口 TLS 私钥（PEM）")
    p.add_argument("--header", action="append", default=[],
                   help="请求头改写 KEY:VALUE（可重复；VALUE 留空表示删除该头）")
    p.add_argument("--response-header", action="append", default=[],
                   help="响应头改写 KEY:VALUE（可重复）")
    p.add_argument("--disabled", action="store_true", help="保存为不启用")
    p.add_argument("--timeout", type=float, default=None, help="读写超时秒数")
    p.set_defaults(func=_handle_add)

    p = subparsers.add_parser("proxy-rm", help="删除一条反向代理规则")
    p.add_argument("--id", dest="rule_id", default="", help="规则 id")
    p.add_argument("--name", default="", help="规则名称（与 --id 二选一）")
    p.set_defaults(func=_handle_remove)

    p = subparsers.add_parser("proxy-serve", help="前台运行反向代理（Ctrl+C 停止）")
    p.add_argument("--listen", default="", help="监听地址，如 127.0.0.1:8888")
    p.add_argument("--target", default="", help="上游目标，如 https://api.example.com")
    p.add_argument("--id", dest="rule_id", default="", help="使用已保存规则（按 id）")
    p.add_argument("--name", default="", help="使用已保存规则（按名称）")
    p.add_argument("--rule", default="", help="从 JSON 文件读取规则（数组或单条对象）")
    p.add_argument("--strip", default="", help="剥离的入口路径前缀，如 /api")
    p.add_argument("--add", default="", dest="add_prefix", help="转发前追加的路径前缀")
    p.add_argument("--host", default="", dest="host_header", help="覆盖 Host 头")
    p.add_argument("--match-host", default="", dest="match_host",
                   help="按 Host 头分发（支持 *.example.com）；留空 = 该端口的兜底规则")
    p.add_argument("--tls-cert", default="", help="入口 TLS 证书（PEM）")
    p.add_argument("--tls-key", default="", help="入口 TLS 私钥（PEM）")
    p.add_argument("--header", action="append", default=[],
                   help="请求头改写 KEY:VALUE（可重复；VALUE 留空表示删除该头）")
    p.add_argument("--response-header", action="append", default=[],
                   help="响应头改写 KEY:VALUE（可重复）")
    p.add_argument("--timeout", type=float, default=None, help="读写超时秒数")
    p.add_argument("--insecure", action="store_true", help="不校验上游 TLS 证书")
    p.add_argument("--duration", type=float, default=0.0, help="运行指定秒数后自动退出（默认一直运行）")
    p.add_argument("--quiet", action="store_true", help="不打印每条访问日志")
    p.set_defaults(func=_handle_serve)


def _parse_listen(text: str) -> tuple:
    """``host:port`` → ``(host, port)``；IPv6 用 ``[::1]:8888``。"""
    text = (text or "").strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port_text = rest.lstrip(":")
        return host, port_text
    host, _, port_text = text.rpartition(":")
    return host, port_text


def _kv_list(items, flag: str) -> dict:
    out = {}
    for item in items or []:
        key, sep, value = item.partition(":")
        if not sep or not key.strip():
            raise ValueError(f"{flag} 需要 KEY:VALUE 形式，收到 {item!r}")
        out[key.strip()] = value.strip()
    return out


def _build_rule_from_args(args) -> dict:
    from network.reverse_proxy import normalize_rule
    host, port_text = _parse_listen(args.listen)
    if not host:
        host = "127.0.0.1"
    if not port_text.isdigit():
        raise ValueError(f"--listen 需要 host:port 形式，收到 {args.listen!r}")
    rule = {
        "name": args.name or f"{host}:{port_text}",
        "listen_host": host,
        "listen_port": int(port_text),
        "upstream": args.target,
        "strip_prefix": args.strip,
        "add_prefix": args.add_prefix,
        "host_header": getattr(args, "host_header", "") or getattr(args, "host", ""),
        "match_host": getattr(args, "match_host", ""),
        "verify_tls": not args.insecure,
        "tls_cert": args.tls_cert,
        "tls_key": args.tls_key,
        "request_headers": _kv_list(getattr(args, "header", []), "--header"),
        "response_headers": _kv_list(getattr(args, "response_header", []), "--response-header"),
    }
    if getattr(args, "disabled", False):
        rule["enabled"] = False
    if getattr(args, "timeout", None):
        rule["read_timeout"] = args.timeout
    return normalize_rule(rule)


def _handle_list(args) -> int:
    from utils.proxy_store import proxy_rule_store
    rules = proxy_rule_store.all()
    if args.json:
        print(json.dumps(rules, ensure_ascii=False, indent=2))
        return 0
    if not rules:
        print("（没有已保存的反向代理规则）")
        return 0
    from network.reverse_proxy import rule_display
    print(f"{'id':<12} {'启用':<5} {'Host 匹配':<22} {'监听/上游'}")
    for rule in rules:
        flag = "是" if rule.get("enabled") else "否"
        name = rule.get("name") or "-"
        match = rule.get("match_host") or "*（兜底）"
        print(f"{rule['id']:<12} {flag:<5} {match:<22} {rule_display(rule)}  [{name}]")
    return 0


def _handle_add(args) -> int:
    from utils.proxy_store import proxy_rule_store
    try:
        rule = _build_rule_from_args(args)
    except Exception as e:  # noqa: BLE001 — 规则非法要如实报错
        print(f"规则非法: {e}", file=sys.stderr)
        return 1
    proxy_rule_store.add(rule)
    from network.reverse_proxy import rule_display
    print(f"已保存规则 {rule['id']}: {rule_display(rule)}"
          f"{'' if rule.get('enabled') else '（未启用）'}")
    return 0


def _handle_remove(args) -> int:
    from utils.proxy_store import proxy_rule_store
    rule_id = args.rule_id
    if not rule_id and args.name:
        for rule in proxy_rule_store.all():
            if rule.get("name") == args.name:
                rule_id = rule["id"]
                break
        if not rule_id:
            print(f"未找到名称为 {args.name!r} 的规则", file=sys.stderr)
            return 1
    if not rule_id:
        print("需要 --id 或 --name", file=sys.stderr)
        return 1
    if proxy_rule_store.remove(rule_id):
        print(f"已删除规则 {rule_id}")
        return 0
    print(f"未找到规则 {rule_id}", file=sys.stderr)
    return 1


def _resolve_serve_rules(args) -> list:
    from network.reverse_proxy import normalize_rule
    from utils.proxy_store import proxy_rule_store
    if args.rule:
        with open(args.rule, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else [data]
        return [normalize_rule(item) for item in items]
    if args.listen or args.target:
        if not (args.listen and args.target):
            raise ValueError("--listen 与 --target 必须同时提供")
        return [_build_rule_from_args(args)]
    if getattr(args, "rule_id", "") or args.name:
        rules = proxy_rule_store.all()
        rid = getattr(args, "rule_id", "")
        chosen = None
        for rule in rules:
            if (rid and rule["id"] == rid) or (args.name and rule.get("name") == args.name):
                chosen = rule
                break
        if chosen is None:
            raise ValueError(f"未找到规则（id={rid!r} name={args.name!r}）")
        # 同一监听端口上的其它启用规则一并拉起：它们共用一个监听器、按 Host 分发，
        # 只启动其中一条会让其它域名的请求落到兜底或 404，与用户预期不符。
        key = (chosen["listen_host"], chosen["listen_port"])
        group = [chosen]
        for rule in rules:
            if rule["id"] == chosen["id"]:
                continue
            if (rule["listen_host"], rule["listen_port"]) == key and rule.get("enabled", True):
                group.append(rule)
        return group
    raise ValueError("需要 --listen/--target、--id/--name 或 --rule 之一")


def _handle_serve(args) -> int:
    from network.reverse_proxy import ReverseProxyServer, ProxyStartError, rule_display
    from utils.logger import setup_logging, info, error as log_error
    setup_logging()
    try:
        rules = _resolve_serve_rules(args)
    except Exception as e:  # noqa: BLE001
        print(f"启动失败: {e}", file=sys.stderr)
        return 1

    server = ReverseProxyServer(rules=rules)
    try:
        result = server.start()
    except ProxyStartError as e:
        print(f"启动失败: {e}", file=sys.stderr)
        return 1

    for item in result["started"]:
        print(f"✓ 监听 {item['listen']} → {item['upstream']}"
              f"{'（TLS 终止）' if item.get('tls') else ''}")
    for item in result["failed"]:
        print(f"✗ {item['listen']}: {item['error']}", file=sys.stderr)
    info(f"proxy-serve 启动 {len(result['started'])} 条规则")

    if not args.quiet:
        print("（Ctrl+C 停止；下面实时打印访问日志）")

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while server.running:
            for event in server.drain_events(200):
                if args.quiet:
                    continue
                status = event.get("status")
                error = event.get("error") or ""
                print(f"[{time.strftime('%H:%M:%S', time.localtime(event.get('ts', time.time())))}]"
                      f" {event.get('method', '?')} {event.get('target', '?')}"
                      f" → {status} {event.get('ms', 0)}ms"
                      f" {event.get('bytes_out', 0)}B"
                      f"{'  ' + error if error else ''}")
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        summary = server.stop()
        print(f"已停止（监听 {summary['stopped']} 条，残留线程 {summary['leaked_threads']}）")
    return 0 if not result["failed"] else 1
