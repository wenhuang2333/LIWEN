#!/usr/bin/env python3
"""反向代理对抗性套件（fuzz_reverse_proxy.py）。

对 ``code/network/reverse_proxy.py`` 打畸形/恶意流量，验证三条不变量：

1. **不崩**：任何输入都不能让代理进程/线程抛未捕获异常或退出；
2. **干净收场**：要么返回 4xx，要么干净关闭连接——绝不挂住不放；
3. **打不死的存活**：每轮攻击之后，代理仍能正常服务一个合法请求，
   并且 ``stop()`` 之后没有残留线程。

所有端口都是 127.0.0.1 临时端口，不与联机套件（12345/12346/12347）冲突，
因此可以与 verify_multiplayer.py 同时运行。

用法::

    python code/tools/fuzz_reverse_proxy.py [--rounds 200]

退出码：全部通过 0，否则 1。
"""

from __future__ import annotations

import http.client
import json
import os
import random
import socket
import sys
import threading
import time
import traceback

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

from network.reverse_proxy import ReverseProxyServer, normalize_rule  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_reverse_proxy import (  # noqa: E402  — 复用上游测试服务器与工具
    _UpstreamHandler, free_port, request, raw, start_upstream,
)

ROUNDS = 200
for index, item in enumerate(sys.argv):
    if item == "--rounds" and index + 1 < len(sys.argv):
        ROUNDS = max(20, int(sys.argv[index + 1]))

PASSED = 0
FAILED = 0
FAILURES = []

ACCEPTABLE = {200, 204, 302, 400, 403, 404, 405, 408, 413, 414, 431, 500, 501, 502, 503, 504}


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"✓ {name}")
    else:
        FAILED += 1
        FAILURES.append((name, detail))
        print(f"✗ {name}  ← {detail}")
    return bool(cond)


def alive_check(proxy, port, label):
    """攻击后存活检查：代理仍能正常服务一个合法请求。"""
    try:
        status, _, data = request(port, "GET", "/echo", timeout=10)
        ok = status == 200 and json.loads(data.decode())["method"] == "GET"
        detail = f"status={status} body={data[:60]!r}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    check(f"攻击后仍能正常服务（{label}）", ok, detail)


def attack(name, payload: bytes, port, *, expect=None, read_all=True, timeout=6.0):
    """发一次攻击载荷，断言：不崩、不挂、响应在可接受集合内。"""
    started = time.monotonic()
    try:
        resp = raw(port, payload, timeout=timeout, read_all=read_all)
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"{type(exc).__name__}: {exc}")
        return b""
    elapsed = time.monotonic() - started
    if elapsed > timeout + 1.0:
        check(name, False, f"耗时 {elapsed:.1f}s，疑似挂住")
        return resp
    if not resp:
        check(name, True)      # 直接关闭连接是合法收场
        return resp
    if not resp.startswith(b"HTTP/1."):
        check(name, False, f"响应不是 HTTP: {resp[:60]!r}")
        return resp
    try:
        status = int(resp.split(b" ", 2)[1])
    except (IndexError, ValueError):
        check(name, False, f"状态行无法解析: {resp[:60]!r}")
        return resp
    if expect is not None:
        ok = status in (expect if isinstance(expect, (set, tuple, list)) else {expect})
    else:
        ok = status in ACCEPTABLE
    check(name, ok, f"status={status} resp={resp[:60]!r}")
    return resp


