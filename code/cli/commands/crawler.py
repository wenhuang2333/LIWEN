"""Web crawler CLI commands."""

import sys
import json


def register(subparsers):
    p = subparsers.add_parser("crawl", help="运行网页爬虫")
    p.add_argument("--url", required=True, help="起始 URL")
    p.add_argument("--fields", help='字段定义 JSON: [{"name":"标题","selector":"h1","type":"css"}]')
    p.add_argument("--fields-file", help="从 JSON 文件读取字段定义")
    p.add_argument("--max-pages", type=int, default=1, help="最大页数 (默认: 1)")
    p.add_argument("--delay", type=float, default=2.0, help="请求间隔秒数 (默认: 2.0)")
    p.add_argument("--timeout", type=int, default=30, help="HTTP 超时秒数 (默认: 30)")
    p.add_argument("--retries", type=int, default=3, help="最大重试次数 (默认: 3)")
    p.add_argument("--proxy", help="代理地址 (如 http://127.0.0.1:8080)")
    p.add_argument("--next-selector", help="下一页链接的 CSS 选择器")
    p.add_argument("--url-pattern", help="URL 翻页模式 (如 https://example.com/page/{page})")
    p.add_argument("--container", help="数据容器 CSS 选择器")
    p.add_argument("-o", "--output", help="输出 JSON 文件路径")
    p.set_defaults(func=_handle_crawl)

    p = subparsers.add_parser("site-check", help="检测网站可爬取性")
    p.add_argument("--url", required=True, help="目标网站 URL")
    p.set_defaults(func=_handle_site_check)


def _handle_crawl(args):
    from core.crawler.engine import CrawlerEngine

    # Parse fields
    if args.fields_file:
        with open(args.fields_file, "r", encoding="utf-8") as f:
            fields = json.load(f)
    elif args.fields:
        fields = json.loads(args.fields)
    else:
        print("错误: 必须提供 --fields 或 --fields-file", file=sys.stderr)
        return 1

    # Build pagination config
    pagination = {
        "strategy": "link" if args.next_selector else ("pattern" if args.url_pattern else "none"),
        "max_pages": args.max_pages,
    }
    if args.next_selector:
        pagination["next_selector"] = args.next_selector
    if args.url_pattern:
        pagination["url_pattern"] = args.url_pattern

    options = {
        "delay": args.delay,
        "timeout": args.timeout,
        "retries": args.retries,
    }
    if args.proxy:
        options["proxy"] = args.proxy
    if args.container:
        options["container_selector"] = args.container

    all_data = []

    def data_row_cb(row):
        all_data.append(row)
        print(f"  [数据] {json.dumps(row, ensure_ascii=False)[:120]}")

    def log_cb(msg):
        print(f"  {msg}", file=sys.stderr)

    def progress_cb(pct):
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r[{bar}] {pct}%", end="", file=sys.stderr, flush=True)

    print(f"开始爬取: {args.url}", file=sys.stderr)
    print(f"字段: {[f['name'] for f in fields]}", file=sys.stderr)

    try:
        result = CrawlerEngine.run_crawl(
            start_url=args.url,
            fields=fields,
            pagination=pagination if pagination["strategy"] != "none" else None,
            options=options,
            progress_cb=progress_cb,
            log_cb=log_cb,
            data_row_cb=data_row_cb,
        )
        print(f"\n爬取完成: {len(result)} 条数据", file=sys.stderr)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"已保存到: {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"\n爬取失败: {e}", file=sys.stderr)
        return 1


def _handle_site_check(args):
    from core.crawler.robot_checker import SiteChecker
    import requests

    print(f"正在检测站点: {args.url}", file=sys.stderr)

    try:
        resp = requests.get(args.url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }, timeout=15, allow_redirects=True)
        html = resp.text[:500000]
        headers = dict(resp.headers)
        status = resp.status_code
    except Exception as e:
        print(f"无法访问站点: {e}", file=sys.stderr)
        return 1

    report = SiteChecker.check_site_feasibility(args.url, html, headers, status)

    print(f"\n站点可行性报告: {args.url}")
    print(f"  HTTP 状态: {status}")
    print(f"  SSL: {report.get('ssl', '未知')}")
    print(f"  robots.txt 允许: {report.get('robots_allowed', '未知')}")
    print(f"  爬取延迟: {report.get('crawl_delay', '无')}s")

    cms = report.get('cms', {})
    if cms:
        print(f"  CMS/框架: {json.dumps(cms, ensure_ascii=False)}")

    anti = report.get('anti_crawl', [])
    if anti:
        print(f"  反爬迹象: {', '.join(anti)}")
    else:
        print(f"  反爬迹象: 无")

    frameworks = report.get('js_frameworks', [])
    if frameworks:
        print(f"  JS 框架: {', '.join(frameworks)}")
    return 0
