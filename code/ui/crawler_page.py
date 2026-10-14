import json
import os
import csv
import io
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QSplitter, QListWidget, QListWidgetItem,
    QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox,
    QMessageBox, QFileDialog, QAbstractItemView, QHeaderView,
    QMenu,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QUrl, QObject, pyqtSlot, QPoint, QRect, QTimer,
)
from PyQt6.QtGui import QAction, QCursor

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebChannel import QWebChannel
    WEBENGINE_AVAILABLE = True
except ImportError:
    QWebEngineView = None
    QWebChannel = None
    WEBENGINE_AVAILABLE = False

from ui.styles import MONO_FONT_FAMILY
from core.workers import SiteCheckWorker, CrawlWorker
from utils.settings import settings
from utils.logger import info as log_info, warn as log_warn

# 数据预览表格最多渲染的行数。QTableWidget 每一行都要新建 N 个
# QTableWidgetItem（每个都是独立的 C++ 对象 + Python 包装），长爬取下会
# 让 GUI 线程越来越卡并吃光内存；超出部分只保留在 _all_crawl_data 中。
_MAX_TABLE_ROWS = 2000

# 内存中最多保留的数据行数（导出上限）。超过后丢弃多余行并计数，
# 导出/界面会如实告知"保留了多少、丢弃了多少"，不做静默截断。
_MAX_STORED_ROWS = 100000

# 单次 HTTP 请求的最长超时（秒）。用户可在界面上配置 5-120s，但
# requests 的超时是"整次请求"级别，取消只能在请求返回后才被观察到，
# 因此这里把传给引擎的超时压到 15s，保证"停止"能在十几秒内生效。
_MAX_REQUEST_TIMEOUT = 15

# 回收"退休线程"的轮询间隔（毫秒）。线程对象必须保留到真正结束，
# 否则 Qt 会在 QThread 仍在运行时析构它并直接 abort 进程。
_RETIRE_POLL_MS = 300

INSPECTOR_JS = r"""
(function() {
if (window.__crawlerInspectorActive) return;
window.__crawlerInspectorActive = true;

let _mode = 'select';  // 'select' | 'browse'
let _overlay = null;
let _highlights = [];
let _lastClicked = null;

function createOverlay() {
    const el = document.createElement('div');
    el.id = '__crawler_overlay';
    el.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;z-index:2147483646;pointer-events:none;display:none;';
    document.body.appendChild(el);
    return el;
}

function showHighlight(el, color) {
    const rect = el.getBoundingClientRect();
    const div = document.createElement('div');
    div.className = '__crawler_hl';
    div.style.cssText = 'position:fixed;z-index:2147483645;pointer-events:none;'
        + 'left:' + (rect.left + window.scrollX) + 'px;'
        + 'top:' + (rect.top + window.scrollY) + 'px;'
        + 'width:' + rect.width + 'px;'
        + 'height:' + rect.height + 'px;'
        + 'border:2px solid ' + color + ';'
        + 'background:' + color.replace(')', ',0.15)').replace('rgb', 'rgba') + ';';
    document.body.appendChild(div);
    _highlights.push(div);
}

function clearHighlights() {
    _highlights.forEach(function(h) { h.remove(); });
    _highlights = [];
}

function buildSelector(el) {
    if (el.id) return '#' + el.id;
    var path = [];
    var current = el;
    while (current && current !== document.body && current !== document.documentElement) {
        var tag = current.tagName.toLowerCase();
        var parent = current.parentElement;
        if (current.id) { path.unshift('#' + current.id); break; }
        if (parent) {
            var siblings = Array.from(parent.children).filter(function(c) { return c.tagName === current.tagName; });
            if (siblings.length > 1) {
                var idx = siblings.indexOf(current) + 1;
                tag += ':nth-of-type(' + idx + ')';
            }
        }
        path.unshift(tag);
        current = parent;
    }
    return path.join(' > ');
}

function buildXPath(el) {
    var id = (el.id || '').replace(/\"/g, '&quot;');
    if (el.id) return '//' + el.tagName.toLowerCase() + '[@id=\"' + id + '\"]';
    var parts = [];
    var current = el;
    while (current && current !== document.body && current !== document.documentElement) {
        var tag = current.tagName.toLowerCase();
        var parent = current.parentElement;
        if (current.id) { var cid = current.id.replace(/\"/g, '&quot;'); parts.unshift(tag + '[@id=\"' + cid + '\"]'); break; }
        if (parent) {
            var siblings = Array.from(parent.children).filter(function(c) { return c.tagName === current.tagName; });
            if (siblings.length > 1) {
                var idx = siblings.indexOf(current) + 1;
                parts.unshift(tag + '[' + idx + ']');
            } else {
                parts.unshift(tag);
            }
        } else { parts.unshift(tag); }
        current = parent;
    }
    return '//' + parts.join('/');
}

function getElementInfo(el) {
    return {
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        classes: (el.className && typeof el.className === 'string') ? el.className.trim() : '',
        text: (el.textContent || '').trim().substring(0, 200),
        href: el.href || '',
        src: el.src || '',
        selector: buildSelector(el),
        xpath: buildXPath(el)
    };
}

_overlay = createOverlay();

document.addEventListener('mouseover', function(e) {
    if (_mode !== 'select') return;
    clearHighlights();
    showHighlight(e.target, 'rgb(137, 180, 250)');
}, true);

document.addEventListener('click', function(e) {
    if (_mode !== 'select') return;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    var info = getElementInfo(e.target);
    _lastClicked = info;
    clearHighlights();
    showHighlight(e.target, 'rgb(166, 227, 161)');
    try {
        var matches = document.querySelectorAll(info.selector);
        matches.forEach(function(m) {
            if (m !== e.target) showHighlight(m, 'rgb(249, 226, 175)');
        });
    } catch(_) {}
    try {
        if (window.__crawler_bridge) {
            window.__crawler_bridge.onElementClicked(JSON.stringify(info));
        }
    } catch(_) {}
}, true);

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        _mode = 'browse';
        clearHighlights();
        if (window.__crawler_bridge) {
            window.__crawler_bridge.onEscapePressed();
        }
    }
}, true);

window.__crawlerSetMode = function(mode) {
    _mode = mode;
    if (mode === 'browse') clearHighlights();
};

window.__crawlerClearMarks = function() {
    clearHighlights();
};

window.__crawlerGetCount = function(selector) {
    try {
        return document.querySelectorAll(selector).length;
    } catch(_) { return 0; }
};

})();
"""


class _BridgeObject(QObject):
    """QWebChannel bridge: receives element-click callbacks from injected JS."""
    element_clicked = pyqtSignal(str)
    escape_pressed = pyqtSignal()

    @pyqtSlot(str)
    def onElementClicked(self, info_json: str):
        self.element_clicked.emit(info_json)

    @pyqtSlot()
    def onEscapePressed(self):
        self.escape_pressed.emit()