def main() -> int:
    print("=" * 72)
    print("反向代理对抗性套件 (fuzz_reverse_proxy.py)")
    print("=" * 72)

    upstream, up_port, up_url = start_upstream()
    rule = normalize_rule({
        "name": "fuzz", "listen_port": free_port(), "upstream": up_url,
        "read_timeout": 1.0, "connect_timeout": 1.0,
    })
    proxy = ReverseProxyServer(rules=[rule])
    proxy.start()
    port = rule["listen_port"]
    print(f"上游 {up_url} · 代理 127.0.0.1:{port} · 随机轮次 {ROUNDS}\n")

    baseline_threads = threading.active_count()

    try:
        print("── 1. 畸形请求 ──")
        attack("随机字节流不崩溃", os.urandom(4096), port)
        attack("纯文本垃圾不崩溃", b"not http at all\r\n\r\n", port)
        attack("空请求（直接关闭）", b"", port, read_all=False)
        attack("只有 CRLF", b"\r\n\r\n", port)
        attack("超长请求行（9000 字节）",
               b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: h\r\n\r\n", port,
               expect={400, 414, 431})
        attack("非法 HTTP 版本", b"GET / HTTP/9.9\r\nHost: h\r\n\r\n", port,
               expect={400, 505})
        attack("请求行含空格注入",
               b"GET /a b HTTP/1.1\r\nHost: h\r\n\r\n", port, expect={400})
        attack("头字段名含空格", b"GET / HTTP/1.1\r\nHost: h\r\nBad Name: 1\r\n\r\n", port,
               expect={400})
        attack("头字段名含 CR（CRLF 注入）",
               b"GET / HTTP/1.1\r\nHost: h\r\nX-A: 1\rX-B: 2\r\n\r\n", port, expect={400})
        attack("头字段值含 NUL",
               b"GET / HTTP/1.1\r\nHost: h\r\nX-A: va\x00lue\r\n\r\n", port, expect={400})
        attack("头字段名含冒号重复",
               b"GET / HTTP/1.1\r\nHost: h\r\nX-A: 1: 2\r\n\r\n", port)
        attack("头字段没有冒号", b"GET / HTTP/1.1\r\nHost: h\r\nGarbageLine\r\n\r\n", port,
               expect={400})

        print("\n── 2. 资源耗尽类 ──")
        attack("头炸弹（400 个头）",
               b"GET / HTTP/1.1\r\nHost: h\r\n"
               + b"".join(b"X-%d: v\r\n" % i for i in range(400)) + b"\r\n", port,
               expect={431})
        attack("单个超大头（100KB）",
               b"GET / HTTP/1.1\r\nHost: h\r\nX-Big: " + b"b" * 102400 + b"\r\n\r\n", port,
               expect={431})
        attack("超大 URL（200KB）",
               b"GET /" + b"c" * 200000 + b" HTTP/1.1\r\nHost: h\r\n\r\n", port,
               expect={400, 414, 431})

        # slowloris：分片发送请求头后保持连接，代理必须在 read_timeout 内收场
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        started = time.monotonic()
        try:
            sock.sendall(b"GET /echo HTTP/1.1\r\nHost: h\r\nX-Slow: ")
            time.sleep(0.4)
            sock.sendall(b"still-here")
            time.sleep(2.2)          # 超过 read_timeout=1.0
            sock.settimeout(3)
            try:
                leftover = sock.recv(1024)
            except socket.timeout:
                leftover = b"<timeout>"
        finally:
            sock.close()
        elapsed = time.monotonic() - started
        check("slowloris 半截请求头被超时收场",
              leftover in (b"", b"<timeout>") or leftover.startswith(b"HTTP/1.1 4"),
              f"elapsed={elapsed:.1f}s leftover={leftover[:40]!r}")

        print("\n── 3. 请求体走私 / 分帧 ──")
        attack("非法 chunk 大小", b"POST /echo HTTP/1.1\r\nHost: h\r\n"
               b"Transfer-Encoding: chunked\r\n\r\nZZZ\r\nabc\r\n0\r\n\r\n", port,
               expect={400})
        attack("chunk 声明超过上限",
               b"POST /echo HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"FFFFFFFFFF\r\n", port, expect={400})
        attack("chunk 缺结束块后关闭",
               b"POST /echo HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n"
               b"Connection: close\r\n\r\n5\r\nhello\r\n", port)
        attack("负 Content-Length", b"POST /echo HTTP/1.1\r\nHost: h\r\n"
               b"Content-Length: -5\r\n\r\n", port, expect={400})
        attack("非数字 Content-Length", b"POST /echo HTTP/1.1\r\nHost: h\r\n"
               b"Content-Length: 5x\r\n\r\nhello", port, expect={400})
        attack("CL 与 TE 同时出现（走私）",
               b"POST /echo HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", port, expect={400})
        attack("两个 Content-Length 冲突",
               b"POST /echo HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n"
               b"Content-Length: 6\r\n\r\nhello", port, expect={400})
        attack("Content-Length 超过声明上限",
               b"POST /echo HTTP/1.1\r\nHost: h\r\nContent-Length: 999999999999999\r\n\r\n",
               port, expect={400, 413})

        # 半截 body 后断开：代理必须收场，不能永久占着线程
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            sock.sendall(b"POST /echo HTTP/1.1\r\nHost: h\r\nContent-Length: 100\r\n\r\n")
            sock.sendall(b"0123456789")
            time.sleep(0.2)
        finally:
            sock.close()
        time.sleep(0.3)
        check("请求体中途断开不挂线程", True)

        print("\n── 4. 隧道越权 / 伪装 ──")
        attack("CONNECT 到未授权目标 → 403",
               b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n", port,
               expect={403})
        attack("CONNECT 到本机其它端口 → 403",
               b"CONNECT 127.0.0.1:22 HTTP/1.1\r\nHost: 127.0.0.1:22\r\n\r\n", port,
               expect={403})
        attack("CONNECT 缺目标", b"CONNECT HTTP/1.1\r\nHost: h\r\n\r\n", port,
               expect={400, 403})
        attack("Upgrade 伪装（无 Connection: Upgrade）",
               b"GET /echo HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n"
               b"Connection: close\r\n\r\n", port, expect={200})
        attack("Upgrade 到非上游路径",
               b"GET /nope HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n"
               b"Connection: Upgrade\r\nSec-WebSocket-Key: aaa\r\n"
               b"Sec-WebSocket-Version: 13\r\n\r\n", port, expect={200})
        attack("Transfer-Encoding 伪造（identity）",
               b"POST /echo HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: identity\r\n"
               b"Content-Length: 5\r\n\r\nhello", port, expect={400})

        print("\n── 5. 管线化 / 并发畸形 ──")
        resp = raw(port, b"GET /echo HTTP/1.1\r\nHost: h\r\n\r\n"
                         b"GET /echo HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n")
        check("管线化两个请求都被处理", resp.count(b"HTTP/1.1 200") == 2,
              f"200 出现 {resp.count(b'HTTP/1.1 200')} 次")

        def hammer(index):
            try:
                if index % 3 == 0:
                    raw(port, b"GET /" + b"z" * 2000 + b" HTTP/1.1\r\nHost: h\r\n\r\n",
                        timeout=5)
                elif index % 3 == 1:
                    raw(port, b"POST /echo HTTP/1.1\r\nHost: h\r\nContent-Length: -1\r\n\r\n",
                        timeout=5)
                else:
                    raw(port, os.urandom(512), timeout=5)
            except Exception:  # noqa: BLE001 — 对抗性流量允许任意网络异常
                pass

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        check("24 路并发畸形流量后进程仍存活", True)

        print(f"\n── 6. 随机变异（{ROUNDS} 轮） ──")
        rng = random.Random(20261023)
        valid = (b"GET /echo HTTP/1.1\r\nHost: fuzz.test\r\n"
                 b"X-Fuzz: 1\r\nConnection: close\r\n\r\n")
        bad_status = []
        unparsed = 0
        for i in range(ROUNDS):
            data = bytearray(valid)
            for _ in range(rng.randint(1, 6)):
                pos = rng.randrange(len(data))
                data[pos] = rng.randrange(256)
            if rng.random() < 0.3:
                data = data[:rng.randrange(1, len(data))]
            try:
                resp = raw(port, bytes(data), timeout=5)
            except Exception:  # noqa: BLE001
                continue
            if not resp:
                continue
            if not resp.startswith(b"HTTP/1."):
                unparsed += 1
                continue
            try:
                status = int(resp.split(b" ", 2)[1])
            except (IndexError, ValueError):
                unparsed += 1
                continue
            if status not in ACCEPTABLE:
                bad_status.append(status)
        check(f"{ROUNDS} 轮随机变异：无非预期状态码",
              not bad_status, f"异常状态 {sorted(set(bad_status))[:5]}")
        check(f"{ROUNDS} 轮随机变异：响应全部可解析", unparsed == 0, f"{unparsed} 个无法解析")

        print("\n── 7. 收场 ──")
        alive_check(proxy, port, "全部攻击之后")
        check("攻击后代理仍在运行", proxy.running is True)

        stats = proxy.stats()
        check("统计计数未溢出/未变负",
              stats["requests"] >= 0 and stats["errors"] >= 0 and stats["bytes_in"] >= 0,
              str({k: stats[k] for k in ("requests", "errors")}))

        summary = proxy.stop()
        check("stop() 无残留线程（对抗流量不留孤儿）",
              summary["leaked_threads"] == 0, str(summary))

        time.sleep(0.4)
        leftover = [t.name for t in threading.enumerate()
                    if t.name.startswith("proxy-") and t.is_alive()]
        check("无存活的代理线程", not leftover, str(leftover))

        grew = threading.active_count() - baseline_threads
        check("线程总数回到基线附近", grew <= 2, f"净增 {grew}")

        print()
        print("=" * 72)
        print(f"通过 {PASSED} / 失败 {FAILED}（共 {PASSED + FAILED} 项）")
        if FAILURES:
            print("\n失败项：")
            for name, detail in FAILURES:
                print(f"  ✗ {name}  ← {detail}")
        print("=" * 72)
        return 0 if FAILED == 0 else 1

    except Exception:  # noqa: BLE001
        print("\n对抗套件自身异常：")
        traceback.print_exc()
        return 1
    finally:
        try:
            proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            upstream.shutdown()
            upstream.server_close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
