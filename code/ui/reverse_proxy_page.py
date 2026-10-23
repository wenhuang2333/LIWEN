"""反向代理页面 — 规则管理 / 启停 / 访问日志 / 流量统计（v6.0.0 正式上线）

按 ``obj/开发文档.md`` §19 实现（该节为冻结规格）：

  * 规则模型与校验：``network.reverse_proxy.normalize_rule()``（与本页共用同一套校验，
    界面**不复制**一份代理实现，也不静默补默认值放行）。
  * 持久化：``utils.proxy_store.proxy_rule_store``（底层走 ``utils.jsonstore`` 原子写入）。
  * 代理内核：``network.reverse_proxy.ReverseProxyServer``（纯标准库、无 Qt）。
  * 同一监听端口可挂多条规则，按请求 ``Host`` 分发（精确 → 最长 ``*.`` 通配 → 兜底），
    组合约束（至多一条兜底 / ``match_host`` 不重复 / 同端口证书一致）由核心
    ``validate_groups()`` 裁决；本页在保存后提前提示，启动失败时如实显示中文原因。
  * 启停放在页面内 ``QThread`` 上执行（不阻塞 GUI 线程），日志用 ``QTimer`` 轮询
    ``drain_events()`` 刷新，``cleanup()`` 幂等。

界面如实说明 §19.6 的限制：不做 MITM 证书拦截（不解密第三方 TLS）、不支持 HTTP/2·HTTP/3、
不做开放代理（``CONNECT`` 只能连到该规则的上游）、日志只记元数据（不落盘请求内容）。
"""
from __future__ import annotations

import os
from datetime import datetime

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from network.reverse_proxy import (
    DEFAULT_LISTEN_HOST,
    DEFAULT_LISTEN_PORT,
    ProxyError,
    ReverseProxyServer,
    RuleError,
    generate_self_signed_cert,
    normalize_rule,
    openssl_available,
    rule_display,
    validate_groups,
)
from utils.helpers import format_size
from utils.logger import error as log_error
from utils.logger import function_scope, info, warn
from utils.paths import data_dir
from utils.proxy_store import proxy_rule_store

# ── 常量 ────────────────────────────────────────────────────────────────
POLL_INTERVAL_MS = 300          # 日志/统计轮询间隔（§19.5）
STATS_EVERY_TICKS = 3           # 每 3 个 tick（≈0.9s）刷新一次统计，降低 UI 抖动
MAX_LOG_ROWS = 2000             # 访问日志表上限（超出自动裁剪最旧行）
STOP_TIMEOUT = 3.0              # ReverseProxyServer.stop() 的等待上限

(COL_ENABLED, COL_NAME, COL_LISTEN, COL_MATCH, COL_UPSTREAM,
 COL_TLS, COL_STATUS) = range(7)
RULE_HEADERS = ["启用", "名称", "监听地址", "Host 匹配", "上游目标", "TLS", "状态"]
MATCH_FALLBACK_TEXT = "*（兜底）"   # 同一端口上不限 Host 的那条规则（§19.4 第 12 条）

LOG_HEADERS = ["时间", "规则", "方法", "路径", "状态", "耗时", "入", "出", "错误"]

C_MUTED = "#9498a4"
C_OK = "#a6e3a1"
C_WARN = "#f9e2af"
C_BAD = "#f38ba8"
C_TEXT = "#e8e8f0"

CSS_INPUT = (
    "QLineEdit { background: #181825; color: #e8e8f0; border: 1px solid #45475a;"
    "  border-radius: 4px; padding: 6px 10px; font-size: 12px; }"
    "QLineEdit:focus { border-color: #89b4fa; }"
)

CSS_TABLE = (
    "QTableWidget { background: #181825; color: #e8e8f0; border: 1px solid #313244;"
    "  gridline-color: #252536; font-size: 12px; }"
    "QTableWidget::item { padding: 2px 6px; border-bottom: 1px solid #252536; }"
    "QTableWidget::item:selected { background: #45475a; }"
    "QHeaderView::section { background: #252536; color: #c8ccd4;"
    "  padding: 4px 8px; border: none; border-right: 1px solid #313244;"
    "  font-weight: 600; font-size: 12px; }"
)

CSS_TABS = (
    "QTabWidget::pane { border: 1px solid #313244; border-radius: 4px;"
    "  background: #181825; }"
    "QTabBar::tab { padding: 6px 18px; background: #252536; color: #9498a4;"
    "  border: 1px solid #313244; border-bottom: none; border-top-left-radius: 4px;"
    "  border-top-right-radius: 4px; font-size: 12px; }"
    "QTabBar::tab:selected { background: #181825; color: #e8e8f0;"
    "  border-bottom: 2px solid #89b4fa; }"
)

CSS_HINT = "color: #9498a4; font-size: 11px;"


# ── 小工具 ──────────────────────────────────────────────────────────────
def _as_int(value, default=0):
    """把契约外的值安全转 int（界面绝不为一个统计字段崩掉）。"""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_text(value, default=""):
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _rule_id_of(item):
    """从 start()/listeners()/stats() 的返回项里取 rule_id（容忍多种形态）。"""
    if isinstance(item, dict):
        rid = item.get("rule_id") or item.get("id")
        return rid if isinstance(rid, str) and rid else None
    if isinstance(item, str) and item:
        return item
    return None


def _fmt_ts(ts):
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return "—"
    if isinstance(ts, str):
        return ts
    return "—"


def _status_color(status):
    code = _as_int(status, -1)
    if 200 <= code < 300:
        return C_OK
    if 300 <= code < 400:
        return "#89b4fa"
    if 400 <= code < 500:
        return C_WARN
    if code >= 500:
        return C_BAD
    return C_MUTED


def _muted_item(text, color=C_TEXT):
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
    return item


# ── 工作线程 ────────────────────────────────────────────────────────────
class _ServerOpWorker(QThread):
    """在后台线程执行服务器操作（启动 / 停止 / 热增删规则）。

    不变量 3：``run()`` 整体 try/except 并 emit ``error``——异常逃出 ``run()`` 会让
    页面永远收不到终态信号（按钮永久禁用），这里必须兜住。
    """

    done = pyqtSignal(str, object)   # (op, result dict)
    error = pyqtSignal(str)

    def __init__(self, op: str, server=None, payload=None, timeout: float = STOP_TIMEOUT):
        super().__init__()
        self._op = op
        self._server = server
        self._payload = payload if isinstance(payload, dict) else {}
        self._timeout = timeout
        # 由 run() 里创建/清空的服务器引用，页面在终态槽里取回
        self.server = None

    def run(self):
        op = self._op
        try:
            if op == "start":
                rules = self._payload.get("rules")
                rules = rules if isinstance(rules, list) else []
                srv = ReverseProxyServer(rules=rules)
                self.server = srv
                result = srv.start()
            elif op == "stop":
                srv = self._server
                result = srv.stop(timeout=self._timeout) if srv is not None else {}
                self.server = None
            elif op == "add_rule":
                rule = self._payload.get("rule")
                result = self._server.add_rule(rule)
            elif op == "remove_rule":
                rid = _as_text(self._payload.get("rule_id"))
                removed = bool(self._server.remove_rule(rid)) if rid else False
                result = {"removed": removed, "rule_id": rid}
            else:
                result = {}
            self.done.emit(op, result if isinstance(result, dict) else {})
        except Exception as exc:                     # noqa: BLE001 — 必须兜住
            log_error(f"ReverseProxyPage 操作 {op} 失败: {exc}")
            self.error.emit(f"{exc}")


# ── 键值表 ──────────────────────────────────────────────────────────────
def _make_kv_table() -> QTableWidget:
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(["名称", "值（留空 = 删除该头）"])
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    table.setColumnWidth(0, 220)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setStyleSheet(CSS_TABLE)
    table.setMinimumHeight(120)
    return table


def _fill_kv_table(table: QTableWidget, mapping: dict):
    table.setRowCount(0)
    for key, value in (mapping or {}).items():
        if not isinstance(key, str):
            continue
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(key))
        table.setItem(row, 1, QTableWidgetItem("" if value is None else str(value)))


