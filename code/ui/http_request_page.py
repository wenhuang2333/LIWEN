"""HTTP 请求发送页面 — 支持全部 HTTP 方法的 API 调试工具

Features:
  - All HTTP methods (GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS/TRACE)
  - Params, Authorization (API Key / Bearer / Basic / Digest), Headers, Body
  - Request timeout, auto-redirect, SSL verify, proxy settings
  - Variable system with {{var}} substitution + environment switching
  - Request history, collections (save/load), import/export
  - Response: body, headers, cookies, HTML preview
  - Assertions / response validation
  - Batch runner for collections
  - Cookie jar management
"""
import json as json_mod
import os
import re
import sys
import time
import urllib.parse

from ui.styles import MONO_FONT_FAMILY

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QTextEdit, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QSplitter, QFileDialog,
    QRadioButton, QButtonGroup, QFormLayout, QMessageBox, QApplication,
    QStyledItemDelegate, QCheckBox, QSpinBox, QListWidget, QListWidgetItem,
    QDialog, QDialogButtonBox, QMenu, QProgressBar, QGroupBox, QScrollArea,
    QStackedWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QUrl
from PyQt6.QtGui import QFont, QTextCursor, QColor, QKeyEvent

from core.workers import HttpRequestWorker
# 关闭时等待在途请求结束的上限。请求本身的超时可配置（默认 30s），
# 这里给一个明确的上限，超时则保留引用交给解释器退出，绝不 terminate()。
_CLEANUP_WAIT_MS = 8000

from utils.helpers import format_size
from utils.logger import info, error, warn
from utils.variable_store import variable_store
from utils.request_history import request_history

HTTP_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"]
BODY_TYPES = ["none", "form-data", "x-www-form-urlencoded", "raw", "binary"]
RAW_CONTENT_TYPES = ["JSON", "XML", "HTML", "Text"]
AUTH_TYPES = ["None", "API Key", "Bearer Token", "Basic Auth", "Digest Auth"]
API_KEY_LOCATIONS = ["Header", "Query Parameter"]

STATUS_COLORS = {
    range(200, 300): ("#a6e3a1", "OK"),
    range(300, 400): ("#89b4fa", "Redirect"),
    range(400, 500): ("#f9e2af", "Client Error"),
    range(500, 600): ("#f38ba8", "Server Error"),
}

CSS_INPUT = (
    "QLineEdit { background: #181825; color: #e8e8f0; border: 1px solid #45475a;"
    "  border-radius: 4px; padding: 8px 12px; font-size: 13px; min-height: 22px; }"
    "QLineEdit:focus { border-color: #89b4fa; }"
)

CSS_COMBO = (
    "QComboBox { background: #313244; color: #e8e8f0; border: 1px solid #45475a;"
    "  border-radius: 4px; padding: 6px 10px; font-size: 13px; min-height: 22px; }"
    "QComboBox:hover { border-color: #89b4fa; }"
    "QComboBox QAbstractItemView { background: #313244; color: #e8e8f0;"
    "  selection-background-color: #45475a; font-size: 13px; }"
)

CSS_TABLE = (
    "QTableWidget { background: #181825; color: #e8e8f0; border: 1px solid #313244;"
    "  gridline-color: #252536; font-size: 13px; }"
    "QTableWidget::item { padding: 4px 6px; border-bottom: 1px solid #252536; }"
    "QTableWidget::item:selected { background: #45475a; }"
    "QHeaderView::section { background: #252536; color: #c8ccd4;"
    "  padding: 4px 8px; border: none; border-right: 1px solid #313244;"
    "  font-weight: 600; font-size: 12px; }"
)

CSS_TAB_WIDGET = (
    "QTabWidget::pane { border: 1px solid #313244; border-radius: 4px;"
    "  background: #181825; }"
    "QTabBar::tab { padding: 6px 18px; background: #252536; color: #9498a4;"
    "  border: 1px solid #313244; border-bottom: none; border-top-left-radius: 4px;"
    "  border-top-right-radius: 4px; font-size: 12px; }"
    "QTabBar::tab:selected { background: #181825; color: #e8e8f0;"
    "  border-bottom: 2px solid #89b4fa; }"
)

CSS_SUB_TABS = (
    "QTabWidget::pane { border: 1px solid #313244; border-radius: 4px;"
    "  background: #181825; }"
    "QTabBar::tab { padding: 4px 14px; background: #252536; color: #9498a4;"
    "  border: 1px solid #313244; border-bottom: none; border-top-left-radius: 4px;"
    "  border-top-right-radius: 4px; font-size: 12px; }"
    "QTabBar::tab:selected { background: #181825; color: #e8e8f0;"
    "  border-bottom: 2px solid #89b4fa; }"
)

CSS_GROUP_BOX = (
    "QGroupBox { color: #c8ccd4; font-size: 12px; font-weight: 600;"
    "  border: 1px solid #313244; border-radius: 4px; margin-top: 10px;"
    "  padding-top: 14px; }"
    "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
)

CSS_LIST = (
    "QListWidget { background: #181825; color: #e8e8f0; border: 1px solid #313244;"
    "  border-radius: 4px; font-size: 12px; }"
    "QListWidget::item { padding: 6px 10px; border-bottom: 1px solid #252536; }"
    "QListWidget::item:selected { background: #45475a; }"
    "QListWidget::item:hover { background: #313244; }"
)


def _status_appearance(code):
    for rng, (color, label) in STATUS_COLORS.items():
        if code in rng:
            return color, label
    return ("#6c7086", "")


# ── Placeholder Delegate ──────────────────────────────────────────

