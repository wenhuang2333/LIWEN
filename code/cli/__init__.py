"""CLI entry point for 帽兜工具.

Usage: LIWENTool.exe <command> [<subcommand>] [--options]

If no command is given, the GUI launches as normal.
"""

import sys
import argparse
from typing import Optional

from cli.commands import (
    compress, transfer, room, crawler,
    http_cmd, ai_cmd, space, diagnostic, settings_cmd,
)

COMMAND_MODULES = [
    compress, transfer, room, crawler,
    http_cmd, ai_cmd, space, diagnostic, settings_cmd,
]


def build_parser() -> argparse.ArgumentParser:
    from utils.constants import APP_NAME_SLUG
    parser = argparse.ArgumentParser(
        prog=APP_NAME_SLUG,
        description="帽兜工具 (LIWEN Tool) CLI — 文件压缩/传输/爬虫/HTTP请求/AI/空间分析",
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    for mod in COMMAND_MODULES:
        mod.register(subparsers)

    return parser


def run_cli(argv: Optional[list] = None) -> int:
    """Parse CLI arguments and dispatch to the appropriate handler.

    Returns exit code: 0 on success, 1 on error.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    handler = getattr(args, 'func', None)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except Exception as e:
        from utils.logger import setup_logging, error as log_error
        setup_logging()
        log_error(f"CLI 命令执行失败: {e}")
        print(f"错误: {e}", file=sys.stderr)
        return 1
