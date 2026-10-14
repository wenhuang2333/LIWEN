"""Diagnostic and repair CLI commands."""

import sys


def register(subparsers):
    p = subparsers.add_parser("diagnostic-check", help="运行系统诊断检测")
    p.add_argument("--mode", default="all", choices=["lan", "basic", "all"],
                   help="检测模式 (默认: all — 局域网+基础)")
    p.set_defaults(func=_handle_check)


def _handle_check(args):
    from ui.diagnostic_page import LAN_CHECKS, BASIC_CHECKS, CheckResult
    import time

    checks_to_run = []
    if args.mode in ("lan", "all"):
        checks_to_run.extend(LAN_CHECKS)
    if args.mode in ("basic", "all"):
        checks_to_run.extend(BASIC_CHECKS)

    print(f"运行诊断检测 (模式: {args.mode}, 共 {len(checks_to_run)} 项)...\n")
    print(f"{'状态':<6} {'类别':<12} {'检测项':<24} {'详情'}")
    print("-" * 100)

    passed = 0
    failed = 0
    warned = 0

    def log_cb(msg):
        pass  # suppress per-check logs in CLI mode

    for check_id, category, name, func, repair_id in checks_to_run:
        t0 = time.time()
        try:
            result = func(log_cb)
        except Exception as e:
            result = CheckResult(
                status="fail",
                detail=f"检查执行异常: {type(e).__name__}: {e}",
                suggestions=["诊断框架内部错误，请联系开发者"])
        result.check_id = check_id
        result.category = category
        result.name = name
        result.repair_id = repair_id
        elapsed = time.time() - t0

        status_icon = {"pass": "✓", "fail": "✗", "warning": "!"}.get(result.status, "?")
        print(f" {status_icon:<5} {category:<12} {name:<24} {result.detail} ({elapsed:.1f}s)")

        if result.status == "pass":
            passed += 1
        elif result.status == "fail":
            failed += 1
            for s in result.suggestions:
                print(f"       → {s}")
        else:
            warned += 1

    print(f"\n{'─' * 60}")
    print(f"结果: {passed} 通过, {failed} 失败, {warned} 警告")

    if failed == 0 and warned == 0:
        print("系统运行状态正常 ✓")
    elif failed > 0:
        print(f"发现 {failed} 个问题 — 可使用 'diagnostic-repair' 或启动 GUI 进行修复")

    return 0 if failed == 0 else 1