def _read_kv_table(table: QTableWidget) -> dict:
    result = {}
    for row in range(table.rowCount()):
        key_item = table.item(row, 0)
        key = key_item.text().strip() if key_item is not None else ""
        if not key:
            continue
        value_item = table.item(row, 1)
        result[key] = value_item.text() if value_item is not None else ""
    return result


def _kv_buttons(table: QTableWidget) -> QHBoxLayout:
    bar = QHBoxLayout()
    add_btn = QPushButton("添加")
    add_btn.setObjectName("smallBtn")
    del_btn = QPushButton("删除选中")
    del_btn.setObjectName("smallBtn")

    def _add():
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(""))
        table.setItem(row, 1, QTableWidgetItem(""))
        table.setCurrentCell(row, 0)

    def _del():
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        for row in rows:
            table.removeRow(row)

    add_btn.clicked.connect(_add)
    del_btn.clicked.connect(_del)
    bar.addWidget(add_btn)
    bar.addWidget(del_btn)
    bar.addStretch()
    return bar


# ── 规则编辑对话框 ──────────────────────────────────────────────────────
class _RuleDialog(QDialog):
    """新建 / 编辑一条反向代理规则。

    保存时一律走 ``normalize_rule()``：校验失败（``RuleError``，中文原因）就弹框并留在
    对话框里，绝不"看起来保存成功"。
    """

    def __init__(self, rule: dict | None = None, parent=None):
        super().__init__(parent)
        self._base = dict(rule) if isinstance(rule, dict) else {}
        self._result_rule = None
        self.setWindowTitle("编辑规则" if rule else "新建规则")
        self.resize(760, 660)
        self._setup_ui()
        self._load(self._base)

    @property
    def rule(self) -> dict | None:
        return self._result_rule

    # ── UI ──
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(CSS_TABS)
        self._tabs.addTab(self._make_basic_tab(), "基础")
        self._tabs.addTab(self._make_rewrite_tab(), "改写与头")
        self._tabs.addTab(self._make_tls_tab(), "TLS")
        root.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _make_basic_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(14, 14, 14, 14)
        form.setSpacing(8)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("显示名，可留空（界面回退显示监听地址）")
        self._name_edit.setStyleSheet(CSS_INPUT)

        self._enabled_check = QCheckBox("启用（关闭后不监听）")
        self._enabled_check.setChecked(True)

        self._host_edit = QLineEdit()
        self._host_edit.setStyleSheet(CSS_INPUT)
        self._host_edit.textChanged.connect(self._on_host_changed)

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)

        self._listen_hint = QLabel("")
        self._listen_hint.setStyleSheet(CSS_HINT)
        self._listen_hint.setWordWrap(True)

        self._upstream_edit = QLineEdit()
        self._upstream_edit.setPlaceholderText("如 https://api.example.com 或 http://127.0.0.1:9000/base")
        self._upstream_edit.setStyleSheet(CSS_INPUT)

        self._connect_spin = QDoubleSpinBox()
        self._connect_spin.setRange(0.5, 600.0)
        self._connect_spin.setSingleStep(1.0)
        self._connect_spin.setDecimals(1)
        self._connect_spin.setSuffix(" 秒")

        self._read_spin = QDoubleSpinBox()
        self._read_spin.setRange(0.5, 600.0)
        self._read_spin.setSingleStep(1.0)
        self._read_spin.setDecimals(1)
        self._read_spin.setSuffix(" 秒")

        self._allow_connect_check = QCheckBox("允许 CONNECT（仅限连到本规则的上游，不做开放代理）")
        self._allow_connect_check.setChecked(True)

        form.addRow("名称", self._name_edit)
        form.addRow("", self._enabled_check)
        form.addRow("监听地址", self._host_edit)
        form.addRow("监听端口", self._port_spin)
        form.addRow("", self._listen_hint)
        form.addRow("上游目标", self._upstream_edit)
        form.addRow("连接超时", self._connect_spin)
        form.addRow("读写超时", self._read_spin)
        form.addRow("", self._allow_connect_check)
        return page

    def _make_rewrite_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)

        self._strip_edit = QLineEdit()
        self._strip_edit.setPlaceholderText("入口路径前缀，命中则从转发路径里去掉")
        self._strip_edit.setStyleSheet(CSS_INPUT)

        self._add_prefix_edit = QLineEdit()
        self._add_prefix_edit.setPlaceholderText("转发前追加到路径前缀，如 /v1")
        self._add_prefix_edit.setStyleSheet(CSS_INPUT)

        self._host_header_edit = QLineEdit()
        self._host_header_edit.setPlaceholderText("留空 = 自动使用上游 host[:port]")
        self._host_header_edit.setStyleSheet(CSS_INPUT)

        self._match_host_edit = QLineEdit()
        self._match_host_edit.setPlaceholderText("api.example.com 或 *.example.com")
        self._match_host_edit.setStyleSheet(CSS_INPUT)
        self._match_host_hint = QLabel(
            "同一端口可挂多条规则，按 Host 分发；留空表示该端口的兜底规则"
        )
        self._match_host_hint.setStyleSheet(CSS_HINT)
        self._match_host_hint.setWordWrap(True)

        self._rewrite_location_check = QCheckBox("把响应 Location 里的上游 origin 改回入口 origin")
        self._rewrite_location_check.setChecked(True)

        self._verify_tls_check = QCheckBox("校验上游 HTTPS 证书（自签上游需关闭）")
        self._verify_tls_check.setChecked(True)

        form.addRow("去掉前缀", self._strip_edit)
        form.addRow("追加前缀", self._add_prefix_edit)
        form.addRow("Host 头", self._host_header_edit)
        form.addRow("Host 匹配（留空=兜底）", self._match_host_edit)
        form.addRow("", self._match_host_hint)
        form.addRow("", self._rewrite_location_check)
        form.addRow("", self._verify_tls_check)
        root.addLayout(form)

        root.addWidget(QLabel("请求头改写（值留空 = 删除该头）"))
        self._req_headers_table = _make_kv_table()
        root.addLayout(_kv_buttons(self._req_headers_table))
        root.addWidget(self._req_headers_table, 1)

        root.addWidget(QLabel("响应头改写（值留空 = 删除该头）"))
        self._resp_headers_table = _make_kv_table()
        root.addLayout(_kv_buttons(self._resp_headers_table))
        root.addWidget(self._resp_headers_table, 1)

        hint = QLabel(
            "请求/响应头默认会补 X-Forwarded-For / X-Forwarded-Proto / X-Forwarded-Host / Via；"
            "hop-by-hop 头（Connection、Transfer-Encoding、Upgrade 等）转发前自动剥离。"
        )
        hint.setStyleSheet(CSS_HINT)
        hint.setWordWrap(True)
        root.addWidget(hint)
        return page

    def _make_tls_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)

        cert_row = QHBoxLayout()
        self._cert_edit = QLineEdit()
        self._cert_edit.setPlaceholderText("监听侧 TLS 证书 PEM（留空 = 不启用入口 TLS）")
        self._cert_edit.setStyleSheet(CSS_INPUT)
        cert_btn = QPushButton("浏览")
        cert_btn.setObjectName("smallBtn")
        cert_btn.clicked.connect(lambda: self._pick_file(self._cert_edit, "选择证书 PEM"))
        cert_row.addWidget(self._cert_edit, 1)
        cert_row.addWidget(cert_btn)

        key_row = QHBoxLayout()
        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("监听侧 TLS 私钥 PEM（必须与证书同时提供）")
        self._key_edit.setStyleSheet(CSS_INPUT)
        key_btn = QPushButton("浏览")
        key_btn.setObjectName("smallBtn")
        key_btn.clicked.connect(lambda: self._pick_file(self._key_edit, "选择私钥 PEM"))
        key_row.addWidget(self._key_edit, 1)
        key_row.addWidget(key_btn)

        form.addRow("证书", cert_row)
        form.addRow("私钥", key_row)
        root.addLayout(form)

        gen_btn = QPushButton("生成自签证书")
        gen_btn.setObjectName("secondaryBtn")
        gen_btn.clicked.connect(self._generate_cert)
        root.addWidget(gen_btn, 0, Qt.AlignmentFlag.AlignLeft)

        hint = QLabel(
            "自签证书只能用于「终止入口 TLS」：客户端必须手动信任该证书，否则会报证书错误。\n"
            "本功能不做 MITM 证书拦截——不会解密任何发往第三方域名的 TLS 流量。"
        )
        hint.setStyleSheet(CSS_HINT)
        hint.setWordWrap(True)
        root.addWidget(hint)
        root.addStretch()
        return page

    # ── 数据装载/收集 ──
    def _load(self, rule: dict):
        self._name_edit.setText(_as_text(rule.get("name")))
        self._enabled_check.setChecked(bool(rule.get("enabled", True)))
        self._host_edit.setText(_as_text(rule.get("listen_host"), DEFAULT_LISTEN_HOST))
        self._port_spin.setValue(_as_int(rule.get("listen_port"), DEFAULT_LISTEN_PORT) or DEFAULT_LISTEN_PORT)
        self._upstream_edit.setText(_as_text(rule.get("upstream")))
        self._strip_edit.setText(_as_text(rule.get("strip_prefix")))
        self._add_prefix_edit.setText(_as_text(rule.get("add_prefix")))
        self._host_header_edit.setText(_as_text(rule.get("host_header")))
        self._match_host_edit.setText(_as_text(rule.get("match_host")))
        self._rewrite_location_check.setChecked(bool(rule.get("rewrite_location", True)))
        self._verify_tls_check.setChecked(bool(rule.get("verify_tls", True)))
        self._allow_connect_check.setChecked(bool(rule.get("allow_connect", True)))
        self._connect_spin.setValue(float(rule.get("connect_timeout", 10.0) or 10.0))
        self._read_spin.setValue(float(rule.get("read_timeout", 60.0) or 60.0))
        self._cert_edit.setText(_as_text(rule.get("tls_cert")))
        self._key_edit.setText(_as_text(rule.get("tls_key")))
        req = rule.get("request_headers")
        resp = rule.get("response_headers")
        _fill_kv_table(self._req_headers_table, req if isinstance(req, dict) else {})
        _fill_kv_table(self._resp_headers_table, resp if isinstance(resp, dict) else {})
        self._on_host_changed(self._host_edit.text())

    def _on_host_changed(self, text: str):
        host = (text or "").strip()
        if host in ("0.0.0.0", "::", ""):
            if host == "":
                self._listen_hint.setText("监听地址留空将按默认 127.0.0.1 处理。")
            else:
                self._listen_hint.setText(
                    "⚠ 监听 0.0.0.0：局域网内任何设备都可以通过本机代理访问该上游，请确认这是预期的。"
                )
            self._listen_hint.setStyleSheet("color: #f9e2af; font-size: 11px;")
        else:
            self._listen_hint.setText("仅本机可访问该入口地址。")
            self._listen_hint.setStyleSheet(CSS_HINT)

    def _collect(self) -> dict:
        raw = {
            "name": self._name_edit.text().strip(),
            "enabled": self._enabled_check.isChecked(),
            "listen_host": self._host_edit.text().strip() or DEFAULT_LISTEN_HOST,
            "listen_port": int(self._port_spin.value()),
            "upstream": self._upstream_edit.text().strip(),
            "strip_prefix": self._strip_edit.text().strip(),
            "add_prefix": self._add_prefix_edit.text().strip(),
            "host_header": self._host_header_edit.text().strip(),
            "match_host": self._match_host_edit.text().strip(),
            "rewrite_location": self._rewrite_location_check.isChecked(),
            "verify_tls": self._verify_tls_check.isChecked(),
            "allow_connect": self._allow_connect_check.isChecked(),
            "connect_timeout": float(self._connect_spin.value()),
            "read_timeout": float(self._read_spin.value()),
            "tls_cert": self._cert_edit.text().strip(),
            "tls_key": self._key_edit.text().strip(),
            "request_headers": _read_kv_table(self._req_headers_table),
            "response_headers": _read_kv_table(self._resp_headers_table),
        }
        rid = self._base.get("id")
        if isinstance(rid, str) and rid:
            raw["id"] = rid
        return raw

    def _on_accept(self):
        try:
            normalized = normalize_rule(self._collect())
        except RuleError as exc:
            QMessageBox.critical(self, "规则无效", str(exc))
            return
        except ProxyError as exc:
            QMessageBox.critical(self, "规则无效", str(exc))
            return
        except Exception as exc:                     # noqa: BLE001
            log_error(f"_RuleDialog 规则校验异常: {exc}")
            QMessageBox.critical(self, "规则无效", f"规则校验失败：{exc}")
            return
        self._result_rule = normalized
        info(f"_RuleDialog 保存规则: {rule_display(normalized)}")
        self.accept()

    def _pick_file(self, target: QLineEdit, title: str):
        path, _ = QFileDialog.getOpenFileName(
            self, title, target.text().strip() or data_dir(create=True),
            "PEM 文件 (*.pem *.crt *.key);;所有文件 (*)",
        )
        if path:
            target.setText(path)

    def _generate_cert(self):
        """调用内核的 generate_self_signed_cert()；失败必须把中文原因显示给用户。"""
        with function_scope("_RuleDialog._generate_cert"):
            try:
                if not openssl_available():
                    QMessageBox.critical(
                        self, "无法生成自签证书",
                        "未检测到可用的 openssl 命令，无法生成自签证书。\n"
                        "请先安装 openssl（Windows 可用 Git 自带版本），或手动指定已有的证书/私钥 PEM。",
                    )
                    return
            except Exception as exc:                 # noqa: BLE001
                log_error(f"_RuleDialog 检测 openssl 失败: {exc}")
                QMessageBox.critical(self, "无法生成自签证书", f"检测 openssl 时出错：{exc}")
                return

            base = data_dir(create=True)
            cert_path, _ = QFileDialog.getSaveFileName(
                self, "保存证书 PEM", os.path.join(base, "proxy_cert.pem"), "PEM 文件 (*.pem)"
            )
            if not cert_path:
                return
            key_path, _ = QFileDialog.getSaveFileName(
                self, "保存私钥 PEM", os.path.join(base, "proxy_key.pem"), "PEM 文件 (*.pem)"
            )
            if not key_path:
                return

            try:
                ok, message = generate_self_signed_cert(cert_path, key_path)
            except Exception as exc:                 # noqa: BLE001
                log_error(f"_RuleDialog 生成自签证书异常: {exc}")
                QMessageBox.critical(self, "生成自签证书失败", f"生成自签证书失败：{exc}")
                return

            if not ok:
                log_error(f"_RuleDialog 生成自签证书失败: {message}")
                QMessageBox.critical(
                    self, "生成自签证书失败",
                    _as_text(message) or "生成自签证书失败，但内核没有给出原因。",
                )
                return

            self._cert_edit.setText(cert_path)
            self._key_edit.setText(key_path)
            info(f"_RuleDialog 已生成自签证书: {cert_path}")
            detail = _as_text(message)
            QMessageBox.information(
                self, "已生成自签证书",
                f"证书：{cert_path}\n私钥：{key_path}\n\n{detail}".strip(),
            )


