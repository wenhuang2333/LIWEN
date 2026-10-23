"""反向代理引擎（HTTP/1.1，纯标准库，无 Qt、无 requests）。

设计规格见 ``obj/开发文档.md`` §19。定位：在本机开一个固定入口
（如 ``127.0.0.1:8888``），把请求转发到**预先配置好的上游**
（如 ``https://api.example.com``），并把上游应答原样回给客户端 —— 用于
客户端里硬编码域名/端口、不方便改代码的联调场景（手机 App、写死
``https://api.xxx.com`` 的 SDK）。

与"抓包/正向代理"的区别（以及为什么不做 MITM）：

* 本模块**不解密**第三方 TLS 流量。要做 MITM 必须在客户端设备安装根证书，
  属于高风险的中间人行为，本工具不做（见 §19.6）。
* ``CONNECT`` 只允许连到**本规则的上游**，不是开放代理。

关键不变量（与 CLAUDE.md 第 6/10/11 条一致）：

1. **边界即拒绝**：请求行/头块/CL/chunk 全部在解析处校验；``Content-Length``
   与 ``Transfer-Encoding`` 同时出现直接 400（请求走私防护）。
2. **不谎报成功**：``start()`` 逐条绑定，部分失败原样返回 ``failed``；证书文件
   缺失、端口占用、openssl 缺失都如实报错。
3. **绝不无限阻塞**：所有 socket 都有超时；``stop()`` 幂等，先关监听、再唤醒
   连接、最后 ``join(timeout)``，绝不 ``terminate()``。
4. **端口不冲突**：拒绝 12345/12346/12347（帽兜工具自身端口）。
"""

from __future__ import annotations

import collections
import os
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from utils.logger import error as log_error
from utils.logger import function_scope
from utils.logger import info, warn

# ── 上限与默认值（测试与文档都引用这些常量，不要各写一份字面量） ──
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8888
MAX_HEAD_BYTES = 65536          # 请求/响应头块总长上限
MAX_HEADERS = 200               # 头字段个数上限
MAX_REQUEST_LINE = 8192         # 请求行长度上限
MAX_CHUNK_LINE = 1024           # chunk-size / trailer 行长上限
MAX_CHUNK_BYTES = 1 << 32       # 单个 chunk 声明上限（流式转发，不预分配）
MAX_DECLARED_BYTES = 1 << 40    # 声明的 Content-Length 上限（1 TiB，仅防荒谬值）
MAX_CONNECTIONS = 128           # 并发连接上限
MAX_LOG_EVENTS = 500            # 环形访问日志容量
RELAY_CHUNK = 65536             # 转发块大小
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 60.0
MIN_TIMEOUT = 0.5
MAX_TIMEOUT = 600.0
MAX_HEADER_RULES = 100          # 单侧头改写规则条数上限

# 帽兜工具自身占用的端口：反向代理入口绝不能占用它们（CLAUDE.md 不变量 8）
RESERVED_PORTS = frozenset({12345, 12346, 12347})

SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# hop-by-hop 头：绝不原样转发（RFC 7230 §6.1）
HOP_BY_HOP = frozenset({
    "connection", "proxy-connection", "keep-alive", "te", "trailer",
    "transfer-encoding", "upgrade", "proxy-authorization", "proxy-authenticate",
})

BODYLESS_STATUS = frozenset({204, 304})
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
#: match_host 允许的形态：单标签、域名、IPv4（不含协议/路径/端口）
_HOST_RE = re.compile(
    r"^[0-9a-z]([0-9a-z-]*[0-9a-z])?(\.[0-9a-z]([0-9a-z-]*[0-9a-z])?)*$")


# ─────────────────────────────── 异常 ───────────────────────────────

class ProxyError(Exception):
    """反向代理相关错误的基类。"""


class RuleError(ProxyError, ValueError):
    """规则非法（消息为中文，可直接展示给用户）。"""


class ProxyStartError(ProxyError):
    """没有任何规则能启动。"""


class _Closed(Exception):
    """对端正常关闭（不是错误）。"""


class _BadRequest(Exception):
    """报文畸形（客户端侧 → 400，上游侧 → 502）。"""


class _TooLarge(Exception):
    """头块超限（客户端侧 → 431，上游侧 → 502）。"""


class _Timeout(Exception):
    """读写超时。"""


# ─────────────────────────────── 规则 ───────────────────────────────

RULE_DEFAULTS = {
    "id": "",
    "name": "",
    "enabled": True,
    "listen_host": DEFAULT_LISTEN_HOST,
    "listen_port": DEFAULT_LISTEN_PORT,
    "match_host": "",
    "upstream": "",
    "strip_prefix": "",
    "add_prefix": "",
    "host_header": "",
    "rewrite_location": True,
    "verify_tls": True,
    "connect_timeout": DEFAULT_CONNECT_TIMEOUT,
    "read_timeout": DEFAULT_READ_TIMEOUT,
    "allow_connect": True,
    "tls_cert": "",
    "tls_key": "",
    "request_headers": {},
    "response_headers": {},
}

#: 界面/CLI 展示顺序（键, 中文标签）
RULE_FIELD_LABELS = (
    ("name", "名称"),
    ("enabled", "启用"),
    ("listen_host", "监听地址"),
    ("listen_port", "监听端口"),
    ("upstream", "上游目标"),
    ("strip_prefix", "剥离路径前缀"),
    ("add_prefix", "追加路径前缀"),
    ("host_header", "Host 头覆盖"),
    ("rewrite_location", "回写 Location"),
    ("verify_tls", "校验上游证书"),
    ("allow_connect", "允许 CONNECT"),
    ("connect_timeout", "连接超时(秒)"),
    ("read_timeout", "读写超时(秒)"),
    ("tls_cert", "入口证书"),
    ("tls_key", "入口私钥"),
    ("request_headers", "请求头改写"),
    ("response_headers", "响应头改写"),
)


def new_rule_id() -> str:
    """生成规则 id（``r`` + 8 位十六进制）。"""
    return "r" + os.urandom(4).hex()


def _clean_str(raw, field: str, *, allow_empty: bool = True,
               max_len: int = 512, strip: bool = True) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise RuleError(f"{field} 必须是字符串（当前是 {type(raw).__name__}）")
    text = raw.strip() if strip else raw
    if not allow_empty and not text:
        raise RuleError(f"{field} 不能为空")
    if len(text) > max_len:
        raise RuleError(f"{field} 过长（上限 {max_len} 字符）")
    if "\r" in text or "\n" in text:
        raise RuleError(f"{field} 不能包含换行（防头注入）")
    return text