class _PlaceholderDelegate(QStyledItemDelegate):
    def __init__(self, placeholder=""):
        super().__init__()
        self._ph = placeholder

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if not index.data(Qt.ItemDataRole.EditRole) and not index.data(Qt.ItemDataRole.DisplayRole):
            painter.save()
            painter.setPen(QColor("#6c7086"))
            painter.drawText(option.rect.adjusted(8, 0, 0, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             self._ph)
            painter.restore()


def _kv_table():
    """Create a 2-column key-value table with stretch on first column."""
    t = QTableWidget(0, 2)
    t.setHorizontalHeaderLabels(["键", "值"])
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setStyleSheet(CSS_TABLE)
    d0 = _PlaceholderDelegate("参数名")
    d1 = _PlaceholderDelegate("参数值")
    t.setItemDelegateForColumn(0, d0)
    t.setItemDelegateForColumn(1, d1)
    t._placeholder_delegates = (d0, d1)
    return t


# ── Variable Editor Dialog ─────────────────────────────────────────

class _VariableEditor(QDialog):
    """Dialog for editing global variables and environment variables."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("变量管理")
        self.setMinimumSize(600, 420)
        self._setup_ui()
        self._load_data()

    def _setup_ui(self):
        self.setStyleSheet(
            "QDialog { background: #1e1e2e; color: #e8e8f0; }"
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        tabs = QTabWidget()
        tabs.setStyleSheet(CSS_TAB_WIDGET)

        # ── Globals tab ──
        gw = QWidget()
        gl = QVBoxLayout(gw)
        gl.setContentsMargins(8, 8, 8, 8)
        gl.setSpacing(6)
        self._global_table = _kv_table()
        self._global_table.setHorizontalHeaderLabels(["变量名", "变量值"])
        gl.addWidget(self._global_table, 1)
        gbr = QHBoxLayout()
        gadd = QPushButton("+ 添加")
        gadd.setObjectName("smallBtn")
        gadd.clicked.connect(lambda: self._add_table_row(self._global_table))
        gbr.addWidget(gadd)
        gdel = QPushButton("− 删除选中")
        gdel.setObjectName("smallBtn")
        gdel.clicked.connect(lambda: self._del_table_row(self._global_table))
        gbr.addWidget(gdel)
        gbr.addStretch()
        gl.addLayout(gbr)
        tabs.addTab(gw, "全局变量")

        # ── Environments tab ──
        ew = QWidget()
        el = QVBoxLayout(ew)
        el.setContentsMargins(8, 8, 8, 8)
        el.setSpacing(6)
        env_top = QHBoxLayout()
        self._env_combo = QComboBox()
        self._env_combo.setStyleSheet(CSS_COMBO)
        self._env_combo.currentTextChanged.connect(self._on_env_selected)
        env_top.addWidget(self._env_combo, 1)
        new_env = QPushButton("+ 新建环境")
        new_env.setObjectName("smallBtn")
        new_env.clicked.connect(self._new_environment)
        env_top.addWidget(new_env)
        del_env = QPushButton("删除环境")
        del_env.setObjectName("smallBtn")
        del_env.clicked.connect(self._delete_environment)
        env_top.addWidget(del_env)
        el.addLayout(env_top)

        self._env_table = _kv_table()
        self._env_table.setHorizontalHeaderLabels(["变量名", "变量值"])
        el.addWidget(self._env_table, 1)
        ebr = QHBoxLayout()
        eadd = QPushButton("+ 添加")
        eadd.setObjectName("smallBtn")
        eadd.clicked.connect(lambda: self._add_table_row(self._env_table))
        ebr.addWidget(eadd)
        edel = QPushButton("− 删除选中")
        edel.setObjectName("smallBtn")
        edel.clicked.connect(lambda: self._del_table_row(self._env_table))
        ebr.addWidget(edel)
        ebr.addStretch()
        el.addLayout(ebr)
        tabs.addTab(ew, "环境变量")

        layout.addWidget(tabs)

        # ── Buttons ──
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        bb.setStyleSheet(
            "QPushButton { padding: 6px 18px; font-size: 12px; }"
        )
        layout.addWidget(bb)

    def _load_data(self):
        # Globals
        for key, val in variable_store.all_global().items():
            r = self._global_table.rowCount()
            self._global_table.insertRow(r)
            self._global_table.setItem(r, 0, QTableWidgetItem(key))
            self._global_table.setItem(r, 1, QTableWidgetItem(val))
        # Environments
        self._env_combo.blockSignals(True)
        self._env_combo.clear()
        for name in variable_store.environments():
            self._env_combo.addItem(name)
        self._env_combo.blockSignals(False)
        if self._env_combo.count() > 0:
            self._env_combo.setCurrentIndex(0)

    def _on_env_selected(self, name):
        self._env_table.setRowCount(0)
        if not name:
            return
        for key, val in variable_store.get_environment(name).items():
            r = self._env_table.rowCount()
            self._env_table.insertRow(r)
            self._env_table.setItem(r, 0, QTableWidgetItem(key))
            self._env_table.setItem(r, 1, QTableWidgetItem(val))

    def _new_environment(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建环境", "环境名称:")
        if ok and name.strip():
            variable_store.create_environment(name.strip())
            self._env_combo.blockSignals(True)
            self._env_combo.addItem(name.strip())
            self._env_combo.setCurrentText(name.strip())
            self._env_combo.blockSignals(False)

    def _delete_environment(self):
        name = self._env_combo.currentText()
        if name:
            variable_store.delete_environment(name)
            self._env_combo.blockSignals(True)
            idx = self._env_combo.currentIndex()
            self._env_combo.removeItem(idx)
            self._env_combo.blockSignals(False)

    def _add_table_row(self, table):
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QTableWidgetItem(""))
        table.setItem(r, 1, QTableWidgetItem(""))

    def _del_table_row(self, table):
        rows = {i.row() for i in table.selectedIndexes()}
        for r in sorted(rows, reverse=True):
            table.removeRow(r)

    def _read_table(self, table) -> dict:
        result = {}
        for r in range(table.rowCount()):
            key = table.item(r, 0).text().strip() if table.item(r, 0) else ""
            if key:
                val = table.item(r, 1).text().strip() if table.item(r, 1) else ""
                result[key] = val
        return result

    def _on_save(self):
        # Save globals
        new_globals = self._read_table(self._global_table)
        variable_store._data["global"] = new_globals
        # Save current env
        name = self._env_combo.currentText()
        if name:
            new_env = self._read_table(self._env_table)
            variable_store._data.setdefault("environments", {})[name] = new_env
        variable_store.save()
        self.accept()


# ── Assertions Widget ──────────────────────────────────────────────

class _AssertionsWidget(QWidget):
    """Inline widget for defining response assertions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["断言类型", "目标", "期望值", "结果"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setStyleSheet(CSS_TABLE)
        layout.addWidget(self._table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ 添加断言")
        add_btn.setObjectName("smallBtn")
        add_btn.clicked.connect(self._add_assertion)
        btn_row.addWidget(add_btn)
        del_btn = QPushButton("− 删除选中")
        del_btn.setObjectName("smallBtn")
        del_btn.clicked.connect(lambda: self._del_row())
        btn_row.addWidget(del_btn)
        run_btn = QPushButton("运行断言")
        run_btn.setObjectName("smallBtn")
        run_btn.clicked.connect(self._run_assertions)
        btn_row.addWidget(run_btn)
        self._summary_lbl = QLabel("")
        self._summary_lbl.setStyleSheet("font-size: 12px; font-weight: 600;")
        btn_row.addWidget(self._summary_lbl)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._last_response = None

    def _add_assertion(self):
        r = self._table.rowCount()
        self._table.insertRow(r)
        type_combo = QComboBox()
        type_combo.addItems([
            "状态码等于", "状态码范围(2xx)", "响应体包含",
            "响应体等于(JSON)", "响应时间 < (ms)",
            "响应头包含", "响应头等于",
        ])
        type_combo.setStyleSheet(CSS_COMBO)
        self._table.setCellWidget(r, 0, type_combo)
        self._table.setItem(r, 1, QTableWidgetItem(""))
        self._table.setItem(r, 2, QTableWidgetItem(""))
        self._table.setItem(r, 3, QTableWidgetItem("⏳"))

    def _del_row(self):
        rows = {i.row() for i in self._table.selectedIndexes()}
        for r in sorted(rows, reverse=True):
            self._table.removeRow(r)

    def set_response(self, result: dict):
        self._last_response = result

    def clear_results(self):
        self._last_response = None
        for r in range(self._table.rowCount()):
            self._table.setItem(r, 3, QTableWidgetItem("⏳"))
        self._summary_lbl.setText("")

    def _run_assertions(self):
        if self._last_response is None:
            self._summary_lbl.setText("⚠ 无响应数据")
            self._summary_lbl.setStyleSheet("color: #f9e2af; font-size: 12px; font-weight: 600;")
            return
        passed = 0
        failed = 0
        for r in range(self._table.rowCount()):
            w = self._table.cellWidget(r, 0)
            atype = w.currentText() if w else ""
            target = self._table.item(r, 1).text().strip() if self._table.item(r, 1) else ""
            expected = self._table.item(r, 2).text().strip() if self._table.item(r, 2) else ""
            ok = self._check(atype, target, expected)
            if ok:
                self._table.setItem(r, 3, QTableWidgetItem("✓ 通过"))
                self._table.item(r, 3).setForeground(QColor("#a6e3a1"))
                passed += 1
            else:
                self._table.setItem(r, 3, QTableWidgetItem("✗ 失败"))
                self._table.item(r, 3).setForeground(QColor("#f38ba8"))
                failed += 1
        total = passed + failed
        self._summary_lbl.setText(f"通过 {passed}/{total}" + ("" if failed == 0 else f"  失败 {failed}"))
        self._summary_lbl.setStyleSheet(
            f"color: {'#a6e3a1' if failed == 0 else '#f38ba8'}; font-size: 12px; font-weight: 600;")

    def _check(self, atype: str, target: str, expected: str) -> bool:
        try:
            resp = self._last_response
            sc = resp.get("status_code", 0)
            body = resp.get("body_text", "") or ""
            headers = resp.get("headers", {})
            time_ms = resp.get("time_ms", 0)

            if atype == "状态码等于":
                return sc == int(expected)
            elif atype == "状态码范围(2xx)":
                return 200 <= sc < 300
            elif atype == "响应体包含":
                return expected.lower() in body.lower()
            elif atype == "响应体等于(JSON)":
                try:
                    return json_mod.loads(body) == json_mod.loads(expected)
                except Exception:
                    return body.strip() == expected.strip()
            elif atype == "响应时间 < (ms)":
                return time_ms < int(expected)
            elif atype == "响应头包含":
                return target.lower() in {k.lower(): v for k, v in headers.items()}
            elif atype == "响应头等于":
                return headers.get(target, "") == expected
        except Exception:
            pass
        return False


# ═══════════════════════════════════════════════════════════════════
# Main Page
# ═══════════════════════════════════════════════════════════════════

class HttpRequestPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._request_start = 0
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(50)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._cookie_jar: dict[str, str] = {}
        self._setup_ui()
        info("HttpRequestPage.__init__")

    # ── UI: Top-level layout ─────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(8)

        # Title + env bar
        top_bar = QHBoxLayout()
        title = QLabel("请求发送")
        title.setObjectName("myCardTitle")
        top_bar.addWidget(title)
        top_bar.addStretch()

        env_lbl = QLabel("环境:")
        env_lbl.setStyleSheet("color: #9498a4; font-size: 12px; font-weight: 600;")
        top_bar.addWidget(env_lbl)
        self._env_selector = QComboBox()
        self._env_selector.setMinimumWidth(100)
        self._env_selector.setStyleSheet(CSS_COMBO + "QComboBox { font-size: 12px; padding: 4px 8px; }")
        self._env_selector.currentTextChanged.connect(self._on_env_changed)
        top_bar.addWidget(self._env_selector)
        vars_btn = QPushButton("管理变量")
        vars_btn.setObjectName("smallBtn")
        vars_btn.clicked.connect(self._open_variable_editor)
        top_bar.addWidget(vars_btn)
        root.addLayout(top_bar)

        # URL bar
        url_bar = QHBoxLayout()
        url_bar.setSpacing(8)

        method_lbl = QLabel("请求方式")
        method_lbl.setStyleSheet("color: #c8ccd4; font-size: 13px; font-weight: 600;")
        url_bar.addWidget(method_lbl)

        self._method_combo = QComboBox()
        self._method_combo.addItems(HTTP_METHODS)
        self._method_combo.setCurrentText("GET")
        self._method_combo.setFixedWidth(100)
        self._method_combo.setStyleSheet(
            "QComboBox { background: #313244; color: #a6e3a1; border: 1px solid #45475a;"
            "  border-radius: 4px; padding: 6px 10px; font-size: 13px; font-weight: 700; }"
            "QComboBox:hover { border-color: #89b4fa; }"
            "QComboBox QAbstractItemView { background: #313244; color: #e8e8f0;"
            "  selection-background-color: #45475a; }"
        )
        url_bar.addWidget(self._method_combo)

        url_lbl = QLabel("请求URL")
        url_lbl.setStyleSheet("color: #c8ccd4; font-size: 13px; font-weight: 600;")
        url_bar.addWidget(url_lbl)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("输入请求地址，例如 {{baseUrl}}/test/get")
        self._url_input.setStyleSheet(CSS_INPUT)
        url_bar.addWidget(self._url_input, 1)

        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("actionBtn")
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.clicked.connect(self._send_request)
        url_bar.addWidget(self._send_btn)

        self._stop_btn = QPushButton("停止")
        self._stop_btn.setObjectName("dangerBtn")
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._stop_request)
        url_bar.addWidget(self._stop_btn)

        save_menu = QMenu(self)
        save_menu.addAction("保存到收藏集", self._save_to_collection)
        save_menu.addAction("导出请求为 JSON", self._export_request)
        save_menu.addAction("导入请求 JSON", self._import_request)
        save_menu.addSeparator()
        save_menu.addAction("导出所有收藏集", self._export_collections)
        save_menu.addAction("导入收藏集文件", self._import_collections)
        self._save_btn = QPushButton("保存 ▼")
        self._save_btn.setObjectName("secondaryBtn")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setMenu(save_menu)
        url_bar.addWidget(self._save_btn)

        self._clear_btn = QPushButton("清空")
        self._clear_btn.setObjectName("secondaryBtn")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.clicked.connect(self._clear_all)
        url_bar.addWidget(self._clear_btn)

        root.addLayout(url_bar)

        # ── Main splitter: sidebar | request+response ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet("QSplitter::handle { background: #313244; }")

        # Left sidebar
        sidebar = self._make_sidebar()
        splitter.addWidget(sidebar)

        # Right content
        right_w = QWidget()
        right_layout = QVBoxLayout(right_w)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        # Request config tabs
        self._request_tabs = QTabWidget()
        self._request_tabs.setStyleSheet(CSS_TAB_WIDGET)
        self._request_tabs.addTab(self._make_params_tab(), "Params")
        self._request_tabs.addTab(self._make_auth_tab(), "Authorization")
        self._request_tabs.addTab(self._make_headers_tab(), "Headers")
        self._request_tabs.addTab(self._make_body_tab(), "Body")
        self._request_tabs.addTab(self._make_settings_tab(), "Settings")
        self._request_tabs.addTab(self._make_assertions_tab(), "Assertions")
        right_layout.addWidget(self._request_tabs)

        # ── Response Area ──
        resp_header_row = QHBoxLayout()
        resp_header_row.setSpacing(8)

        self._response_status = QLabel("")
        self._response_status.setStyleSheet("font-size: 14px; font-weight: 700;")
        resp_header_row.addWidget(self._response_status)

        resp_header_row.addStretch()

        for label, attr in [("响应时间:", "_response_time"),
                             ("响应大小:", "_response_size")]:
            l = QLabel(label)
            l.setStyleSheet("color: #9498a4; font-size: 11px;")
            resp_header_row.addWidget(l)
            w = QLabel("—")
            w.setStyleSheet("color: #c8ccd4; font-size: 12px; font-weight: 600;")
            resp_header_row.addWidget(w)
            setattr(self, attr, w)

        root.addLayout(resp_header_row)

        # Response body
        self._response_body = QTextEdit()
        self._response_body.setReadOnly(True)
        self._response_body.setStyleSheet(
            "QTextEdit { background: #1e1e2e; color: #e8e8f0; border: 1px solid #313244;"
            f"  border-radius: 4px; font-family: {MONO_FONT_FAMILY};"
            "  font-size: 12px; padding: 10px; }"
        )
        right_layout.addWidget(self._response_body, 1)

        # HTML preview
        self._html_preview = None  # lazy init

        # Response sub-tabs
        self._resp_sub_tabs = QTabWidget()
        self._resp_sub_tabs.setStyleSheet(CSS_SUB_TABS)

        self._resp_headers_table = QTableWidget(0, 2)
        self._resp_headers_table.setHorizontalHeaderLabels(["响应头", "值"])
        self._resp_headers_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._resp_headers_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._resp_headers_table.verticalHeader().setVisible(False)
        self._resp_headers_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._resp_headers_table.setStyleSheet(
            "QTableWidget { background: #181825; color: #e8e8f0; border: none;"
            "  gridline-color: #252536; font-size: 12px; }"
            "QTableWidget::item { padding: 3px 6px; }"
            "QHeaderView::section { background: #252536; color: #c8ccd4;"
            "  padding: 3px 6px; border: none; font-weight: 600; font-size: 11px; }"
        )
        self._resp_sub_tabs.addTab(self._resp_headers_table, "响应头")

        self._resp_cookies_table = QTableWidget(0, 4)
        self._resp_cookies_table.setHorizontalHeaderLabels(["Cookie", "值", "Domain", "Path"])
        self._resp_cookies_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._resp_cookies_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._resp_cookies_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._resp_cookies_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._resp_cookies_table.verticalHeader().setVisible(False)
        self._resp_cookies_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._resp_cookies_table.setStyleSheet(self._resp_headers_table.styleSheet())
        self._resp_sub_tabs.addTab(self._resp_cookies_table, "Cookies")

        # Cookie jar tab
        cj_widget = QWidget()
        cj_layout = QVBoxLayout(cj_widget)
        cj_layout.setContentsMargins(4, 4, 4, 4)
        cj_layout.setSpacing(4)
        self._cookie_jar_table = QTableWidget(0, 3)
        self._cookie_jar_table.setHorizontalHeaderLabels(["Cookie 名", "值", "Domain"])
        self._cookie_jar_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._cookie_jar_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._cookie_jar_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._cookie_jar_table.verticalHeader().setVisible(False)
        self._cookie_jar_table.setStyleSheet(CSS_TABLE)
        cj_layout.addWidget(self._cookie_jar_table, 1)
        cj_btn_row = QHBoxLayout()
        cj_clear = QPushButton("清空 Cookie Jar")
        cj_clear.setObjectName("smallBtn")
        cj_clear.clicked.connect(self._clear_cookie_jar)
        cj_btn_row.addWidget(cj_clear)
        cj_del = QPushButton("删除选中")
        cj_del.setObjectName("smallBtn")
        cj_del.clicked.connect(lambda: self._del_row(self._cookie_jar_table))
        cj_btn_row.addWidget(cj_del)
        cj_btn_row.addStretch()
        cj_layout.addLayout(cj_btn_row)
        self._resp_sub_tabs.addTab(cj_widget, "Cookie Jar")

        # Preview tab
        self._preview_container = QWidget()
        preview_layout = QVBoxLayout(self._preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self._preview_label = QLabel("发送请求后，若响应为 HTML 则在此预览")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setStyleSheet("color: #6c7086; font-size: 13px;")
        preview_layout.addWidget(self._preview_label)
        self._resp_sub_tabs.addTab(self._preview_container, "Preview")

        right_layout.addWidget(self._resp_sub_tabs)

        splitter.addWidget(right_w)
        splitter.setSizes([220, 800])
        root.addWidget(splitter, 1)

        # Status bar
        self._status_label = QLabel("就绪 — 输入 URL 并发送请求")
        self._status_label.setStyleSheet("color: #9498a4; font-size: 12px;")
        root.addWidget(self._status_label)

        # Refresh sidebar
        self._refresh_history_list()
        self._refresh_collections_list()
        self._refresh_env_selector()

    # ── Sidebar ──────────────────────────────────────────────────

    def _make_sidebar(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(6)

        # Search
        self._history_search = QLineEdit()
        self._history_search.setPlaceholderText("搜索历史...")
        self._history_search.setStyleSheet(CSS_INPUT + "QLineEdit { padding: 6px 8px; font-size: 12px; }")
        self._history_search.textChanged.connect(self._on_history_search)
        layout.addWidget(self._history_search)

        # Tabs for History / Collections
        side_tabs = QTabWidget()
        side_tabs.setStyleSheet(CSS_SUB_TABS)

        # History list
        self._history_list = QListWidget()
        self._history_list.setStyleSheet(CSS_LIST)
        self._history_list.itemClicked.connect(self._on_history_clicked)
        self._history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._history_list.customContextMenuRequested.connect(self._history_context_menu)
        side_tabs.addTab(self._history_list, "历史")

        # Collections list
        coll_w = QWidget()
        coll_layout = QVBoxLayout(coll_w)
        coll_layout.setContentsMargins(0, 0, 0, 0)
        coll_layout.setSpacing(4)
        self._collections_list = QListWidget()
        self._collections_list.setStyleSheet(CSS_LIST)
        self._collections_list.itemClicked.connect(self._on_collection_clicked)
        self._collections_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._collections_list.customContextMenuRequested.connect(self._collection_context_menu)
        coll_layout.addWidget(self._collections_list, 1)
        side_tabs.addTab(coll_w, "收藏集")

        layout.addWidget(side_tabs, 1)

        # Bottom buttons
        btn_row = QHBoxLayout()
        clear_hist = QPushButton("清空历史")
        clear_hist.setObjectName("smallBtn")
        clear_hist.clicked.connect(self._clear_history)
        btn_row.addWidget(clear_hist)
        batch_btn = QPushButton("批量运行")
        batch_btn.setObjectName("smallBtn")
        batch_btn.clicked.connect(self._batch_run)
        btn_row.addWidget(batch_btn)
        layout.addLayout(btn_row)

        return w

    def _refresh_history_list(self):
        self._history_list.clear()
        for entry in request_history.get_all():
            sc = entry.get("response_status", 0)
            color, _ = _status_appearance(sc)
            sc_text = str(sc) if sc else "..."
            label = f"[{entry.get('method', 'GET')}] {sc_text}  {entry.get('url', '')[:60]}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(
                f"{entry.get('method')} {entry.get('url')}\n"
                f"状态: {sc}  时间: {entry.get('response_time_ms', '...')}ms\n"
                f"{entry.get('timestamp', '')}"
            )
            self._history_list.addItem(item)

    def _refresh_collections_list(self):
        self._collections_list.clear()
        for name in request_history.collections():
            items = request_history.get_collection(name)
            item = QListWidgetItem(f"📁 {name} ({len(items)})")
            item.setData(Qt.ItemDataRole.UserRole, {"type": "folder", "name": name})
            item.setForeground(QColor("#89b4fa"))
            self._collections_list.addItem(item)

    def _on_history_search(self, text):
        self._history_list.clear()
        entries = request_history.search(text) if text else request_history.get_all()
        for entry in entries:
            sc = entry.get("response_status", 0)
            sc_text = str(sc) if sc else "..."
            label = f"[{entry.get('method', 'GET')}] {sc_text}  {entry.get('url', '')[:60]}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self._history_list.addItem(item)

    def _on_history_clicked(self, item):
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        self._load_request_data(entry)

    def _history_context_menu(self, pos):
        item = self._history_list.currentItem()
        if not item:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("加载请求", lambda: self._load_request_data(entry))
        menu.addAction("保存到收藏集", lambda: self._save_entry_to_collection(entry))
        menu.exec(self._history_list.mapToGlobal(pos))

    def _on_collection_clicked(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data.get("type") == "folder":
            name = data["name"]
            self._collections_list.clear()
            # Show back button
            back_item = QListWidgetItem("↩ 返回收藏集列表")
            back_item.setData(Qt.ItemDataRole.UserRole, {"type": "back"})
            back_item.setForeground(QColor("#89b4fa"))
            self._collections_list.addItem(back_item)
            for i, entry in enumerate(request_history.get_collection(name)):
                label = f"[{entry.get('method', 'GET')}] {entry.get('url', '')[:55]}"
                item2 = QListWidgetItem(label)
                item2.setData(Qt.ItemDataRole.UserRole, {"type": "request", "entry": entry, "collection": name, "index": i})
                self._collections_list.addItem(item2)
        elif data.get("type") == "back":
            self._refresh_collections_list()
        elif data.get("type") == "request":
            self._load_request_data(data["entry"])

    def _collection_context_menu(self, pos):
        item = self._collections_list.currentItem()
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        menu = QMenu(self)
        if data.get("type") == "folder":
            name = data["name"]
            menu.addAction("重命名", lambda: self._rename_collection(name))
            menu.addAction("删除", lambda: self._delete_collection(name))
        elif data.get("type") == "request":
            menu.addAction("删除此项", lambda: self._delete_collection_item(data["collection"], data["index"]))
        menu.exec(self._collections_list.mapToGlobal(pos))

    def _rename_collection(self, old_name):
        from PyQt6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(self, "重命名", "新名称:", text=old_name)
        if ok and new_name.strip() and new_name.strip() != old_name:
            request_history.rename_collection(old_name, new_name.strip())
            self._refresh_collections_list()

    def _delete_collection(self, name):
        if QMessageBox.question(self, "确认", f"删除收藏集 '{name}'？") == QMessageBox.StandardButton.Yes:
            request_history.delete_collection(name)
            self._refresh_collections_list()

    def _delete_collection_item(self, name, idx):
        request_history.delete_collection_item(name, idx)
        self._on_collection_clicked(
            self._collections_list.item(0) if self._collections_list.count() > 0 else None)
        if self._collections_list.count() <= 1:
            self._refresh_collections_list()

    def _clear_history(self):
        request_history.clear()
        self._refresh_history_list()

    def _save_entry_to_collection(self, entry):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "保存到收藏集", "收藏集名称:")
        if ok and name.strip():
            request_history.save_to_collection(name.strip(), entry)
            self._refresh_collections_list()

    def _load_request_data(self, data):
        self._method_combo.setCurrentText(data.get("method", "GET"))
        self._url_input.setText(data.get("url", ""))
        self._load_kv_table(self._params_table, data.get("params", {}))
        self._load_kv_table(self._headers_table, data.get("headers", {}))
        self._load_auth_config(data.get("auth_config", {}))
        bc = data.get("body_config", {"type": "none"})
        self._load_body_config(bc)
        sc = data.get("settings_config", {})
        self._timeout_spin.setValue(sc.get("timeout", 30))
        self._redirect_cb.setChecked(sc.get("allow_redirects", True))
        self._ssl_cb.setChecked(sc.get("verify_ssl", True))
        self._proxy_input.setText(sc.get("proxy", ""))

    def _load_kv_table(self, table, d: dict):
        table.setRowCount(0)
        for k, v in d.items():
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(k))
            table.setItem(r, 1, QTableWidgetItem(v))

    def _load_auth_config(self, ac: dict):
        atype = ac.get("type", "None")
        idx = AUTH_TYPES.index(atype) if atype in AUTH_TYPES else 0
        self._auth_type_combo.setCurrentIndex(idx)
        self._on_auth_type_changed(idx)
        self._api_key_name.setText(ac.get("api_key_name", ""))
        self._api_key_value.setText(ac.get("api_key_value", ""))
        loc = ac.get("api_key_location", "Header")
        self._api_key_location.setCurrentText(loc)
        self._bearer_token.setText(ac.get("bearer_token", ""))
        self._basic_user.setText(ac.get("basic_user", ""))
        self._basic_pass.setText(ac.get("basic_pass", ""))
        self._digest_user.setText(ac.get("digest_user", ""))
        self._digest_pass.setText(ac.get("digest_pass", ""))

    def _load_body_config(self, bc: dict):
        bt = bc.get("type", "none")
        idx = BODY_TYPES.index(bt) if bt in BODY_TYPES else 0
        self._body_group.button(idx).setChecked(True)
        self._on_body_type_changed(idx)
        if bt in ("form-data", "x-www-form-urlencoded"):
            self._body_form_table.setRowCount(0)
            for row_data in bc.get("form_data", []):
                r = self._body_form_table.rowCount()
                self._body_form_table.insertRow(r)
                self._body_form_table.setItem(r, 0, QTableWidgetItem(row_data.get("key", "")))
                if bt == "form-data":
                    type_combo = QComboBox()
                    type_combo.addItems(["text", "file"])
                    type_combo.setCurrentText(row_data.get("row_type", "text"))
                    type_combo.setStyleSheet(
                        "QComboBox { background: #313244; color: #e8e8f0; border: 1px solid #45475a;"
                        "  border-radius: 2px; padding: 2px 6px; font-size: 12px; }")
                    self._body_form_table.setCellWidget(r, 1, type_combo)
                self._body_form_table.setItem(r, 2 if bt == "form-data" else 1, QTableWidgetItem(row_data.get("value", "")))
        elif bt == "raw":
            self._raw_editor.setPlainText(bc.get("raw_text", ""))
            ct = bc.get("content_type", "json")
            self._raw_content_type.setCurrentText(ct.upper() if ct in ["json", "xml", "html", "text"] else "JSON")
        elif bt == "binary":
            path = bc.get("file_path", "")
            if path:
                self._binary_path_label.setText(path)
                self._binary_path_label.setStyleSheet(
                    "color: #a6e3a1; font-size: 13px; padding: 6px 10px;"
                    "background: #1e1e2e; border: 1px solid #45475a; border-radius: 4px;")

    # ── Params tab ────────────────────────────────────────────────

    def _make_params_tab(self):
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._params_table = _kv_table()
        self._params_table.setHorizontalHeaderLabels(["参数名", "参数值"])
        self._params_table.setMinimumHeight(120)
        layout.addWidget(self._params_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ 添加")
        add_btn.setObjectName("smallBtn")
        add_btn.clicked.connect(lambda: self._add_row(self._params_table))
        btn_row.addWidget(add_btn)
        del_btn = QPushButton("− 删除选中")
        del_btn.setObjectName("smallBtn")
        del_btn.clicked.connect(lambda: self._del_row(self._params_table))
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        hint = QLabel("参数将自动拼接到 URL 查询字符串中。支持 {{变量}} 如 {{baseUrl}}")
        hint.setStyleSheet("color: #6c7086; font-size: 12px;")
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #181825; }"
            "QScrollBar:vertical { background: #181825; width: 8px; border-radius: 4px; }"
            "QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        return scroll

    # ── Authorization tab ─────────────────────────────────────────

    def _make_auth_tab(self):
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        type_row = QHBoxLayout()
        tlbl = QLabel("认证类型:")
        tlbl.setStyleSheet("color: #c8ccd4; font-size: 13px; font-weight: 600;")
        type_row.addWidget(tlbl)
        self._auth_type_combo = QComboBox()
        self._auth_type_combo.addItems(AUTH_TYPES)
        self._auth_type_combo.setStyleSheet(CSS_COMBO)
        self._auth_type_combo.currentIndexChanged.connect(self._on_auth_type_changed)
        type_row.addWidget(self._auth_type_combo)
        type_row.addStretch()
        layout.addLayout(type_row)

        self._auth_stack = QStackedWidget()

        # None page
        none_p = QLabel("无需认证")
        none_p.setAlignment(Qt.AlignmentFlag.AlignCenter)
        none_p.setStyleSheet("color: #6c7086; font-size: 13px;")
        self._auth_stack.addWidget(none_p)

        # API Key page
        api_w = QWidget()
        api_l = QFormLayout(api_w)
        api_l.setContentsMargins(0, 8, 0, 0)
        api_l.setSpacing(10)
        self._api_key_name = QLineEdit()
        self._api_key_name.setPlaceholderText("例如 X-API-Key")
        self._api_key_name.setStyleSheet(CSS_INPUT)
        api_l.addRow("Key 名称:", self._api_key_name)
        self._api_key_value = QLineEdit()
        self._api_key_value.setPlaceholderText("输入 API Key 值")
        self._api_key_value.setStyleSheet(CSS_INPUT)
        api_l.addRow("Key 值:", self._api_key_value)
        self._api_key_location = QComboBox()
        self._api_key_location.addItems(API_KEY_LOCATIONS)
        self._api_key_location.setStyleSheet(CSS_COMBO)
        api_l.addRow("添加位置:", self._api_key_location)
        self._auth_stack.addWidget(api_w)

        # Bearer Token page
        bearer_w = QWidget()
        bearer_l = QFormLayout(bearer_w)
        bearer_l.setContentsMargins(0, 8, 0, 0)
        bearer_l.setSpacing(10)
        self._bearer_token = QLineEdit()
        self._bearer_token.setPlaceholderText("输入 Bearer Token（不含 Bearer 前缀）")
        self._bearer_token.setStyleSheet(CSS_INPUT)
        self._bearer_token.setEchoMode(QLineEdit.EchoMode.Password)
        bearer_l.addRow("Token:", self._bearer_token)
        show_row = QHBoxLayout()
        show_cb = QCheckBox("显示 Token")
        show_cb.toggled.connect(lambda checked: self._bearer_token.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        show_cb.setStyleSheet("color: #9498a4; font-size: 12px;")
        show_row.addWidget(show_cb)
        show_row.addStretch()
        bearer_l.addRow("", show_row)
        self._auth_stack.addWidget(bearer_w)

        # Basic Auth page
        basic_w = QWidget()
        basic_l = QFormLayout(basic_w)
        basic_l.setContentsMargins(0, 8, 0, 0)
        basic_l.setSpacing(10)
        self._basic_user = QLineEdit()
        self._basic_user.setPlaceholderText("用户名")
        self._basic_user.setStyleSheet(CSS_INPUT)
        basic_l.addRow("用户名:", self._basic_user)
        self._basic_pass = QLineEdit()
        self._basic_pass.setPlaceholderText("密码")
        self._basic_pass.setStyleSheet(CSS_INPUT)
        self._basic_pass.setEchoMode(QLineEdit.EchoMode.Password)
        basic_l.addRow("密码:", self._basic_pass)
        self._auth_stack.addWidget(basic_w)

        # Digest Auth page
        digest_w = QWidget()
        digest_l = QFormLayout(digest_w)
        digest_l.setContentsMargins(0, 8, 0, 0)
        digest_l.setSpacing(10)
        self._digest_user = QLineEdit()
        self._digest_user.setPlaceholderText("用户名")
        self._digest_user.setStyleSheet(CSS_INPUT)
        digest_l.addRow("用户名:", self._digest_user)
        self._digest_pass = QLineEdit()
        self._digest_pass.setPlaceholderText("密码")
        self._digest_pass.setStyleSheet(CSS_INPUT)
        self._digest_pass.setEchoMode(QLineEdit.EchoMode.Password)
        digest_l.addRow("密码:", self._digest_pass)
        self._auth_stack.addWidget(digest_w)

        layout.addWidget(self._auth_stack, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #181825; }"
            "QScrollBar:vertical { background: #181825; width: 8px; border-radius: 4px; }"
            "QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        return scroll

    def _on_auth_type_changed(self, idx):
        self._auth_stack.setCurrentIndex(idx)

    def _get_auth_config(self) -> dict:
        atype = self._auth_type_combo.currentText()
        config = {"type": atype}
        if atype == "API Key":
            config.update({
                "api_key_name": self._api_key_name.text().strip(),
                "api_key_value": self._api_key_value.text().strip(),
                "api_key_location": self._api_key_location.currentText(),
            })
        elif atype == "Bearer Token":
            config["bearer_token"] = self._bearer_token.text().strip()
        elif atype == "Basic Auth":
            config.update({
                "basic_user": self._basic_user.text().strip(),
                "basic_pass": self._basic_pass.text().strip(),
            })
        elif atype == "Digest Auth":
            config.update({
                "digest_user": self._digest_user.text().strip(),
                "digest_pass": self._digest_pass.text().strip(),
            })
        return config

    def _apply_auth_to_headers(self, headers: dict) -> dict:
        config = self._get_auth_config()
        atype = config["type"]
        if atype == "API Key":
            key_name = variable_store.resolve(config.get("api_key_name", ""))
            key_value = variable_store.resolve(config.get("api_key_value", ""))
            if key_name and key_value:
                if config.get("api_key_location") == "Query Parameter":
                    pass  # handled in _get_params_dict
                else:
                    headers[key_name] = key_value
        elif atype == "Bearer Token":
            token = variable_store.resolve(config.get("bearer_token", ""))
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif atype == "Basic Auth":
            import base64
            user = variable_store.resolve(config.get("basic_user", ""))
            pwd = variable_store.resolve(config.get("basic_pass", ""))
            if user or pwd:
                creds = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                headers["Authorization"] = f"Basic {creds}"
        elif atype == "Digest Auth":
            pass  # Requests handles Digest via auth param
        return headers

    # ── Headers tab ───────────────────────────────────────────────

    def _make_headers_tab(self):
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._headers_table = _kv_table()
        self._headers_table.setHorizontalHeaderLabels(["请求头名称", "请求头值"])
        self._headers_table.setMinimumHeight(120)
        layout.addWidget(self._headers_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ 添加")
        add_btn.setObjectName("smallBtn")
        add_btn.clicked.connect(lambda: self._add_row(self._headers_table))
        btn_row.addWidget(add_btn)
        del_btn = QPushButton("− 删除选中")
        del_btn.setObjectName("smallBtn")
        del_btn.clicked.connect(lambda: self._del_row(self._headers_table))
        btn_row.addWidget(del_btn)
        paste_btn = QPushButton("批量粘贴")
        paste_btn.setObjectName("smallBtn")
        paste_btn.clicked.connect(self._batch_paste_headers)
        btn_row.addWidget(paste_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        hint = QLabel("批量粘贴格式: Header-Name: value（每行一个）  支持 {{变量}}")
        hint.setStyleSheet("color: #6c7086; font-size: 12px;")
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #181825; }"
            "QScrollBar:vertical { background: #181825; width: 8px; border-radius: 4px; }"
            "QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        return scroll

    # ── Body tab ──────────────────────────────────────────────────

    def _make_body_tab(self):
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        type_row = QHBoxLayout()
        type_row.setSpacing(4)
        self._body_group = QButtonGroup(inner)
        for i, bt in enumerate(BODY_TYPES):
            rb = QRadioButton(bt)
            rb.setStyleSheet(
                "QRadioButton { color: #c8ccd4; font-size: 12px; spacing: 4px; }"
                "QRadioButton::indicator { width: 14px; height: 14px; }"
                "QRadioButton:checked { color: #89b4fa; font-weight: 700; }"
            )
            if i == 0:
                rb.setChecked(True)
            self._body_group.addButton(rb, i)
            type_row.addWidget(rb)
        type_row.addStretch()
        self._body_group.idClicked.connect(self._on_body_type_changed)
        layout.addLayout(type_row)

        raw_type_row = QHBoxLayout()
        rt_lbl = QLabel("内容类型:")
        rt_lbl.setStyleSheet("color: #c8ccd4; font-size: 13px;")
        raw_type_row.addWidget(rt_lbl)
        self._raw_content_type = QComboBox()
        self._raw_content_type.addItems(RAW_CONTENT_TYPES)
        self._raw_content_type.setCurrentText("JSON")
        self._raw_content_type.setStyleSheet(CSS_COMBO)
        raw_type_row.addWidget(self._raw_content_type)
        raw_type_row.addStretch()
        self._raw_type_row_widget = QWidget()
        self._raw_type_row_widget.setLayout(raw_type_row)
        self._raw_type_row_widget.setVisible(False)
        layout.addWidget(self._raw_type_row_widget)

        # Form widget
        form_w = QWidget()
        form_layout = QVBoxLayout(form_w)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(4)
        self._body_form_table = QTableWidget(0, 3)
        self._body_form_table.setHorizontalHeaderLabels(["键", "类型", "值"])
        self._body_form_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._body_form_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._body_form_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._body_form_table.verticalHeader().setVisible(False)
        self._body_form_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._body_form_table.setStyleSheet(CSS_TABLE)
        self._body_form_table.setMinimumHeight(100)
        form_layout.addWidget(self._body_form_table, 1)
        form_btn_row = QHBoxLayout()
        add_f_btn = QPushButton("+ 添加")
        add_f_btn.setObjectName("smallBtn")
        add_f_btn.clicked.connect(lambda: self._add_form_row())
        form_btn_row.addWidget(add_f_btn)
        del_f_btn = QPushButton("− 删除选中")
        del_f_btn.setObjectName("smallBtn")
        del_f_btn.clicked.connect(lambda: self._del_row(self._body_form_table))
        form_btn_row.addWidget(del_f_btn)
        form_btn_row.addStretch()
        form_layout.addLayout(form_btn_row)
        self._body_form_widget = form_w
        self._body_form_widget.setVisible(False)
        layout.addWidget(self._body_form_widget)

        # Raw editor
        raw_w = QWidget()
        raw_layout = QVBoxLayout(raw_w)
        raw_layout.setContentsMargins(0, 0, 0, 0)
        raw_layout.setSpacing(4)
        self._raw_editor = QTextEdit()
        self._raw_editor.setPlaceholderText("输入请求体内容...  支持 {{变量}}")
        self._raw_editor.setMinimumHeight(120)
        self._raw_editor.setStyleSheet(
            "QTextEdit { background: #1e1e2e; color: #e8e8f0; border: 1px solid #313244;"
            f"  border-radius: 4px; font-family: {MONO_FONT_FAMILY};"
            "  font-size: 13px; padding: 8px; }"
        )
        raw_layout.addWidget(self._raw_editor, 1)
        json_btn_row = QHBoxLayout()
        self._json_format_btn = QPushButton("格式化 JSON")
        self._json_format_btn.setObjectName("smallBtn")
        self._json_format_btn.clicked.connect(self._format_json)
        json_btn_row.addWidget(self._json_format_btn)
        self._json_validate_btn = QPushButton("校验 JSON")
        self._json_validate_btn.setObjectName("smallBtn")
        self._json_validate_btn.clicked.connect(self._validate_json)
        json_btn_row.addWidget(self._json_validate_btn)
        self._json_status_lbl = QLabel("")
        self._json_status_lbl.setStyleSheet("font-size: 12px; font-weight: 600;")
        json_btn_row.addWidget(self._json_status_lbl)
        json_btn_row.addStretch()
        raw_layout.addLayout(json_btn_row)
        self._body_raw_widget = raw_w
        self._body_raw_widget.setVisible(False)
        layout.addWidget(self._body_raw_widget)

        # Binary
        binary_w = QWidget()
        binary_layout = QHBoxLayout(binary_w)
        binary_layout.setContentsMargins(0, 0, 0, 0)
        binary_layout.setSpacing(8)
        self._binary_path_label = QLabel("未选择文件")
        self._binary_path_label.setMinimumHeight(22)
        self._binary_path_label.setStyleSheet(
            "color: #6c7086; font-size: 13px; padding: 6px 10px;"
            "background: #1e1e2e; border: 1px solid #313244; border-radius: 4px;")
        binary_layout.addWidget(self._binary_path_label, 1)
        pick_btn = QPushButton("选择文件")
        pick_btn.setObjectName("secondaryBtn")
        pick_btn.clicked.connect(self._pick_binary_file)
        binary_layout.addWidget(pick_btn)
        self._body_binary_widget = binary_w
        self._body_binary_widget.setVisible(False)
        layout.addWidget(self._body_binary_widget)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #181825; }"
            "QScrollBar:vertical { background: #181825; width: 8px; border-radius: 4px; }"
            "QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        return scroll

    # ── Settings tab ──────────────────────────────────────────────

    def _make_settings_tab(self):
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(14)

        # Timeout
        g1 = QGroupBox("请求超时")
        g1.setStyleSheet(CSS_GROUP_BOX)
        g1l = QHBoxLayout(g1)
        g1l.setContentsMargins(10, 16, 10, 12)
        g1l.setSpacing(10)
        lbl = QLabel("超时时间 (秒):")
        lbl.setStyleSheet("color: #c8ccd4; font-size: 13px;")
        g1l.addWidget(lbl)
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(1, 300)
        self._timeout_spin.setValue(30)
        self._timeout_spin.setMinimumWidth(80)
        self._timeout_spin.setStyleSheet(
            "QSpinBox { background: #181825; color: #e8e8f0; border: 1px solid #45475a;"
            "  border-radius: 4px; padding: 8px 10px; font-size: 13px; min-height: 22px; }"
        )
        g1l.addWidget(self._timeout_spin)
        g1l.addStretch()
        layout.addWidget(g1)

        # Redirect & SSL
        g2 = QGroupBox("请求行为")
        g2.setStyleSheet(CSS_GROUP_BOX)
        g2l = QVBoxLayout(g2)
        g2l.setContentsMargins(10, 16, 10, 12)
        g2l.setSpacing(10)
        self._redirect_cb = QCheckBox("自动跟随重定向")
        self._redirect_cb.setChecked(True)
        self._redirect_cb.setStyleSheet("color: #c8ccd4; font-size: 13px; spacing: 8px;")
        self._redirect_cb.setMinimumHeight(22)
        g2l.addWidget(self._redirect_cb)
        self._ssl_cb = QCheckBox("验证 SSL 证书")
        self._ssl_cb.setChecked(True)
        self._ssl_cb.setStyleSheet("color: #c8ccd4; font-size: 13px; spacing: 8px;")
        self._ssl_cb.setMinimumHeight(22)
        g2l.addWidget(self._ssl_cb)
        layout.addWidget(g2)

        # Proxy
        g3 = QGroupBox("代理设置")
        g3.setStyleSheet(CSS_GROUP_BOX)
        g3l = QHBoxLayout(g3)
        g3l.setContentsMargins(10, 16, 10, 12)
        g3l.setSpacing(10)
        plbl = QLabel("HTTP/HTTPS 代理:")
        plbl.setStyleSheet("color: #c8ccd4; font-size: 13px;")
        g3l.addWidget(plbl)
        self._proxy_input = QLineEdit()
        self._proxy_input.setPlaceholderText("例如 http://127.0.0.1:8080  (留空表示不使用代理)")
        self._proxy_input.setStyleSheet(CSS_INPUT)
        g3l.addWidget(self._proxy_input, 1)
        layout.addWidget(g3)

        layout.addStretch()

        # Wrap in scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #181825; }"
            "QScrollBar:vertical { background: #181825; width: 8px; border-radius: 4px; }"
            "QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        return scroll

    def _get_settings_config(self) -> dict:
        return {
            "timeout": self._timeout_spin.value(),
            "allow_redirects": self._redirect_cb.isChecked(),
            "verify_ssl": self._ssl_cb.isChecked(),
            "proxy": self._proxy_input.text().strip(),
        }

    # ── Assertions tab ────────────────────────────────────────────

    def _make_assertions_tab(self):
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self._assertions_widget = _AssertionsWidget()
        layout.addWidget(self._assertions_widget, 1)
        hint = QLabel("定义断言规则，验证响应是否符合预期。发送请求后点击「运行断言」。")
        hint.setStyleSheet("color: #6c7086; font-size: 12px;")
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #181825; }"
            "QScrollBar:vertical { background: #181825; width: 8px; border-radius: 4px; }"
            "QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        return scroll

    # ── Table helpers ─────────────────────────────────────────────

    def _add_row(self, table, col_count=2):
        row = table.rowCount()
        table.insertRow(row)
        for c in range(col_count):
            table.setItem(row, c, QTableWidgetItem(""))

    def _add_form_row(self):
        row = self._body_form_table.rowCount()
        self._body_form_table.insertRow(row)
        self._body_form_table.setItem(row, 0, QTableWidgetItem(""))
        bt = BODY_TYPES[self._body_group.checkedId()] if self._body_group.checkedId() >= 0 else "form-data"
        if bt == "form-data":
            type_combo = QComboBox()
            type_combo.addItems(["text", "file"])
            type_combo.setStyleSheet(
                "QComboBox { background: #313244; color: #e8e8f0; border: 1px solid #45475a;"
                "  border-radius: 2px; padding: 2px 6px; font-size: 12px; }")
            self._body_form_table.setCellWidget(row, 1, type_combo)
        self._body_form_table.setItem(row, 2, QTableWidgetItem(""))

    def _del_row(self, table):
        rows = set()
        for idx in table.selectedIndexes():
            rows.add(idx.row())
        for r in sorted(rows, reverse=True):
            table.removeRow(r)

    # ── Headers batch paste ───────────────────────────────────────

    def _batch_paste_headers(self):
        text = QApplication.clipboard().text()
        if not text:
            return
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            match = re.match(r'^([^:]+):\s*(.*)$', line)
            if match:
                key = match.group(1).strip()
                val = match.group(2).strip()
                row = self._headers_table.rowCount()
                self._headers_table.insertRow(row)
                self._headers_table.setItem(row, 0, QTableWidgetItem(key))
                self._headers_table.setItem(row, 1, QTableWidgetItem(val))

    # ── Body type switching ──────────────────────────────────────

    def _on_body_type_changed(self, idx):
        bt = BODY_TYPES[idx]
        self._body_form_widget.setVisible(bt in ("form-data", "x-www-form-urlencoded"))
        self._body_raw_widget.setVisible(bt == "raw")
        self._body_binary_widget.setVisible(bt == "binary")
        self._raw_type_row_widget.setVisible(bt == "raw")

        if bt in ("form-data", "x-www-form-urlencoded"):
            cols = 3 if bt == "form-data" else 2
            self._body_form_table.setColumnCount(cols)
            if cols == 2:
                self._body_form_table.setHorizontalHeaderLabels(["键", "值"])
            else:
                self._body_form_table.setHorizontalHeaderLabels(["键", "类型", "值"])

        if bt == "raw":
            ct = self._raw_content_type.currentText()
            self._json_format_btn.setVisible(ct == "JSON")
            self._json_validate_btn.setVisible(ct == "JSON")
            self._json_status_lbl.setVisible(ct == "JSON")

    # ── JSON tools ────────────────────────────────────────────────

    def _format_json(self):
        text = self._raw_editor.toPlainText().strip()
        if not text:
            return
        try:
            obj = json_mod.loads(text)
            formatted = json_mod.dumps(obj, indent=2, ensure_ascii=False)
            self._raw_editor.setPlainText(formatted)
            self._json_status_lbl.setText("✓ JSON 格式有效")
            self._json_status_lbl.setStyleSheet("color: #a6e3a1; font-size: 11px; font-weight: 600;")
        except json_mod.JSONDecodeError as e:
            self._json_status_lbl.setText(f"✗ JSON 无效: {e}")
            self._json_status_lbl.setStyleSheet("color: #f38ba8; font-size: 11px; font-weight: 600;")

    def _validate_json(self):
        text = self._raw_editor.toPlainText().strip()
        if not text:
            self._json_status_lbl.setText("(空)")
            self._json_status_lbl.setStyleSheet("color: #6c7086; font-size: 11px;")
            return
        try:
            json_mod.loads(text)
            self._json_status_lbl.setText("✓ JSON 格式有效")
            self._json_status_lbl.setStyleSheet("color: #a6e3a1; font-size: 11px; font-weight: 600;")
        except json_mod.JSONDecodeError as e:
            self._json_status_lbl.setText(f"✗ JSON 无效: {e}")
            self._json_status_lbl.setStyleSheet("color: #f38ba8; font-size: 11px; font-weight: 600;")

    def _pick_binary_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件")
        if path:
            self._binary_path_label.setText(path)
            self._binary_path_label.setStyleSheet(
                "color: #a6e3a1; font-size: 13px; padding: 6px 10px;"
                "background: #1e1e2e; border: 1px solid #45475a; border-radius: 4px;")

    # ── Variable / Env management ─────────────────────────────────

    def _refresh_env_selector(self):
        self._env_selector.blockSignals(True)
        self._env_selector.clear()
        self._env_selector.addItem("(无环境)")
        for name in variable_store.environments():
            self._env_selector.addItem(name)
        active = variable_store.active_environment()
        if active:
            self._env_selector.setCurrentText(active)
        self._env_selector.blockSignals(False)

    def _on_env_changed(self, name):
        if name == "(无环境)":
            variable_store.set_active_environment(None)
        else:
            variable_store.set_active_environment(name)

    def _open_variable_editor(self):
        dlg = _VariableEditor(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_env_selector()

    # ── Data extraction ──────────────────────────────────────────

    def _get_params_dict(self):
        params = {}
        for r in range(self._params_table.rowCount()):
            key_item = self._params_table.item(r, 0)
            val_item = self._params_table.item(r, 1)
            key = key_item.text().strip() if key_item else ""
            if key:
                val = val_item.text().strip() if val_item else ""
                # Append API key if configured
                auth = self._get_auth_config()
                if auth["type"] == "API Key" and auth.get("api_key_location") == "Query Parameter":
                    ak_name = variable_store.resolve(auth.get("api_key_name", ""))
                    ak_val = variable_store.resolve(auth.get("api_key_value", ""))
                    if ak_name and ak_name not in params:
                        params[ak_name] = ak_val
                # Resolve variables
                params[variable_store.resolve(key)] = variable_store.resolve(val)
        return params

    def _get_headers_dict(self):
        headers = {}
        for r in range(self._headers_table.rowCount()):
            key_item = self._headers_table.item(r, 0)
            val_item = self._headers_table.item(r, 1)
            key = key_item.text().strip() if key_item else ""
            if key:
                val = val_item.text().strip() if val_item else ""
                headers[variable_store.resolve(key)] = variable_store.resolve(val)
        # Apply auth headers
        headers = self._apply_auth_to_headers(headers)
        # Attach cookie jar
        if self._cookie_jar:
            cookie_str = "; ".join(f"{k}={v}" for k, v in self._cookie_jar.items())
            if cookie_str:
                headers["Cookie"] = cookie_str
        return headers

    def _get_body_config(self):
        idx = self._body_group.checkedId()
        if idx < 0:
            idx = 0
        bt = BODY_TYPES[idx]
        config = {"type": bt}

        if bt in ("form-data", "x-www-form-urlencoded"):
            form_data = []
            for r in range(self._body_form_table.rowCount()):
                key_item = self._body_form_table.item(r, 0)
                val_item = self._body_form_table.item(r, 2)
                key = key_item.text().strip() if key_item else ""
                if key:
                    val = val_item.text().strip() if val_item else ""
                    row_type = "text"
                    if bt == "form-data" and self._body_form_table.cellWidget(r, 1):
                        row_type = self._body_form_table.cellWidget(r, 1).currentText()
                    form_data.append({
                        "key": variable_store.resolve(key),
                        "value": variable_store.resolve(val),
                        "row_type": row_type
                    })
            config["form_data"] = form_data
        elif bt == "raw":
            raw_text = variable_store.resolve(self._raw_editor.toPlainText())
            config["raw_text"] = raw_text
            config["content_type"] = self._raw_content_type.currentText().lower()
        elif bt == "binary":
            config["file_path"] = self._binary_path_label.text()
        return config

    # ── Send / Stop ───────────────────────────────────────────────

    def _send_request(self):
        url = variable_store.resolve(self._url_input.text().strip())
        if not url:
            QMessageBox.warning(self, "提示", "请输入请求 URL")
            return

        if not url.startswith(("http://", "https://")):
            url = "http://" + url
            self._url_input.setText(url)

        info(f"HttpRequestPage._send_request: {self._method_combo.currentText()} {url}")

        self._send_btn.setVisible(False)
        self._stop_btn.setVisible(True)
        self._response_body.clear()
        self._response_status.setText("")
        self._response_time.setText("...")
        self._response_size.setText("...")
        self._resp_headers_table.setRowCount(0)
        self._resp_cookies_table.setRowCount(0)
        self._status_label.setText("正在发送请求...")
        self._status_label.setStyleSheet("color: #89b4fa; font-size: 12px;")
        self._assertions_widget.clear_results()

        method = self._method_combo.currentText()
        params = self._get_params_dict()
        headers = self._get_headers_dict()
        body_config = self._get_body_config()
        settings_config = self._get_settings_config()
        auth_config = self._get_auth_config()

        self._request_start = time.time()
        self._elapsed_timer.start()

        self._worker = HttpRequestWorker(
            method, url, params, headers, body_config,
            timeout=settings_config["timeout"],
            allow_redirects=settings_config["allow_redirects"],
            verify_ssl=settings_config["verify_ssl"],
            proxy=settings_config.get("proxy") or None,
        )
        self._worker.finished.connect(self._on_request_finished)
        self._worker.error.connect(self._on_request_error)
        self._worker.status.connect(self._status_label.setText)
        self._worker.start()

    def _stop_request(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._status_label.setText("请求已取消")
            self._status_label.setStyleSheet("color: #f9e2af; font-size: 12px;")
        self._on_request_done()
        info("HttpRequestPage._stop_request")

    def _on_request_done(self):
        self._elapsed_timer.stop()
        self._send_btn.setVisible(True)
        self._stop_btn.setVisible(False)
        if self._worker:
            try:
                self._worker.disconnect()
            except Exception:
                pass
            self._worker = None

    def _update_elapsed(self):
        if self._request_start:
            elapsed = time.time() - self._request_start
            self._response_time.setText(f"{elapsed:.1f}s")

    def _on_request_finished(self, result):
        code = result["status_code"]
        color, label = _status_appearance(code)

        self._response_status.setText(f"{code} {result['reason']}")
        self._response_status.setStyleSheet(
            f"font-size: 14px; font-weight: 700; color: {color};")
        self._response_time.setText(f"{result['time_ms']}ms")
        self._response_size.setText(format_size(result["size"]))

        body = result.get("body_text", "")
        if body:
            self._response_body.setPlainText(body)
            cursor = self._response_body.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self._response_body.setTextCursor(cursor)

        # Response headers
        self._resp_headers_table.setRowCount(0)
        for key, val in result.get("headers", {}).items():
            r = self._resp_headers_table.rowCount()
            self._resp_headers_table.insertRow(r)
            self._resp_headers_table.setItem(r, 0, QTableWidgetItem(key))
            self._resp_headers_table.setItem(r, 1, QTableWidgetItem(val))

        # Cookies
        self._resp_cookies_table.setRowCount(0)
        for ck in result.get("cookies", []):
            r = self._resp_cookies_table.rowCount()
            self._resp_cookies_table.insertRow(r)
            self._resp_cookies_table.setItem(r, 0, QTableWidgetItem(ck.get("name", "")))
            self._resp_cookies_table.setItem(r, 1, QTableWidgetItem(ck.get("value", "")))
            self._resp_cookies_table.setItem(r, 2, QTableWidgetItem(ck.get("domain", "")))
            self._resp_cookies_table.setItem(r, 3, QTableWidgetItem(ck.get("path", "")))
            # Auto-add to cookie jar
            name = ck.get("name", "")
            if name:
                self._cookie_jar[name] = ck.get("value", "")
                domain = ck.get("domain", "")
                # Update cookie jar table
                found = False
                for cj_r in range(self._cookie_jar_table.rowCount()):
                    if self._cookie_jar_table.item(cj_r, 0).text() == name:
                        self._cookie_jar_table.setItem(cj_r, 1, QTableWidgetItem(ck.get("value", "")))
                        found = True
                        break
                if not found:
                    cj_r = self._cookie_jar_table.rowCount()
                    self._cookie_jar_table.insertRow(cj_r)
                    self._cookie_jar_table.setItem(cj_r, 0, QTableWidgetItem(name))
                    self._cookie_jar_table.setItem(cj_r, 1, QTableWidgetItem(ck.get("value", "")))
                    self._cookie_jar_table.setItem(cj_r, 2, QTableWidgetItem(domain))

        # HTML preview
        content_type = result.get("headers", {}).get("Content-Type", "").lower()
        if "html" in content_type and body:
            self._show_html_preview(body)
        else:
            self._hide_html_preview()

        # Assertions
        self._assertions_widget.set_response(result)

        # Save to history
        method = self._method_combo.currentText()
        url = self._url_input.text().strip()
        request_history.add(
            method=method,
            url=url,
            params=self._get_params_dict(),
            headers=self._get_headers_dict(),
            body_config=self._get_body_config(),
            auth_config=self._get_auth_config(),
            settings_config=self._get_settings_config(),
            response_status=code,
            response_time_ms=result["time_ms"],
            response_size=result["size"],
        )
        self._refresh_history_list()

        self._status_label.setText(
            f"完成 — {code} {result['reason']}  |  {result['time_ms']}ms  |  {format_size(result['size'])}")
        self._status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self._on_request_done()
        info(f"HttpRequestPage request complete: {code} {result['time_ms']}ms")

    def _on_request_error(self, msg):
        self._response_status.setText("✗ 错误")
        self._response_status.setStyleSheet("font-size: 14px; font-weight: 700; color: #f38ba8;")
        self._response_time.setText("—")
        self._response_size.setText("—")
        self._response_body.setPlainText(msg)
        self._status_label.setText(f"请求失败: {msg}")
        self._status_label.setStyleSheet("color: #f38ba8; font-size: 12px;")
        self._hide_html_preview()
        self._on_request_done()
        error(f"HttpRequestPage request error: {msg}")

        # Save to history even on error
        method = self._method_combo.currentText()
        url = self._url_input.text().strip()
        request_history.add(
            method=method,
            url=url,
            params=self._get_params_dict(),
            headers=self._get_headers_dict(),
            body_config=self._get_body_config(),
            auth_config=self._get_auth_config(),
            settings_config=self._get_settings_config(),
            response_status=0,
            response_time_ms=0,
            response_size=0,
        )
        self._refresh_history_list()

    # ── HTML Preview ──────────────────────────────────────────────

    def _show_html_preview(self, html: str):
        try:
            from PyQt6.QtWebEngineWidgets import QWebEngineView
        except ImportError:
            self._preview_label.setText("(PyQt6-WebEngine 未安装，无法预览 HTML)")
            self._preview_label.setStyleSheet("color: #6c7086; font-size: 13px;")
            return
        if self._html_preview is None:
            self._html_preview = QWebEngineView()
            self._preview_container.layout().addWidget(self._html_preview)
            self._preview_label.setVisible(False)
        self._html_preview.setHtml(html)
        self._html_preview.setVisible(True)

    def _hide_html_preview(self):
        if self._html_preview is not None:
            self._html_preview.setVisible(False)
        self._preview_label.setText("响应不是 HTML 格式")
        self._preview_label.setStyleSheet("color: #6c7086; font-size: 13px;")
        self._preview_label.setVisible(True)

    # ── Cookie Jar ────────────────────────────────────────────────

    def _clear_cookie_jar(self):
        self._cookie_jar.clear()
        self._cookie_jar_table.setRowCount(0)
        info("HttpRequestPage: cookie jar cleared")

    # ── Save / Export / Import ────────────────────────────────────

    def _save_to_collection(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "保存到收藏集", "收藏集名称:")
        if ok and name.strip():
            request_history.save_to_collection(name.strip(), {
                "method": self._method_combo.currentText(),
                "url": self._url_input.text().strip(),
                "params": self._get_params_dict(),
                "headers": self._get_headers_dict(),
                "body_config": self._get_body_config(),
                "auth_config": self._get_auth_config(),
                "settings_config": self._get_settings_config(),
            })
            self._refresh_collections_list()
            info(f"HttpRequestPage: saved to collection '{name.strip()}'")

    def _export_request(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出请求", "request.json",
                                               "JSON 文件 (*.json)")
        if not path:
            return
        data = {
            "method": self._method_combo.currentText(),
            "url": self._url_input.text().strip(),
            "params": self._get_params_dict(),
            "headers": self._get_headers_dict(),
            "body_config": self._get_body_config(),
            "auth_config": self._get_auth_config(),
            "settings_config": self._get_settings_config(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json_mod.dump(data, f, ensure_ascii=False, indent=2)
        info(f"HttpRequestPage: exported to {path}")

    def _import_request(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入请求", "",
                                               "JSON 文件 (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json_mod.load(f)
            self._load_request_data(data)
            info(f"HttpRequestPage: imported from {path}")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法解析文件: {e}")

    def _export_collections(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出所有收藏集", "collections.json",
                                               "JSON 文件 (*.json)")
        if not path:
            return
        request_history.export_to_file(path)
        info(f"HttpRequestPage: exported collections to {path}")

    def _import_collections(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入收藏集", "",
                                               "JSON 文件 (*.json)")
        if not path:
            return
        try:
            count = request_history.import_from_file(path)
            self._refresh_collections_list()
            QMessageBox.information(self, "导入成功", f"成功导入 {count} 条请求")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法解析文件: {e}")

    # ── Batch Run ─────────────────────────────────────────────────

    def _batch_run(self):
        from PyQt6.QtWidgets import QInputDialog
        if getattr(self, "_batch_running", False):
            QMessageBox.information(self, "提示", "批量运行正在进行中")
            return
        collections = request_history.collections()
        if not collections:
            QMessageBox.information(self, "提示", "没有可用的收藏集，请先保存请求")
            return
        name, ok = QInputDialog.getItem(self, "批量运行", "选择收藏集:", collections, 0, False)
        if not ok or not name:
            return
        entries = request_history.get_collection(name)
        if not entries:
            QMessageBox.information(self, "提示", "收藏集为空")
            return

        info(f"HttpRequestPage: batch run collection '{name}' ({len(entries)} requests)")

        self._batch_running = True

        progress = QProgressBar(self)
        progress.setRange(0, len(entries))
        progress.setStyleSheet(
            "QProgressBar { background: #181825; border: 1px solid #313244; border-radius: 4px;"
            "  text-align: center; color: #e8e8f0; }"
            "QProgressBar::chunk { background: #89b4fa; border-radius: 3px; }"
        )
        self._status_label.setText(f"批量运行中: {name}  (0/{len(entries)})")
        status_parent = self._status_label.parent()
        if status_parent:
            idx = status_parent.layout().indexOf(self._status_label)
            self._status_label.setVisible(False)
            status_parent.layout().insertWidget(idx, progress)

        self._batch_entries = entries
        self._batch_index = 0
        self._batch_results = []
        self._batch_progress = progress
        self._batch_status_parent = status_parent
        self._batch_worker = None
        self._run_next_batch_request()

    def _run_next_batch_request(self):
        if self._batch_index >= len(self._batch_entries):
            self._finish_batch_run()
            return

        # Clean up previous worker
        if self._batch_worker is not None:
            try:
                self._batch_worker.finished.disconnect()
                self._batch_worker.error.disconnect()
            except Exception:
                pass
            self._batch_worker = None

        entry = self._batch_entries[self._batch_index]
        self._batch_progress.setValue(self._batch_index)
        self._status_label.setText(
            f"批量运行: [{self._batch_index + 1}/{len(self._batch_entries)}] "
            f"{entry.get('method', 'GET')} {entry.get('url', '')[:50]}"
        )
        self._status_label.setStyleSheet("color: #89b4fa; font-size: 12px;")
        self._status_label.setVisible(True)

        # Load request data without mutating body form table (avoid type-change side effects)
        self._method_combo.setCurrentText(entry.get("method", "GET"))
        self._url_input.setText(entry.get("url", ""))
        self._load_kv_table(self._params_table, entry.get("params", {}))
        self._load_kv_table(self._headers_table, entry.get("headers", {}))
        self._load_auth_config(entry.get("auth_config", {}))
        sc = entry.get("settings_config", {})
        self._timeout_spin.setValue(sc.get("timeout", 30))
        self._redirect_cb.setChecked(sc.get("allow_redirects", True))
        self._ssl_cb.setChecked(sc.get("verify_ssl", True))
        self._proxy_input.setText(sc.get("proxy", ""))

        settings_config = entry.get("settings_config", {})
        url = variable_store.resolve(entry.get("url", ""))
        if not url.startswith(("http://", "https://")):
            url = "http://" + url

        self._batch_worker = HttpRequestWorker(
            entry.get("method", "GET"), url,
            entry.get("params", {}), entry.get("headers", {}),
            entry.get("body_config", {"type": "none"}),
            timeout=settings_config.get("timeout", 30),
            allow_redirects=settings_config.get("allow_redirects", True),
            verify_ssl=settings_config.get("verify_ssl", True),
            proxy=settings_config.get("proxy") or None,
        )
        self._batch_worker.finished.connect(self._on_batch_finished)
        self._batch_worker.error.connect(self._on_batch_error)
        self._batch_worker.start()

    def _on_batch_finished(self, result):
        self._batch_results.append({
            "entry": self._batch_entries[self._batch_index],
            "status_code": result.get("status_code", 0),
            "time_ms": result.get("time_ms", 0),
            "size": result.get("size", 0),
            "error": None,
        })
        self._batch_index += 1
        self._run_next_batch_request()

    def _on_batch_error(self, msg):
        self._batch_results.append({
            "entry": self._batch_entries[self._batch_index],
            "status_code": 0,
            "time_ms": 0,
            "size": 0,
            "error": msg,
        })
        self._batch_index += 1
        self._run_next_batch_request()

    def _finish_batch_run(self):
        self._batch_running = False

        # Clean up last worker
        if self._batch_worker is not None:
            try:
                self._batch_worker.finished.disconnect()
                self._batch_worker.error.disconnect()
            except Exception:
                pass
            self._batch_worker = None

        self._batch_progress.setValue(len(self._batch_entries))
        status_parent = self._batch_status_parent
        if status_parent:
            layout = status_parent.layout()
            layout.removeWidget(self._batch_progress)
            self._batch_progress.deleteLater()
            self._batch_progress = None

        total = len(self._batch_results)
        success = sum(1 for r in self._batch_results if r["error"] is None and 200 <= r["status_code"] < 400)
        failed = total - success

        self._status_label.setText(
            f"批量运行完成: {success} 成功, {failed} 失败  (共 {total} 个请求)"
        )
        self._status_label.setStyleSheet(
            f"color: {'#a6e3a1' if failed == 0 else '#f38ba8'}; font-size: 12px;")
        self._status_label.setVisible(True)
        info(f"HttpRequestPage batch run complete: {success}/{total} success")

        # Show summary
        summary_lines = ["批量运行结果:", ""]
        for i, r in enumerate(self._batch_results):
            entry = r["entry"]
            sc = r["status_code"]
            err = r.get("error")
            line = f"  [{i + 1}] {entry.get('method', 'GET')} {entry.get('url', '')[:50]}"
            if err:
                line += f" — 错误: {err}"
            else:
                line += f" — {sc} ({r['time_ms']}ms)"
            summary_lines.append(line)
        QMessageBox.information(self, "批量运行完成", "\n".join(summary_lines[:50]))

    # ── Clear ─────────────────────────────────────────────────────

    def _clear_all(self):
        self._url_input.clear()
        self._method_combo.setCurrentText("GET")
        self._params_table.setRowCount(0)
        self._headers_table.setRowCount(0)
        self._body_form_table.setRowCount(0)
        self._raw_editor.clear()
        self._binary_path_label.setText("未选择文件")
        self._binary_path_label.setStyleSheet(
            "color: #6c7086; font-size: 13px; padding: 6px 10px;"
            "background: #1e1e2e; border: 1px solid #313244; border-radius: 4px;")
        self._body_group.button(0).setChecked(True)
        self._on_body_type_changed(0)
        self._auth_type_combo.setCurrentIndex(0)
        self._api_key_name.clear()
        self._api_key_value.clear()
        self._bearer_token.clear()
        self._basic_user.clear()
        self._basic_pass.clear()
        self._digest_user.clear()
        self._digest_pass.clear()
        self._timeout_spin.setValue(30)
        self._redirect_cb.setChecked(True)
        self._ssl_cb.setChecked(True)
        self._proxy_input.clear()
        self._response_body.clear()
        self._response_status.setText("")
        self._response_time.setText("—")
        self._response_size.setText("—")
        self._resp_headers_table.setRowCount(0)
        self._resp_cookies_table.setRowCount(0)
        self._status_label.setText("就绪 — 输入 URL 并发送请求")
        self._status_label.setStyleSheet("color: #9498a4; font-size: 12px;")
        self._json_status_lbl.setText("")
        self._assertions_widget.clear_results()
        self._hide_html_preview()
        info("HttpRequestPage._clear_all")

    # ── Cleanup ───────────────────────────────────────────────────

    def cleanup(self):
        # 先断开信号再等待：页面正在销毁，此时若工作线程还在跑并发出信号，
        # 回调会打到已析构的控件上。原来的 wait(3000) 对默认 30 秒超时的
        # 请求来说太短，超时后直接返回，线程仍在后台运行。
        for w in (self._worker, getattr(self, "_batch_worker", None)):
            if w is None:
                continue
            try:
                w.finished.disconnect()
                w.error.disconnect()
            except Exception:
                pass

        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            if not self._worker.wait(_CLEANUP_WAIT_MS):
                warn("HttpRequestPage.cleanup: 单个请求线程未按时结束，保留引用等待其自然退出")

        if getattr(self, "_batch_running", False):
            self._batch_running = False
            bw = getattr(self, "_batch_worker", None)
            if bw is not None and getattr(bw, "isRunning", lambda: False)():
                bw.cancel()
                if not bw.wait(_CLEANUP_WAIT_MS):
                    warn("HttpRequestPage.cleanup: 批量请求线程未按时结束，保留引用等待其自然退出")
                self._batch_worker = None

        self._elapsed_timer.stop()
        info("HttpRequestPage.cleanup")
