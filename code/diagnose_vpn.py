r"""
VPN 诊断脚本 — 请以管理员身份运行:
   右键终端 → 「以管理员身份运行」
   在项目根目录执行:  python code/diagnose_vpn.py

This script tests the Rust lan_vpn core step by step and captures
detailed error information to help diagnose TUN device creation failures.
"""
import os
import sys

if sys.platform != "win32":
    print("VPN 诊断工具仅在 Windows 上可用")
    sys.exit(0)

import time
import glob
import ctypes
import subprocess

def _console_encoding() -> str:
    """子进程控制台输出的解码编码（跨平台）。

    项目规范禁止硬编码 ``encoding="gbk"``：中文 Windows 的 OEM 代码页通常是
    cp936（等价于 gbk），但非中文 Windows 是 cp437/cp850，Linux/macOS 是
    UTF-8。硬编码 gbk 在非中文系统上会把正常输出解成乱码。
    这里优先用系统首选编码，取不到时退回 utf-8。
    """
    import locale
    try:
        enc = locale.getpreferredencoding(False) or ""
    except Exception:
        enc = ""
    if not enc or enc.lower() in ("ascii", "us-ascii", "ansi_x3.4-1968"):
        # 中文 Windows 下 getpreferredencoding 有时返回 ascii（未调用
        # setlocale 时），此时 OEM 代码页才是正确的解码方式。
        try:
            import sys
            if sys.platform == "win32":
                import ctypes
                cp = ctypes.windll.kernel32.GetOEMCP()
                if cp:
                    return f"cp{cp}"
        except Exception:
            pass
        return "utf-8"
    return enc


# ── 项目根目录（本文件位于 code/ 下一级） ──
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Fix venv path for admin CMD (no venv activated) ──
_VENV_DIR = os.path.join(_PROJECT_ROOT, ".venv")
_VENV_CANDIDATES = [
    os.path.join(_VENV_DIR, "Lib", "site-packages"),   # Windows 布局
]
_VENV_CANDIDATES += glob.glob(
    os.path.join(_VENV_DIR, "lib", "python*", "site-packages")   # POSIX 布局
)
for _candidate in _VENV_CANDIDATES:
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)


RESET = "\033[0m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"


def header(msg):
    print(f"\n{CYAN}{'='*60}{RESET}")
    print(f"{CYAN}  {msg}{RESET}")
    print(f"{CYAN}{'='*60}{RESET}")


def ok(msg):
    print(f"  {GREEN}[OK]{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def err(msg):
    print(f"  {RED}[ERROR]{RESET} {msg}")


def info(msg):
    print(f"  {msg}")


# ── Step 1: Environment ──
header("Step 1: 运行环境")
info(f"Python: {sys.executable}")
info(f"工作目录: {os.getcwd()}")
info(f"sys.path 条目数: {len(sys.path)}")
try:
    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    if is_admin:
        ok("已获得管理员权限")
    else:
        warn("未获得管理员权限 — TUN 创建将会失败！")
except Exception as e:
    err(f"无法检查管理员权限: {e}")

# ── Step 2: Check wintun.dll ──
header("Step 2: wintun.dll 检查")
seen_dirs = set()
for search_dir in [
    os.getcwd(),
    _PROJECT_ROOT,
    os.path.dirname(sys.executable),
]:
    resolved = os.path.normpath(os.path.abspath(search_dir))
    if resolved in seen_dirs:
        continue
    seen_dirs.add(resolved)
    candidate = os.path.join(resolved, "wintun.dll")
    if os.path.exists(candidate):
        try:
            ps_cmd = (
                f'(Get-Item "{candidate}").VersionInfo | '
                f'Select-Object FileVersion, ProductVersion'
            )
            result = subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=5,
            )
            ok(f"找到: {candidate}")
            for line in result.stdout.strip().splitlines():
                if line.strip():
                    info(f"    {line.strip()}")
        except Exception:
            ok(f"找到: {candidate}")
        break
else:
    warn("未找到 wintun.dll！TUN 创建将会失败。")

# ── Step 3: Check existing adapters ──
header("Step 3: 现有网络适配器 (检查冲突)")
try:
    result = subprocess.run(
        ["netsh", "interface", "show", "interface"],
        capture_output=True, text=True, timeout=5,
        encoding=_console_encoding(), errors="replace",
    )
    for line in result.stdout.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in ["wintun", "tun ", "radmin", "tap-", "vpn"]):
            warn(f"可能存在冲突: {line.strip()}")
    if not any(kw in result.stdout.lower() for kw in ["wintun", "tun ", "radmin", "tap-", "vpn"]):
        ok("未发现明显冲突的网卡")