class CrawlerPage(QWidget):
    crawl_started = pyqtSignal()
    crawl_finished = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._field_defs = []
        self._current_html = ""
        self._current_url = ""
        self._site_check_worker = None
        self._crawl_worker = None
        self._crawl_running = False
        self._bridge = _BridgeObject()
        self._selection_mode = "select"
        self._all_crawl_data = []  # Accumulated rows for export
        self._dropped_rows = 0  # 因内存上限被丢弃的行数（导出时如实报告）
        # 已"退休"但仍可能运行中的线程引用。必须一直持有到 isFinished()，
        # 否则 Python 侧引用计数归零会析构运行中的 QThread（进程 abort）。
        self._retired_workers = []
        self._bottom_height = 200  # Default height of bottom panel
        self._bottom_container = None
        self._resize_handle = None
        self._resizing = False
        self._resize_start_y = 0
        self._resize_start_h = 0

        # 退休线程的回收轮询：只在确实有退休线程时启动
        self._retire_timer = QTimer(self)
        self._retire_timer.setInterval(_RETIRE_POLL_MS)
        self._retire_timer.timeout.connect(self._prune_retired_workers)

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top Toolbar ──
        toolbar = QWidget()
        toolbar.setObjectName("bottomBar")
        toolbar.setFixedHeight(36)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(12, 4, 12, 4)
        tb_layout.setSpacing(6)

        self._url_bar = QLineEdit()
        self._url_bar.setPlaceholderText("输入网址后按回车加载...")
        self._url_bar.returnPressed.connect(self._navigate_to_url)
        tb_layout.addWidget(self._url_bar, 1)

        nav_btn_style = (
            "QPushButton { background: #45475a; color: #e8e8f0; border: none;"
            "  border-radius: 4px; padding: 2px 10px; font-size: 12px; }"
            "QPushButton:hover { background: #585b70; }"
        )

        self._btn_back = QPushButton("◀")
        self._btn_back.setStyleSheet(nav_btn_style)
        self._btn_back.setFixedWidth(32)
        self._btn_back.clicked.connect(lambda: self._browser and self._browser.back())
        tb_layout.addWidget(self._btn_back)

        self._btn_fwd = QPushButton("▶")
        self._btn_fwd.setStyleSheet(nav_btn_style)
        self._btn_fwd.setFixedWidth(32)
        self._btn_fwd.clicked.connect(lambda: self._browser and self._browser.forward())
        tb_layout.addWidget(self._btn_fwd)

        self._btn_refresh = QPushButton("⟳")
        self._btn_refresh.setStyleSheet(nav_btn_style)
        self._btn_refresh.setFixedWidth(32)
        self._btn_refresh.clicked.connect(lambda: self._browser and self._browser.reload())
        tb_layout.addWidget(self._btn_refresh)

        ac_style = (
            "QPushButton { background: #89b4fa; color: #1e1e2e; font-weight: 600;"
            "  border: none; border-radius: 4px; padding: 2px 12px; font-size: 12px; }"
            "QPushButton:hover { background: #b4d0fb; }"
            "QPushButton:disabled { background: #45475a; color: #9498a4; }"
        )
        stop_style = (
            "QPushButton { background: #f38ba8; color: #1e1e2e; font-weight: 600;"
            "  border: none; border-radius: 4px; padding: 2px 12px; font-size: 12px; }"
            "QPushButton:hover { background: #eba0ac; }"
            "QPushButton:disabled { background: #45475a; color: #9498a4; }"
        )
        dim_style = (
            "QPushButton { background: #45475a; color: #c8ccd4;"
            "  border: none; border-radius: 4px; padding: 2px 12px; font-size: 12px; }"
            "QPushButton:hover { background: #585b70; }"
        )

        self._btn_check = QPushButton("检测站点")
        self._btn_check.setStyleSheet(dim_style)
        self._btn_check.clicked.connect(self._on_check_site)
        tb_layout.addWidget(self._btn_check)

        self._btn_save = QPushButton("保存配置")
        self._btn_save.setStyleSheet(dim_style)
        self._btn_save.clicked.connect(self._on_save_config)
        tb_layout.addWidget(self._btn_save)

        self._btn_load = QPushButton("加载配置")
        self._btn_load.setStyleSheet(dim_style)
        self._btn_load.clicked.connect(self._on_load_config)
        tb_layout.addWidget(self._btn_load)

        self._btn_preview = QPushButton("预览首页")
        self._btn_preview.setStyleSheet(ac_style)
        self._btn_preview.clicked.connect(lambda: self._start_crawl(debug=True))
        tb_layout.addWidget(self._btn_preview)

        self._btn_run = QPushButton("开始爬取")
        self._btn_run.setStyleSheet(ac_style)
        self._btn_run.clicked.connect(lambda: self._start_crawl(debug=False))
        tb_layout.addWidget(self._btn_run)

        self._btn_stop = QPushButton("停止")
        self._btn_stop.setStyleSheet(stop_style)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop_crawl)
        tb_layout.addWidget(self._btn_stop)

        # Export button
        self._btn_export = QPushButton("导出 ▼")
        self._btn_export.setStyleSheet(dim_style)
        self._btn_export.setToolTip("导出数据为 CSV 或 JSON")
        self._btn_export.clicked.connect(self._on_export_menu)
        tb_layout.addWidget(self._btn_export)

        root.addWidget(toolbar)

        # ── Main body: left tools | center browser | right config ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        # Left tool panel
        left_panel = QWidget()
        left_panel.setFixedWidth(48)
        left_panel.setObjectName("settingsPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 8, 4, 8)
        left_layout.setSpacing(6)

        tool_style = (
            "QPushButton { background: transparent; color: #c8ccd4; border: 1px solid #45475a;"
            "  border-radius: 4px; padding: 6px 4px; font-size: 11px; }"
            "QPushButton:checked { background: #89b4fa; color: #1e1e2e; border-color: #89b4fa; }"
            "QPushButton:hover { border-color: #89b4fa; }"
        )

        self._btn_select = QPushButton("🖐\n选择")
        self._btn_select.setCheckable(True)
        self._btn_select.setChecked(True)
        self._btn_select.setStyleSheet(tool_style)
        self._btn_select.setToolTip(
            "点击\"选择\" → 在页面中点击列表项容器 → 切换到\"②字段规则\"添加字段\n"
            "按 Esc 退出选择模式"
        )
        self._btn_select.clicked.connect(lambda: self._set_inspector_mode("select"))
        left_layout.addWidget(self._btn_select)

        self._btn_browse = QPushButton("✋\n浏览")
        self._btn_browse.setCheckable(True)
        self._btn_browse.setStyleSheet(tool_style)
        self._btn_browse.setToolTip("正常浏览网页，不会触发选择器")
        self._btn_browse.clicked.connect(lambda: self._set_inspector_mode("browse"))
        left_layout.addWidget(self._btn_browse)

        self._btn_clear = QPushButton("✕\n清除")
        self._btn_clear.setStyleSheet(tool_style)
        self._btn_clear.setToolTip("清除页面上的高亮标记")
        self._btn_clear.clicked.connect(self._clear_highlights)
        left_layout.addWidget(self._btn_clear)

        left_layout.addStretch()
        splitter.addWidget(left_panel)

        # Center: QWebEngineView (or fallback if unavailable)
        if WEBENGINE_AVAILABLE:
            self._browser = QWebEngineView()
            self._browser.setMinimumWidth(400)
            self._browser.urlChanged.connect(self._on_url_changed)
            self._browser.loadFinished.connect(self._on_load_finished)

            channel = QWebChannel()
            channel.registerObject("__crawler_bridge", self._bridge)
            self._browser.page().setWebChannel(channel)
            self._browser.setUrl(QUrl("about:blank"))
            splitter.addWidget(self._browser)
        else:
            self._browser = None
            fallback = QWidget()
            fallback.setMinimumWidth(400)
            fl = QVBoxLayout(fallback)
            fl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_lbl = QLabel("🌐")
            icon_lbl.setStyleSheet("font-size: 48px; color: #585b70;")
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fl.addWidget(icon_lbl)
            msg = QLabel(
                "网页爬虫预览需要 QtWebEngine 组件\n\n"
                "您可以继续使用爬虫功能（输入URL、配置规则、运行爬取），\n"
                "但浏览器预览和可视化元素选择暂不可用。\n\n"
                "如需完整功能，请安装 PyQt6-WebEngine：\n"
                "pip install PyQt6-WebEngine"
            )
            msg.setWordWrap(True)
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setStyleSheet("color: #9498a4; font-size: 13px; line-height: 1.6;")
            fl.addWidget(msg)
            self._webengine_fallback = fallback
            splitter.addWidget(fallback)

        # Right config panel — step-based tabs
        right_panel = QWidget()
        right_panel.setMinimumWidth(280)
        right_panel.setMaximumWidth(450)
        right_panel.setObjectName("settingsPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)

        right_tabs = QTabWidget()
        right_tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #45475a; border-radius: 4px; }"
            "QTabBar::tab { padding: 4px 10px; background: #252536; color: #c8ccd4;"
            "  border: 1px solid #45475a; border-bottom: none; font-size: 11px; }"
            "QTabBar::tab:selected { background: #313244; color: #e8e8f0; }"
        )
        right_tabs.addTab(self._make_container_tab(), "① 列表容器")
        right_tabs.addTab(self._make_fields_tab(), "② 字段规则")
        right_tabs.addTab(self._make_flow_tab(), "③ 翻页设置")
        right_tabs.addTab(self._make_options_tab(), "④ 反爬选项")
        right_layout.addWidget(right_tabs)
        splitter.addWidget(right_panel)

        splitter.setSizes([48, 600, 340])
        root.addWidget(splitter, 1)

        # Disable browser-dependent controls if WebEngine unavailable
        if not WEBENGINE_AVAILABLE:
            for btn in [self._btn_select, self._btn_browse, self._btn_clear,
                        self._btn_back, self._btn_fwd, self._btn_refresh]:
                btn.setEnabled(False)
                btn.setToolTip("需要安装 QtWebEngine 组件")

        # ── Bottom panel (floating overlay with draggable top edge) ──
        self._bottom_container = QWidget(self)
        self._bottom_container.setObjectName("bottomPanel")
        self._bottom_container.setStyleSheet(
            "#bottomPanel { background: #1e1e2e; border-top: 1px solid #45475a; }"
        )

        # Drag handle bar at the top of the bottom panel
        self._resize_handle = QWidget(self._bottom_container)
        self._resize_handle.setFixedHeight(5)
        self._resize_handle.setCursor(QCursor(Qt.CursorShape.SplitVCursor))
        self._resize_handle.setStyleSheet(
            "background: #45475a; border-top-left-radius: 2px; border-top-right-radius: 2px;"
        )
        self._resize_handle.setToolTip("拖拽调整面板高度")
        self._resize_handle.mousePressEvent = self._on_resize_handle_press
        self._resize_handle.mouseMoveEvent = self._on_resize_handle_move
        self._resize_handle.mouseReleaseEvent = self._on_resize_handle_release

        bottom_layout = QVBoxLayout(self._bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)
        bottom_layout.addWidget(self._resize_handle)
        self._bottom_tabs = QTabWidget()
        bottom_layout.addWidget(self._bottom_tabs)
        self._bottom_tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #45475a; border-radius: 4px; }"
            "QTabBar::tab { padding: 3px 12px; background: #1e1e2e; color: #9498a4;"
            "  border: 1px solid #45475a; border-bottom: none; font-size: 11px; }"
            "QTabBar::tab:selected { background: #252536; color: #e8e8f0; }"
        )

        self._setup_log_tab()
        self._setup_data_tab()
        self._setup_report_tab()

        self._bottom_container.raise_()

    # ── Right Panel Tabs ──

    def _make_container_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel("第1步：定位页面中的列表项容器\n"
                      "点击左侧面板的\"选择\"按钮，然后在页面中点击一个列表项（如商品卡片）")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #89b4fa; font-size: 11px; padding: 4px 6px;"
                          "background: #1e1e2e; border-radius: 4px;")
        layout.addWidget(hint)

        layout.addWidget(QLabel("列表容器 CSS 选择器:"))
        self._container_sel_edit = QLineEdit()
        self._container_sel_edit.setPlaceholderText(
            "如: .product-item, li.result, div[class*='card']")
        self._container_sel_edit.textChanged.connect(self._on_container_changed)
        layout.addWidget(self._container_sel_edit)

        self._container_count_lbl = QLabel("")
        self._container_count_lbl.setStyleSheet("color: #a6e3a1; font-size: 11px;")
        layout.addWidget(self._container_count_lbl)

        btn_row = QHBoxLayout()
        self._btn_set_container = QPushButton("设为容器")
        self._btn_set_container.setObjectName("actionBtn")
        self._btn_set_container.setToolTip("将当前点击的元素选择器设为列表容器")
        self._btn_set_container.clicked.connect(self._on_set_container)
        btn_row.addWidget(self._btn_set_container)

        self._btn_clear_container = QPushButton("清除")
        self._btn_clear_container.setObjectName("dangerBtn")
        self._btn_clear_container.clicked.connect(self._on_clear_container)
        btn_row.addWidget(self._btn_clear_container)
        layout.addLayout(btn_row)

        self._last_clicked_sel = ""
        sel_hint = QLabel("提示：在\"选择\"模式下点页面元素后，点击\"设为容器\"设置容器选择器，\n"
                         "然后切换到\"② 字段规则\"继续添加子字段。")
        sel_hint.setWordWrap(True)
        sel_hint.setStyleSheet("color: #9498a4; font-size: 10px;")
        layout.addWidget(sel_hint)

        layout.addStretch()
        return w

    def _make_fields_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Show current container
        container_row = QHBoxLayout()
        container_row.addWidget(QLabel("容器:"))
        self._field_container_lbl = QLabel("(未设置)")
        self._field_container_lbl.setStyleSheet(
            "color: #89b4fa; font-size: 10px; font-family: monospace;")
        self._field_container_lbl.setWordWrap(True)
        container_row.addWidget(self._field_container_lbl, 1)
        layout.addLayout(container_row)

        layout.addWidget(QLabel("已定义字段:"))
        self._field_list = QListWidget()
        self._field_list.setAlternatingRowColors(True)
        self._field_list.setMaximumHeight(110)
        self._field_list.itemClicked.connect(self._on_field_selected)
        layout.addWidget(self._field_list)

        layout.addWidget(QLabel("字段名称:"))
        self._field_name_edit = QLineEdit()
        self._field_name_edit.setPlaceholderText("如: 商品标题")
        layout.addWidget(self._field_name_edit)

        layout.addWidget(QLabel("子选择器 (相对容器):"))
        self._field_sel_edit = QLineEdit()
        self._field_sel_edit.setPlaceholderText(
            "相对容器的选择器，如 .title, .price, a")
        layout.addWidget(self._field_sel_edit)

        type_row = QHBoxLayout()
        self._field_type_cb = QComboBox()
        self._field_type_cb.addItem("CSS 选择器", "css")
        self._field_type_cb.addItem("XPath", "xpath")
        self._field_type_cb.setMaximumWidth(140)
        type_row.addWidget(self._field_type_cb)
        type_row.addStretch()
        layout.addLayout(type_row)

        # Attribute extraction
        attr_row = QHBoxLayout()
        attr_row.addWidget(QLabel("提取属性 (可选):"))
        self._field_attr_edit = QLineEdit()
        self._field_attr_edit.setPlaceholderText("如: href, src, data-id")
        self._field_attr_edit.setMaximumWidth(140)
        attr_row.addWidget(self._field_attr_edit)
        attr_row.addStretch()
        layout.addLayout(attr_row)

        btn_row = QHBoxLayout()
        self._btn_add_field = QPushButton("添加字段")
        self._btn_add_field.setObjectName("actionBtn")
        self._btn_add_field.clicked.connect(self._on_add_field)
        btn_row.addWidget(self._btn_add_field)

        self._btn_del_field = QPushButton("删除")
        self._btn_del_field.setObjectName("dangerBtn")
        self._btn_del_field.clicked.connect(self._on_del_field)
        btn_row.addWidget(self._btn_del_field)
        layout.addLayout(btn_row)

        # Live preview
        layout.addWidget(QLabel("即时预览 (当前页面第1个容器):"))
        self._preview_table = QTableWidget(0, 2)
        self._preview_table.setHorizontalHeaderLabels(["字段", "预览值"])
        self._preview_table.horizontalHeader().setStretchLastSection(True)
        self._preview_table.setMaximumHeight(100)
        self._preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._preview_table)

        return w

    def _make_flow_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(QLabel("翻页策略:"))

        self._pag_strategy_cb = QComboBox()
        self._pag_strategy_cb.addItem("下一页链接 (CSS选择器)", "link")
        self._pag_strategy_cb.addItem("URL 模式 (如 /page/{page})", "pattern")
        self._pag_strategy_cb.currentIndexChanged.connect(self._on_pag_strategy_changed)
        layout.addWidget(self._pag_strategy_cb)

        self._pag_link_w = QWidget()
        pag_link_layout = QVBoxLayout(self._pag_link_w)
        pag_link_layout.setContentsMargins(0, 0, 0, 0)
        pag_link_layout.setSpacing(4)
        pag_link_layout.addWidget(QLabel("下一页选择器:"))
        self._pag_sel_edit = QLineEdit()
        self._pag_sel_edit.setPlaceholderText("如: a.next, .pagination .next")
        pag_link_layout.addWidget(self._pag_sel_edit)
        layout.addWidget(self._pag_link_w)

        self._pag_pattern_w = QWidget()
        pag_pattern_layout = QVBoxLayout(self._pag_pattern_w)
        pag_pattern_layout.setContentsMargins(0, 0, 0, 0)
        pag_pattern_layout.setSpacing(4)
        pag_pattern_layout.addWidget(QLabel("URL 模式:"))

        pattern_row = QHBoxLayout()
        self._pag_pattern_edit = QLineEdit()
        self._pag_pattern_edit.setPlaceholderText(
            "如: https://example.com/page/{page}")
        pattern_row.addWidget(self._pag_pattern_edit, 1)
        pag_pattern_layout.addLayout(pattern_row)

        pattern_hint = QLabel("{page} 将被替换为页码 (1, 2, 3...)\n{p} 也可使用")
        pattern_hint.setStyleSheet("color: #9498a4; font-size: 10px;")
        pattern_hint.setWordWrap(True)
        pag_pattern_layout.addWidget(pattern_hint)
        self._pag_pattern_w.hide()
        layout.addWidget(self._pag_pattern_w)

        layout.addWidget(QLabel("最大页数:"))
        self._pag_max_spin = QSpinBox()
        self._pag_max_spin.setRange(1, 1000)
        self._pag_max_spin.setValue(1)
        layout.addWidget(self._pag_max_spin)

        layout.addWidget(QLabel("页面等待 (秒):"))
        self._pag_wait_spin = QDoubleSpinBox()
        self._pag_wait_spin.setRange(0, 30)
        self._pag_wait_spin.setValue(0.5)
        self._pag_wait_spin.setSingleStep(0.1)
        layout.addWidget(self._pag_wait_spin)

        layout.addStretch()
        return w

    def _make_options_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(QLabel("请求间隔 (秒):"))
        self._opt_delay_spin = QDoubleSpinBox()
        self._opt_delay_spin.setRange(0.5, 30)
        self._opt_delay_spin.setValue(settings.get("crawler_default_delay"))
        self._opt_delay_spin.setSingleStep(0.5)
        layout.addWidget(self._opt_delay_spin)

        layout.addWidget(QLabel("超时 (秒):"))
        self._opt_timeout_spin = QSpinBox()
        self._opt_timeout_spin.setRange(5, 120)
        self._opt_timeout_spin.setValue(settings.get("crawler_default_timeout"))
        layout.addWidget(self._opt_timeout_spin)

        layout.addWidget(QLabel("最大重试次数:"))
        self._opt_retry_spin = QSpinBox()
        self._opt_retry_spin.setRange(0, 10)
        self._opt_retry_spin.setValue(settings.get("crawler_max_retries"))
        layout.addWidget(self._opt_retry_spin)

        layout.addWidget(QLabel("User-Agent:"))
        self._opt_ua_edit = QLineEdit()
        self._opt_ua_edit.setText(settings.get("crawler_default_ua"))
        self._opt_ua_edit.setPlaceholderText("留空使用默认")
        layout.addWidget(self._opt_ua_edit)

        layout.addWidget(QLabel("代理地址:"))
        self._opt_proxy_edit = QLineEdit()
        self._opt_proxy_edit.setPlaceholderText("如: http://127.0.0.1:7890")
        layout.addWidget(self._opt_proxy_edit)

        layout.addStretch()
        return w

    # ── Bottom Panel Tabs ──

    def _setup_log_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 4, 8, 4)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet(
            f"font-size: 11px; font-family: {MONO_FONT_FAMILY};"
            " background-color: #11111b; color: #cdd6f4;"
        )
        layout.addWidget(self._log_view)
        self._bottom_tabs.addTab(w, "实时日志")

    def _setup_data_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 4, 8, 4)

        # Export toolbar
        export_row = QHBoxLayout()
        self._data_count_lbl = QLabel("共 0 条")
        self._data_count_lbl.setStyleSheet("color: #9498a4; font-size: 11px;")
        export_row.addWidget(self._data_count_lbl)
        export_row.addStretch()

        self._btn_export_csv = QPushButton("导出 CSV")
        self._btn_export_csv.setFixedWidth(80)
        self._btn_export_csv.setStyleSheet(
            "QPushButton { background: #45475a; color: #c8ccd4; border: none;"
            "  border-radius: 4px; padding: 2px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #585b70; }")
        self._btn_export_csv.clicked.connect(lambda: self._on_export_data("csv"))
        export_row.addWidget(self._btn_export_csv)

        self._btn_export_json = QPushButton("导出 JSON")
        self._btn_export_json.setFixedWidth(80)
        self._btn_export_json.setStyleSheet(
            "QPushButton { background: #45475a; color: #c8ccd4; border: none;"
            "  border-radius: 4px; padding: 2px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #585b70; }")
        self._btn_export_json.clicked.connect(lambda: self._on_export_data("json"))
        export_row.addWidget(self._btn_export_json)
        layout.addLayout(export_row)

        self._data_table = QTableWidget(0, 2)
        self._data_table.setHorizontalHeaderLabels(["#", "数据"])
        self._data_table.horizontalHeader().setStretchLastSection(True)
        self._data_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._data_table.setAlternatingRowColors(True)
        layout.addWidget(self._data_table, 1)
        self._bottom_tabs.addTab(w, "数据预览")

    def _setup_report_tab(self):
        self._report_view = QTextEdit()
        self._report_view.setReadOnly(True)
        self._report_view.setStyleSheet(
            f"font-size: 11px; font-family: {MONO_FONT_FAMILY};"
            " background-color: #11111b; color: #cdd6f4;"
        )
        self._bottom_tabs.addTab(self._report_view, "站点检测")

    # ── Signal Connections ──

    def _connect_signals(self):
        self._bridge.element_clicked.connect(self._on_element_clicked)
        self._bridge.escape_pressed.connect(self._on_escape_pressed)

    # ── 后台线程生命周期管理 ──

    def _hold_worker(self, worker):
        """保留 worker 的引用直到线程真正结束（非阻塞）。

        QThread 的 finished 信号是在 run() 返回**之前**发出的，
        此时线程可能还没退出。若在终槽里直接丢掉最后一个引用，
        Python 会析构这个仍在运行的 QThread，Qt 打印
        "QThread: Destroyed while thread is still running" 并 abort。
        """
        if worker is None:
            return
        if worker not in self._retired_workers:
            self._retired_workers.append(worker)
        if not self._retire_timer.isActive():
            self._retire_timer.start()

    def _disconnect_worker(self, worker):
        """断开 worker 到本页面的全部信号，防止旧任务继续改写新任务的状态。"""
        for name in ("log", "progress", "data_row", "finished", "error", "status"):
            sig = getattr(worker, name, None)
            if sig is None:
                continue
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass

    def _retire_worker(self, worker):
        """让旧 worker "退休"：断开信号 + 请求取消，然后不再管它。

        全程不阻塞 GUI 线程。注意 disconnect() **不会**撤销已经排队的
        信号事件，所以各终槽还必须用 ``self.sender()`` 守卫兜底。
        """
        if worker is None:
            return
        self._disconnect_worker(worker)
        try:
            worker.cancel()
        except Exception as e:
            log_warn(f"取消后台线程失败: {e}")
        self._hold_worker(worker)

    def _prune_retired_workers(self):
        """回收已经结束的退休线程（QTimer 轮询，不阻塞 GUI）。"""
        alive = []
        for w in self._retired_workers:
            try:
                if w.isFinished():
                    w.deleteLater()
                else:
                    alive.append(w)
            except RuntimeError:
                pass  # 底层 C++ 对象已销毁
        self._retired_workers = alive
        if not alive:
            self._retire_timer.stop()

    # ── Browser Interaction ──

    def _navigate_to_url(self):
        url = self._url_bar.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self._current_url = url
        self._url_bar.setText(url)
        if WEBENGINE_AVAILABLE and self._browser is not None:
            self._browser.setUrl(QUrl(url))

    def _on_url_changed(self, url: QUrl):
        url_str = url.toString()
        if url_str and url_str != "about:blank":
            self._url_bar.setText(url_str)
            self._current_url = url_str

    def _on_load_finished(self, ok: bool):
        if not WEBENGINE_AVAILABLE:
            return
        if not ok:
            self._log("⚠ 页面加载失败")
            return
        self._browser.page().runJavaScript(INSPECTOR_JS)
        self._log("页面加载完成，选择器注入就绪")
        self._browser.page().toHtml(self._on_html_received)

    def _on_html_received(self, html: str):
        self._current_html = html
        self._update_field_preview()
        self._update_container_count()

    def _set_inspector_mode(self, mode: str):
        self._selection_mode = mode
        self._btn_select.setChecked(mode == "select")
        self._btn_browse.setChecked(mode == "browse")
        if not WEBENGINE_AVAILABLE:
            return
        js_mode = "select" if mode == "select" else "browse"
        self._browser.page().runJavaScript(
            f"window.__crawlerSetMode('{js_mode}');")

    def _clear_highlights(self):
        if not WEBENGINE_AVAILABLE:
            return
        self._browser.page().runJavaScript("window.__crawlerClearMarks();")

    # ── Element Selection Callback ──

    def _on_element_clicked(self, info_json: str):
        try:
            info = json.loads(info_json)
        except json.JSONDecodeError:
            return

        selector = info.get("selector", "")
        xpath = info.get("xpath", "")
        tag = info.get("tag", "")
        text = info.get("text", "")

        sel_type = self._field_type_cb.currentData()
        if sel_type == "xpath":
            display_sel = xpath
        else:
            display_sel = selector

        # Always store the CSS selector for container detection
        self._last_clicked_sel = selector

        # Check which tab we're on — if container tab active, suggest container
        self._field_sel_edit.setText(display_sel)

        tag_hint = f"{tag}"
        if text:
            tag_hint += f" — {text[:50]}"
        self._field_name_edit.setPlaceholderText(tag_hint)

        self._log(f"[选择] {tag}: {display_sel}")
        # Update container count after selection
        self._update_container_count()

    def _on_escape_pressed(self):
        self._btn_select.setChecked(False)
        self._btn_browse.setChecked(True)
        self._selection_mode = "browse"
        self._log("[模式] 已退出选择模式")

    def _on_set_container(self):
        sel = self._last_clicked_sel or self._field_sel_edit.text().strip()
        if not sel:
            QMessageBox.warning(self, "提示", "请先在页面中点击一个列表项元素")
            return
        self._container_sel_edit.setText(sel)
        self._field_container_lbl.setText(sel)
        self._log(f"[容器] 已设置: {sel}")
        self._update_container_count()
        self._update_field_preview()

    def _on_clear_container(self):
        self._container_sel_edit.clear()
        self._field_container_lbl.setText("(未设置)")
        self._log("[容器] 已清除")
        self._update_field_preview()

    def _on_container_changed(self):
        self._field_container_lbl.setText(
            self._container_sel_edit.text() or "(未设置)")
        self._update_container_count()
        self._update_field_preview()

    def _update_container_count(self):
        sel = self._container_sel_edit.text().strip()
        if not sel or not self._current_html:
            self._container_count_lbl.setText("")
            return
        try:
            from core.crawler.extractor import DataExtractor
            count = DataExtractor.validate_container(self._current_html, sel)
            if count > 0:
                self._container_count_lbl.setText(f"✓ 当前页面匹配到 {count} 个容器 → {count} 条数据")
            else:
                self._container_count_lbl.setText("⚠ 当前页面未匹配到任何容器")
        except Exception:
            self._container_count_lbl.setText("")

        # Also query live count from browser if available
        if WEBENGINE_AVAILABLE and self._browser:
            self._browser.page().runJavaScript(
                f"window.__crawlerGetCount('{sel}');",
                lambda n: self._container_count_lbl.setText(
                    f"✓ 当前页面匹配到 {n} 个容器 → {n} 条数据") if n > 0
                    else self._container_count_lbl.setText("⚠ 未匹配到容器"))

    def _on_pag_strategy_changed(self):
        strategy = self._pag_strategy_cb.currentData()
        self._pag_link_w.setVisible(strategy == "link")
        self._pag_pattern_w.setVisible(strategy == "pattern")

    # ── Field Management ──

    def _on_add_field(self):
        name = self._field_name_edit.text().strip()
        selector = self._field_sel_edit.text().strip()
        sel_type = self._field_type_cb.currentData()
        attr = self._field_attr_edit.text().strip()

        if not name or not selector:
            QMessageBox.warning(self, "提示", "请填写字段名称和选择器")
            return

        field = {
            "name": name,
            "selector": selector,
            "type": sel_type,
        }
        if attr:
            field["attr"] = attr

        self._field_defs.append(field)

        attr_hint = f" [{attr}]" if attr else ""
        type_hint = sel_type.upper()
        item_text = f"{name}{attr_hint}  [{type_hint}]  {selector[:35]}{'...' if len(selector) > 35 else ''}"
        item = QListWidgetItem(item_text)
        item.setData(Qt.ItemDataRole.UserRole, field)
        self._field_list.addItem(item)

        self._field_name_edit.clear()
        self._field_sel_edit.clear()
        self._field_attr_edit.clear()
        self._field_name_edit.setPlaceholderText("")
        self._update_field_preview()

    def _on_del_field(self):
        row = self._field_list.currentRow()
        if row >= 0:
            self._field_list.takeItem(row)
            del self._field_defs[row]
            self._update_field_preview()

    def _on_field_selected(self, item: QListWidgetItem):
        field = item.data(Qt.ItemDataRole.UserRole)
        if field:
            self._field_name_edit.setText(field.get("name", ""))
            self._field_sel_edit.setText(field.get("selector", ""))
            idx = self._field_type_cb.findData(field.get("type", "css"))
            if idx >= 0:
                self._field_type_cb.setCurrentIndex(idx)
            self._field_attr_edit.setText(field.get("attr", ""))

    def _update_field_preview(self):
        self._preview_table.setRowCount(0)
        if not self._current_html or not self._field_defs:
            return

        from core.crawler.extractor import DataExtractor
        container = self._container_sel_edit.text().strip() or None

        for field in self._field_defs:
            vals = DataExtractor.validate_selector(
                self._current_html, field["selector"],
                field.get("type", "css"), container)
            row = self._preview_table.rowCount()
            self._preview_table.insertRow(row)
            self._preview_table.setItem(row, 0, QTableWidgetItem(field["name"]))
            self._preview_table.setItem(row, 1, QTableWidgetItem(
                ", ".join(vals[:3]) if vals else "(无匹配)"))

    # ── Site Check ──

    def _on_check_site(self):
        url = self._url_bar.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先输入网址")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        self._log(f"[检测] 正在分析站点: {url}")
        # 按钮保持禁用直到本轮检测的终止信号到达：双击不会起两个检测线程
        self._btn_check.setEnabled(False)
        self._report_view.clear()

        # 旧检测线程退休（不阻塞 GUI 线程）；它迟到的报告会被 sender 守卫丢弃
        if self._site_check_worker is not None:
            self._retire_worker(self._site_check_worker)
            self._site_check_worker = None

        self._site_check_worker = SiteCheckWorker(url)
        self._site_check_worker.finished.connect(self._on_site_check_done)
        self._site_check_worker.error.connect(self._on_site_check_error)
        self._site_check_worker.start()

    def _on_site_check_done(self, report: dict):
        # 迟到的旧检测报告绝不能覆盖新一轮检测的结果（也不能提前解锁按钮）
        if self.sender() is not None and self.sender() is not self._site_check_worker:
            log_info("忽略迟到的站点检测报告（来自已退休的检测线程）")
            return
        self._hold_worker(self._site_check_worker)
        self._site_check_worker = None
        # 爬取进行中时检测按钮保持禁用（_set_running 负责最终状态）
        self._btn_check.setEnabled(not self._crawl_running)
        lines = [
            "══════ 站点可行性检测报告 ══════",
            f"URL: {report.get('url', '')}",
            f"HTTP 状态码: {report.get('status_code', '?')}",
            "",
            f"robots.txt 允许: {'✓ 是' if report.get('robots_ok') else '✗ 否'}",
            f"需要 JS 渲染: {'⚠ 是' if report.get('needs_js') else '✓ 否'}",
            f"检测到验证码: {'✗ 是' if report.get('has_captcha') else '✓ 否'}",
            f"需要登录: {'⚠ 是' if report.get('has_login') else '✓ 否'}",
            f"CMS/框架: {report.get('cms') or '(未检测到)'}",
            f"SSL 有效: {'✓ 是' if report.get('ssl_valid') else '⚠ 否'}",
        ]
        signs = report.get("anti_crawl_signs", [])
        if signs:
            lines.append(f"\n反爬迹象: {', '.join(signs)}")
        else:
            lines.append("\n反爬迹象: 未检测到")
        rob = report.get("robots_details", {})
        if rob:
            lines.append(f"\nrobots.txt 爬取延迟: {rob.get('crawl_delay') or '未指定'}")
        self._report_view.setText("\n".join(lines))
        self._log("[检测] 站点检测完成")
        self._bottom_tabs.setCurrentIndex(2)

    def _on_site_check_error(self, msg: str):
        # 同 _on_site_check_done：旧检测线程的错误不能打断新一轮检测
        if self.sender() is not None and self.sender() is not self._site_check_worker:
            log_info("忽略迟到的站点检测错误（来自已退休的检测线程）")
            return
        self._hold_worker(self._site_check_worker)
        self._site_check_worker = None
        self._btn_check.setEnabled(not self._crawl_running)
        self._report_view.setText(f"检测失败: {msg}")
        self._log(f"[检测错误] {msg}")
        self._bottom_tabs.setCurrentIndex(2)

    # ── Crawl Execution ──

    def _start_crawl(self, debug: bool = False):
        url = self._current_url or self._url_bar.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先加载目标网址")
            return
        if not self._field_defs:
            QMessageBox.warning(self, "提示", "请至少添加一个提取字段")
            return

        # 旧任务退休：断开信号 + 请求取消，但绝不阻塞 GUI 线程。
        # 已经排队、无法撤回的旧信号由各终槽的 sender 守卫丢弃。
        if self._crawl_worker is not None:
            log_info("启动新爬取任务，旧任务转入退休状态")
            self._retire_worker(self._crawl_worker)
            self._crawl_worker = None

        max_pages = 1 if debug else self._pag_max_spin.value()
        container = self._container_sel_edit.text().strip()
        strategy = self._pag_strategy_cb.currentData()

        pagination = {
            "selector": self._pag_sel_edit.text().strip(),
            "max_pages": max_pages,
            "wait": self._pag_wait_spin.value(),
            "strategy": strategy,
        }
        if strategy == "pattern":
            pagination["url_pattern"] = self._pag_pattern_edit.text().strip()

        # 请求超时压缩：用户可配 5-120s，但取消只能在请求返回后才被观察到。
        # 这里把实际传给引擎的超时封顶到 _MAX_REQUEST_TIMEOUT，保证"停止"
        # 能在十几秒内生效（引擎侧仍是标量超时，见 _MAX_REQUEST_TIMEOUT 注释）。
        configured_timeout = int(self._opt_timeout_spin.value())
        effective_timeout = min(configured_timeout, _MAX_REQUEST_TIMEOUT)
        if effective_timeout != configured_timeout:
            self._log(f"[提示] 请求超时已由 {configured_timeout}s 压缩为 "
                      f"{effective_timeout}s，以保证“停止”能及时生效")

        config = {
            "start_url": url,
            "fields": self._field_defs,
            "pagination": pagination,
            "options": {
                "delay": self._opt_delay_spin.value(),
                "timeout": effective_timeout,
                "max_retries": self._opt_retry_spin.value(),
                "proxy": self._opt_proxy_edit.text().strip(),
                "headers": {},
                "container_selector": container,
            },
        }

        if self._opt_ua_edit.text().strip():
            config["options"]["headers"]["User-Agent"] = self._opt_ua_edit.text().strip()

        self._data_table.setRowCount(0)
        self._all_crawl_data = []
        self._dropped_rows = 0
        col_count = len(self._field_defs) + 1
        self._data_table.setColumnCount(col_count)
        headers = ["#"] + [f["name"] for f in self._field_defs]
        self._data_table.setHorizontalHeaderLabels(headers)
        self._data_table.horizontalHeader().setStretchLastSection(True)
        self._update_data_count()

        self._set_running(True)
        strategy_names = {"link": "链接翻页", "pattern": "URL模式翻页", "scroll": "URL模式翻页"}
        strat_name = strategy_names.get(strategy, "链接翻页")
        self._log(f"[{'预览' if debug else '正式爬取'}] "
                  f"{'容器模式' if container else '全局模式'}, "
                  f"最多 {max_pages} 页, {len(self._field_defs)} 个字段, "
                  f"{strat_name}")

        self._crawl_worker = CrawlWorker(config)
        self._crawl_worker.log.connect(self._on_worker_log)
        self._crawl_worker.progress.connect(self._on_crawl_progress)
        self._crawl_worker.data_row.connect(self._on_data_row)
        self._crawl_worker.finished.connect(self._on_crawl_done)
        self._crawl_worker.error.connect(self._on_crawl_error)
        self._crawl_worker.start()
        self.crawl_started.emit()

    def _on_stop_crawl(self):
        if self._crawl_worker and self._crawl_worker.isRunning():
            self._crawl_worker.cancel()
            self._log("[停止] 已发送取消信号，等待当前请求完成...")
            log_info("用户请求停止爬取任务")

    def _on_worker_log(self, msg: str):
        """只转发当前任务的日志，避免退休任务的日志干扰界面。"""
        if self.sender() is not None and self.sender() is not self._crawl_worker:
            return
        self._log(msg)

    def _on_data_row(self, row: dict):
        # 只有当前任务的信号能写表格：旧任务已经排队的 data_row 事件
        # 无法被 disconnect 撤销，必须靠 sender 守卫丢弃，否则两轮数据会混在一起
        if self.sender() is not None and self.sender() is not self._crawl_worker:
            return
        # 全量数据用于导出，但有硬上限；超限丢弃并计数，导出时如实报告
        if len(self._all_crawl_data) < _MAX_STORED_ROWS:
            self._all_crawl_data.append(row)
        else:
            self._dropped_rows += 1
        # 表格只渲染前 _MAX_TABLE_ROWS 行，避免长爬取把 GUI 线程拖死
        if self._data_table.rowCount() < _MAX_TABLE_ROWS:
            table_row = self._data_table.rowCount()
            self._data_table.insertRow(table_row)
            self._data_table.setItem(table_row, 0,
                QTableWidgetItem(str(table_row + 1)))
            for col, field in enumerate(self._field_defs):
                val = row.get(field["name"], "")
                self._data_table.setItem(table_row, col + 1, QTableWidgetItem(str(val)))
            self._data_table.scrollToBottom()
        self._update_data_count()
        self._bottom_tabs.setCurrentIndex(1)

    def _on_crawl_progress(self, pct: int):
        pass

    def _on_crawl_done(self, data: list):
        # 已退休任务迟到的终止信号：绝不能改写新一轮任务的状态
        if self.sender() is not None and self.sender() is not self._crawl_worker:
            log_info("忽略迟到的爬取完成信号（来自已退休的任务）")
            return
        self._hold_worker(self._crawl_worker)
        self._crawl_worker = None
        self._set_running(False)
        self._log(f"[完成] 共提取 {len(data)} 条数据")
        self._update_data_count()
        self.crawl_finished.emit({"count": len(data)})

    def _on_crawl_error(self, msg: str):
        # 同 _on_crawl_done：旧任务的错误不能把新任务的状态重置为空闲
        if self.sender() is not None and self.sender() is not self._crawl_worker:
            log_info("忽略迟到的爬取错误信号（来自已退休的任务）")
            return
        # 先摘掉引用再弹模态框：模态框里跑的是嵌套事件循环，
        # 期间任何回调都不该再看到这个已经结束的 worker
        self._hold_worker(self._crawl_worker)
        self._crawl_worker = None
        self._set_running(False)
        self._log(f"[错误] {msg}")
        QMessageBox.critical(self, "爬取失败", msg)
        self.crawl_finished.emit({"count": 0, "error": msg})

    def _set_running(self, running: bool):
        # 只有"爬取未运行且没有在途检测"时才允许点检测：双击不会起两个线程
        self._crawl_running = running
        self._btn_run.setEnabled(not running)
        self._btn_preview.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._btn_check.setEnabled(not running and self._site_check_worker is None)
        if running:
            self._btn_run.setText("爬取中...")
            self._btn_preview.setText("爬取中...")
        else:
            self._btn_run.setText("开始爬取")
            self._btn_preview.setText("预览首页")

    def _update_data_count(self):
        table_rows = self._data_table.rowCount()
        total = len(self._all_crawl_data) + self._dropped_rows
        if self._dropped_rows:
            text = (f"共 {total} 条（内存上限，仅保留前 "
                    f"{len(self._all_crawl_data)} 条，已丢弃 {self._dropped_rows} 条）")
        elif total > table_rows:
            text = f"共 {total} 条（表格仅显示前 {table_rows} 条，导出完整）"
        else:
            text = f"共 {total} 条"
        self._data_count_lbl.setText(text)
        for i in range(self._bottom_tabs.count()):
            if self._bottom_tabs.tabText(i).startswith("数据预览"):
                self._bottom_tabs.setTabText(i, f"数据预览 ({total}条)")

    # ── Export ──

    def _on_export_menu(self):
        menu = QMenu(self)
        act_csv = menu.addAction("导出为 CSV")
        act_json = menu.addAction("导出为 JSON")
        action = menu.exec(self._btn_export.mapToGlobal(
            self._btn_export.rect().bottomLeft()))
        if action == act_csv:
            self._on_export_data("csv")
        elif action == act_json:
            self._on_export_data("json")

    def _on_export_data(self, fmt: str):
        # 导出的是全量数据（_all_crawl_data），不是被截断的表格行数
        count = len(self._all_crawl_data)
        if count == 0:
            QMessageBox.warning(self, "提示", "没有可导出的数据，请先运行爬取")
            return

        ext_filter = "CSV 文件 (*.csv)" if fmt == "csv" else "JSON 文件 (*.json)"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出数据", f"crawler_export.{fmt}", ext_filter)
        if not path:
            return

        try:
            if fmt == "csv":
                self._export_csv(path)
            else:
                self._export_json(path)
            self._log(f"[导出] 已导出 {count} 条数据到: {os.path.basename(path)}")
            if self._dropped_rows:
                # 不静默截断：明确告知本次导出不包含被丢弃的行
                self._log(f"[导出] 注意: 因内存上限（{_MAX_STORED_ROWS} 条）"
                          f"已丢弃 {self._dropped_rows} 条数据，本次导出不含这些行")
                log_warn(f"爬虫导出不完整: 保留 {count} 条, 丢弃 {self._dropped_rows} 条")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _export_csv(self, path: str):
        headers = [f["name"] for f in self._field_defs]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row_data in self._all_crawl_data:
                writer.writerow([row_data.get(h, "") for h in headers])

    def _export_json(self, path: str):
        # Clean data: use field names as keys
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._all_crawl_data, f, ensure_ascii=False, indent=2)

    # ── Save / Load Config ──

    def _on_save_config(self):
        if not self._field_defs:
            QMessageBox.warning(self, "提示", "没有可保存的字段配置")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存爬虫配置", "crawler_config.json", "JSON (*.json)")
        if not path:
            return

        strategy = self._pag_strategy_cb.currentData()
        pagination = {
            "selector": self._pag_sel_edit.text().strip(),
            "max_pages": self._pag_max_spin.value(),
            "wait": self._pag_wait_spin.value(),
            "strategy": strategy,
        }
        if strategy == "pattern":
            pagination["url_pattern"] = self._pag_pattern_edit.text().strip()

        config = {
            "url": self._current_url,
            "container_selector": self._container_sel_edit.text().strip(),
            "fields": self._field_defs,
            "pagination": pagination,
            "options": {
                "delay": self._opt_delay_spin.value(),
                "timeout": self._opt_timeout_spin.value(),
                "max_retries": self._opt_retry_spin.value(),
                "proxy": self._opt_proxy_edit.text().strip(),
                "user_agent": self._opt_ua_edit.text().strip(),
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        self._log(f"[保存] 配置已保存到: {os.path.basename(path)}")

    def _on_load_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "加载爬虫配置", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            self._load_config(config)
            self._log(f"[加载] 配置已加载: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "加载失败", str(e))

    # ── Logging ──

    def _log(self, msg: str):
        self._log_view.append(msg)
        doc = self._log_view.document()
        max_blocks = 500
        excess = doc.blockCount() - max_blocks
        if excess > 0:
            from PyQt6.QtGui import QTextCursor
            cursor = QTextCursor(doc.findBlockByNumber(0))
            for _ in range(min(excess, 50)):
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()

    # ── Public API ──

    def handle_drop(self, paths: list[str]):
        for p in paths:
            if p.lower().endswith(".json"):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    self._load_config(config)
                    self._log(f"[加载] 配置已加载: {os.path.basename(p)}")
                except Exception as e:
                    self._log(f"[错误] 无法加载配置: {e}")

    def _load_config(self, config: dict):
        if not isinstance(config, dict):
            return
        if config.get("url"):
            url = str(config["url"])
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                self._log(f"[警告] URL 协议不支持，已忽略: {url}")
            else:
                self._url_bar.setText(url)
                self._current_url = url

        # Container selector
        container = str(config.get("container_selector", ""))
        self._container_sel_edit.setText(container)
        self._field_container_lbl.setText(container or "(未设置)")

        # Fields
        self._field_defs.clear()
        self._field_list.clear()
        for f in config.get("fields", []):
            if not isinstance(f, dict):
                continue
            name = str(f.get("name", "")).strip()
            selector = str(f.get("selector", "")).strip()
            sel_type = f.get("type", "css")
            if sel_type not in ("css", "xpath"):
                sel_type = "css"
            attr = f.get("attr", "")
            if not name or not selector:
                continue
            field = {
                "name": name, "selector": selector, "type": sel_type,
            }
            if attr:
                field["attr"] = attr
            self._field_defs.append(field)
            attr_hint = f" [{attr}]" if attr else ""
            item_text = f"{name}{attr_hint}  [{sel_type.upper()}]  {selector[:35]}..."
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, field)
            self._field_list.addItem(item)

        # Pagination
        pag = config.get("pagination", {})
        if pag:
            self._pag_sel_edit.setText(pag.get("selector", ""))
            self._pag_max_spin.setValue(pag.get("max_pages", 1))
            self._pag_wait_spin.setValue(pag.get("wait", 0.5))
            strategy = pag.get("strategy", "link")
            idx = self._pag_strategy_cb.findData(strategy)
            if idx >= 0:
                self._pag_strategy_cb.setCurrentIndex(idx)
            self._pag_pattern_edit.setText(pag.get("url_pattern", ""))
        # Update strategy widget visibility
        self._on_pag_strategy_changed()

        # Options
        opts = config.get("options", {})
        if opts:
            self._opt_delay_spin.setValue(opts.get("delay", 2.0))
            self._opt_timeout_spin.setValue(opts.get("timeout", 30))
            self._opt_retry_spin.setValue(opts.get("max_retries", 3))
            self._opt_proxy_edit.setText(opts.get("proxy", ""))
            self._opt_ua_edit.setText(opts.get("user_agent", ""))

    # ── Floating bottom panel positioning & resize ──

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_bottom_panel()

    def _position_bottom_panel(self):
        if self._bottom_container is None:
            return
        w = self.width()
        h = max(60, min(self._bottom_height, self.height() - 80))
        self._bottom_container.setGeometry(
            QRect(0, self.height() - h, w, h))
        self._bottom_container.raise_()

    def _on_resize_handle_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._resizing = True
            self._resize_start_y = event.globalPosition().toPoint().y()
            self._resize_start_h = self._bottom_container.height()
            self._resize_handle.setCursor(QCursor(Qt.CursorShape.SplitVCursor))

    def _on_resize_handle_move(self, event):
        if not self._resizing:
            return
        delta_y = self._resize_start_y - event.globalPosition().toPoint().y()
        new_h = self._resize_start_h + delta_y
        new_h = max(60, min(new_h, self.height() - 80))
        self._bottom_height = new_h
        self._position_bottom_panel()

    def _on_resize_handle_release(self, event):
        self._resizing = False

    def cleanup(self):
        """关闭窗口时清理后台线程。

        这是唯一的阻塞点（应用退出路径）。引擎侧单次请求超时已被压到
        <= _MAX_REQUEST_TIMEOUT 秒，因此这里的等待有明确上界；超时后
        线程会转入退休状态在后台自然结束，不会让关闭流程卡死。
        """
        workers = [self._crawl_worker, self._site_check_worker]
        workers += list(self._retired_workers)
        for w in workers:
            if w is None:
                continue
            try:
                w.cancel()
            except Exception:
                pass

        deadline = time.monotonic() + 3.0
        for w in workers:
            if w is None:
                continue
            try:
                if not w.isRunning():
                    continue
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    log_warn("关闭时仍有爬虫线程未结束，放弃等待")
                    break
                w.wait(remaining_ms)
            except Exception as e:
                log_warn(f"等待爬虫线程结束失败: {e}")

        # 保留引用不释放：仍有线程在跑时丢掉引用会触发 QThread 析构 abort
        if self._retire_timer.isActive():
            self._retire_timer.stop()
        if WEBENGINE_AVAILABLE and self._browser is not None:
            try:
                self._browser.setUrl(QUrl("about:blank"))
            except Exception:
                pass
