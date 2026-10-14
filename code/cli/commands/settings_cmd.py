"""Settings management CLI commands."""

import sys
import json


def register(subparsers):
    p = subparsers.add_parser("settings-get", help="读取设置项")
    p.add_argument("key", help="设置键名")
    p.set_defaults(func=_handle_get)

    p = subparsers.add_parser("settings-set", help="写入设置项")
    p.add_argument("key", help="设置键名")
    p.add_argument("value", help="设置值 (JSON 格式: 字符串需加引号, 如 '\"hello\"')")
    p.set_defaults(func=_handle_set)

    p = subparsers.add_parser("settings-list", help="列出所有设置")
    p.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    p.set_defaults(func=_handle_list)


def _handle_get(args):
    from utils.settings import settings

    settings.load()
    value = settings.get(args.key)
    if value is None:
        print(f"null  (键 '{args.key}' 未设置)")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def _handle_set(args):
    from utils.settings import settings

    settings.load()
    # Parse value as JSON for correct typing
    try:
        value = json.loads(args.value)
    except (json.JSONDecodeError, ValueError):
        value = args.value

    settings.set(args.key, value)
    settings.save()
    print(f"已设置: {args.key} = {json.dumps(value, ensure_ascii=False)}")
    return 0


def _handle_list(args):
    from utils.settings import settings

    settings.load()
    all_settings = settings.get_all()

    if args.json:
        print(json.dumps(all_settings, ensure_ascii=False, indent=2))
    else:
        for key, value in sorted(all_settings.items()):
            # Mask sensitive values
            if "key" in key.lower() or "password" in key.lower() or "secret" in key.lower():
                display = "***" if value else "(空)"
            else:
                display = json.dumps(value, ensure_ascii=False)
            print(f"{key} = {display}")
    return 0