except Exception as e:
    err(f"无法检查: {e}")

# Check hidden adapters via PowerShell
try:
    result = subprocess.run(
        ["powershell", "-Command",
         "Get-NetAdapter -IncludeHidden | Where-Object { "
         "$_.InterfaceDescription -match 'Wintun|TUN' } | "
         "Format-Table Name, InterfaceDescription, Status -AutoSize"],
        capture_output=True, text=True, timeout=5,
        encoding="utf-8", errors="replace",
    )
    output = result.stdout.strip()
    if output and "Wintun" in output:
        warn("发现隐藏的 Wintun/TUN 适配器:")
        for line in output.splitlines():
            info(f"  {line}")
    else:
        ok("未发现隐藏的 Wintun/TUN 适配器")
except Exception:
    pass

# ── Step 4: Import lan_vpn ──
header("Step 4: 导入 lan_vpn 模块")
try:
    import lan_vpn
    ok(f"lan_vpn 已导入 ({lan_vpn.__file__})")
    VpnCore = lan_vpn.VpnCore
    PeerInfo = lan_vpn.PeerInfo
except ImportError as e:
    err(f"导入失败: {e}")
    info("尝试查找 lan_vpn 安装位置...")
    for p in sys.path:
        candidate = os.path.join(p, "lan_vpn")
        if os.path.isdir(candidate):
            info(f"  找到目录但无法导入: {candidate}")
            for f in os.listdir(candidate):
                info(f"    {f}")
    sys.exit(1)

# ── Step 5: Create VpnCore ──
header("Step 5: VpnCore 实例化")
try:
    core = VpnCore()
    pubkey = core.get_public_key_hex()
    peer_id = core.get_peer_id()
    ok(f"核心已创建 — 公钥: {pubkey[:32]}...")
    info(f"  PeerID: {peer_id if peer_id else '(空)'}")
    info(f"  is_running(): {core.is_running()}")
except Exception as e:
    err(f"创建失败: {e}")
    sys.exit(1)

# ── Step 6: Test LAN mode start ──
header("Step 6: 测试 LAN 模式启动 (start_lan)")
test_ip = "10.99.1.10"
test_port = 12345

info(f"参数: IP={test_ip}, Port={test_port}")
try:
    core.start_lan(test_ip, test_port)
    ok("start_lan() 返回成功")
    info(f"  is_running(): {core.is_running()}")
except Exception as e:
    err(f"start_lan() 异常: {e}")
    sys.exit(1)

# ── Step 7: Drain ALL events with full data ──
header("Step 7: 轮询事件 (等待 5 秒)")
start_time = time.time()
event_count = 0
error_events = []

while time.time() - start_time < 5.0:
    time.sleep(0.3)
    try:
        events = core.drain_events()
    except Exception as e:
        err(f"drain_events() 异常: {e}")
        break

    for evt in events:
        event_count += 1
        etype = evt.get("type", "?")
        if etype == "log":
            info(f"  [LOG] {evt.get('message', '')}")
        elif etype == "error":
            error_events.append(evt)
            err(f"  [ERROR] {evt.get('message', '')}")
            info(f"    完整事件: {evt}")
        else:
            info(f"  [{etype.upper()}] {evt}")

info(f"\n总事件数: {event_count}, 错误数: {len(error_events)}")

# ── Step 8: State check ──
header("Step 8: 运行状态检查")
try:
    tx, rx = core.traffic_stats()
    info(f"  流量: TX={tx}, RX={rx}")
except Exception as e:
    err(f"traffic_stats() 失败: {e}")

try:
    peers = core.peers()
    info(f"  在线节点: {len(peers)}")
    for p in peers:
        info(f"    - {p.peer_id} @ {p.virtual_ip} (connected={p.connected})")
except Exception as e:
    err(f"peers() 失败: {e}")

info(f"  is_running(): {core.is_running()}")

# ── Step 9: Stop and verify ──
header("Step 9: 停止并验证")
try:
    core.stop()
    info(f"  stop() 返回成功")
    info(f"  is_running() after stop: {core.is_running()}")
    if core.is_running():
        warn("stop() 成功但 is_running() 仍为 True — 核心状态异常！")
    else:
        ok("核心已完全停止")
except Exception as e:
    err(f"stop() 失败: {e}")

# ── Done ──
header("诊断完成")
if error_events:
    print(f"\n请将以上输出完整复制，特别是 {len(error_events)} 个 [ERROR] 事件。")
else:
    print(f"\n无错误事件。请将以上输出完整复制。")
try:
    input("\n按 Enter 退出...")
except (EOFError, KeyboardInterrupt):
    print()