def _clean_bool(raw, field: str, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    raise RuleError(f"{field} 必须是 true/false（当前是 {type(raw).__name__}）")


def _clean_timeout(raw, field: str, default: float) -> float:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise RuleError(f"{field} 必须是数字（当前是 bool）")
    if not isinstance(raw, (int, float)):
        try:
            raw = float(str(raw))
        except (TypeError, ValueError):
            raise RuleError(f"{field} 必须是数字（当前是 {raw!r}）")
    value = float(raw)
    if not (MIN_TIMEOUT <= value <= MAX_TIMEOUT):
        raise RuleError(f"{field} 必须在 {MIN_TIMEOUT}–{MAX_TIMEOUT} 秒之间（当前 {value}）")
    return value


def _clean_headers(raw, field: str) -> dict:
    """校验头改写表：键必须是合法 token，值不得含换行。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuleError(f"{field} 必须是键值对象（当前是 {type(raw).__name__}）")
    if len(raw) > MAX_HEADER_RULES:
        raise RuleError(f"{field} 条目过多（上限 {MAX_HEADER_RULES} 条）")
    out = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _TOKEN_RE.match(key):
            raise RuleError(f"{field} 的头名不合法: {key!r}")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise RuleError(f"{field}[{key}] 的值必须是字符串")
        text = str(value)
        if "\r" in text or "\n" in text:
            raise RuleError(f"{field}[{key}] 的值不能包含换行（防头注入）")
        if len(text) > 8192:
            raise RuleError(f"{field}[{key}] 的值过长")
        out[key] = text
    return out


def parse_upstream(url) -> tuple:
    """解析上游基址，返回 ``(scheme, host, port, base_path)``。

    非法输入抛 :class:`RuleError`（中文原因）。不接受 userinfo/query/fragment ——
    上游是"基址"而不是一条完整 URL，混进查询串会让路径拼接产生歧义。
    """
    if not isinstance(url, str) or not url.strip():
        raise RuleError("上游目标不能为空，例如 https://api.example.com")
    text = url.strip()
    if " " in text or "\t" in text:
        raise RuleError("上游目标不能包含空格")
    try:
        parts = urlsplit(text)
    except ValueError as e:
        raise RuleError(f"上游目标无法解析: {e}")
    scheme = (parts.scheme or "").lower()
    if scheme not in SCHEMES:
        raise RuleError(f"上游目标必须以 http:// 或 https:// 开头（当前 {scheme or '无协议'}）")
    if parts.username or parts.password:
        raise RuleError("上游目标不能包含用户名/密码")
    if parts.query or parts.fragment:
        raise RuleError("上游目标不能包含查询串或片段")
    try:
        host = parts.hostname or ""
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as e:
        raise RuleError(f"上游端口非法: {e}")
    if not host:
        raise RuleError("上游目标缺少主机名")
    if not (1 <= port <= 65535):
        raise RuleError(f"上游端口越界: {port}")
    base = parts.path or ""
    base = "" if base in ("", "/") else "/" + base.strip("/")
    return scheme, host, port, base


def normalize_rule(raw) -> dict:
    """把任意来源（JSON 文件 / 界面 / CLI）的规则规范化并校验。

    * 非 dict → :class:`RuleError`；
    * 关键字段类型/取值非法 → :class:`RuleError`（**不静默补默认值放行**）；
    * 未知键丢弃；返回仅含 :data:`RULE_DEFAULTS` 全部键的新 dict。
    """
    if raw is None:
        raise RuleError("规则不能为空")
    if not isinstance(raw, dict):
        raise RuleError(f"规则必须是 JSON 对象（当前是 {type(raw).__name__}）")

    rule = dict(RULE_DEFAULTS)

    rid = raw.get("id", "")
    if rid:
        rid = _clean_str(rid, "id", max_len=64)
        if not _RULE_ID_RE.match(rid):
            raise RuleError("id 只允许字母/数字/下划线/连字符（≤64 字符）")
    else:
        rid = new_rule_id()
    rule["id"] = rid

    name = raw.get("name", "")
    if not isinstance(name, str):
        raise RuleError("name 必须是字符串")
    rule["name"] = _clean_str(name, "name", max_len=120)

    rule["enabled"] = _clean_bool(raw.get("enabled"), "enabled", True)

    host = _clean_str(raw.get("listen_host", DEFAULT_LISTEN_HOST), "listen_host",
                      allow_empty=False, max_len=253)
    if ":" in host:
        try:
            socket.inet_pton(socket.AF_INET6, host.strip("[]"))
        except OSError:
            raise RuleError(f"监听地址不是合法的 IPv6 地址: {host}")
    elif host != "localhost":
        try:
            socket.inet_aton(host)
        except OSError:
            raise RuleError(f"监听地址不合法: {host}（可填 127.0.0.1 / 0.0.0.0 / localhost）")
    rule["listen_host"] = host

    port_raw = raw.get("listen_port", DEFAULT_LISTEN_PORT)
    if isinstance(port_raw, bool):
        raise RuleError("listen_port 必须是数字")
    if isinstance(port_raw, str) and port_raw.strip().isdigit():
        port_raw = int(port_raw.strip())
    if not isinstance(port_raw, int):
        raise RuleError(f"listen_port 必须是 1–65535 的整数（当前 {port_raw!r}）")
    if not (1 <= port_raw <= 65535):
        raise RuleError(f"listen_port 越界: {port_raw}（应在 1–65535）")
    if port_raw in RESERVED_PORTS:
        raise RuleError(f"端口 {port_raw} 是帽兜工具自身端口（12345/12346/12347），请换一个")
    rule["listen_port"] = port_raw

    rule["upstream"] = _clean_str(raw.get("upstream"), "upstream", allow_empty=False, max_len=2048)
    parse_upstream(rule["upstream"])  # 立即校验，错误信息更贴近用户输入

    for field in ("strip_prefix", "add_prefix"):
        value = _clean_str(raw.get(field, ""), field, max_len=1024)
        if value and not value.startswith("/"):
            raise RuleError(f"{field} 必须以 / 开头（当前 {value!r}）")
        rule[field] = "" if value in ("", "/") else value.rstrip("/")

    rule["host_header"] = _clean_str(raw.get("host_header", ""), "host_header", max_len=253)

    # match_host：按 Host 头分发（同一监听端口上可挂多条规则）
    match = _clean_str(raw.get("match_host", ""), "match_host", max_len=253).strip().lower()
    match = match.rstrip(".")
    if match:
        if "://" in match or "/" in match:
            raise RuleError("match_host 只填域名，不含协议与路径")
        if match.startswith("."):
            raise RuleError("match_host 的通配请写成 *.example.com")
        core = match[2:] if match.startswith("*.") else match
        if not core or not _HOST_RE.match(core):
            raise RuleError(f"match_host 不是合法域名: {match!r}")
    rule["match_host"] = match

    for field, default in (("rewrite_location", True), ("verify_tls", True),
                           ("allow_connect", True)):
        rule[field] = _clean_bool(raw.get(field), field, default)

    rule["connect_timeout"] = _clean_timeout(
        raw.get("connect_timeout"), "connect_timeout", DEFAULT_CONNECT_TIMEOUT)
    rule["read_timeout"] = _clean_timeout(
        raw.get("read_timeout"), "read_timeout", DEFAULT_READ_TIMEOUT)

    cert = _clean_str(raw.get("tls_cert", ""), "tls_cert", max_len=1024)
    key = _clean_str(raw.get("tls_key", ""), "tls_key", max_len=1024)
    if bool(cert) != bool(key):
        raise RuleError("入口 TLS 必须同时提供证书与私钥（tls_cert / tls_key）")
    rule["tls_cert"], rule["tls_key"] = cert, key

    rule["request_headers"] = _clean_headers(raw.get("request_headers"), "request_headers")
    rule["response_headers"] = _clean_headers(raw.get("response_headers"), "response_headers")
    return rule


def rule_display(rule: dict) -> str:
    """``"127.0.0.1:8888 → https://api.example.com"`` 形式的单行摘要。"""
    if not isinstance(rule, dict):
        return ""
    host = str(rule.get("listen_host", DEFAULT_LISTEN_HOST))
    port = rule.get("listen_port", DEFAULT_LISTEN_PORT)
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port} → {rule.get('upstream', '')}"


# ─────────────────────────── 规则持久化 ───────────────────────────

def default_rules_path() -> str:
    """``%APPDATA%/LIWENTool/proxy_rules.json``（跨平台由 settings 决定）。"""
    from utils.settings import SETTINGS_DIR
    return os.path.join(SETTINGS_DIR, "proxy_rules.json")


class ProxyRuleStore:
    """反向代理规则的持久化（原子写、损坏逐条跳过）。"""

    def __init__(self, path: str = None):
        self.path = path or default_rules_path()
        self._rules = []
        self.load()

    def load(self) -> list:
        """读取规则。文件缺失/损坏 → 空列表；单条非法 → 跳过并记日志。"""
        from utils import jsonstore
        raw = jsonstore.load_json(self.path, default=[], expect_type=list)
        rules = []
        for item in raw:
            try:
                rules.append(normalize_rule(item))
            except RuleError as e:
                warn(f"反向代理规则已跳过（非法）: {e}")
        self._rules = rules
        return [dict(r) for r in self._rules]

    def save(self, rules=None) -> bool:
        from utils import jsonstore
        if rules is not None:
            self._rules = [normalize_rule(r) for r in rules]
        return jsonstore.save_json(self.path, [dict(r) for r in self._rules])

    def all(self) -> list:
        return [dict(r) for r in self._rules]

    def get(self, rule_id: str):
        for rule in self._rules:
            if rule.get("id") == rule_id:
                return dict(rule)
        return None

    def add(self, rule) -> dict:
        rule = normalize_rule(rule)
        self._rules = [r for r in self._rules if r.get("id") != rule["id"]]
        self._rules.append(rule)
        self.save()
        return dict(rule)

    def update(self, rule) -> dict:
        """原地更新（保留原有顺序）；规则不存在时追加到末尾。

        界面按列表顺序显示规则，早期实现直接复用 ``add()``（移除后追加），
        编辑/启停一条规则会让它在表格里跳到末尾，与用户认知不一致。
        """
        rule = normalize_rule(rule)
        for index, existing in enumerate(self._rules):
            if existing.get("id") == rule["id"]:
                self._rules[index] = rule
                self.save()
                return dict(rule)
        self._rules.append(rule)
        self.save()
        return dict(rule)

    def remove(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.get("id") != rule_id]
        if len(self._rules) != before:
            self.save()
            return True
        return False


# ───────────────────────────── 读写缓冲 ─────────────────────────────

class _Reader:
    """带缓冲的 socket 读取器（头块 / 定长 / chunked / 行）。"""

    def __init__(self, sock, label: str = "socket"):
        self._sock = sock
        self._label = label
        self._buf = bytearray()

    def has_buffered(self) -> bool:
        return bool(self._buf)

    def buffered(self) -> bytes:
        data = bytes(self._buf)
        self._buf.clear()
        return data

    def read_some(self, limit: int) -> bytes:
        if self._buf:
            take = min(limit, len(self._buf))
            data = bytes(self._buf[:take])
            del self._buf[:take]
            return data
        try:
            data = self._sock.recv(limit)
        except socket.timeout:
            raise _Timeout(f"{self._label} 读取超时")
        except OSError as e:
            raise _Closed(f"{self._label} 读取失败: {e}")
        if not data:
            raise _Closed(f"{self._label} 已关闭")
        return data

    def read_exact(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            out += self.read_some(size - len(out))
        return bytes(out)

    def read_head(self, max_bytes: int) -> bytes:
        """读到空行结束（``\\r\\n\\r\\n``，兼容裸 ``\\n\\n``）。"""
        while True:
            idx, sep_len = self._find_head_end()
            if idx >= 0:
                head = bytes(self._buf[:idx])
                del self._buf[:idx + sep_len]
                return head
            if len(self._buf) > max_bytes:
                raise _TooLarge(f"{self._label} 头块超过 {max_bytes} 字节")
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                raise _Timeout(f"{self._label} 读取超时")
            except OSError as e:
                raise _Closed(f"{self._label} 读取失败: {e}")
            if not chunk:
                if not self._buf:
                    raise _Closed(f"{self._label} 已关闭")
                raise _BadRequest(f"{self._label} 报文在头块结束前中断")
            self._buf += chunk

    def _find_head_end(self) -> tuple:
        i = self._buf.find(b"\r\n\r\n")
        j = self._buf.find(b"\n\n")
        if i < 0 and j < 0:
            return -1, 0
        if i < 0:
            return j, 2
        if j < 0:
            return i, 4
        return (i, 4) if i <= j else (j, 2)

    def read_line(self, max_len: int = MAX_CHUNK_LINE) -> bytes:
        """读一行（含行尾）。超长抛 :class:`_BadRequest`。"""
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx + 1])
                del self._buf[:idx + 1]
                if len(line) > max_len:
                    raise _BadRequest(f"{self._label} 单行超过 {max_len} 字节")
                return line
            if len(self._buf) > max_len:
                raise _BadRequest(f"{self._label} 单行超过 {max_len} 字节")
            self._buf += self.read_some(4096)


# ───────────────────────────── 头操作 ─────────────────────────────

def _tokens(value: str) -> list:
    return [t.strip().lower() for t in str(value).split(",") if t.strip()]


def _hget(headers: list, name: str):
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value
    return None


def _hdel(headers: list, name: str) -> None:
    target = name.lower()
    headers[:] = [(k, v) for k, v in headers if k.lower() != target]


def _hset(headers: list, name: str, value: str) -> None:
    _hdel(headers, name)
    headers.append((name, value))


class _Request:
    """解析后的请求。"""

    __slots__ = ("method", "target", "version", "headers", "path", "query",
                 "authority", "host_header", "upgrade")

    def __init__(self):
        self.method = "GET"
        self.target = "/"
        self.version = "HTTP/1.1"
        self.headers = []
        self.path = "/"
        self.query = ""
        self.authority = ""
        self.host_header = ""
        self.upgrade = False


def _parse_request_head(head: bytes, max_line: int = MAX_REQUEST_LINE,
                        max_headers: int = MAX_HEADERS) -> _Request:
    """解析请求头块。畸形一律 :class:`_BadRequest`（→ 400）。"""
    if b"\x00" in head:
        raise _BadRequest("请求头包含 NUL 字节")
    # 裸 CR（不是 CRLF 的一部分）：不同实现对齐方式不同 → 头注入/走私温床，直接拒绝
    if b"\r" in head.replace(b"\r\n", b""):
        raise _BadRequest("请求头包含单独的 CR（非 CRLF 换行）")
    if b"\n" in head.replace(b"\r\n", b""):
        raise _BadRequest("请求头包含单独的 LF（非 CRLF 换行）")
    text = head.decode("latin-1")
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise _BadRequest("空请求")

    start_line = lines[0]
    if len(start_line) > max_line:
        raise _BadRequest(f"请求行过长（上限 {max_line} 字节）")
    parts = start_line.split(" ")
    if len(parts) != 3:
        raise _BadRequest("请求行格式非法（应为 METHOD TARGET VERSION）")
    method, target, version = parts
    if not _TOKEN_RE.match(method):
        raise _BadRequest(f"HTTP 方法非法: {method!r}")
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise _BadRequest(f"仅支持 HTTP/1.1 与 HTTP/1.0（收到 {version}）")
    if not target:
        raise _BadRequest("请求目标为空")

    req = _Request()
    req.method = method.upper()
    req.target = target
    req.version = version

    if len(lines) - 1 > max_headers:
        raise _TooLarge(f"请求头字段过多（上限 {max_headers}）")
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in (" ", "\t"):
            raise _BadRequest("不支持折行的请求头")
        if ":" not in line:
            raise _BadRequest(f"请求头缺少冒号: {line[:40]!r}")
        name, _, value = line.partition(":")
        name = name.strip()
        if not _TOKEN_RE.match(name):
            raise _BadRequest(f"请求头名非法: {name!r}")
        req.headers.append((name, value.strip()))

    # 目标形式：origin-form / absolute-form / authority-form(CONNECT) / asterisk-form
    if req.method == "CONNECT":
        req.authority = target
        if " " in target or not target:
            raise _BadRequest("CONNECT 目标非法")
    elif target.startswith("http://") or target.startswith("https://"):
        try:
            parts_u = urlsplit(target)
        except ValueError as e:
            raise _BadRequest(f"绝对形式请求目标非法: {e}")
        if not parts_u.hostname:
            raise _BadRequest("绝对形式请求目标缺少主机名")
        req.path = parts_u.path or "/"
        req.query = parts_u.query
        host = parts_u.hostname
        if parts_u.port:
            host = f"{host}:{parts_u.port}"
        req.headers.append(("Host", host))
    elif target == "*":
        req.path = "*"
    elif target.startswith("/"):
        path, _, query = target.partition("?")
        req.path = path
        req.query = query
    else:
        raise _BadRequest(f"不支持的请求目标形式: {target[:40]!r}")

    # 重复 Content-Length（走私经典手法）：值不一致必须拒绝，一致则去重
    cl_values = [v.strip() for k, v in req.headers if k.lower() == "content-length"]
    if cl_values:
        if len(set(cl_values)) > 1:
            raise _BadRequest(f"Content-Length 冲突: {cl_values}")
        if len(cl_values) > 1:
            seen = False
            deduped = []
            for name, value in req.headers:
                if name.lower() == "content-length":
                    if seen:
                        continue
                    seen = True
                deduped.append((name, value))
            req.headers = deduped

    req.host_header = _hget(req.headers, "host") or ""
    conn_tokens = _tokens(_hget(req.headers, "connection") or "")
    upgrade = (_hget(req.headers, "upgrade") or "").strip()
    req.upgrade = bool(upgrade) and "upgrade" in conn_tokens
    return req


def _request_body_mode(req: _Request) -> tuple:
    """返回 ``("none"|"length"|"chunked", size)``。走私/畸形一律 400。"""
    te = _hget(req.headers, "transfer-encoding")
    cl = _hget(req.headers, "content-length")
    if te is not None and cl is not None:
        raise _BadRequest("同时存在 Content-Length 与 Transfer-Encoding（疑似请求走私）")
    if te is not None:
        tokens = _tokens(te)
        if len(tokens) != 1 or tokens[0] != "chunked":
            raise _BadRequest(f"不支持的 Transfer-Encoding: {te}")
        return "chunked", 0
    if cl is not None:
        text = cl.strip()
        if not text.isdigit():
            raise _BadRequest(f"Content-Length 不是合法数字: {cl!r}")
        size = int(text)
        if size > MAX_DECLARED_BYTES:
            raise _BadRequest(f"Content-Length 超过上限: {size}")
        return "length", size
    return "none", 0


# ───────────────────────────── 证书工具 ─────────────────────────────

def openssl_available() -> bool:
    """系统是否有可用的 openssl（自签证书生成依赖它）。"""
    return shutil.which("openssl") is not None


def generate_self_signed_cert(cert_path: str, key_path: str,
                              common_name: str = "localhost",
                              days: int = 825) -> tuple:
    """用系统 openssl 生成自签证书。返回 ``(成功?, 中文说明)``。

    **绝不假装成功**：openssl 缺失或命令失败时返回 ``False`` 与真实原因
    （stderr 摘要），调用方必须把原因展示给用户。
    """
    if not cert_path or not key_path:
        return False, "证书与私钥路径都不能为空"
    if not openssl_available():
        return False, ("未找到 openssl 命令，无法自动生成证书。"
                       "请安装 openssl，或手动生成后用「入口证书/私钥」选择文件")
    if not isinstance(common_name, str) or not common_name.strip():
        common_name = "localhost"
    common_name = common_name.strip()[:253]
    if "\n" in common_name or "\r" in common_name:
        return False, "证书 CN 不能包含换行"

    try:
        os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
    except OSError as e:
        return False, f"无法创建证书目录: {e}"

    cfg_path = os.path.abspath(cert_path) + ".openssl.cnf"
    cfg = (
        "[req]\n"
        "distinguished_name = dn\n"
        "x509_extensions = v3\n"
        "prompt = no\n"
        "[dn]\n"
        f"CN = {common_name}\n"
        "O = LIWEN Tool\n"
        "[v3]\n"
        "basicConstraints = CA:FALSE\n"
        "keyUsage = digitalSignature,keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectAltName = @alt\n"
        "[alt]\n"
        "DNS.1 = localhost\n"
        f"DNS.2 = {common_name}\n"
        "IP.1 = 127.0.0.1\n"
    )
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", os.path.abspath(key_path), "-out", os.path.abspath(cert_path),
        "-days", str(int(days)), "-config", cfg_path,
    ]
    kwargs = {"capture_output": True, "timeout": 60}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = None
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(cfg)
        proc = subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"调用 openssl 失败: {e}"
    finally:
        try:
            os.remove(cfg_path)
        except OSError:
            pass

    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        return False, f"openssl 生成失败: {tail}"
    for path in (cert_path, key_path):
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False, f"openssl 报告成功但文件未生成: {path}"
    info(f"已生成自签证书: {cert_path}")
    return True, f"已生成自签证书（CN={common_name}, {int(days)} 天）"


# ───────────────────────────── 运行引擎 ─────────────────────────────

def _make_client_ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _host_only(host_header: str) -> str:
    """从 ``Host`` 头里取出小写主机名（去掉端口与 IPv6 方括号）。"""
    text = (host_header or "").strip().lower()
    if not text:
        return ""
    if text.startswith("["):                      # [::1]:8080
        end = text.find("]")
        return text[1:end] if end > 0 else text
    if text.count(":") == 1:                      # host:port
        return text.split(":", 1)[0]
    return text


def validate_groups(rules) -> None:
    """校验「同一监听端口上的多条规则」这一组合是否合法。

    同一端口可以挂多条规则（按 Host 分发），但必须满足：
    至多一条不限 Host 的兜底规则；``match_host`` 互不重复；证书配置一致。
    """
    groups = {}
    for rule in rules:
        groups.setdefault((rule["listen_host"], rule["listen_port"]), []).append(rule)
    for (host, port), group in groups.items():
        seen = {}
        for rule in group:
            key = rule.get("match_host") or ""
            if key in seen:
                other = seen[key]
                label = other.get("name") or other["id"]
                if key:
                    raise RuleError(
                        f"{host}:{port} 上 match_host={key} 已被规则「{label}」占用")
                raise RuleError(
                    f"{host}:{port} 只能有一条不限 Host 的兜底规则"
                    f"（已被规则「{label}」占用）")
            seen[key] = rule
        tls = {(r.get("tls_cert") or "", r.get("tls_key") or "") for r in group}
        if len(tls) > 1:
            raise RuleError(
                f"{host}:{port} 上的多条规则必须使用相同的入口证书"
                "（同一端口只能有一份 TLS 配置）")


class _Target:
    """编译后的上游目标。"""

    __slots__ = ("scheme", "host", "port", "base", "tls_context", "origin")

    def __init__(self, rule: dict):
        scheme, host, port, base = parse_upstream(rule["upstream"])
        self.scheme = scheme
        self.host = host
        self.port = port
        self.base = base
        self.origin = f"{scheme}://{host}" + (
            f":{port}" if port != DEFAULT_PORTS[scheme] else "")
        self.tls_context = None
        if scheme == "https":
            self.tls_context = _make_client_ssl_context(bool(rule.get("verify_tls", True)))

    def host_header(self) -> str:
        if self.port == DEFAULT_PORTS[self.scheme]:
            return self.host
        return f"{self.host}:{self.port}"


class _Listener:
    """一个监听端口（``listen_host:listen_port``）上的一组规则，按 Host 分发。"""

    __slots__ = ("host", "port", "routes", "sock", "thread", "tls_context",
                 "conns", "family", "closed")

    def __init__(self, host: str, port: int):
        self.host = str(host)
        self.port = int(port)
        #: match_host（小写，"" 表示兜底）→ (rule, _Target)
        self.routes = {}
        self.sock = None
        self.thread = None
        self.tls_context = None
        self.conns = set()
        self.family = socket.AF_INET
        #: 该端口被热移除/停止时置位：让在途连接的空闲等待立刻退出
        self.closed = threading.Event()

    @property
    def key(self) -> tuple:
        return (self.host, self.port)

    def add(self, rule: dict) -> None:
        self.routes[rule.get("match_host") or ""] = (rule, _Target(rule))

    def remove(self, rule_id: str) -> bool:
        for key, (rule, _target) in list(self.routes.items()):
            if rule["id"] == rule_id:
                del self.routes[key]
                return True
        return False

    def rules(self) -> list:
        return [rule for rule, _target in self.routes.values()]

    def default_rule(self) -> tuple:
        """兜底规则：显式的不限 Host 规则；只有一个候选时就是它。"""
        entry = self.routes.get("")
        if entry is None and len(self.routes) == 1:
            entry = next(iter(self.routes.values()))
        return entry or (None, None)

    def pick(self, host_header: str) -> tuple:
        """按 Host 选规则：精确 → 通配（取最长） → 兜底。

        返回 ``(rule, target)``；``(None, None)`` 表示没有可用的规则。
        """
        host = _host_only(host_header)
        if host:
            exact = self.routes.get(host)
            if exact is not None:
                return exact
            best = None
            for key, entry in self.routes.items():
                if not key.startswith("*."):
                    continue
                suffix = key[1:]                       # ".example.com"
                head = host[:-len(suffix)] if host.endswith(suffix) else ""
                # 通配只吃一级标签：api.example.com 命中，a.api.example.com 不命中
                if head and "." not in head:
                    if best is None or len(key) > len(best[0]):
                        best = (key, entry)
            if best is not None:
                return best[1]
        # 兜底：显式的 match_host="" 规则；只有一条规则时用它；
        # 多条 Host 规则且都不匹配 → (None, None)，由调用方回 404
        return self.default_rule()

    def listen_label(self) -> str:
        host = self.host
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{self.port}"


class ReverseProxyServer:
    """多规则反向代理服务器。

    ``start()`` 逐条绑定监听（部分失败照常返回 ``failed``）；``stop()`` 幂等。
    事件与统计供界面/CLI 展示，核心层不依赖任何 GUI。
    """

    def __init__(self, rules=None, on_event=None):
        self._rules = []
        self._listeners = {}
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._threads = set()
        self._active = set()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._events = collections.deque(maxlen=MAX_LOG_EVENTS)
        self._stats = {"requests": 0, "errors": 0, "bytes_in": 0, "bytes_out": 0}
        self._rule_stats = {}
        self._on_event = on_event
        self._started = False
        for rule in rules or []:
            self._rules.append(normalize_rule(rule))

    # ── 生命周期 ──

    @property
    def running(self) -> bool:
        return self._started and not self._stop.is_set()

    def start(self) -> dict:
        """绑定所有启用规则的监听端口（同一端口的规则共用一个监听器）。

        返回 ``{"started": [...], "failed": [...]}``；全部失败抛
        :class:`ProxyStartError`（**不谎报成功**）。
        """
        with function_scope("ReverseProxyServer.start"):
            with self._lock:
                if self.running:
                    return {"started": self.listeners(), "failed": []}
                self._stop = threading.Event()
                self._listeners = {}
                self._started = True

            enabled = [r for r in self._rules if r.get("enabled", True)]
            if not enabled:
                self._started = False
                raise ProxyStartError("没有已启用的规则，请先新建并启用一条规则")
            try:
                validate_groups(enabled)
            except RuleError as e:
                self._started = False
                raise ProxyStartError(f"规则配置冲突：{e}")

            started, failed = [], []
            groups = {}
            for rule in enabled:
                groups.setdefault((rule["listen_host"], rule["listen_port"]), []).append(rule)

            for key, group in groups.items():
                try:
                    listener = self._bind_listener(group)
                except (ProxyError, RuleError, OSError, ValueError) as e:
                    reason = self._friendly_bind_error(group[0], e)
                    for rule in group:
                        failed.append({"rule_id": rule["id"], "name": rule.get("name", ""),
                                       "listen": rule_display(rule), "error": reason})
                    log_error(f"反向代理监听失败 ({key[0]}:{key[1]}): {reason}")
                    continue
                with self._lock:
                    self._listeners[key] = listener
                    for rule in group:
                        self._rule_stats.setdefault(rule["id"], {
                            "requests": 0, "errors": 0, "bytes_in": 0, "bytes_out": 0})
                thread = threading.Thread(
                    target=self._accept_loop, args=(listener,),
                    name=f"proxy-accept-{key[0]}-{key[1]}", daemon=True)
                listener.thread = thread
                with self._lock:
                    self._threads.add(thread)
                thread.start()
                started.extend(self._listener_infos(listener))
                for rule in group:
                    info(f"反向代理已监听 {listener.listen_label()}"
                         f"{' [Host ' + rule['match_host'] + ']' if rule.get('match_host') else ''}"
                         f" → {rule['upstream']}"
                         f"{'（TLS 终止）' if listener.tls_context else ''}")

            if not started:
                self._started = False
                reasons = "；".join(f"{f['listen']}: {f['error']}" for f in failed)
                raise ProxyStartError(f"所有规则都未能启动：{reasons}")
            return {"started": started, "failed": failed}

    def stop(self, timeout: float = 3.0) -> dict:
        """关闭监听与活动连接并等工作线程退出（幂等）。"""
        with self._lock:
            was_running = self._started or bool(self._listeners)
            self._stop.set()
            listeners = list(self._listeners.values())
            self._listeners = {}
            threads = list(self._threads)
            active = list(self._active)
            self._started = False
        if not was_running and not threads:
            return {"stopped": 0, "leaked_threads": 0}

        for listener in listeners:
            listener.closed.set()
            self._close_socket(listener.sock)
        for sock in active:
            self._close_socket(sock)

        deadline = time.monotonic() + max(0.1, float(timeout))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if thread.is_alive():
                thread.join(remaining)

        with self._lock:
            alive = [t for t in threads if t.is_alive()]
            self._threads -= set(threads) - set(alive)
            self._active.clear()
        if alive:
            warn(f"反向代理停止时仍有 {len(alive)} 个工作线程未退出"
                 f"（{', '.join(t.name for t in alive)}）——保留引用，绝不 terminate()")
        info(f"反向代理已停止（监听 {len(listeners)} 条，残留线程 {len(alive)}）")
        return {"stopped": len(listeners), "leaked_threads": len(alive)}

    def listeners(self) -> list:
        """每条规则一条记录（同一端口的多条规则各自报告，便于界面标注状态）。"""
        with self._lock:
            out = []
            for listener in self._listeners.values():
                out.extend(self._listener_infos(listener))
            return out

    def _rule_listening(self, rule_id: str) -> bool:
        """该规则当前是否挂在某个已绑定的监听器上（同端口多规则时逐条判断）。"""
        for listener in self._listeners.values():
            if any(rule["id"] == rule_id for rule in listener.rules()):
                return True
        return False

    def _listener_infos(self, listener: _Listener) -> list:
        infos = []
        for rule in listener.rules():
            infos.append({
                "rule_id": rule["id"],
                "name": rule.get("name", ""),
                "listen_host": listener.host,
                "listen_port": listener.port,
                "listen": listener.listen_label(),
                "upstream": rule["upstream"],
                "match_host": rule.get("match_host", ""),
                "tls": listener.tls_context is not None,
                "shared": len(listener.routes) > 1,
            })
        return infos

    def _listener_info(self, listener: _Listener, rule: dict = None) -> dict:
        infos = self._listener_infos(listener)
        if rule is None:
            return infos[0] if infos else {}
        for info in infos:
            if info["rule_id"] == rule["id"]:
                return info
        return infos[0] if infos else {}

    def add_rule(self, rule) -> dict:
        """热添加一条规则；``enabled`` 时立即生效。

        同一监听端口上已存在监听器时**复用**它（按 Host 分发），不重建 socket，
        因此在途连接不受影响。
        """
        rule = normalize_rule(rule)
        with self._lock:
            self._rules = [r for r in self._rules if r["id"] != rule["id"]]
            self._rules.append(rule)
            self._rule_stats.setdefault(rule["id"], {
                "requests": 0, "errors": 0, "bytes_in": 0, "bytes_out": 0})
            enabled = [r for r in self._rules if r.get("enabled", True)]
        try:
            validate_groups(enabled)
        except RuleError as e:
            return {"started": [], "failed": [{
                "rule_id": rule["id"], "name": rule.get("name", ""),
                "listen": rule_display(rule), "error": str(e)}]}

        result = {"started": [], "failed": []}
        if not rule.get("enabled", True) or not self._started:
            return result

        key = (rule["listen_host"], rule["listen_port"])
        with self._lock:
            listener = self._listeners.get(key)
        if listener is not None:
            listener.add(rule)
            result["started"].append(self._listener_info(listener, rule))
            return result

        try:
            listener = self._bind_listener([rule])
        except (ProxyError, RuleError, OSError, ValueError) as e:
            reason = self._friendly_bind_error(rule, e)
            result["failed"].append({"rule_id": rule["id"], "name": rule.get("name", ""),
                                     "listen": rule_display(rule), "error": reason})
            log_error(f"反向代理规则启动失败 ({rule_display(rule)}): {reason}")
            return result
        with self._lock:
            self._listeners[key] = listener
        thread = threading.Thread(target=self._accept_loop, args=(listener,),
                                  name=f"proxy-accept-{key[0]}-{key[1]}", daemon=True)
        listener.thread = thread
        with self._lock:
            self._threads.add(thread)
        thread.start()
        result["started"].append(self._listener_info(listener, rule))
        return result

    def remove_rule(self, rule_id: str) -> bool:
        """热移除规则。

        该端口上还有其它规则时只摘掉这一条路由（监听保留、在途连接自然结束）；
        一条不剩时才关闭监听与连接。
        """
        with self._lock:
            existed = any(r["id"] == rule_id for r in self._rules)
            self._rules = [r for r in self._rules if r["id"] != rule_id]
        emptied = None
        with self._lock:
            for key, listener in list(self._listeners.items()):
                if listener.remove(rule_id):
                    if not listener.routes:
                        self._listeners.pop(key, None)
                        emptied = listener
                    break
        if emptied is not None:
            # 先置位再关 socket：空闲等待用 select 分片轮询该标志，
            # 不依赖"关闭 fd 能唤醒阻塞的 recv"（macOS 上 shutdown 可能抛
            # ENOTCONN，此时 close() 并不会唤醒已在 recv 里的线程 —— 实测泄漏）
            emptied.closed.set()
            self._close_socket(emptied.sock)
            for sock in list(emptied.conns):
                self._close_socket(sock)
        return existed

    def replace_rules(self, rules) -> dict:
        """全量替换规则（界面保存后应用）。"""
        normalized = [normalize_rule(r) for r in (rules or [])]
        self.stop()
        with self._lock:
            self._rules = normalized
            self._rule_stats = {r["id"]: {"requests": 0, "errors": 0,
                                          "bytes_in": 0, "bytes_out": 0}
                                for r in normalized}
        try:
            result = self.start()
        except ProxyStartError as e:
            return {"started": [], "stopped": 0,
                    "failed": [{"rule_id": "", "name": "", "listen": "", "error": str(e)}]}
        result["stopped"] = 0
        return result

    # ── 统计与日志 ──

    def stats(self) -> dict:
        with self._lock:
            out = dict(self._stats)
            out["active_connections"] = len(self._active)
            rules = []
            for rule in self._rules:
                entry = dict(self._rule_stats.get(rule["id"], {
                    "requests": 0, "errors": 0, "bytes_in": 0, "bytes_out": 0}))
                entry.update({"rule_id": rule["id"], "name": rule.get("name", ""),
                              "listening": self._rule_listening(rule["id"])})
                rules.append(entry)
            out["rules"] = rules
            return out

    def drain_events(self, limit: int = 200) -> list:
        """取出并清空环形日志（线程安全）。"""
        with self._lock:
            events = list(self._events)[:max(0, int(limit))]
            for _ in events:
                try:
                    self._events.popleft()
                except IndexError:
                    break
            return events

    def _record(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)
            self._stats["requests"] += 1
            self._stats["bytes_in"] += int(event.get("bytes_in") or 0)
            self._stats["bytes_out"] += int(event.get("bytes_out") or 0)
            if int(event.get("status") or 0) >= 400:
                self._stats["errors"] += 1
            rule_stat = self._rule_stats.setdefault(event.get("rule_id", ""), {
                "requests": 0, "errors": 0, "bytes_in": 0, "bytes_out": 0})
            rule_stat["requests"] += 1
            rule_stat["bytes_in"] += int(event.get("bytes_in") or 0)
            rule_stat["bytes_out"] += int(event.get("bytes_out") or 0)
            if int(event.get("status") or 0) >= 400:
                rule_stat["errors"] += 1
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception as e:  # noqa: BLE001 — 回调异常不能影响转发
                log_error(f"反向代理事件回调失败: {e}")

    # ── 监听 ──

    def _friendly_bind_error(self, rule: dict, exc: Exception) -> str:
        if isinstance(exc, RuleError):
            return str(exc)
        port = rule.get("listen_port")
        if isinstance(exc, OSError):
            if exc.errno in (48, 98, 10048):  # EADDRINUSE (macOS/Linux/Windows)
                return f"端口 {port} 已被占用"
            if exc.errno in (13, 10013):      # EACCES
                return f"没有权限绑定端口 {port}（1024 以下端口通常需要管理员权限）"
            return f"绑定失败: {exc}"
        return str(exc)

    def _bind_listener(self, group: list) -> _Listener:
        """为一个监听端口（可能挂多条按 Host 分发的规则）绑定 socket。"""
        group = list(group)
        first = group[0]
        listener = _Listener(first["listen_host"], first["listen_port"])
        for rule in group:
            listener.add(rule)

        cert, key = first.get("tls_cert") or "", first.get("tls_key") or ""
        if cert:
            for path in (cert, key):
                if not os.path.isfile(path):
                    raise RuleError(f"入口 TLS 证书/私钥文件不存在: {path}")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.set_alpn_protocols(["http/1.1"])
            try:
                ctx.load_cert_chain(certfile=cert, keyfile=key)
            except (ssl.SSLError, OSError) as e:
                raise RuleError(f"加载入口 TLS 证书失败: {e}")
            listener.tls_context = ctx

        host = first["listen_host"]
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        listener.family = family
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            # Windows 的 SO_REUSEADDR 允许"抢注"同一端口，会让端口占用检测失效
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((host, first["listen_port"]))
            sock.listen(128)
            sock.settimeout(0.5)
        except BaseException:
            self._close_socket(sock)
            raise
        listener.sock = sock
        return listener

    def _accept_loop(self, listener: _Listener) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = listener.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:  # noqa: BLE001 — 监听线程绝不允许退出
                log_error(f"反向代理 accept 异常（继续监听）: {e}")
                continue
            if not self._slots.acquire(blocking=False):
                self._send_raw(conn, 503, "Service Unavailable",
                               f"代理并发连接已达上限（{MAX_CONNECTIONS}）")
                self._close_socket(conn)
                continue
            with self._lock:
                self._active.add(conn)
                listener.conns.add(conn)
            thread = threading.Thread(
                target=self._client_entry, args=(listener, conn, addr),
                name=f"proxy-conn-{listener.host}-{listener.port}", daemon=True)
            with self._lock:
                self._threads.add(thread)
            thread.start()

    def _client_entry(self, listener: _Listener, conn, addr) -> None:
        try:
            if listener.tls_context is not None:
                try:
                    tls_conn = listener.tls_context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError) as e:
                    warn(f"反向代理入口 TLS 握手失败 "
                         f"({addr[0] if isinstance(addr, tuple) else addr}): {e}")
                    return
                with self._lock:
                    self._active.discard(conn)
                    listener.conns.discard(conn)
                    self._active.add(tls_conn)
                    listener.conns.add(tls_conn)
                conn = tls_conn
            self._handle_client(listener, conn, addr)
        except Exception as e:  # noqa: BLE001 — 单个连接异常绝不影响监听
            log_error(f"反向代理连接处理异常 ({addr}): {e}")
        finally:
            self._close_socket(conn)
            with self._lock:
                self._active.discard(conn)
                listener.conns.discard(conn)
                self._threads.discard(threading.current_thread())
            self._slots.release()

    def _track_upstream(self, sock) -> None:
        with self._lock:
            self._active.add(sock)

    def _drop_socket(self, sock) -> None:
        """关闭并停止跟踪一个上游 socket（stop() 时会被一并关闭）。"""
        with self._lock:
            self._active.discard(sock)
        self._close_socket(sock)

    @staticmethod
    def _close_socket(sock) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError, NotImplementedError):
            pass
        try:
            sock.close()
        except (OSError, ValueError):
            pass

    def _send_raw(self, sock, status: int, reason: str, detail: str) -> None:
        body = f"{status} {reason}\n{detail}\n".encode("utf-8")
        head = (f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n").encode("latin-1")
        try:
            sock.sendall(head + body)
        except OSError:
            pass

    # ── 单连接处理 ──

    def _wait_readable(self, sock, listener: _Listener, deadline: float) -> bool:
        """空闲等待：分片 select 轮询，能立刻感知 stop/规则被移除。

        不依赖"关闭 fd 唤醒阻塞 recv"—— macOS 上 shutdown() 对已进入
        CLOSE_WAIT 的连接可能抛 ENOTCONN，随后的 close() 并不会唤醒已阻塞
        在 recv 里的线程（实测会留下永不退出的工作线程）。
        """
        while not self._stop.is_set() and not listener.closed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                ready, _, _ = select.select([sock], [], [], min(0.5, remaining))
            except (OSError, ValueError):
                return False
            if ready:
                return True
        return False

    def _handle_client(self, listener: _Listener, conn, addr) -> None:
        client_ip = addr[0] if isinstance(addr, tuple) and addr else ""
        base_rule, _base_target = listener.default_rule()
        read_timeout = float(base_rule["read_timeout"]) if base_rule else DEFAULT_READ_TIMEOUT
        conn.settimeout(read_timeout)
        reader = _Reader(conn, "客户端")
        upstream = None
        idle_deadline = time.monotonic() + read_timeout
        try:
            while not self._stop.is_set() and not listener.closed.is_set():
                if not reader.has_buffered():
                    if not self._wait_readable(conn, listener, idle_deadline):
                        return
                try:
                    head = reader.read_head(MAX_HEAD_BYTES)
                except _Closed:
                    return
                except _TooLarge as e:
                    self._error_event(base_rule, "?", "?", 431, client_ip, str(e))
                    self._send_raw(conn, 431, "Request Header Fields Too Large", str(e))
                    return
                except _Timeout:
                    return
                except _BadRequest as e:
                    self._error_event(base_rule, "?", "?", 400, client_ip, str(e))
                    self._send_raw(conn, 400, "Bad Request", str(e))
                    return

                try:
                    req = _parse_request_head(head)
                    body_mode, body_size = _request_body_mode(req)
                except _TooLarge as e:
                    self._error_event(base_rule, "?", "?", 431, client_ip, str(e))
                    self._send_raw(conn, 431, "Request Header Fields Too Large", str(e))
                    return
                except _BadRequest as e:
                    self._error_event(base_rule, "?", "?", 400, client_ip, str(e))
                    self._send_raw(conn, 400, "Bad Request", str(e))
                    return

                # 同一入口可挂多条规则：按 Host 头（CONNECT 用 authority）分发
                rule, target = listener.pick(
                    req.authority if req.method == "CONNECT" else req.host_header)
                if rule is None:
                    detail = (f"该入口没有匹配 Host {req.host_header or '(未提供)'} 的规则"
                              "（可用规则: " + listener.listen_label() + "）")
                    self._send_raw(conn, 404, "Not Found", detail)
                    # 无归属事件（rule_id=""）：这类 404 恰恰是用户最需要看到的，
                    # 只写日志文件的话界面上的访问日志会"看起来一切正常"
                    self._record({"ts": time.time(), "rule_id": "", "method": req.method,
                                  "target": req.target, "status": 404, "ms": 0,
                                  "bytes_in": 0, "bytes_out": 0, "client": client_ip,
                                  "error": detail, "upgrade": False})
                    warn(f"反向代理无匹配规则: {detail}")
                    return

                read_timeout = float(rule["read_timeout"])
                conn.settimeout(read_timeout)
                idle_deadline = time.monotonic() + read_timeout

                if req.method == "CONNECT":
                    self._handle_connect(listener, conn, reader, req, client_ip,
                                         rule, target)
                    return

                started = time.monotonic()
                keep_client = self._want_keepalive(req)
                upstream, keep_upstream, status, error, bytes_in, bytes_out = self._forward(
                    listener, conn, reader, req, body_mode, body_size, upstream,
                    client_ip, rule, target)
                if status is None:
                    return
                self._record({
                    "ts": time.time(), "rule_id": rule["id"],
                    "method": req.method, "target": req.target, "status": status,
                    "ms": int((time.monotonic() - started) * 1000),
                    "bytes_in": bytes_in, "bytes_out": bytes_out,
                    "client": client_ip, "error": error, "upgrade": False,
                })
                if error or not keep_client or not keep_upstream:
                    return
        finally:
            if upstream is not None:
                self._drop_socket(upstream[0])

    @staticmethod
    def _want_keepalive(req: _Request) -> bool:
        tokens = _tokens(_hget(req.headers, "connection") or "")
        if "close" in tokens:
            return False
        if req.version == "HTTP/1.0":
            return "keep-alive" in tokens
        return True

    def _error_event(self, rule: dict, method: str, target: str, status: int,
                     client: str, error: str) -> None:
        if not rule:
            return
        self._record({"ts": time.time(), "rule_id": rule["id"], "method": method,
                      "target": target, "status": status, "ms": 0, "bytes_in": 0,
                      "bytes_out": 0, "client": client, "error": error,
                      "upgrade": False})

    # ── 转发 ──

    def _forward(self, listener: _Listener, conn, reader: _Reader, req: _Request,
                 body_mode: str, body_size: int, upstream, client_ip: str,
                 rule: dict, target: _Target):
        """转发一次请求（``rule``/``target`` 是本次按 Host 选中的规则）。

        返回 ``(upstream, keep_upstream, status, error, bytes_in, bytes_out)``；
        ``status is None`` 表示本次不记事件（隧道或客户端提前断开）。
        """
        bytes_in = len(req.target)  # 请求行+头的粗略计数在下面累加
        bytes_out = 0

        # 1) 连接上游（按需复用）
        if upstream is not None and upstream[3] and upstream[2] is target:
            up_sock, up_reader = upstream[0], upstream[1]
        else:
            if upstream is not None:
                self._drop_socket(upstream[0])
            try:
                up_sock = self._connect_upstream(rule, target)
            except _Timeout as e:
                return (None, False, 504,
                        self._fail(conn, rule, req, 504, "Gateway Timeout", str(e), client_ip),
                        0, 0)
            except (OSError, ssl.SSLError, ProxyError) as e:
                return (None, False, 502,
                        self._fail(conn, rule, req, 502, "Bad Gateway", str(e), client_ip),
                        0, 0)
            up_sock.settimeout(float(rule["read_timeout"]))
            up_reader = _Reader(up_sock, "上游")
            self._track_upstream(up_sock)

        # 2) 请求头
        try:
            head_bytes = self._build_request_head(req, target, rule, listener, client_ip,
                                                  chunked=(body_mode == "chunked"))
        except _BadRequest as e:
            self._drop_socket(up_sock)
            return (None, False, 400,
                    self._fail(conn, rule, req, 400, "Bad Request", str(e), client_ip), 0, 0)

        expect = (_hget(req.headers, "expect") or "").lower()
        if not req.upgrade and "100-continue" in expect:
            try:
                conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            except OSError:
                self._drop_socket(up_sock)
                return (None, False, None, None, 0, 0)

        # 3) 发送请求头 + 流式 body
        try:
            up_sock.sendall(head_bytes)
            bytes_in += len(head_bytes)
            if body_mode == "length" and body_size:
                bytes_in += self._relay_exact(reader, up_sock, body_size)
            elif body_mode == "chunked":
                bytes_in += self._relay_chunked(reader, up_sock)
        except _BadRequest as e:
            self._drop_socket(up_sock)
            return (None, False, 400,
                    self._fail(conn, rule, req, 400, "Bad Request", str(e), client_ip),
                    bytes_in, 0)
        except (_Closed, _Timeout, OSError):
            self._drop_socket(up_sock)
            return (None, False, None, None, 0, 0)

        # 4) 读上游响应头（1xx 原样透传）
        try:
            while True:
                resp_head = up_reader.read_head(MAX_HEAD_BYTES)
                status, reason, resp_headers = self._parse_response_head(resp_head)
                if 100 <= status < 200 and status != 101:
                    conn.sendall(resp_head + b"\r\n\r\n")
                    bytes_out += len(resp_head) + 4
                    continue
                break
        except _TooLarge as e:
            self._drop_socket(up_sock)
            return (None, False, 502,
                    self._fail(conn, rule, req, 502, "Bad Gateway", str(e), client_ip),
                    bytes_in, bytes_out)
        except _BadRequest as e:
            self._drop_socket(up_sock)
            return (None, False, 502,
                    self._fail(conn, rule, req, 502, "Bad Gateway", str(e), client_ip),
                    bytes_in, bytes_out)
        except _Timeout as e:
            self._drop_socket(up_sock)
            return (None, False, 504,
                    self._fail(conn, rule, req, 504, "Gateway Timeout", str(e), client_ip),
                    bytes_in, bytes_out)
        except (_Closed, OSError) as e:
            self._drop_socket(up_sock)
            return (None, False, 502,
                    self._fail(conn, rule, req, 502, "Bad Gateway",
                               f"上游未返回响应: {e}", client_ip), bytes_in, bytes_out)

        # 5) 101 → 原始双向隧道
        if status == 101:
            try:
                conn.sendall(self._build_response_head(
                    status, reason, resp_headers, rule, req, target, close_client=False))
            except OSError:
                self._drop_socket(up_sock)
                return (None, False, None, None, 0, 0)
            self._record({"ts": time.time(), "rule_id": rule["id"], "method": req.method,
                          "target": req.target, "status": 101, "ms": 0, "bytes_in": bytes_in,
                          "bytes_out": bytes_out, "client": client_ip, "error": "",
                          "upgrade": True})
            self._tunnel(conn, reader, up_sock, up_reader)
            self._drop_socket(up_sock)
            return (None, False, None, None, 0, 0)

        # 6) 回写响应头
        resp_mode, resp_size = self._response_body_mode(req, status, resp_headers)
        keep_upstream = resp_mode != "eof" and not self._header_says_close(resp_headers)
        close_client = (resp_mode == "eof") or (not keep_upstream) or \
            (not self._want_keepalive(req))
        try:
            head_out = self._build_response_head(status, reason, resp_headers, rule, req,
                                                 target, close_client=close_client,
                                                 chunked=(resp_mode == "chunked"))
            conn.sendall(head_out)
            bytes_out += len(head_out)
        except OSError:
            self._drop_socket(up_sock)
            return (None, False, None, None, 0, 0)

        # 7) 流式 body
        try:
            if status in BODYLESS_STATUS or req.method == "HEAD":
                pass
            elif resp_mode == "length" and resp_size:
                bytes_out += self._relay_exact(up_reader, conn, resp_size)
            elif resp_mode == "chunked":
                bytes_out += self._relay_chunked(up_reader, conn)
            elif resp_mode == "eof":
                bytes_out += self._relay_eof(up_reader, conn)
        except (_BadRequest, _TooLarge) as e:
            log_error(f"上游响应体畸形（{target.host}）: {e}")
            self._drop_socket(up_sock)
            return (None, False, status, str(e), bytes_in, bytes_out)
        except (_Closed, _Timeout, OSError):
            self._drop_socket(up_sock)
            return (None, False, None, None, 0, 0)

        return ((up_sock, up_reader, target, keep_upstream), keep_upstream,
                status, "", bytes_in, bytes_out)

    def _fail(self, conn, rule: dict, req: _Request, status: int, reason: str,
              detail: str, client_ip: str) -> str:
        self._send_raw(conn, status, reason, detail)
        self._error_event(rule, req.method, req.target, status, client_ip, detail)
        return detail

    def _connect_upstream(self, rule: dict, target: _Target) -> socket.socket:
        timeout = float(rule["connect_timeout"])
        try:
            infos = socket.getaddrinfo(target.host, target.port, socket.AF_UNSPEC,
                                       socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise ProxyError(f"上游域名解析失败 {target.host}: {e}")
        last_error = None
        timed_out = False
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                if target.scheme == "https":
                    sock = target.tls_context.wrap_socket(sock, server_hostname=target.host)
                return sock
            except socket.timeout:
                self._close_socket(sock)
                timed_out = True
                last_error = f"连接超时（{timeout}s）"
            except ssl.SSLCertVerificationError as e:
                self._close_socket(sock)
                reason = getattr(e, "verify_message", None) or str(e)
                raise ProxyError(
                    f"上游 TLS 证书校验失败（{target.host}）：{reason}。"
                    "自签上游请在规则里关闭「校验上游证书」")
            except ssl.SSLError as e:
                self._close_socket(sock)
                raise ProxyError(f"上游 TLS 握手失败（{target.host}）: {e}")
            except OSError as e:
                self._close_socket(sock)
                last_error = str(e)
        if timed_out:
            raise _Timeout(f"连接上游超时（{timeout}s）: {target.host}:{target.port}")
        raise ProxyError(f"无法连接上游 {target.host}:{target.port}（{last_error}）")

    def _build_request_head(self, req: _Request, target: _Target, rule: dict,
                            listener: _Listener, client_ip: str,
                            chunked: bool = False) -> bytes:
        """构造转发给上游的请求头。

        ``chunked=True`` 时必须保留 ``Transfer-Encoding: chunked`` —— 我们
        是**原样转发 chunk 分帧**的，少了这个头上游会把分帧当正文解析。
        """
        headers = []
        conn_tokens = set(_tokens(_hget(req.headers, "connection") or ""))
        upgrade = req.upgrade

        for name, value in req.headers:
            lower = name.lower()
            if lower in ("host", "expect"):
                continue
            if lower == "transfer-encoding":
                if chunked:
                    headers.append(("Transfer-Encoding", "chunked"))
                continue
            if lower in HOP_BY_HOP:
                continue
            if lower in conn_tokens:
                continue
            headers.append((name, value))

        if upgrade:
            headers.append(("Connection", "Upgrade"))
            headers.append(("Upgrade", _hget(req.headers, "upgrade") or "websocket"))
        else:
            headers.append(("Connection", "keep-alive"))

        headers.append(("Host", rule.get("host_header") or target.host_header()))

        forwarded_for = _hget(req.headers, "x-forwarded-for")
        if forwarded_for and client_ip:
            headers.append(("X-Forwarded-For", f"{forwarded_for}, {client_ip}"))
        elif forwarded_for:
            headers.append(("X-Forwarded-For", forwarded_for))
        elif client_ip:
            headers.append(("X-Forwarded-For", client_ip))
        headers.append(("X-Forwarded-Proto", "https" if listener.tls_context else "http"))
        if req.host_header:
            headers.append(("X-Forwarded-Host", req.host_header))
        headers.append(("Via", "1.1 LIWENTool"))

        for name, value in (rule.get("request_headers") or {}).items():
            if value == "":
                _hdel(headers, name)
            else:
                _hset(headers, name, value)

        path = self._map_path(req, rule, target)
        blob = f"{req.method} {path} {req.version}\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
        try:
            return blob.encode("latin-1")
        except UnicodeEncodeError as e:
            raise _BadRequest(f"请求头包含无法编码的字符: {e}")

    @staticmethod
    def _map_path(req: _Request, rule: dict, target: _Target) -> str:
        """路径映射：strip_prefix 命中则去掉，再追加 add_prefix 与上游基址。"""
        if req.path == "*":
            return "*"
        path = req.path or "/"
        strip = rule.get("strip_prefix") or ""
        if strip and (path == strip or path.startswith(strip + "/")):
            path = path[len(strip):] or "/"
        add = rule.get("add_prefix") or ""
        if add:
            path = add + (path if path.startswith("/") else "/" + path)
        full = (target.base + path) if target.base else path
        if not full.startswith("/"):
            full = "/" + full
        if req.query:
            full += "?" + req.query
        return full

    @staticmethod
    def _parse_response_head(head: bytes) -> tuple:
        if b"\x00" in head:
            raise _BadRequest("上游响应头包含 NUL 字节")
        text = head.decode("latin-1")
        lines = text.replace("\r\n", "\n").split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        if not lines:
            raise _BadRequest("上游返回空响应")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise _BadRequest(f"上游状态行非法: {lines[0][:40]!r}")
        try:
            status = int(parts[1])
        except ValueError:
            raise _BadRequest(f"上游状态码非法: {parts[1]!r}")
        if not (100 <= status <= 599):
            raise _BadRequest(f"上游状态码越界: {status}")
        reason = parts[2] if len(parts) > 2 else ""
        if len(lines) - 1 > MAX_HEADERS:
            raise _TooLarge(f"上游响应头字段过多（上限 {MAX_HEADERS}）")
        headers = []
        for line in lines[1:]:
            if not line:
                continue
            if line[0] in (" ", "\t"):
                raise _BadRequest("上游响应头存在折行")
            if ":" not in line:
                raise _BadRequest(f"上游响应头缺少冒号: {line[:40]!r}")
            name, _, value = line.partition(":")
            headers.append((name.strip(), value.strip()))
        return status, reason, headers

    @staticmethod
    def _response_body_mode(req: _Request, status: int, headers: list) -> tuple:
        if status in BODYLESS_STATUS or req.method == "HEAD":
            return "none", 0
        te = _hget(headers, "transfer-encoding")
        if te is not None and "chunked" in _tokens(te):
            return "chunked", 0
        cl = _hget(headers, "content-length")
        if cl is not None:
            text = cl.strip()
            if text.isdigit():
                return "length", int(text)
            return "eof", 0
        return "eof", 0

    @staticmethod
    def _header_says_close(headers: list) -> bool:
        return "close" in _tokens(_hget(headers, "connection") or "")

    def _build_response_head(self, status: int, reason: str, headers: list, rule: dict,
                             req: _Request, target: _Target,
                             close_client: bool, chunked: bool = False) -> bytes:
        out = []
        conn_tokens = set(_tokens(_hget(headers, "connection") or ""))
        upgrade = status == 101
        for name, value in headers:
            lower = name.lower()
            if lower in ("connection", "keep-alive", "proxy-connection"):
                continue
            if lower == "transfer-encoding":
                # chunk 分帧是原样转发的，必须把 TE 头一起带过去
                if chunked:
                    out.append(("Transfer-Encoding", "chunked"))
                continue
            if lower in HOP_BY_HOP:
                if upgrade and lower == "upgrade":
                    out.append((name, value))
                continue
            if lower in conn_tokens:
                continue
            if lower == "location" and rule.get("rewrite_location", True):
                value = self._rewrite_location(value, target, req)
            out.append((name, value))

        if upgrade:
            out.append(("Connection", "Upgrade"))
        elif close_client:
            out.append(("Connection", "close"))
        elif req.version == "HTTP/1.0":
            out.append(("Connection", "keep-alive"))

        for name, value in (rule.get("response_headers") or {}).items():
            if value == "":
                _hdel(out, name)
            else:
                _hset(out, name, value)

        blob = f"HTTP/1.1 {status} {reason}\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in out) + "\r\n"
        return blob.encode("latin-1", "replace")

    @staticmethod
    def _rewrite_location(value: str, target: _Target, req: _Request) -> str:
        """把指向上游自身 origin 的 Location 改回客户端看到的入口 origin。"""
        lower = value.lower()
        upstream_prefix = target.origin.lower()
        if lower.startswith(upstream_prefix):
            rest = value[len(upstream_prefix):]
            if not rest.startswith(("/", "?", "#")):
                return value
            entry = req.host_header or f"{DEFAULT_LISTEN_HOST}"
            scheme = "https" if req.host_header.startswith("https") else "http"
            return f"{scheme}://{entry}{rest}"
        return value

    # ── 转发原语 ──

    def _relay_exact(self, reader: _Reader, out, size: int) -> int:
        remaining = int(size)
        total = 0
        while remaining > 0:
            chunk = reader.read_some(min(RELAY_CHUNK, remaining))
            out.sendall(chunk)
            remaining -= len(chunk)
            total += len(chunk)
        return total

    def _relay_chunked(self, reader: _Reader, out) -> int:
        total = 0
        while True:
            line = reader.read_line(MAX_CHUNK_LINE)
            out.sendall(line)
            total += len(line)
            size_text = line.split(b";", 1)[0].strip()
            if not size_text:
                raise _BadRequest("chunk 大小缺失")
            if not _HEX_RE.match(size_text.decode("latin-1")):
                raise _BadRequest(f"chunk 大小不是十六进制: {size_text[:20]!r}")
            size = int(size_text, 16)
            if size > MAX_CHUNK_BYTES:
                raise _BadRequest(f"chunk 大小超过上限: {size}")
            if size == 0:
                while True:
                    trailer = reader.read_line(MAX_CHUNK_LINE)
                    out.sendall(trailer)
                    total += len(trailer)
                    if trailer in (b"\r\n", b"\n"):
                        return total
            total += self._relay_exact(reader, out, size)
            tail = reader.read_exact(2)
            if tail not in (b"\r\n", b"\n\r"):
                raise _BadRequest("chunk 数据后缺少 CRLF")
            out.sendall(tail)
            total += 2

    def _relay_eof(self, reader: _Reader, out) -> int:
        total = 0
        while True:
            try:
                chunk = reader.read_some(RELAY_CHUNK)
            except _Closed:
                return total
            out.sendall(chunk)
            total += len(chunk)

    # ── CONNECT 与隧道 ──

    def _handle_connect(self, listener: _Listener, conn, reader: _Reader,
                        req: _Request, client_ip: str,
                        rule: dict = None, target: _Target = None) -> None:
        if rule is None or target is None:
            rule, target = listener.pick(_host_only(req.authority))
        if rule is None or target is None:
            detail = "该入口没有匹配 CONNECT 目标的规则"
            self._send_raw(conn, 403, "Forbidden", detail)
            return
        started = time.monotonic()
        if not rule.get("allow_connect", True):
            self._send_raw(conn, 403, "Forbidden", "该规则已禁用 CONNECT 隧道")
            self._error_event(rule, req.method, req.target, 403, client_ip, "CONNECT 已禁用")
            return
        host, _, port_text = req.authority.rpartition(":")
        if not host or not port_text.isdigit():
            self._send_raw(conn, 400, "Bad Request", "CONNECT 目标应为 host:port")
            self._error_event(rule, req.method, req.target, 400, client_ip, "CONNECT 目标非法")
            return
        port = int(port_text)
        if host.strip("[]").lower() != target.host.lower() or port != target.port:
            detail = (f"CONNECT 只允许连到本规则的上游 {target.host}:{target.port}"
                      f"（收到 {host}:{port}）—— 本工具不做开放代理")
            self._send_raw(conn, 403, "Forbidden", detail)
            self._error_event(rule, req.method, req.target, 403, client_ip, detail)
            return
        try:
            up_sock = self._connect_upstream(rule, target)
        except (ProxyError, OSError, ssl.SSLError) as e:
            self._send_raw(conn, 502, "Bad Gateway", str(e))
            self._error_event(rule, req.method, req.target, 502, client_ip, str(e))
            return
        up_sock.settimeout(float(rule["read_timeout"]))
        try:
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except OSError:
            self._drop_socket(up_sock)
            return
        self._record({"ts": time.time(), "rule_id": rule["id"], "method": "CONNECT",
                      "target": req.authority, "status": 200,
                      "ms": int((time.monotonic() - started) * 1000), "bytes_in": 0,
                      "bytes_out": 0, "client": client_ip, "error": "", "upgrade": True})
        self._tunnel(conn, reader, up_sock, _Reader(up_sock, "上游"))
        self._drop_socket(up_sock)

    def _tunnel(self, client_sock, client_reader: _Reader, up_sock,
                up_reader: _Reader, listener: _Listener = None) -> None:
        """101/CONNECT 之后的原始双向隧道（含半关闭转发）。"""
        try:
            pending = client_reader.buffered()
            if pending:
                up_sock.sendall(pending)
            pending = up_reader.buffered()
            if pending:
                client_sock.sendall(pending)
        except OSError:
            return
        alive = {client_sock: up_sock, up_sock: client_sock}
        while alive and not self._stop.is_set() and not (
                listener is not None and listener.closed.is_set()):
            try:
                readable, _, _ = select.select(list(alive.keys()), [], [], 0.5)
            except (OSError, ValueError):
                return
            for sock in readable:
                other = alive[sock]
                try:
                    data = sock.recv(RELAY_CHUNK)
                except socket.timeout:
                    continue
                except OSError:
                    data = b""
                if not data:
                    try:
                        other.shutdown(socket.SHUT_WR)
                    except (OSError, ValueError, NotImplementedError):
                        pass
                    alive.pop(sock, None)
                    continue
                try:
                    other.sendall(data)
                except OSError:
                    return


def start_rules(rules) -> ReverseProxyServer:
    """便捷函数：按规则列表启动一个服务器（CLI 用）。"""
    server = ReverseProxyServer(rules=rules)
    server.start()
    return server
