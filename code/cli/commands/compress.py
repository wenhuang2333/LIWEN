"""File compression/decompression CLI commands."""

import sys
import os
import json


def register(subparsers):
    # compress
    p = subparsers.add_parser("compress", help="压缩文件/目录为归档")
    p.add_argument("paths", nargs="+", help="要压缩的文件/目录路径")
    p.add_argument("-o", "--output", required=True, help="输出归档路径")
    p.add_argument("-f", "--format", default="zip",
                   choices=["zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "zhans"],
                   help="压缩格式 (默认: zip)")
    p.add_argument("-p", "--password", help="归档密码")
    p.add_argument("-V", "--volume-size", type=int, help="分卷大小 (字节, 仅7z)")
    p.add_argument("-l", "--level", type=int, default=0, choices=list(range(10)),
                   help="压缩级别 0=存储 9=最大, 默认0")
    p.add_argument("--seed-code", help="自定义压缩种子码 (仅zhans格式)")
    p.set_defaults(func=_handle_compress)

    # decompress
    p = subparsers.add_parser("decompress", help="解压归档文件")
    p.add_argument("archive", help="归档文件路径")
    p.add_argument("-d", "--dest", default=".", help="解压目标目录 (默认: 当前目录)")
    p.add_argument("-p", "--password", help="归档密码")
    p.add_argument("--seed-code", help="自定义解压种子码 (仅zhans格式)")
    p.set_defaults(func=_handle_decompress)

    # preview
    p = subparsers.add_parser("preview", help="列出归档内容 (不解压)")
    p.add_argument("archive", help="归档文件路径")
    p.set_defaults(func=_handle_preview)

    # extract single file
    p = subparsers.add_parser("extract", help="从归档中提取单个文件")
    p.add_argument("archive", help="归档文件路径")
    p.add_argument("member", help="归档内文件路径/名称")
    p.add_argument("-d", "--dest", default=".", help="输出目录 (默认: 当前目录)")
    p.add_argument("-p", "--password", help="归档密码")
    p.set_defaults(func=_handle_extract)

    # check (detect format)
    p = subparsers.add_parser("check", help="检测归档格式和加密状态")
    p.add_argument("archive", help="归档文件路径")
    p.set_defaults(func=_handle_check)


def _progress_printer(pct: int):
    bar_len = 40
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r[{bar}] {pct}%", end="", file=sys.stderr, flush=True)


def _handle_compress(args):
    from core.formats import FormatRegistry
    from utils.helpers import format_size

    fmt_name = "custom" if args.format == "zhans" else args.format
    handler = FormatRegistry.get_by_name(fmt_name)
    if handler is None or not handler.can_compress():
        print(f"错误: 不支持的格式 '{args.format}'", file=sys.stderr)
        return 1

    kwargs = {
        "password": args.password,
        "volume_size": args.volume_size,
        "progress_callback": _progress_printer,
        "compress_level": args.level,
    }
    if args.seed_code:
        kwargs["seed_code"] = args.seed_code

    try:
        result = handler.compress(args.paths, args.output, **kwargs)
        print(f"\n压缩完成: {result['path']} ({format_size(result.get('size', 0))}, {result.get('file_count', 0)} 个文件)")
        return 0
    except Exception as e:
        print(f"\n压缩失败: {e}", file=sys.stderr)
        return 1


def _handle_decompress(args):
    from core.formats import FormatRegistry

    handler = FormatRegistry.detect(args.archive)
    if handler is None or not handler.can_decompress():
        print(f"错误: 无法处理此格式: {args.archive}", file=sys.stderr)
        return 1

    kwargs = {"password": args.password, "progress_callback": _progress_printer}
    if args.seed_code:
        kwargs["seed_code"] = args.seed_code

    try:
        result = handler.decompress(args.archive, args.dest, **kwargs)
        print(f"\n解压完成: {args.dest} ({result.get('file_count', 0)} 个文件)")
        return 0
    except Exception as e:
        print(f"\n解压失败: {e}", file=sys.stderr)
        return 1


def _handle_preview(args):
    from core.formats import FormatRegistry
    from utils.helpers import format_size

    handler = FormatRegistry.detect(args.archive)
    if handler is None:
        print(f"错误: 不支持的格式", file=sys.stderr)
        return 1
    try:
        contents = handler.list_contents(args.archive)
        if not contents:
            print("归档为空")
        else:
            print(f"{'名称':<50} {'大小':>12} {'类型'}")
            print("-" * 70)
            for item in contents:
                ftype = "[DIR]" if item.get('is_dir') else "[FILE]"
                print(f"{item['name']:<50} {format_size(item.get('size', 0)):>12} {ftype}")
            print(f"\n共 {len(contents)} 项")
        return 0
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def _handle_extract(args):
    from core.formats import FormatRegistry

    handler = FormatRegistry.detect(args.archive)
    if handler is None:
        print(f"错误: 不支持的格式", file=sys.stderr)
        return 1
    try:
        out_path = handler.extract_single(args.archive, args.member, args.dest, password=args.password)
        print(f"已提取: {out_path}")
        return 0
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def _handle_check(args):
    from core.formats import FormatRegistry
    from utils.helpers import format_size

    handler = FormatRegistry.detect(args.archive)
    if handler is None:
        print("格式: 未知")
        return 1

    fsize = os.path.getsize(args.archive) if os.path.isfile(args.archive) else 0
    encrypted = handler.is_encrypted(args.archive)
    print(f"格式: {handler.format_name()}")
    print(f"大小: {format_size(fsize)}")
    print(f"加密: {'是' if encrypted else '否'}")
    return 0
