"""AI chat CLI commands."""

import sys


def register(subparsers):
    p = subparsers.add_parser("ai-chat", help="发送单条消息给 AI (非流式)")
    p.add_argument("-m", "--message", required=True, help="用户消息内容")
    p.add_argument("-s", "--system", help="系统提示词")
    p.add_argument("--provider", help="AI 提供商名称 (从设置中读取配置, 不指定则用默认)")
    p.set_defaults(func=_handle_ai_chat)

    p = subparsers.add_parser("ai-test-connection", help="测试 AI API 连接")
    p.add_argument("--provider", help="AI 提供商名称 (不指定则用默认)")
    p.set_defaults(func=_handle_ai_test)


def _handle_ai_chat(args):
    from utils.settings import settings
    from core.ai_client import AiClient, build_messages

    settings.load()

    if args.provider:
        cfg = settings.get_provider_config(args.provider)
        if not cfg or not cfg.get("api_url"):
            print(f"错误: 未找到提供商 '{args.provider}' 的配置", file=sys.stderr)
            return 1
        api_url = cfg["api_url"]
        api_key = cfg.get("api_key", "")
        model = cfg.get("model", "")
    else:
        api_url, api_key, model, temperature, max_tokens, system_prompt = settings.get_active_ai_config()

    if not api_url or not model:
        print("错误: AI 配置不完整 (缺少 api_url 或 model), 请在 GUI 设置中配置", file=sys.stderr)
        return 1

    system_prompt = args.system or ""
    messages = build_messages(system_prompt, [{"role": "user", "content": args.message}])

    client = AiClient(
        base_url=api_url,
        api_key=api_key,
        model=model,
    )

    print("正在等待 AI 回复...", file=sys.stderr)
    try:
        # Use non-streaming for CLI
        response = client.chat(messages)
        print(response)
        return 0
    except Exception as e:
        print(f"AI 请求失败: {e}", file=sys.stderr)
        return 1


def _handle_ai_test(args):
    from utils.settings import settings
    from core.ai_connection_test import test_ai_connection

    settings.load()

    if args.provider:
        cfg = settings.get_provider_config(args.provider)
        if not cfg or not cfg.get("api_url"):
            print(f"错误: 未找到提供商 '{args.provider}' 的配置", file=sys.stderr)
            return 1
        config = {
            "api_url": cfg["api_url"],
            "api_key": cfg.get("api_key", ""),
            "model": cfg.get("model", ""),
        }
    else:
        api_url, api_key, model, _, _, _ = settings.get_active_ai_config()
        config = {"api_url": api_url, "api_key": api_key, "model": model}

    def log_cb(msg):
        print(f"  {msg}")

    def status_cb(msg):
        print(f"[{msg}]", file=sys.stderr)

    print("AI 连接测试", file=sys.stderr)
    print("-" * 40, file=sys.stderr)

    success, message = test_ai_connection(
        config,
        log_callback=log_cb,
        status_callback=status_cb,
    )

    print("-" * 40, file=sys.stderr)
    if success:
        print(f"✓ {message}")
    else:
        print(f"✗ {message}", file=sys.stderr)
    return 0 if success else 1