# ── 页面 ────────────────────────────────────────────────────────────────
class ReverseProxyPage(QWidget):
    """反向代理页面（互联网分类第 4 个子标签）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = None                 # 仅在启动成功后持有
        self._worker = None                 # 单个操作槽：start/stop/add_rule/remove_rule
        self._after_op = None               # 上一个 op 成功后的后续动作（联动用）
        self._pending_hot = []              # 排队中的热更新规则（单 worker 串行执行）
        self._leaked = []                   # wait() 超时仍存活的线程，保引用不 terminate
        self._rules: list[dict] = []
        self._running_ids: set[str] = set()
        self._shared_ids: set[str] = set()   # 与其它规则共用监听端口的规则（按 Host 分发）
        self._failed_rules: dict[str, str] = {}
        self._group_warn_pending = False     # 组合冲突提示去重（避免一次操作弹两次）
        self._cleaned = False                # cleanup() 之后，延迟回调不得再碰控件
        self._log_records: list[dict] = []   # 日志环形缓冲（过滤时按需重绘）
        self._tick = 0
        self._suppress_item_changed = False

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

        self._setup_ui()
        self._load_rules()
        self._refresh_rules_table()
        self._update_status_label()
        self._update_buttons()
        self._timer.start()
        info("ReverseProxyPage.__init__")

    # ── UI 搭建 ──
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("反向代理")
        title.setObjectName("myCardTitle")
        top.addWidget(title)

        self._status_label = QLabel("未运行")
        self._status_label.setStyleSheet(f"color: {C_MUTED}; font-size: 12px; font-weight: 600;")
        top.addWidget(self._status_label)
        top.addStretch()

        self._btn_new = QPushButton("新建规则")
        self._btn_new.setObjectName("smallBtn")
        self._btn_new.clicked.connect(self._new_rule)

        self._btn_edit = QPushButton("编辑")
        self._btn_edit.setObjectName("smallBtn")
        self._btn_edit.clicked.connect(self._edit_selected_rule)

        self._btn_delete = QPushButton("删除")
        self._btn_delete.setObjectName("smallBtn")
        self._btn_delete.clicked.connect(self._delete_selected_rule)

        self._btn_start = QPushButton("启动")
        self._btn_start.setObjectName("smallBtn")
        self._btn_start.clicked.connect(self._start_server)

        self._btn_stop = QPushButton("停止")
        self._btn_stop.setObjectName("smallBtn")
        self._btn_stop.clicked.connect(self._stop_server)

        self._btn_restart = QPushButton("重启")
        self._btn_restart.setObjectName("smallBtn")
        self._btn_restart.clicked.connect(self._restart_server)

        for btn in (self._btn_new, self._btn_edit, self._btn_delete,
                    self._btn_start, self._btn_stop, self._btn_restart):
            top.addWidget(btn)
        root.addLayout(top)

        self._rules_table = QTableWidget(0, len(RULE_HEADERS))
        self._rules_table.setHorizontalHeaderLabels(RULE_HEADERS)
        self._rules_table.verticalHeader().setVisible(False)
        self._rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._rules_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._rules_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._rules_table.customContextMenuRequested.connect(self._rule_context_menu)
        self._rules_table.cellDoubleClicked.connect(self._on_rule_double_clicked)
        self._rules_table.itemChanged.connect(self._on_rule_item_changed)
        self._rules_table.itemSelectionChanged.connect(self._update_buttons)
        header = self._rules_table.horizontalHeader()
        header.setSectionResizeMode(COL_ENABLED, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_LISTEN, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_MATCH, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_UPSTREAM, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_TLS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Interactive)
        self._rules_table.setColumnWidth(COL_NAME, 170)
        self._rules_table.setColumnWidth(COL_LISTEN, 155)
        self._rules_table.setColumnWidth(COL_MATCH, 165)
        self._rules_table.setColumnWidth(COL_STATUS, 260)
        self._rules_table.setStyleSheet(CSS_TABLE)
        self._rules_table.setMinimumHeight(160)
        root.addWidget(self._rules_table, 3)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(CSS_TABS)
        self._tabs.addTab(self._make_log_tab(), "访问日志")
        self._tabs.addTab(self._make_stats_tab(), "统计")
        root.addWidget(self._tabs, 4)

        hint = QLabel(
            "仅支持 HTTP/1.1（上游 ALPN 固定 http/1.1，不支持 HTTP/2/HTTP/3）；"
            "不做 MITM 证书拦截，不解密第三方 TLS；CONNECT 只能连到该规则的上游（非开放代理）；"
            "同一监听端口可挂多条规则并按 Host 分发（精确 → 最长 *. 通配 → 兜底，"
            "多条 Host 规则都不命中时返回 404）；"
            "访问日志只记元数据（方法/路径/状态/耗时/字节数/错误），不落盘请求体。"
        )
        hint.setObjectName("pageHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

    def _make_log_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        bar = QHBoxLayout()
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("过滤：路径 / 方法 / 状态码 / 规则名")
        self._filter_edit.setStyleSheet(CSS_INPUT)
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        bar.addWidget(self._filter_edit, 1)

        self._log_count_label = QLabel("0 条")
        self._log_count_label.setStyleSheet(CSS_HINT)
        bar.addWidget(self._log_count_label)

        clear_btn = QPushButton("清空日志")
        clear_btn.setObjectName("smallBtn")
        clear_btn.clicked.connect(self._clear_log)
        bar.addWidget(clear_btn)
        root.addLayout(bar)

        self._log_table = QTableWidget(0, len(LOG_HEADERS))
        self._log_table.setHorizontalHeaderLabels(LOG_HEADERS)
        self._log_table.verticalHeader().setVisible(False)
        self._log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._log_table.setWordWrap(False)
        log_header = self._log_table.horizontalHeader()
        for col in range(len(LOG_HEADERS)):
            log_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        log_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._log_table.setColumnWidth(0, 76)
        self._log_table.setColumnWidth(1, 110)
        self._log_table.setColumnWidth(2, 60)
        self._log_table.setColumnWidth(4, 60)
        self._log_table.setColumnWidth(5, 70)
        self._log_table.setColumnWidth(6, 76)
        self._log_table.setColumnWidth(7, 76)
        self._log_table.setColumnWidth(8, 200)
        self._log_table.setStyleSheet(CSS_TABLE)
        root.addWidget(self._log_table, 1)
        return page

    def _make_stats_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(2)
        self._stat_labels = {}
        for idx, (key, title) in enumerate((
            ("requests", "总请求"),
            ("errors", "错误"),
            ("bytes_in", "流入"),
            ("bytes_out", "流出"),
            ("active_connections", "活动连接"),
        )):
            title_lbl = QLabel(title)
            title_lbl.setStyleSheet(f"color: {C_MUTED}; font-size: 11px; font-weight: 600;")
            value_lbl = QLabel("0")
            value_lbl.setStyleSheet(f"color: {C_TEXT}; font-size: 18px; font-weight: 700;")
            grid.addWidget(title_lbl, 0, idx)
            grid.addWidget(value_lbl, 1, idx)
            self._stat_labels[key] = value_lbl
        root.addLayout(grid)

        root.addWidget(QLabel("按规则细分"))
        self._rule_stats_table = QTableWidget(0, 7)
        self._rule_stats_table.setHorizontalHeaderLabels(
            ["规则", "监听地址", "请求", "错误", "流入", "流出", "活动连接"]
        )
        self._rule_stats_table.verticalHeader().setVisible(False)
        self._rule_stats_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        rs_header = self._rule_stats_table.horizontalHeader()
        rs_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        rs_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        for col in range(2, 7):
            rs_header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._rule_stats_table.setColumnWidth(0, 180)
        self._rule_stats_table.setColumnWidth(1, 180)
        self._rule_stats_table.setStyleSheet(CSS_TABLE)
        root.addWidget(self._rule_stats_table, 1)
        return page

    # ── 规则读写 ──
    def _load_rules(self):
        with function_scope("ReverseProxyPage._load_rules"):
            try:
                stored = proxy_rule_store.all()
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage 读取规则失败: {exc}")
                stored = []
            self._rules = [dict(r) for r in stored if isinstance(r, dict)]
            info(f"ReverseProxyPage 读取到 {len(self._rules)} 条规则")

    def _persist(self) -> bool:
        try:
            saved = proxy_rule_store.save()
        except Exception as exc:                     # noqa: BLE001
            log_error(f"ReverseProxyPage 保存规则失败: {exc}")
            QMessageBox.warning(self, "保存失败", f"规则保存失败：{exc}")
            return False
        if saved is False:                           # 契约 save() -> bool，False 即失败
            log_error("ReverseProxyPage 保存规则失败: save() 返回 False")
            QMessageBox.warning(self, "保存失败", "规则保存失败（写入被拒绝），详见 data/log.txt。")
            return False
        # 组合合法性（至多一条兜底 / match_host 不重复 / 同端口证书一致）核心要到启动
        # 才校验；这里在保存后如实提前提示，避免用户点了「启动」才看到失败。不阻止保存。
        if not self._group_warn_pending:
            self._group_warn_pending = True
            QTimer.singleShot(0, self._check_group_conflicts)
        return True

    def _check_group_conflicts(self):
        """保存后校验「同一监听端口上的多条规则」组合，冲突则给出核心的中文原因。"""
        self._group_warn_pending = False
        if self._cleaned:
            return
        enabled = [r for r in self._rules if isinstance(r, dict) and r.get("enabled", True)]
        if len(enabled) < 2:
            return
        try:
            validate_groups(enabled)
        except (RuleError, ProxyError) as exc:
            warn(f"ReverseProxyPage 规则组合冲突: {exc}")
            QMessageBox.warning(
                self, "规则组合冲突",
                f"{exc}\n\n该组合在启动时会被拒绝，请修改后再启动。",
            )
        except Exception as exc:                     # noqa: BLE001
            log_error(f"ReverseProxyPage 规则组合校验异常: {exc}")

    def _rule_by_id(self, rule_id: str):
        for rule in self._rules:
            if _as_text(rule.get("id")) == rule_id:
                return rule
        return None

    def _selected_rule_id(self):
        rows = self._rules_table.selectionModel().selectedRows() if self._rules_table.selectionModel() else []
        row = rows[0].row() if rows else self._rules_table.currentRow()
        if row is None or row < 0 or row >= self._rules_table.rowCount():
            return None
        item = self._rules_table.item(row, COL_ENABLED)
        if item is None:
            return None
        rid = item.data(Qt.ItemDataRole.UserRole)
        return rid if isinstance(rid, str) and rid else None

    def _refresh_rules_table(self):
        table = self._rules_table
        table.blockSignals(True)
        try:
            table.setRowCount(0)
            for rule in self._rules:
                row = table.rowCount()
                table.insertRow(row)
                rid = _as_text(rule.get("id"))

                enabled_item = QTableWidgetItem("")
                enabled_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                enabled_item.setCheckState(
                    Qt.CheckState.Checked if rule.get("enabled", True) else Qt.CheckState.Unchecked
                )
                enabled_item.setData(Qt.ItemDataRole.UserRole, rid)
                table.setItem(row, COL_ENABLED, enabled_item)

                name = _as_text(rule.get("name")) or rule_display(rule)
                table.setItem(row, COL_NAME, QTableWidgetItem(name))

                listen = f"{_as_text(rule.get('listen_host'))}:{_as_int(rule.get('listen_port'))}"
                listen_item = QTableWidgetItem(listen)
                table.setItem(row, COL_LISTEN, listen_item)

                match = _as_text(rule.get("match_host"))
                if match:
                    match_item = QTableWidgetItem(match)
                    match_item.setToolTip(f"仅分发 Host 为 {match} 的请求")
                else:
                    match_item = _muted_item(MATCH_FALLBACK_TEXT, C_MUTED)
                    match_item.setToolTip("该端口的兜底规则：没有 Host 规则匹配的请求都走它")
                table.setItem(row, COL_MATCH, match_item)

                upstream = _as_text(rule.get("upstream"))
                up_item = QTableWidgetItem(upstream)
                up_item.setToolTip(upstream)
                table.setItem(row, COL_UPSTREAM, up_item)

                tls_on = bool(_as_text(rule.get("tls_cert"))) and bool(_as_text(rule.get("tls_key")))
                tls_item = _muted_item("HTTPS" if tls_on else "HTTP", C_OK if tls_on else C_MUTED)
                table.setItem(row, COL_TLS, tls_item)

                table.setItem(row, COL_STATUS, QTableWidgetItem(""))
        finally:
            table.blockSignals(False)
        self._update_rule_statuses()
        self._update_buttons()

    def _update_rule_statuses(self):
        table = self._rules_table
        server_running = self._server_running()
        self._suppress_item_changed = True
        try:
            for row in range(table.rowCount()):
                id_item = table.item(row, COL_ENABLED)
                if id_item is None:
                    continue
                rid = id_item.data(Qt.ItemDataRole.UserRole)
                rule = self._rule_by_id(rid) if isinstance(rid, str) else None
                if rule is None:
                    continue
                enabled = bool(rule.get("enabled", True))
                if not enabled:
                    text, color = "已停用", C_MUTED
                elif rid in self._running_ids:
                    text, color = "运行中", C_OK
                elif rid in self._failed_rules:
                    text, color = f"未启动：{self._failed_rules[rid]}", C_BAD
                elif server_running:
                    text, color = "未启动", C_WARN
                else:
                    text, color = "未运行", C_MUTED
                status_item = table.item(row, COL_STATUS)
                if status_item is None:
                    status_item = QTableWidgetItem("")
                    table.setItem(row, COL_STATUS, status_item)
                if status_item.text() != text:
                    status_item.setText(text)
                status_item.setForeground(QColor(color))
                status_item.setToolTip(text)

                listen_item = table.item(row, COL_LISTEN)
                if listen_item is not None:
                    tip = (
                        "该端口上还挂着其它规则，按 Host 分发（精确 → 最长通配 → 兜底）"
                        if rid in self._shared_ids else ""
                    )
                    if listen_item.toolTip() != tip:
                        listen_item.setToolTip(tip)
        finally:
            self._suppress_item_changed = False

    def _on_rule_item_changed(self, item: QTableWidgetItem):
        if self._suppress_item_changed or item.column() != COL_ENABLED:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(rid, str) or not rid:
            return
        rule = self._rule_by_id(rid)
        if rule is None:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        if bool(rule.get("enabled", True)) == enabled:
            return
        with function_scope("ReverseProxyPage._on_rule_item_changed"):
            updated = dict(rule)
            updated["enabled"] = enabled
            try:
                normalized = normalize_rule(updated)
            except RuleError as exc:
                QMessageBox.critical(self, "规则无效", str(exc))
                self._refresh_rules_table()
                return
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage 启用状态校验失败: {exc}")
                QMessageBox.critical(self, "规则无效", f"规则校验失败：{exc}")
                self._refresh_rules_table()
                return
            try:
                proxy_rule_store.update(normalized)
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage 更新规则失败: {exc}")
                QMessageBox.warning(self, "保存失败", f"更新规则失败：{exc}")
                self._refresh_rules_table()
                return
            self._persist()
            # 表格重建会销毁当前正在发信号的那个 item，推迟到信号返回之后再刷新
            QTimer.singleShot(0, lambda r=normalized: self._finish_toggle(r))

    def _finish_toggle(self, normalized: dict):
        if self._cleaned:                    # 页面已清理，延迟回调不再触碰控件
            return
        self._load_rules()
        self._refresh_rules_table()
        self._apply_rule_hot(normalized)

    # ── 规则操作入口 ──
    def _new_rule(self):
        with function_scope("ReverseProxyPage._new_rule"):
            dialog = _RuleDialog(None, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            rule = dialog.rule
            if not isinstance(rule, dict):
                return
            try:
                proxy_rule_store.add(rule)
            except ProxyError as exc:
                QMessageBox.critical(self, "保存失败", str(exc))
                return
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage 新增规则失败: {exc}")
                QMessageBox.critical(self, "保存失败", f"新增规则失败：{exc}")
                return
            self._persist()
            self._load_rules()
            self._refresh_rules_table()
            info(f"ReverseProxyPage 新增规则: {rule_display(rule)}")
            if self._server_running():
                self._apply_rule_hot(rule)

    def _edit_selected_rule(self):
        rid = self._selected_rule_id()
        if not rid:
            QMessageBox.information(self, "提示", "请先在规则表里选中一条规则。")
            return
        self._edit_rule(rid)

    def _edit_rule(self, rule_id: str):
        with function_scope("ReverseProxyPage._edit_rule"):
            rule = self._rule_by_id(rule_id)
            if rule is None:
                return
            dialog = _RuleDialog(rule, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            updated = dialog.rule
            if not isinstance(updated, dict):
                return
            try:
                proxy_rule_store.update(updated)
            except ProxyError as exc:
                QMessageBox.critical(self, "保存失败", str(exc))
                return
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage 更新规则失败: {exc}")
                QMessageBox.critical(self, "保存失败", f"更新规则失败：{exc}")
                return
            self._persist()
            self._load_rules()
            self._refresh_rules_table()
            info(f"ReverseProxyPage 编辑规则: {rule_display(updated)}")
            self._apply_rule_hot(updated)

    def _duplicate_rule(self, rule_id: str):
        with function_scope("ReverseProxyPage._duplicate_rule"):
            rule = self._rule_by_id(rule_id)
            if rule is None:
                return
            copy = dict(rule)
            copy.pop("id", None)
            base_name = _as_text(rule.get("name")) or "规则"
            copy["name"] = f"{base_name} 副本"
            try:
                normalized = normalize_rule(copy)
                proxy_rule_store.add(normalized)
            except RuleError as exc:
                QMessageBox.critical(self, "复制失败", str(exc))
                return
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage 复制规则失败: {exc}")
                QMessageBox.critical(self, "复制失败", f"复制规则失败：{exc}")
                return
            self._persist()
            self._load_rules()
            self._refresh_rules_table()
            info(f"ReverseProxyPage 复制规则: {rule_display(normalized)}")

    def _delete_selected_rule(self):
        rid = self._selected_rule_id()
        if not rid:
            QMessageBox.information(self, "提示", "请先在规则表里选中一条规则。")
            return
        self._delete_rule(rid)

    def _delete_rule(self, rule_id: str):
        with function_scope("ReverseProxyPage._delete_rule"):
            rule = self._rule_by_id(rule_id)
            if rule is None:
                return
            label = _as_text(rule.get("name")) or rule_display(rule)
            answer = QMessageBox.question(
                self, "删除规则", f"确定删除规则「{label}」？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

            if self._server_running():
                # 先让服务器撤掉监听器，再落盘删除；撤监听失败就不删，避免界面与实况不一致
                self._after_op = lambda _result: self._delete_rule_persist(rule_id)
                if self._run_op("remove_rule", {"rule_id": rule_id}):
                    return
                self._after_op = None
            self._delete_rule_persist(rule_id)

    def _delete_rule_persist(self, rule_id: str):
        try:
            proxy_rule_store.remove(rule_id)
        except Exception as exc:                     # noqa: BLE001
            log_error(f"ReverseProxyPage 删除规则失败: {exc}")
            QMessageBox.warning(self, "删除失败", f"删除规则失败：{exc}")
            return
        self._persist()
        self._failed_rules.pop(rule_id, None)
        self._running_ids.discard(rule_id)
        self._shared_ids.discard(rule_id)
        self._load_rules()
        self._refresh_rules_table()
        info(f"ReverseProxyPage 删除规则 {rule_id}")

    def _rule_context_menu(self, pos):
        index = self._rules_table.indexAt(pos)
        if not index.isValid():
            return
        self._rules_table.selectRow(index.row())
        rid = self._selected_rule_id()
        if not rid:
            return
        menu = QMenu(self)
        act_edit = menu.addAction("编辑")
        act_dup = menu.addAction("复制")
        act_start = menu.addAction("单独启动该规则")
        menu.addSeparator()
        act_delete = menu.addAction("删除")
        chosen = menu.exec(self._rules_table.viewport().mapToGlobal(pos))
        if chosen is act_edit:
            self._edit_rule(rid)
        elif chosen is act_dup:
            self._duplicate_rule(rid)
        elif chosen is act_start:
            self._start_single_rule(rid)
        elif chosen is act_delete:
            self._delete_rule(rid)

    def _on_rule_double_clicked(self, row: int, _column: int):
        item = self._rules_table.item(row, COL_ENABLED)
        if item is None:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(rid, str) and rid:
            self._edit_rule(rid)

    # ── 服务器生命周期 ──
    def _server_running(self) -> bool:
        server = self._server
        if server is None:
            return False
        try:
            return bool(getattr(server, "running", False))
        except Exception:                            # noqa: BLE001
            return False

    def _run_op(self, op: str, payload: dict | None = None) -> bool:
        """把一次服务器操作丢到后台线程；同一时刻只允许一个在途操作。"""
        if self._worker is not None:
            warn(f"ReverseProxyPage._run_op: 上一个操作尚未结束，忽略 {op}")
            return False
        server = self._server
        if op != "start" and server is None:
            QMessageBox.information(self, "提示", "反向代理尚未启动。")
            return False
        worker = _ServerOpWorker(op, server=server, payload=payload or {}, timeout=STOP_TIMEOUT)
        self._worker = worker
        worker.done.connect(self._on_op_done)
        worker.error.connect(self._on_op_error)
        worker.start()
        self._update_buttons()
        info(f"ReverseProxyPage 操作开始: {op}")
        return True

    def _start_server(self, rules=None):
        with function_scope("ReverseProxyPage._start_server"):
            if self._worker is not None:
                return
            if self._server_running():
                info("ReverseProxyPage 服务器已在运行，忽略重复启动")
                return
            snapshot = rules if isinstance(rules, list) else [dict(r) for r in self._rules]
            if not snapshot:
                QMessageBox.information(self, "提示", "还没有规则，请先新建一条规则。")
                return
            self._run_op("start", {"rules": snapshot})

    def _stop_server(self):
        with function_scope("ReverseProxyPage._stop_server"):
            if self._worker is not None:
                return
            if self._server is None:
                return
            self._run_op("stop")

    def _restart_server(self):
        with function_scope("ReverseProxyPage._restart_server"):
            if self._worker is not None:
                return
            rules = [dict(r) for r in self._rules]
            if not rules:
                QMessageBox.information(self, "提示", "还没有规则，请先新建一条规则。")
                return
            if self._server is None:
                self._start_server(rules)
                return
            # 先停（工作线程），在终态槽里接着启动，两步都在后台完成
            self._after_op = lambda _result: self._start_server(rules)
            if not self._run_op("stop"):
                self._after_op = None

    def _start_single_rule(self, rule_id: str):
        with function_scope("ReverseProxyPage._start_single_rule"):
            rule = self._rule_by_id(rule_id)
            if rule is None:
                return
            if self._worker is not None:
                return
            if not self._server_running():
                # 尚未运行：只按这条规则启动（不是"启动全部"）
                self._start_server([dict(rule)])
                return
            self._run_op("add_rule", {"rule": dict(rule)})

    def _apply_rule_hot(self, rule: dict):
        """规则变更后热应用到运行中的服务器（排队串行执行，快速连续操作不会丢）。"""
        if not self._server_running():
            return
        self._pending_hot.append(dict(rule))
        self._drain_pending_hot()

    def _drain_pending_hot(self):
        if self._worker is not None or not self._pending_hot or not self._server_running():
            return
        rule = self._pending_hot.pop(0)
        rid = _as_text(rule.get("id"))
        if not rid:
            QTimer.singleShot(0, self._drain_pending_hot)
            return
        if rule.get("enabled") is False:
            self._run_op("remove_rule", {"rule_id": rid})
            return
        snapshot = dict(rule)

        def _add(_result=None):
            self._run_op("add_rule", {"rule": snapshot})

        self._after_op = _add
        if not self._run_op("remove_rule", {"rule_id": rid}):
            self._after_op = None

    # ── 操作终态 ──
    def _on_op_done(self, op: str, result):
        # 不变量 4：只认当前 worker 的终态信号
        if self.sender() is not self._worker:
            return
        worker = self._worker
        data = result if isinstance(result, dict) else {}
        try:
            if op == "start":
                server = getattr(worker, "server", None)
                if server is not None:
                    self._server = server
                self._failed_rules = {}
                self._running_ids = set()
                started_ids, failures = self._parse_start_result(data)
                self._running_ids.update(started_ids)
                for failure in failures:
                    # 用户主动关掉的规则不算"启动失败"，只有真起不来的才计入状态标签
                    rule = self._rule_by_id(failure["rule_id"])
                    if rule is not None and not rule.get("enabled", True):
                        continue
                    self._failed_rules[failure["rule_id"]] = failure["reason"]
                info(
                    f"ReverseProxyPage 启动完成: 成功 {len(started_ids)} 条，"
                    f"失败 {len(failures)} 条"
                )
                for failure in failures:
                    log_error(f"ReverseProxyPage 规则未启动: {failure['reason']}")
            elif op == "stop":
                self._server = None
                self._running_ids = set()
                self._failed_rules = {}
                self._pending_hot.clear()
                info("ReverseProxyPage 已停止所有监听")
            elif op == "remove_rule":
                rid = _as_text(data.get("rule_id"))
                if rid:
                    self._running_ids.discard(rid)
                    self._shared_ids.discard(rid)
            elif op == "add_rule":
                payload = getattr(worker, "_payload", None)
                payload_rule = payload.get("rule") if isinstance(payload, dict) else None
                rid = _rule_id_of(data) or _as_text(data.get("rule_id"))
                if not rid and isinstance(payload_rule, dict):
                    rid = _as_text(payload_rule.get("id"))
                failures = []
                if isinstance(data.get("failed"), list):
                    failures = self._parse_failures(data.get("failed"))
                if failures:
                    for failure in failures:
                        self._failed_rules[failure["rule_id"]] = failure["reason"]
                elif isinstance(data.get("error"), str) and data.get("error") and rid:
                    self._failed_rules[rid] = data["error"]
                else:
                    if isinstance(data.get("started"), list):
                        for item in data["started"]:
                            sid = _rule_id_of(item)
                            if sid:
                                self._running_ids.add(sid)
                    if rid:
                        self._failed_rules.pop(rid, None)
                        if not isinstance(payload_rule, dict) or payload_rule.get("enabled", True):
                            self._running_ids.add(rid)
                if rid and rid in self._failed_rules:
                    log_error(f"ReverseProxyPage 规则热加载失败: {self._failed_rules[rid]}")
        except Exception as exc:                     # noqa: BLE001
            log_error(f"ReverseProxyPage 处理 {op} 结果失败: {exc}")
        finally:
            self._clear_worker()
            self._refresh_rules_table()
            self._update_status_label()
            self._update_buttons()
            self._refresh_stats()
            callback = self._after_op
            self._after_op = None
            if callback is not None:
                try:
                    callback(data)
                except Exception as exc:             # noqa: BLE001
                    log_error(f"ReverseProxyPage 后续动作失败: {exc}")
            self._drain_pending_hot()

    def _on_op_error(self, message: str):
        if self.sender() is not self._worker:
            return
        op = getattr(self._worker, "_op", "?")
        log_error(f"ReverseProxyPage 操作 {op} 出错: {message}")
        if op == "start":
            self._server = None
            self._running_ids = set()
            text = _as_text(message) or "未知错误"
            for rule in self._rules:
                if rule.get("enabled", True):
                    rid = _as_text(rule.get("id"))
                    if rid:
                        self._failed_rules[rid] = text
        self._clear_worker()
        self._refresh_rules_table()
        self._update_status_label()
        self._update_buttons()
        self._after_op = None
        QMessageBox.critical(self, "反向代理操作失败", f"{op} 失败：{message}")

    def _clear_worker(self):
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        # 不变量 2：取消 → 等待 → 断开单个信号 → 置 None；绝不 terminate()
        for sig in (worker.done, worker.error):
            try:
                sig.disconnect()
            except Exception:                        # noqa: BLE001
                pass
        try:
            if worker.isRunning():
                if not worker.wait(3000):
                    log_error("ReverseProxyPage: 操作线程未在 3s 内结束，保留引用等待其自然退出")
                    self._leaked.append(worker)
        except Exception as exc:                     # noqa: BLE001
            log_error(f"ReverseProxyPage 清理操作线程时出错: {exc}")

    def _parse_failures(self, items) -> list[dict]:
        failures = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            rid = _rule_id_of(item)
            if not rid:
                continue
            name = _as_text(item.get("name"))
            listen = _as_text(item.get("listen"))
            reason = _as_text(item.get("error")) or "未知原因"
            label = name or listen or rid
            failures.append({"rule_id": rid, "reason": f"{label}：{reason}"})
        return failures

    def _parse_start_result(self, data: dict):
        started_ids = []
        for item in data.get("started") or []:
            rid = _rule_id_of(item)
            if rid:
                started_ids.append(rid)
        return started_ids, self._parse_failures(data.get("failed"))

    # ── 轮询刷新 ──
    def _poll(self):
        self._tick += 1
        server = self._server
        if server is not None:
            try:
                events = server.drain_events(200)
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage 读取访问日志失败: {exc}")
                events = []
            if events:
                self._append_events(events)
        if self._tick % STATS_EVERY_TICKS == 0:
            self._refresh_stats()

    def _reset_stats_display(self):
        """服务器不在运行：统计归零，不留上一轮的陈旧数字（尤其是活动连接）。"""
        self._stat_labels["requests"].setText("0")
        self._stat_labels["errors"].setText("0")
        self._stat_labels["bytes_in"].setText(format_size(0))
        self._stat_labels["bytes_out"].setText(format_size(0))
        self._stat_labels["active_connections"].setText("0")
        self._rule_stats_table.setRowCount(0)

    def _refresh_stats(self):
        server = self._server
        if server is None:
            self._running_ids = set()
            self._shared_ids = set()
            self._reset_stats_display()
            self._update_rule_statuses()
            self._update_status_label()
            return
        try:
            stats = server.stats()
        except Exception as exc:                     # noqa: BLE001
            log_error(f"ReverseProxyPage 读取统计失败: {exc}")
            stats = {}
        if not isinstance(stats, dict):
            stats = {}

        try:
            listeners = server.listeners()
        except Exception as exc:                     # noqa: BLE001
            log_error(f"ReverseProxyPage 读取监听器失败: {exc}")
            listeners = []
        running_ids = {rid for rid in (_rule_id_of(x) for x in (listeners or [])) if rid}
        self._running_ids = running_ids
        shared = set()
        for entry in (listeners or []):
            rid = _rule_id_of(entry)
            if rid and isinstance(entry, dict) and entry.get("shared"):
                shared.add(rid)
        self._shared_ids = shared
        self._update_rule_statuses()
        self._update_status_label()

        self._stat_labels["requests"].setText(str(_as_int(stats.get("requests"))))
        self._stat_labels["errors"].setText(str(_as_int(stats.get("errors"))))
        self._stat_labels["bytes_in"].setText(format_size(_as_int(stats.get("bytes_in"))))
        self._stat_labels["bytes_out"].setText(format_size(_as_int(stats.get("bytes_out"))))
        self._stat_labels["active_connections"].setText(
            str(_as_int(stats.get("active_connections")))
        )

        rows = stats.get("rules")
        rows = rows if isinstance(rows, list) else []
        table = self._rule_stats_table
        table.setRowCount(0)
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            rid = _rule_id_of(entry)
            rule = self._rule_by_id(rid) if rid else None
            name = _as_text(entry.get("name"))
            if not name and rule is not None:
                name = _as_text(rule.get("name")) or rule_display(rule)
            if not name:
                name = rid or "—"
            listen = _as_text(entry.get("listen"))
            if not listen and rule is not None:
                listen = f"{_as_text(rule.get('listen_host'))}:{_as_int(rule.get('listen_port'))}"
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(name))
            table.setItem(row, 1, QTableWidgetItem(listen or "—"))
            table.setItem(row, 2, QTableWidgetItem(str(_as_int(entry.get("requests")))))
            table.setItem(row, 3, QTableWidgetItem(str(_as_int(entry.get("errors")))))
            table.setItem(row, 4, QTableWidgetItem(format_size(_as_int(entry.get("bytes_in")))))
            table.setItem(row, 5, QTableWidgetItem(format_size(_as_int(entry.get("bytes_out")))))
            table.setItem(row, 6, QTableWidgetItem(str(_as_int(entry.get("active_connections")))))

    # ── 访问日志 ──
    def _normalize_event(self, event: dict) -> dict:
        rid = _as_text(event.get("rule_id"))
        rule = self._rule_by_id(rid) if rid else None
        rule_name = _as_text(event.get("rule_name"))
        if not rule_name and rule is not None:
            rule_name = _as_text(rule.get("name")) or rule_display(rule)
        return {
            "ts": _fmt_ts(event.get("ts")),
            "rule": rule_name or "—",
            "method": _as_text(event.get("method")) or "—",
            "target": _as_text(event.get("target")),
            "status": _as_int(event.get("status"), 0),
            "ms": _as_int(event.get("ms")),
            "bytes_in": _as_int(event.get("bytes_in")),
            "bytes_out": _as_int(event.get("bytes_out")),
            "error": _as_text(event.get("error")),
        }

    def _record_matches_filter(self, record: dict, needle: str) -> bool:
        if not needle:
            return True
        blob = " ".join((record["rule"], record["method"], record["target"],
                         str(record["status"]), record["error"])).lower()
        return needle in blob

    def _add_log_row(self, record: dict):
        table = self._log_table
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(record["ts"]))
        table.setItem(row, 1, QTableWidgetItem(record["rule"]))
        table.setItem(row, 2, QTableWidgetItem(record["method"]))
        target_item = QTableWidgetItem(record["target"] or "—")
        target_item.setToolTip(record["target"])
        table.setItem(row, 3, target_item)
        table.setItem(row, 4, _muted_item(str(record["status"]) if record["status"] else "—",
                                          _status_color(record["status"])))
        table.setItem(row, 5, QTableWidgetItem(f"{record['ms']} ms"))
        table.setItem(row, 6, QTableWidgetItem(format_size(record["bytes_in"])))
        table.setItem(row, 7, QTableWidgetItem(format_size(record["bytes_out"])))
        err_item = QTableWidgetItem(record["error"])
        if record["error"]:
            err_item.setForeground(QColor(C_BAD))
            err_item.setToolTip(record["error"])
        table.setItem(row, 8, err_item)

    def _append_events(self, events):
        table = self._log_table
        needle = self._filter_edit.text().strip().lower()
        appended = 0
        table.setUpdatesEnabled(False)
        try:
            for event in events:
                if not isinstance(event, dict):
                    continue               # 契约外形态直接丢，不让界面崩
                record = self._normalize_event(event)
                self._log_records.append(record)
                if not self._record_matches_filter(record, needle):
                    continue
                self._add_log_row(record)
                appended += 1

            # 不变量 9：历史与表格行数都有界，超出上限裁掉最旧的
            del self._log_records[:-MAX_LOG_ROWS]
            overflow = table.rowCount() - MAX_LOG_ROWS
            for _ in range(max(0, overflow)):
                table.removeRow(0)
        finally:
            table.setUpdatesEnabled(True)
        if appended:
            table.scrollToBottom()
        self._update_log_count()

    def _update_log_count(self):
        total = len(self._log_records)
        shown = self._log_table.rowCount()
        if self._filter_edit.text().strip():
            self._log_count_label.setText(f"{shown} / {total} 条（已过滤）")
        elif total >= MAX_LOG_ROWS:
            self._log_count_label.setText(f"{shown} 条（上限 {MAX_LOG_ROWS} 行，最旧的已裁剪）")
        else:
            self._log_count_label.setText(f"{shown} 条")

    def _on_filter_changed(self, _text: str):
        # 过滤作用于全部已收到的日志（不只是新来的行），用户输入即时生效
        needle = self._filter_edit.text().strip().lower()
        table = self._log_table
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(0)
            for record in self._log_records:
                if self._record_matches_filter(record, needle):
                    self._add_log_row(record)
            del self._log_records[:-MAX_LOG_ROWS]
        finally:
            table.setUpdatesEnabled(True)
        table.scrollToBottom()
        self._update_log_count()

    def _clear_log(self):
        with function_scope("ReverseProxyPage._clear_log"):
            self._log_records.clear()
            self._log_table.setRowCount(0)
            self._update_log_count()

    # ── 状态/按钮 ──
    def _update_status_label(self):
        running = self._server_running()
        failed = len(self._failed_rules)
        first_reason = next(iter(self._failed_rules.values()), "")
        if self._worker is not None and self._server is None and not running:
            text, color = "正在启动…", C_WARN
        elif not running:
            text, color = "未运行", C_MUTED
            if failed:
                # 全部失败时也要给出原因：否则用户只看到条数，得再点一次才知道为什么
                text = f"部分失败：{failed} 条未启动" + (f"（{first_reason}）" if first_reason else "")
                color = C_BAD
        elif failed:
            first = next(iter(self._failed_rules.values()))
            text, color = f"部分失败：{failed} 条未启动（{first}）", C_BAD
        else:
            text, color = f"运行中 {len(self._running_ids)} 条规则", C_OK
        self._status_label.setText(text)
        self._status_label.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 600;")
        self._status_label.setToolTip(text)

    def _update_buttons(self):
        busy = self._worker is not None
        running = self._server_running()
        has_selection = bool(self._selected_rule_id())
        self._btn_new.setEnabled(not busy)
        self._btn_edit.setEnabled(not busy and has_selection)
        self._btn_delete.setEnabled(not busy and has_selection)
        self._btn_start.setEnabled(not busy and not running and bool(self._rules))
        self._btn_stop.setEnabled(not busy and self._server is not None)
        self._btn_restart.setEnabled(not busy and bool(self._rules))

    # ── 清理（幂等） ──
    def cleanup(self):
        """停服务器 → 停定时器 → 等/清 worker；调用两次不报错，绝不 terminate()。"""
        with function_scope("ReverseProxyPage.cleanup"):
            try:
                self._timer.stop()
            except Exception as exc:                 # noqa: BLE001
                log_error(f"ReverseProxyPage.cleanup 停止定时器失败: {exc}")

            self._after_op = None
            self._pending_hot.clear()

            worker = self._worker
            self._worker = None
            if worker is not None:
                for sig in (worker.done, worker.error):
                    try:
                        sig.disconnect()
                    except Exception:                # noqa: BLE001
                        pass
                try:
                    if worker.isRunning() and not worker.wait(6000):
                        log_error("ReverseProxyPage.cleanup: 操作线程未在 6s 内结束，保留引用")
                        self._leaked.append(worker)
                except Exception as exc:             # noqa: BLE001
                    log_error(f"ReverseProxyPage.cleanup 等待操作线程失败: {exc}")

            server = self._server
            self._server = None
            if server is not None:
                try:
                    stop_worker = _ServerOpWorker("stop", server=server, payload={},
                                                  timeout=STOP_TIMEOUT)
                    stop_worker.start()
                    if not stop_worker.wait(15000):
                        log_error("ReverseProxyPage.cleanup: 停止服务器线程未在 15s 内结束，保留引用")
                        self._leaked.append(stop_worker)
                except Exception as exc:             # noqa: BLE001
                    log_error(f"ReverseProxyPage.cleanup 停止服务器失败: {exc}")
                    try:
                        server.stop(timeout=STOP_TIMEOUT)
                    except Exception as exc2:        # noqa: BLE001
                        log_error(f"ReverseProxyPage.cleanup 兜底停止服务器也失败: {exc2}")

            self._running_ids = set()
            self._shared_ids = set()
            self._cleaned = True                 # 之后的延迟回调（singleShot）会自行退出
            info("ReverseProxyPage.cleanup")
