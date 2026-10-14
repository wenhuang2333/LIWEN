"""HTTP request CLI command."""

import sys
import json


def register(subparsers):
    p = subparsers.add_parser("http-request", help="发送 HTTP 请求 (类似 curl)")
    p.add_argument("-X", "--method", default="GET",
                   choices=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                   help="HTTP 方法 (默认: GET)")
    p.add_argument("--url", required=True, help="请求 URL")
    p.add_argument("--params", help='查询参数 JSON: {"key":"value"}')
    p.add_argument("--headers", help='请求头 JSON: {"Content-Type":"application/json"}')
    p.add_argument("--body-type", default="none",
                   choices=["none", "form-data", "x-www-form-urlencoded", "raw", "binary"],
                   help="请求体类型 (默认: none)")
    p.add_argument("--body", help="请求体内容 (JSON字符串 或 纯文本)")
    p.add_argument("--body-file", help="从文件读取请求体")
    p.add_argument("--timeout", type=int, default=30, help="超时秒数 (默认: 30)")
    p.add_argument("--no-redirect", action="store_true", help="禁止重定向")
    p.add_argument("--no-ssl", action="store_true", help="忽略 SSL 证书验证")
    p.add_argument("--proxy", help="代理地址 (如 http://127.0.0.1:8080)")
    p.add_argument("-o", "--output", help="将响应体写入文件")
    p.add_argument("--include-headers", action="store_true", help="输出中包含响应头")
    p.set_defaults(func=_handle_http)


def _handle_http(args):
    from core.http_client import send_http_request

    params = {}
    if args.params:
        params = json.loads(args.params)

    headers = {}
    if args.headers:
        headers = json.loads(args.headers)

    body_config = {"type": args.body_type}
    if args.body_type == "raw":
        raw_text = args.body or ""
        if args.body_file:
            with open(args.body_file, "r", encoding="utf-8") as f:
                raw_text = f.read()
        body_config["raw_text"] = raw_text
        # Auto-detect content type
        stripped = raw_text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            body_config["content_type"] = "json"
        elif stripped.startswith("<"):
            body_config["content_type"] = "xml"
        else:
            body_config["content_type"] = "text"
    elif args.body_type == "form-data" and args.body:
        body_config["form_data"] = json.loads(args.body)
    elif args.body_type == "x-www-form-urlencoded" and args.body:
        body_config["form_data"] = json.loads(args.body)
    elif args.body_type == "binary":
        body_config["file_path"] = args.body_file or args.body or ""

    try:
        result = send_http_request(
            method=args.method,
            url=args.url,
            params=params,
            headers=headers,
            body_config=body_config,
            timeout=args.timeout,
            allow_redirects=not args.no_redirect,
            verify_ssl=not args.no_ssl,
            proxy=args.proxy,
        )

        if not result:
            print("请求未返回结果", file=sys.stderr)
            return 1

        # Status line
        status_icon = "✓" if 200 <= result["status_code"] < 300 else "✗"
        print(f"{status_icon} HTTP {result['status_code']} {result['reason']} — {result['time_ms']}ms — {result['size']} bytes")

        if args.include_headers:
            print(f"\n响应头:")
            for k, v in result.get("headers", {}).items():
                print(f"  {k}: {v}")

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(result.get("body_text", ""))
            print(f"响应体已写入: {args.output}")
        else:
            body = result.get("body_text", "")
            if len(body) > 100 * 1024:
                print(f"\n响应体 (截断, 共 {len(body)} 字符):")
                print(body[:50000])
                print(f"\n... (省略 {len(body) - 50000} 字符) ...")
            else:
                print(f"\n{body}")

        return 0 if 200 <= result["status_code"] < 400 else 1

    except Exception as e:
        print(f"请求失败: {e}", file=sys.stderr)
        return 1
