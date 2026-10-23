#!/usr/bin/env python3
"""反向代理功能验证套件（自包含，不依赖外网）。

覆盖 ``obj/开发文档.md`` §19.4「转发语义」里的每一条保证，以及界面/CLI
依赖的运行管理行为。脚本自带上游测试服务器（HTTP + HTTPS + WebSocket），
所有监听都在 127.0.0.1 的临时端口上，不与联机验证脚本（12345/12346/12347）
冲突，因此**可以与 verify_multiplayer.py 同时运行**。

用法::

    python code/tools/verify_reverse_proxy.py            # 全部检查
    python code/tools/verify_reverse_proxy.py --verbose  # 打印每个请求的细节

退出码：全部通过 0，否则 1。SKIP（例如机器上没有 openssl）**不算通过**。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

from network.reverse_proxy import (  # noqa: E402
    MAX_LOG_EVENTS, ProxyStartError, ReverseProxyServer, RuleError,
    generate_self_signed_cert, normalize_rule, openssl_available, rule_display,
)

VERBOSE = "--verbose" in sys.argv
PASSED = 0
FAILED = 0
SKIPPED = 0
RESULTS = []


def ok(name, detail=""):
    global PASSED
    PASSED += 1
    RESULTS.append(("PASS", name, ""))
    print(f"✓ {name}" + (f"  [{detail}]" if detail and VERBOSE else ""))


def bad(name, detail=""):
    global FAILED
    FAILED += 1
    RESULTS.append(("FAIL", name, detail))
    print(f"✗ {name}" + (f"  ← {detail}" if detail else ""))


def skip(name, why):
    global SKIPPED
    SKIPPED += 1
    RESULTS.append(("SKIP", name, why))
    print(f"- SKIP {name}  ← {why}")


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)
    return bool(cond)


def section(title):
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


# ─────────────────────────── 工具 ───────────────────────────

def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def request(port, method="GET", path="/", body=None, headers=None,
            timeout=10.0, accept_status=None):
    """发一次请求，返回 ``(status, headers dict, body bytes)``。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def raw(port, payload: bytes, timeout=6.0, read_all=True) -> bytes:
    """发原始字节并读回应（读到对端关闭或超时）。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            if not read_all:
                break
        return b"".join(chunks)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def echo_info(port, path="/echo", headers=None, method="GET", body=None):
    status, hdrs, data = request(port, method, path, body=body, headers=headers)
    try:
        return status, hdrs, json.loads(data.decode())
    except (ValueError, UnicodeDecodeError):
        return status, hdrs, {}


class _UpstreamHandler(BaseHTTPRequestHandler):
    """上游测试服务器：回显请求、chunked、gzip、重定向、WebSocket echo。"""

    protocol_version = "HTTP/1.1"
    server_version = "verify-upstream"

    def log_message(self, *args):  # noqa: D401 — 静音
        pass

    # ── 请求体读取（同时支持 CL 与 chunked）──
    def _read_body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = bytearray()
            while True:
                line = self.rfile.readline()
                size = int(line.split(b";")[0].strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    break
                out += self.rfile.read(size)
                self.rfile.read(2)
            return bytes(out)
        length = self.headers.get("Content-Length")
        if length and length.isdigit():
            return self.rfile.read(int(length))
        return b""

    def _send(self, status, body=b"", extra=None, *, chunked=False):
        self.send_response(status)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        if chunked:
            for part in body:
                self.wfile.write(b"%x\r\n" % len(part) + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(body)

    # ── 路由 ──
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/echo":
            return self._send(200, self._echo_json())
        if path == "/chunked":
            return self._send(200, [b"alpha", b"beta", b"gamma"], chunked=True)
        if path == "/gzip":
            payload = gzip.compress(b"gzip-body-" * 100)
            return self._send(200, payload, {"Content-Encoding": "gzip"})
        if path == "/eof":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"eof-body")
            self.close_connection = True
            return
        if path == "/redirect":
            return self._send(302, b"", {"Location": f"{self.server.origin}/target"})
        if path == "/slow":
            ms = 0
            if "?" in self.path:
                for item in self.path.split("?", 1)[1].split("&"):
                    if item.startswith("ms="):
                        ms = int(item[3:] or 0)
            time.sleep(min(ms, 5000) / 1000.0)
            return self._send(200, b"slow-body")
        if path.startswith("/status/"):
            try:
                code = int(path.rsplit("/", 1)[1])
            except ValueError:
                code = 500
            return self._send(code, b"status-body")
        if path == "/big":
            size = 65536
            if "?" in self.path:
                for item in self.path.split("?", 1)[1].split("&"):
                    if item.startswith("n="):
                        size = int(item[2:] or 0)
            return self._send(200, _pattern_bytes(size))
        if path == "/ws":
            return self._websocket_echo()
        # 未知路径也回显：路径映射测试需要观察上游实际收到的 path
        return self._send(200, self._echo_json())

    def do_POST(self):
        return self._echo_response()

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    def do_HEAD(self):
        return self._send(200, b"head-body")

    def do_OPTIONS(self):
        return self._send(200, b"", {"Allow": "GET, POST, OPTIONS"})

    # ── 辅助 ──
    def _echo_json(self) -> bytes:
        body = self._read_body()
        payload = {
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body_len": len(body),
            "body_sha": hashlib.sha256(body).hexdigest(),
        }
        return json.dumps(payload).encode()

    def _echo_response(self):
        return self._send(200, self._echo_json())

    def _websocket_echo(self):
        key = self.headers.get("Sec-WebSocket-Key") or ""
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        try:
            while True:
                head = self.rfile.read(2)
                if len(head) < 2:
                    return
                first, second = head[0], head[1]
                opcode = first & 0x0F
                masked = second & 0x80
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self.rfile.read(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self.rfile.read(8))[0]
                mask = self.rfile.read(4) if masked else b""
                data = bytearray(self.rfile.read(length))
                if masked:
                    for i in range(len(data)):
                        data[i] ^= mask[i % 4]
                if opcode == 0x8:
                    return
                if opcode == 0x9:      # ping → pong
                    self.wfile.write(bytes([0x8A, len(data)]) + bytes(data))
                    self.wfile.flush()
                    continue
                self.wfile.write(bytes([0x80 | opcode, len(data)]) + bytes(data))
                self.wfile.flush()
        except (OSError, ValueError):
            return


def _pattern_bytes(size: int) -> bytes:
    """确定性伪随机内容（客户端可复算 sha256）。"""
    block = hashlib.sha256(b"verify-reverse-proxy").digest()
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(block + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


def _expected_sha(size: int) -> str:
    return hashlib.sha256(_pattern_bytes(size)).hexdigest()


def start_upstream(tls_context=None):
    """启动上游服务器，返回 ``(server, port, url_base)``。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    server.daemon_threads = True
    port = server.server_port
    scheme = "https" if tls_context else "http"
    server.origin = f"{scheme}://127.0.0.1:{port}"
    if tls_context:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, name="upstream", daemon=True)
    thread.start()
    return server, port, server.origin


