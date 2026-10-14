"""
Pure AI connection test function — extracted from AiConnectionTestWorker
so the logic can be used from CLI without any Qt dependency.
"""

import time
import socket
from urllib.parse import urlparse
from typing import Optional, Callable, Tuple

import requests


def test_ai_connection(
    config: dict,
    cancel_check: Optional[Callable[[], bool]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    status_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """Test AI API connectivity end-to-end.

    Steps: validate URL → DNS resolution → TCP connect → send minimal API request.

    Args:
        config: {"api_url": str, "api_key": str, "model": str}
        cancel_check: Return True to abort.
        log_callback: Called with diagnostic log messages.
        status_callback: Called with short status labels.

    Returns:
        (success: bool, message: str)
    """

    def _cancelled():
        return cancel_check() if cancel_check else False

    def _log(msg: str):
        if log_callback:
            log_callback(msg)

    def _status(msg: str):
        if status_callback:
            status_callback(msg)

    api_url = config.get("api_url", "").strip()
    api_key = config.get("api_key", "").strip()
    model = config.get("model", "").strip()

    if _cancelled():
        return False, "已取消"

    _log(f"目标地址: {api_url}")
    _log(f"模型名称: {model or '(未配置)'}")

    # Step 1: Validate URL
    if not api_url:
        _log("✗ API 地址为空")
        return False, "API 地址未配置"

    if not (api_url.startswith("http://") or api_url.startswith("https://")):
        _log(f"✗ 地址格式无效: {api_url}")
        return False, "API 地址格式无效（需以 http:// 或 https:// 开头）"

    if not model:
        _log("✗ 模型名称未配置")
        return False, "模型名称未配置"

    # Step 2: Parse URL
    try:
        parsed = urlparse(api_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception as e:
        _log(f"✗ 无法解析 URL: {e}")
        return False, f"无法解析 API 地址: {e}"

    # Step 3: DNS resolution
    _status("正在解析 DNS…")
    _log(f"解析 DNS: {host}…")
    try:
        addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        ip = addrs[0][4][0] if addrs else None
        _log(f"  DNS 解析成功: {host} → {ip}")
    except socket.gaierror as e:
        _log(f"✗ DNS 解析失败: {host} — {e}")
        return False, f"DNS 解析失败: {host}\n请检查网络连接和 API 地址是否正确"

    # Step 4: TCP connection test
    _status("正在测试 TCP 连接…")
    _log(f"连接 {host}:{port}…")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    try:
        sock.connect((ip, port))
        sock.close()
        _log(f"  TCP 连接成功 {host}:{port}")
    except socket.timeout:
        _log(f"✗ TCP 连接超时 {host}:{port}")
        return False, f"无法连接到 {host}:{port}\n连接超时（8秒）\n请检查防火墙或代理设置"
    except OSError as e:
        _log(f"✗ TCP 连接失败: {e}")
        return False, f"无法连接到 {host}:{port}\n{e}"
    finally:
        try:
            sock.close()
        except Exception:
            pass

    # Step 5: Send minimal API request
    _status("正在发送测试请求…")
    _log("发送最小化 API 测试请求…")

    try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 1,
            "stream": False,
        }
        req_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        _log(f"  POST {api_url}")

        t0 = time.time()
        resp = requests.post(api_url, headers=req_headers, json=payload,
                             timeout=(10, 30))
        elapsed = time.time() - t0

        if resp.status_code == 200:
            _log(f"  ✓ HTTP 200 OK ({elapsed:.2f}s)")
            _status("连接成功！")
            return True, f"连接测试通过 (HTTP 200, {elapsed:.1f}s)"
        elif resp.status_code == 401:
            body = resp.text[:200]
            _log("  HTTP 401 — API Key 无效")
            _log(f"  响应: {body}")
            return False, "API Key 无效 (401 Unauthorized)\n请检查 API Key 是否正确"
        elif resp.status_code == 403:
            _log("  HTTP 403 — 访问被拒绝")
            return False, "访问被拒绝 (403 Forbidden)\n请检查 API Key 权限或账户余额"
        elif resp.status_code == 404:
            _log("  HTTP 404 — 端点不存在")
            return False, f"API 端点不存在 (404)\n请检查 API 地址是否正确:\n{api_url}"
        elif resp.status_code == 429:
            _log("  HTTP 429 — 请求频率限制")
            return False, "请求过于频繁 (429 Rate Limited)\n请稍后重试"
        else:
            body = resp.text[:200]
            _log(f"  HTTP {resp.status_code}")
            _log(f"  响应: {body}")
            return False, f"服务器返回 HTTP {resp.status_code}\n{body}"
    except requests.exceptions.SSLError as e:
        _log(f"✗ SSL 证书错误: {e}")
        return False, "SSL 证书验证失败\n请检查系统时间或 API 地址的 HTTPS 证书"
    except requests.exceptions.ConnectionError as e:
        _log(f"✗ 连接错误: {e}")
        return False, "无法连接到服务器\n请检查网络和代理设置"
    except requests.exceptions.Timeout:
        _log("✗ 请求超时")
        return False, "API 请求超时（30秒）\n服务器可能不可用"
    except Exception as e:
        _log(f"✗ 异常: {e}")
        return False, f"连接测试失败: {e}"
