"""Disk space analysis CLI commands."""

import sys
import os
import json


def register(subparsers):
    p = subparsers.add_parser("space-scan", help="扫描磁盘空间使用情况")
    p.add_argument("path", help="要扫描的根路径 (如 C:\\\\ 或 C:\\\\Users)")
    p.add_argument("-o", "--output", help="输出 JSON 报告文件")
    p.set_defaults(func=_handle_space_scan)

    p = subparsers.add_parser("space-query", help="查询已扫描索引中的目录大小")
    p.add_argument("path", help="要查询的目录路径")
    p.set_defaults(func=_handle_space_query)


def _handle_space_scan(args):
    from core.file_index import IndexDatabase, _index_db_path, scan_directory
    from utils.helpers import format_size

    root_path = os.path.abspath(args.path)
    if not os.path.isdir(root_path):
        print(f"错误: 路径不存在或不是目录: {root_path}", file=sys.stderr)
        return 1

    db_path = _index_db_path(root_path)
    db = IndexDatabase(db_path)

    def progress_cb(scanned, total, elapsed, speed):
        bar_len = 30
        pct = int(scanned / total * 100) if total > 0 else 0
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r[{bar}] {pct}% — {format_size(scanned)} / {format_size(total)} — {format_size(speed)}/s", end="", file=sys.stderr, flush=True)

    def path_cb(p):
        print(f"\r扫描: {p}", end="", file=sys.stderr, flush=True)

    def status_cb(msg):
        print(f"\n{msg}", file=sys.stderr)

    print(f"开始扫描: {root_path}", file=sys.stderr)
    print(f"索引数据库: {db_path}", file=sys.stderr)

    try:
        result = scan_directory(
            root_path, db,
            progress_callback=progress_cb,
            scanning_path_callback=path_cb,
            status_callback=status_cb,
        )

        if result["cancelled"]:
            print("\n扫描已取消")
        else:
            print(f"\n扫描完成: {format_size(result['scanned_bytes'])} 已索引, "
                  f"{result['dir_count']} 个目录, {result['skipped_dirs']} 跳过, "
                  f"{result['elapsed']:.1f}s")

            # Print top-level results
            root_info = db.get_by_path(root_path)
            if root_info:
                children = db.get_children(root_info['id'])
                if children:
                    print(f"\n{'名称':<40} {'大小':>12} {'类型'}")
                    print("-" * 60)
                    for child in children[:20]:
                        ftype = "[DIR]" if child['is_dir'] else "[FILE]"
                        print(f"{child['name']:<40} {format_size(child['size']):>12} {ftype}")
                    if len(children) > 20:
                        print(f"... 还有 {len(children) - 20} 项")

            if args.output:
                _export_report(db, root_path, root_info, args.output)

        db.close()
        return 0
    except Exception as e:
        print(f"\n扫描失败: {e}", file=sys.stderr)
        try:
            db.close()
        except Exception:
            pass
        return 1


def _handle_space_query(args):
    from core.file_index import IndexDatabase, _index_db_path
    from utils.helpers import format_size

    root_path = os.path.abspath(args.path)
    # Find the drive letter
    drive = root_path[0].upper()
    db_path = _index_db_path(drive + ":\\")

    if not os.path.exists(db_path):
        print(f"错误: 未找到扫描索引 — 请先运行 'space-scan {drive}:\\'", file=sys.stderr)
        return 1

    db = IndexDatabase(db_path)
    try:
        entry = db.get_by_path(root_path)
        if entry is None:
            print(f"未找到路径: {root_path}")
            return 1

        print(f"路径: {entry['path']}")
        print(f"大小: {format_size(entry['size'])}")
        print(f"类型: {'目录' if entry['is_dir'] else '文件'}")
        if entry['is_dir']:
            print(f"文件数: {entry['file_count']}")
            print(f"子目录数: {entry['dir_count']}")

            children = db.get_children(entry['id'])
            if children:
                print(f"\n子项:")
                print(f"{'名称':<40} {'大小':>12} {'类型'}")
                print("-" * 60)
                for child in children:
                    ftype = "[DIR]" if child['is_dir'] else "[FILE]"
                    print(f"{child['name']:<40} {format_size(child['size']):>12} {ftype}")

        return 0
    finally:
        db.close()


def _export_report(db, root_path, root_info, output_path):
    from utils.helpers import format_size

    report = {
        "root_path": root_path,
        "total_size": root_info['size'] if root_info else 0,
        "scanned_at": db.get_all_meta().get("scan_time", ""),
    }
    if root_info:
        children = db.get_children(root_info['id'])
        report["items"] = [
            {"name": c['name'], "size": c['size'], "is_dir": bool(c['is_dir']),
             "file_count": c.get('file_count', 0), "dir_count": c.get('dir_count', 0)}
            for c in children
        ]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"报告已保存到: {output_path}")