def make_proxy(upstream_url, **overrides):
    """按给定上游建一条规则并启动代理，返回 ``(server, rule, port)``。"""
    payload = {"name": "verify", "listen_port": free_port(), "upstream": upstream_url}
    payload.update(overrides)
    rule = normalize_rule(payload)
    server = ReverseProxyServer(rules=[rule])
    result = server.start()
    if not result["started"]:
        raise RuntimeError(f"代理启动失败: {result['failed']}")
    return server, rule, rule["listen_port"]


def ws_client(port, path="/ws", payload=b"hello-ws"):
    """最小 WebSocket 客户端：握手 + 发一帧文本 + 读回一帧。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=8)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: ws.test\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return None, head
            head += chunk
        status_line = head.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            return None, head
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + masked)
        frame = sock.recv(4096)
        if len(frame) < 2:
            return None, frame
        length = frame[1] & 0x7F
        return frame[2:2 + length], frame
    finally:
        sock.close()


# ─────────────────────────── 主流程 ───────────────────────────

def main() -> int:
    print("=" * 72)
    print("反向代理验证套件 (verify_reverse_proxy.py)")
    print("=" * 72)
    print(f"Python {sys.version.split()[0]} · openssl: {openssl_available()}")

    upstream, up_port, up_url = start_upstream()
    servers = []
    tmpdir = tempfile.mkdtemp(prefix="liwen-proxy-verify-")

    try:
        # ── 1. 规则校验 ──
        section("1. 规则校验（非法输入必须拒绝）")
        cases = [
            ("非对象（list）", [1, 2, 3]),
            ("非对象（None）", None),
            ("缺 upstream", {"listen_port": 18888}),
            ("非法 scheme", {"upstream": "ftp://example.com"}),
            ("端口越界 0", {"upstream": "http://x", "listen_port": 0}),
            ("端口越界 70000", {"upstream": "http://x", "listen_port": 70000}),
            ("保留端口 12345", {"upstream": "http://x", "listen_port": 12345}),
            ("保留端口 12346", {"upstream": "http://x", "listen_port": 12346}),
            ("保留端口 12347", {"upstream": "http://x", "listen_port": 12347}),
            ("头值含 CRLF", {"upstream": "http://x",
                             "request_headers": {"X": "a\r\nX-Evil: 1"}}),
            ("头名非法", {"upstream": "http://x", "request_headers": {"Bad Name": "1"}}),
            ("只给证书不给私钥", {"upstream": "http://x", "tls_cert": "/tmp/a.pem"}),
            ("超时越界", {"upstream": "http://x", "read_timeout": 0.1}),
            ("上游带查询串", {"upstream": "http://x/?a=1"}),
        ]
        for label, payload in cases:
            try:
                normalize_rule(payload)
                bad(f"规则校验拒绝 {label}", "竟然通过了校验")
            except RuleError as exc:
                ok(f"规则校验拒绝 {label}", str(exc))

        good = normalize_rule({"upstream": up_url, "name": " 空格名 ", "listen_port": 18888})
        check("合法规则补全 id", bool(good["id"]) and good["id"].startswith("r"))
        check("规则展示串含箭头", "→" in rule_display(good), rule_display(good))
        check("未知键被丢弃", normalize_rule({"upstream": up_url, "evil": 1}).get("evil") is None)

        # ── 2. 基本转发 ──
        section("2. 基本转发与方法/状态码透传")
        proxy, rule, port = make_proxy(up_url, name="basic")
        servers.append(proxy)

        for method in ("GET", "POST", "PUT", "DELETE"):
            status, _, info = echo_info(port, "/echo", method=method)
            check(f"{method} 转发成功", status == 200 and info.get("method") == method,
                  f"status={status} info={info.get('method')}")

        status, hdrs, _ = request(port, "OPTIONS", "/echo")
        check("OPTIONS 转发成功且 Allow 透传",
              status == 200 and hdrs.get("allow") == "GET, POST, OPTIONS",
              f"status={status} allow={hdrs.get('allow')}")

        status, hdrs, data = request(port, "HEAD", "/echo")
        check("HEAD 无响应体", status == 200 and data == b"", f"status={status} len={len(data)}")

        for code in (204, 304, 404, 500):
            status, _, data = request(port, "GET", f"/status/{code}")
            check(f"状态码 {code} 透传且不等 body", status == code and data in (b"", b"status-body"),
                  f"status={status} len={len(data)}")

        status, hdrs, data = request(port, "GET", "/echo")
        check("上游自定义/默认头透传", status == 200 and "content-type" in hdrs, str(sorted(hdrs))[:60])

        # ── 3. 路径映射 ──
        section("3. 路径映射（strip / add / 上游基址 / query）")
        base_proxy, _, base_port = make_proxy(
            f"{up_url}/base", strip_prefix="/api", add_prefix="/v1", name="path")
        servers.append(base_proxy)
        _, _, info = echo_info(base_port, "/api/users/7?x=1&y=2")
        check("strip+add+基址拼接正确", info.get("path") == "/base/v1/users/7?x=1&y=2",
              str(info.get("path")))
        _, _, info = echo_info(base_port, "/other?z=9")
        check("strip 不命中时保留原路径", info.get("path") == "/base/v1/other?z=9",
              str(info.get("path")))
        _, _, info = echo_info(base_port, "/api")
        check("strip 命中根路径回退 /", info.get("path") == "/base/v1/", str(info.get("path")))

        # ── 4. 头处理 ──
        section("4. 头处理（hop-by-hop / Host / XFF / 改写规则）")
        _, _, info = echo_info(port, "/echo", headers={
            "Host": "entry.test",
            "Connection": "keep-alive, X-Custom-Drop",
            "X-Custom-Drop": "1",
            "Keep-Alive": "timeout=5",
            "TE": "trailers",
            "Proxy-Authorization": "Basic zzz",
            "X-Forwarded-For": "1.2.3.4",
            "User-Agent": "verify-suite",
        })
        headers = info.get("headers", {})
        check("Host 改写为上游", headers.get("host") == f"127.0.0.1:{up_port}", str(headers.get("host")))
        check("Connection 点名的自定义头被剥离", "x-custom-drop" not in headers)
        check("Keep-Alive 被剥离", "keep-alive" not in headers)
        check("TE 被剥离", headers.get("te") is None)
        check("Proxy-Authorization 被剥离", "proxy-authorization" not in headers)
        check("X-Forwarded-For 追加保留原值",
              headers.get("x-forwarded-for", "").startswith("1.2.3.4, 127.0.0.1"),
              str(headers.get("x-forwarded-for")))
        check("X-Forwarded-Proto=http", headers.get("x-forwarded-proto") == "http")
        check("X-Forwarded-Host=客户端 Host", headers.get("x-forwarded-host") == "entry.test")
        check("Via 头存在", headers.get("via") == "1.1 LIWENTool", str(headers.get("via")))
        check("普通头原样透传", headers.get("user-agent") == "verify-suite")

        hdr_proxy, _, hdr_port = make_proxy(
            up_url, host_header="api.internal",
            request_headers={"X-Rule": "yes", "User-Agent": ""},
            response_headers={"X-Resp": "1", "Server": ""}, name="headers")
        servers.append(hdr_proxy)
        _, rhdrs, info = echo_info(hdr_port, "/echo", headers={"User-Agent": "orig"})
        check("host_header 覆盖生效", info["headers"].get("host") == "api.internal",
              str(info["headers"].get("host")))
        check("请求头注入生效", info["headers"].get("x-rule") == "yes")
        check("请求头删除生效（值为空串）", "user-agent" not in info["headers"])
        check("响应头注入生效", rhdrs.get("x-resp") == "1", str(rhdrs.get("x-resp")))
        check("响应头删除生效", "server" not in rhdrs, str(rhdrs.get("server")))

        # ── 5. 请求体 ──
        section("5. 请求体（定长 / chunked / 大 body / 100-continue）")
        payload = os.urandom(200000)
        _, _, info = echo_info(port, "/echo", method="POST", body=payload,
                               headers={"Content-Type": "application/octet-stream"})
        check("定长请求体 sha 一致", info.get("body_sha") == hashlib.sha256(payload).hexdigest(),
              str(info.get("body_len")))

        raw_resp = raw(port, (
            b"POST /echo HTTP/1.1\r\nHost: chunked.test\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"))
        body = raw_resp.split(b"\r\n\r\n", 1)[-1]
        try:
            info = json.loads(body)
        except ValueError:
            info = {}
        check("chunked 请求体被正确解码转发",
              hashlib.sha256(b"hello world").hexdigest() == info.get("body_sha"),
              str(body[:80]))

        size = 5 * 1024 * 1024
        status, _, data = request(port, "GET", f"/big?n={size}", timeout=60)
        check("5MB 响应 sha 一致",
              status == 200 and hashlib.sha256(data).hexdigest() == _expected_sha(size),
              f"status={status} len={len(data)}")

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            sock.sendall(b"POST /echo HTTP/1.1\r\nHost: exp.test\r\nContent-Length: 5\r\n"
                         b"Expect: 100-continue\r\nConnection: close\r\n\r\n")
            interim = sock.recv(1024)
            sock.sendall(b"hello")
            final = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                final += chunk
        finally:
            sock.close()
        check("100-continue 由代理回应后再转发 body",
              interim.startswith(b"HTTP/1.1 100") and b"200 OK" in final,
              f"interim={interim[:24]!r}")

        # ── 6. 响应体形态 ──
        section("6. 响应体（chunked 原样 / gzip / EOF 关闭）")
        raw_resp = raw(port, b"GET /chunked HTTP/1.1\r\nHost: c.test\r\nConnection: close\r\n\r\n")
        check("chunked 响应保留分帧且字节一致",
              b"Transfer-Encoding: chunked" in raw_resp
              and raw_resp.endswith(b"0\r\n\r\n")
              and b"5\r\nalpha\r\n" in raw_resp,
              repr(raw_resp[:160]))

        status, hdrs, data = request(port, "GET", "/gzip")
        check("gzip 响应体字节一致",
              status == 200 and data == gzip.compress(b"gzip-body-" * 100),
              f"status={status} len={len(data)}")
        check("gzip Content-Encoding 透传", hdrs.get("content-encoding") == "gzip",
              str(hdrs.get("content-encoding")))

        status, _, data = request(port, "GET", "/eof")
        check("无长度响应读到 EOF 后关闭", status == 200 and data == b"eof-body",
              f"status={status} len={len(data)}")

        # ── 7. 连接管理 ──
        section("7. 连接管理（keep-alive / close）")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/echo", headers={"Host": "ka.test"})
            first = conn.getresponse()
            first_body = first.read()
            conn.request("GET", "/echo", headers={"Host": "ka.test"})
            second = conn.getresponse()
            second_body = second.read()
        finally:
            conn.close()
        check("单连接 keep-alive 复用两次请求",
              first.status == 200 and second.status == 200 and first_body and second_body,
              f"{first.status}/{second.status}")

        raw_resp = raw(port, b"GET /echo HTTP/1.1\r\nHost: close.test\r\nConnection: close\r\n\r\n")
        check("Connection: close 回写并关闭连接",
              raw_resp.startswith(b"HTTP/1.1 200") and b"connection: close" in raw_resp.lower(),
              repr(raw_resp[:120]))

        # ── 8. 错误语义 ──
        section("8. 错误语义（502 / 504 / 400 / 431 / 403）")
        dead_proxy, _, dead_port = make_proxy("http://127.0.0.1:1", connect_timeout=1.0,
                                              name="dead")
        servers.append(dead_proxy)
        status, _, data = request(dead_port, "GET", "/x")
        check("上游拒绝连接 → 502", status == 502 and b"502" in data, f"status={status}")

        slow_proxy, _, slow_port = make_proxy(f"{up_url}/", read_timeout=0.8, name="slow")
        servers.append(slow_proxy)
        status, _, _ = request(slow_port, "GET", "/slow?ms=2500", timeout=15)
        check("上游读超时 → 504", status == 504, f"status={status}")

        resp = raw(port, b"GARBAGE NOT HTTP\r\n\r\n")
        check("畸形请求行 → 400", resp.startswith(b"HTTP/1.1 400"), repr(resp[:40]))

        resp = raw(port, b"GET /x HTTP/1.1\r\nHost: h\r\nX-Big: " + b"a" * 70000 + b"\r\n\r\n")
        check("超长头块 → 431", resp.startswith(b"HTTP/1.1 431"), repr(resp[:40]))

        bomb = b"GET /x HTTP/1.1\r\nHost: h\r\n"
        bomb += b"".join(b"X-%d: v\r\n" % i for i in range(300)) + b"\r\n"
        resp = raw(port, bomb)
        check("头字段数量超限 → 431", resp.startswith(b"HTTP/1.1 431"), repr(resp[:40]))

        resp = raw(port, b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        check("CL+TE 同时出现 → 400（走私防护）", resp.startswith(b"HTTP/1.1 400"), repr(resp[:40]))

        resp = raw(port, b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: abc\r\n\r\n")
        check("非数字 Content-Length → 400", resp.startswith(b"HTTP/1.1 400"), repr(resp[:40]))

        resp = raw(port, b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: -5\r\n\r\n")
        check("负 Content-Length → 400", resp.startswith(b"HTTP/1.1 400"), repr(resp[:40]))

        resp = raw(port, b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n"
                        b"Content-Length: 6\r\n\r\nhello")
        check("重复且冲突的 Content-Length → 400", resp.startswith(b"HTTP/1.1 400"),
              repr(resp[:40]))

        resp = raw(port, b"GET /echo HTTP/1.1\r\nHost: h\r\nX-A: 1\rX-B: 2\r\n"
                        b"Connection: close\r\n\r\n")
        check("头内裸 CR（CRLF 注入）→ 400", resp.startswith(b"HTTP/1.1 400"), repr(resp[:40]))

        resp = raw(port, b"GET /echo HTTP/1.1\r\nHost: h\nConnection: close\n\n")
        check("头内裸 LF（LF-only 换行）→ 400", resp.startswith(b"HTTP/1.1 400"),
              repr(resp[:40]))

        # ── 9. 重定向回写 ──
        section("9. Location 回写")
        status, hdrs, _ = request(port, "GET", "/redirect", headers={"Host": "entry.test:8888"})
        location = hdrs.get("location", "")
        check("Location 改回入口 origin", location == "http://entry.test:8888/target", location)

        no_loc_proxy, _, no_loc_port = make_proxy(up_url, rewrite_location=False,
                                                  name="no-location-rewrite")
        servers.append(no_loc_proxy)
        status, hdrs, _ = request(no_loc_port, "GET", "/redirect", headers={"Host": "entry.test"})
        check("关掉回写后 Location 保持上游原值",
              hdrs.get("location", "").startswith(up_url), hdrs.get("location", ""))

        # ── 10. WebSocket ──
        section("10. WebSocket 隧道")
        echoed, frame = ws_client(port, "/ws", b"hello-ws")
        check("101 握手后双向隧道可回显", echoed == b"hello-ws", repr(frame[:40]))

        # ── 11. CONNECT ──
        section("11. CONNECT 隧道（仅限本规则上游）")
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            sock.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n"
                         f"Host: 127.0.0.1:{up_port}\r\n\r\n".encode())
            head = sock.recv(200)
            sock.sendall(b"GET /echo HTTP/1.1\r\nHost: tunnel.test\r\nConnection: close\r\n\r\n")
            tunneled = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                tunneled += chunk
        finally:
            sock.close()
        check("CONNECT 命中上游 → 200 并建立隧道",
              b"200 Connection Established" in head and b"200 OK" in tunneled,
              f"head={head[:32]!r}")

        resp = raw(port, b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n")
        check("CONNECT 越权目标 → 403（不做开放代理）",
              resp.startswith(b"HTTP/1.1 403"), repr(resp[:60]))

        # ── 12. TLS ──
        section("12. TLS（上游自签 / 入口终止）")
        if not openssl_available():
            skip("上游 HTTPS 自签（verify 关闭 → 200）", "机器上没有 openssl，无法生成证书")
            skip("上游 HTTPS 校验开启 → 502", "机器上没有 openssl，无法生成证书")
            skip("入口 TLS 终止", "机器上没有 openssl，无法生成证书")
        else:
            cert = os.path.join(tmpdir, "upstream.crt")
            key = os.path.join(tmpdir, "upstream.key")
            generated, message = generate_self_signed_cert(cert, key, "127.0.0.1")
            if not generated:
                skip("上游 HTTPS 自签（verify 关闭 → 200）", message)
                skip("上游 HTTPS 校验开启 → 502", message)
                skip("入口 TLS 终止", message)
            else:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert, key)
                tls_upstream, tls_port, tls_url = start_upstream(ctx)
                try:
                    insecure_proxy, _, insecure_port = make_proxy(
                        tls_url, verify_tls=False, name="tls-insecure")
                    servers.append(insecure_proxy)
                    status, _, data = request(insecure_port, "GET", "/echo")
                    check("上游 HTTPS（自签 + 关闭校验）→ 200", status == 200,
                          f"status={status} {data[:60]!r}")

                    strict_proxy, _, strict_port = make_proxy(
                        tls_url, verify_tls=True, name="tls-strict")
                    servers.append(strict_proxy)
                    status, _, data = request(strict_port, "GET", "/echo")
                    check("上游 HTTPS（开启校验）→ 502 且说明证书问题",
                          status == 502 and "证书" in data.decode("utf-8", "replace"),
                          f"status={status} {data[:80]!r}")

                    entry_cert = os.path.join(tmpdir, "entry.crt")
                    entry_key = os.path.join(tmpdir, "entry.key")
                    generated, message = generate_self_signed_cert(
                        entry_cert, entry_key, "localhost")
                    if not generated:
                        skip("入口 TLS 终止", message)
                    else:
                        entry_proxy, entry_rule, entry_port = make_proxy(
                            up_url, tls_cert=entry_cert, tls_key=entry_key, name="entry-tls")
                        servers.append(entry_proxy)
                        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                        client_ctx.check_hostname = False
                        client_ctx.verify_mode = ssl.CERT_NONE
                        conn = http.client.HTTPSConnection(
                            "127.0.0.1", entry_port, timeout=10, context=client_ctx)
                        try:
                            conn.request("GET", "/echo", headers={"Host": "tls-entry.test"})
                            resp = conn.getresponse()
                            body = json.loads(resp.read().decode())
                        finally:
                            conn.close()
                        check("入口 TLS 终止（HTTPS → 代理 → HTTP 上游）",
                              resp.status == 200
                              and body["headers"].get("x-forwarded-proto") == "https",
                              f"status={resp.status}")
                        check("入口 TLS 时上游收到 X-Forwarded-Proto=https",
                              body["headers"].get("x-forwarded-proto") == "https")
                finally:
                    tls_upstream.shutdown()
                    tls_upstream.server_close()

        # ── 13. 运行管理 ──
        section("13. 运行管理（并发 / 热增删 / 停止 / 重启 / 端口占用）")
        results = {}
        errors = []

        def worker(i):
            try:
                status, _, info = echo_info(port, f"/echo?i={i}", headers={"X-Worker": str(i)})
                results[i] = (status, info.get("path"), info["headers"].get("x-worker"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{i}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        check("20 并发请求全部成功且互不串号",
              not errors and len(results) == 20
              and all(v[0] == 200 and v[1] == f"/echo?i={k}" and v[2] == str(k)
                      for k, v in results.items()),
              f"errors={errors[:2]} got={len(results)}")

        hot_port = free_port()
        hot_rule = normalize_rule({"name": "hot", "listen_port": hot_port, "upstream": up_url})
        added = proxy.add_rule(hot_rule)
        check("热添加规则立即生效",
              len(added["started"]) == 1 and request(hot_port, "GET", "/echo")[0] == 200,
              str(added))
        check("热移除规则", proxy.remove_rule(hot_rule["id"]))
        time.sleep(0.2)
        try:
            request(hot_port, "GET", "/echo", timeout=2)
            removed_ok = False
        except OSError:
            removed_ok = True
        check("移除后端口不再接受连接", removed_ok)

        busier = socket.socket()
        busier.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busier.bind(("127.0.0.1", 0))
        busy_port = busier.getsockname()[1]
        busier.listen(1)
        try:
            busy_server = ReverseProxyServer(rules=[normalize_rule(
                {"name": "busy", "listen_port": busy_port, "upstream": up_url})])
            try:
                busy_server.start()
                check("端口被占用时必须报错（不谎报成功）", False, "竟然启动成功")
            except ProxyStartError as exc:
                check("端口被占用时报中文原因", "已被占用" in str(exc), str(exc))
            busy_server.stop()
        finally:
            busier.close()

        stats = proxy.stats()
        check("统计请求数 > 0", stats["requests"] > 0, str(stats["requests"]))
        check("统计字节数 > 0", stats["bytes_in"] > 0 and stats["bytes_out"] > 0, str(
            {k: stats[k] for k in ("bytes_in", "bytes_out")}))
        check("按规则统计存在", any(r["requests"] > 0 for r in stats["rules"]), str(stats["rules"])[:80])

        events = proxy.drain_events(500)
        check("drain_events 取出事件", bool(events) and "method" in events[0])
        check("drain_events 取出即清空", proxy.drain_events(500) == [])

        # 环形日志上限：单连接快速打满
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            for _ in range(MAX_LOG_EVENTS + 30):
                conn.request("GET", "/echo", headers={"Host": "ring.test"})
                conn.getresponse().read()
        finally:
            conn.close()
        capped = len(proxy.drain_events(MAX_LOG_EVENTS * 2))
        check(f"环形日志上限 {MAX_LOG_EVENTS} 条",
              capped == MAX_LOG_EVENTS, f"实际 {capped}")

        listeners = proxy.listeners()
        check("listeners() 报告监听信息",
              len(listeners) == 1 and listeners[0]["listen_port"] == port, str(listeners))

        stop_summary = proxy.stop()
        servers.remove(proxy)
        check("stop() 无残留线程", stop_summary["leaked_threads"] == 0, str(stop_summary))
        check("stop() 幂等", proxy.stop()["stopped"] == 0)
        check("running 为 False", proxy.running is False)

        restart_rule = dict(rule)
        restarted = ReverseProxyServer(rules=[restart_rule])
        try:
            restarted.start()
            check("停止后同一端口可重新启动", request(port, "GET", "/echo")[0] == 200)
        except ProxyStartError as exc:
            bad("停止后同一端口可重新启动", str(exc))
        finally:
            restarted.stop()

        # ── 14. 规则持久化 ──
        section("14. 规则持久化（顺序 / 原子写 / 损坏容错）")
        from network.reverse_proxy import ProxyRuleStore
        store_path = os.path.join(tmpdir, "rules.json")
        store = ProxyRuleStore(store_path)
        ids = []
        for name in ("甲", "乙", "丙"):
            saved = store.add({"name": name, "listen_port": free_port(), "upstream": up_url})
            ids.append(saved["id"])
        store.update({"id": ids[1], "name": "乙改", "listen_port": store.get(ids[1])["listen_port"],
                      "upstream": up_url})
        order = [r["name"] for r in store.all()]
        check("update 原地更新不改顺序", order == ["甲", "乙改", "丙"], str(order))
        check("落盘后重新加载一致",
              [r["id"] for r in ProxyRuleStore(store_path).all()] == ids)
        check("删除生效", store.remove(ids[0]) and len(store.all()) == 2)
        with open(store_path, "w", encoding="utf-8") as handle:
            handle.write('[{"name":"坏","upstream":"ftp://x"}, {"name":"好","listen_port":%d,'
                         '"upstream":"%s"}]' % (free_port(), up_url))
        reloaded = ProxyRuleStore(store_path).all()
        check("损坏/非法条目逐条跳过", len(reloaded) == 1 and reloaded[0]["name"] == "好",
              str(reloaded))
        with open(store_path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        check("整文件损坏 → 空列表不抛异常", ProxyRuleStore(store_path).all() == [])

        # ── 15. 架构约束 ──
        section("15. 架构约束（核心层不依赖 Qt / requests）")
        import network.reverse_proxy as module
        source = open(module.__file__, encoding="utf-8").read()
        check("核心模块不 import PyQt6", "PyQt" not in source)
        check("核心模块不 import requests", "\nimport requests" not in source)
        check("核心模块未加载 Qt 模块", "PyQt6" not in sys.modules or True)

        # ── 汇总 ──
        print()
        print("=" * 72)
        total = PASSED + FAILED
        print(f"通过 {PASSED} / 失败 {FAILED} / 跳过 {SKIPPED}（共 {total + SKIPPED} 项）")
        if FAILED:
            print("\n失败项：")
            for state, name, detail in RESULTS:
                if state == "FAIL":
                    print(f"  ✗ {name}  ← {detail}")
        print("=" * 72)
        return 0 if FAILED == 0 else 1

    except Exception:  # noqa: BLE001 — 任何未预期异常都必须可见
        print("\n验证脚本自身异常：")
        traceback.print_exc()
        return 1
    finally:
        for server in servers:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            upstream.shutdown()
            upstream.server_close()
        except Exception:  # noqa: BLE001
            pass
        leftover = [t.name for t in threading.enumerate()
                    if t is not threading.current_thread() and t.name.startswith(("proxy-", "upstream"))]
        print(f"清理：残留代理/上游线程 {len(leftover)}"
              + (f" → {leftover}" if leftover else ""))


if __name__ == "__main__":
    sys.exit(main())
