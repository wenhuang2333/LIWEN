import os
import socket
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QComboBox, QLabel, QFileDialog, QCheckBox, QSpinBox,
    QScrollArea, QSizePolicy, QTextEdit, QDoubleSpinBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from utils.settings import settings, ACCENT_COLORS
from core.ai_client import PRESET_PROVIDERS


class MyPage(QWidget):
    settings_changed = pyqtSignal()

    # 设置自动保存的防抖间隔（毫秒）。原先每敲一个字就"序列化整份设置 →
    # 落盘 → 重建全局 QSS → 重新枚举网卡"，长文本输入明显卡顿。
    SAVE_DEBOUNCE_MS = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self.SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._flush_settings)
        self._setup_ui()
        self._load_settings()
        self._loading = False

    def _flush_settings(self):
        """真正落盘（防抖到期 / 页面隐藏 / 退出前被强制调用时）。

        这里再读一次控件值，确保写盘的是**最终稳定状态**——AI provider 切换
        等操作会连续触发多次 textChanged，只在最后一次读取才不会写入中间态。
        """
        if not self._loading:
            self._apply_widget_values()
        settings.save()
        self.settings_changed.emit()

    def flush_pending_save(self):
        """立即落盘尚未保存的改动（页面隐藏 / 程序退出前调用）。"""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._flush_settings()

    def hideEvent(self, event):
        # 切走页面时把挂起的改动写掉，避免"改了设置立刻关程序"丢失
        self.flush_pending_save()
        super().hideEvent(event)

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Title
        title = QLabel("设置")
        title.setObjectName("myCardTitle")
        title.setContentsMargins(24, 16, 24, 12)
        root.addWidget(title)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background-color: transparent; } QScrollArea > QWidget > QWidget { background-color: transparent; }")

        scroll_content = QWidget()
        scroll_content.setAutoFillBackground(False)
        scroll_content.setStyleSheet("background-color: transparent;")
        scroll_content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        scroll.setWidget(scroll_content)
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setStyleSheet("background-color: transparent;")
        layout = QVBoxLayout(scroll_content)
        layout.setContentsMargins(20, 4, 20, 20)
        layout.setSpacing(16)

        # ==== 个人设置 ====
        layout.addWidget(self._section_label("个人设置"))

        personal_card = self._card()
        personal_layout = QVBoxLayout(personal_card)
        personal_layout.setSpacing(14)
        personal_layout.setContentsMargins(24, 20, 24, 20)

        self.name_edit = self._form_row(personal_layout, "本机名称",
            "局域网内其他用户将以此名称识别您")
        self.name_edit.setPlaceholderText(socket.gethostname())
        self.name_edit.textChanged.connect(self._auto_save)

        self.theme_combo = self._combo_row(personal_layout, "主题颜色",
            list(ACCENT_COLORS.keys()))
        self.theme_combo.currentTextChanged.connect(self._on_theme_changed)

        layout.addWidget(personal_card)

        # ==== 局域网传输 ====
        layout.addWidget(self._section_label("局域网传输"))

        lan_card = self._card()
        lan_layout = QVBoxLayout(lan_card)
        lan_layout.setSpacing(14)
        lan_layout.setContentsMargins(24, 20, 24, 20)

        path_label = QLabel("默认接收路径")
        path_label.setObjectName("sectionTitle")
        lan_layout.addWidget(path_label)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(os.path.join(os.path.expanduser("~"), "Downloads"))
        path_row.addWidget(self.path_edit)
        browse_btn = QPushButton("浏览")
        browse_btn.setObjectName("secondaryBtn")
        browse_btn.clicked.connect(self._browse_path)
        path_row.addWidget(browse_btn)
        lan_layout.addLayout(path_row)

        self.pwd_edit = self._form_row(lan_layout, "默认密码",
            "压缩/解压时自动填入的默认密码，留空则每次手动输入")
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd_edit.setPlaceholderText("留空则不设置默认密码")
        self.pwd_edit.textChanged.connect(self._auto_save)

        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        port_label = QLabel("默认传输端口")
        port_label.setObjectName("sectionTitle")
        port_label.setFixedWidth(120)
        port_row.addWidget(port_label)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(settings.get("lan_default_port"))
        self.port_spin.valueChanged.connect(self._auto_save)
        port_row.addWidget(self.port_spin)
        port_row.addStretch()
        lan_layout.addLayout(port_row)

        auto_row = QHBoxLayout()
        self.auto_accept_check = QCheckBox("自动同意局域网传输请求")
        self.auto_accept_check.toggled.connect(self._auto_save)
        auto_row.addWidget(self.auto_accept_check)
        auto_row.addStretch()
        lan_layout.addLayout(auto_row)

        layout.addWidget(lan_card)

        # ==== 联机大厅 ====
        layout.addWidget(self._section_label("联机大厅"))

        room_card = self._card()
        room_layout = QVBoxLayout(room_card)
        room_layout.setSpacing(14)
        room_layout.setContentsMargins(24, 20, 24, 20)

        self.room_pwd_edit = self._form_row(room_layout, "默认房间密码",
            "创建/加入房间时自动填入的默认密码，留空则每次手动输入")
        self.room_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.room_pwd_edit.setPlaceholderText("留空则不设置默认密码")
        self.room_pwd_edit.textChanged.connect(self._auto_save)

        voice_row = QHBoxLayout()
        self.voice_check = QCheckBox("启用语音聊天")
        self.voice_check.setChecked(settings.get("room_voice_enabled"))
        self.voice_check.toggled.connect(self._auto_save)
        voice_row.addWidget(self.voice_check)
        voice_row.addStretch()
        room_layout.addLayout(voice_row)

        layout.addWidget(room_card)

        # ==== 网页爬虫 ====
        layout.addWidget(self._section_label("网页爬虫"))

        crawler_card = self._card()
        crawler_layout = QVBoxLayout(crawler_card)
        crawler_layout.setSpacing(14)
        crawler_layout.setContentsMargins(24, 20, 24, 20)

        spin_row1 = QHBoxLayout()
        spin_row1.setSpacing(24)
        delay_layout = QHBoxLayout()
        delay_layout.setSpacing(6)
        delay_label = QLabel("爬取延迟 (秒)")
        delay_label.setFixedWidth(100)
        delay_layout.addWidget(delay_label)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 30)
        self.delay_spin.setValue(int(settings.get("crawler_default_delay")))
        self.delay_spin.valueChanged.connect(self._auto_save)
        self.delay_spin.setSuffix(" 秒")
        delay_layout.addWidget(self.delay_spin)
        spin_row1.addLayout(delay_layout)

        timeout_layout = QHBoxLayout()
        timeout_layout.setSpacing(6)
        timeout_label = QLabel("请求超时 (秒)")
        timeout_label.setFixedWidth(100)
        timeout_layout.addWidget(timeout_label)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 120)
        self.timeout_spin.setValue(settings.get("crawler_default_timeout"))
        self.timeout_spin.valueChanged.connect(self._auto_save)
        self.timeout_spin.setSuffix(" 秒")
        timeout_layout.addWidget(self.timeout_spin)
        spin_row1.addLayout(timeout_layout)

        spin_row1.addStretch()
        crawler_layout.addLayout(spin_row1)

        retry_row = QHBoxLayout()
        retry_label = QLabel("最大重试次数")
        retry_label.setFixedWidth(100)
        retry_row.addWidget(retry_label)
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 10)
        self.retry_spin.setValue(settings.get("crawler_max_retries"))
        self.retry_spin.valueChanged.connect(self._auto_save)
        retry_row.addWidget(self.retry_spin)
        retry_row.addStretch()
        crawler_layout.addLayout(retry_row)

        ua_label = QLabel("默认 User-Agent")
        ua_label.setObjectName("sectionTitle")
        crawler_layout.addWidget(ua_label)

        self.ua_edit = QTextEdit()
        self.ua_edit.setMaximumHeight(72)
        self.ua_edit.setPlaceholderText(settings.get("crawler_default_ua"))
        self.ua_edit.textChanged.connect(self._auto_save)
        crawler_layout.addWidget(self.ua_edit)

        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        export_label = QLabel("数据导出路径")
        export_label.setObjectName("sectionTitle")
        export_label.setFixedWidth(120)
        export_row.addWidget(export_label)
        self.export_edit = QLineEdit()
        self.export_edit.setReadOnly(True)
        self.export_edit.setPlaceholderText(os.path.join(os.path.expanduser("~"), "Downloads"))
        export_row.addWidget(self.export_edit)
        export_browse = QPushButton("浏览")
        export_browse.setObjectName("secondaryBtn")
        export_browse.clicked.connect(self._browse_export_path)
        export_row.addWidget(export_browse)
        crawler_layout.addLayout(export_row)

        layout.addWidget(crawler_card)

        # ==== 生成式AI ====
        layout.addWidget(self._section_label("生成式AI"))

        ai_card = self._card()
        ai_layout = QVBoxLayout(ai_card)
        ai_layout.setSpacing(14)
        ai_layout.setContentsMargins(24, 20, 24, 20)

        # Provider preset
        provider_row = QHBoxLayout()
        provider_row.setSpacing(8)
        provider_label = QLabel("服务商预设")
        provider_label.setFixedWidth(100)
        provider_label.setObjectName("sectionTitleLabel")
        provider_label.setStyleSheet("color: #a6adc8; font-size: 13px;")
        provider_row.addWidget(provider_label)
        self.ai_provider_combo = QComboBox()
        self.ai_provider_combo.addItems(list(PRESET_PROVIDERS.keys()))
        self.ai_provider_combo.setStyleSheet(
            "QComboBox { background: #313244; color: #e8e8f0; border: 1px solid #45475a;"
            "  border-radius: 4px; padding: 6px 10px; font-size: 13px; }"
            "QComboBox:hover { border-color: #89b4fa; }"
            "QComboBox QAbstractItemView { background: #313244; color: #e8e8f0;"
            "  border: 1px solid #45475a; selection-background-color: #45475a; }"
        )
        self.ai_provider_combo.currentTextChanged.connect(self._on_ai_provider_changed)
        provider_row.addWidget(self.ai_provider_combo)
        provider_row.addStretch()
        ai_layout.addLayout(provider_row)

        # API URL
        self.ai_url_edit = self._form_row(ai_layout, "API 端点",
            "完整的 API 端点地址，预设已包含路径，自定义地址将原样使用")
        self.ai_url_edit.setPlaceholderText("https://api.openai.com/v1/chat/completions")
        self.ai_url_edit.textChanged.connect(self._auto_save)

        # API Key
        self.ai_key_edit = self._form_row(ai_layout, "API Key",
            "在对应平台获取的 API 密钥 (sk-...)")
        self.ai_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key_edit.setPlaceholderText("sk-...")
        self.ai_key_edit.textChanged.connect(self._auto_save)

        # Model
        self.ai_model_edit = self._form_row(ai_layout, "模型名称",
            "例如: gpt-4o, deepseek-chat, llama3 等")
        self.ai_model_edit.setPlaceholderText("gpt-4o")
        self.ai_model_edit.textChanged.connect(self._auto_save)

        # Temperature + Max Tokens row
        temp_tokens_row = QHBoxLayout()
        temp_tokens_row.setSpacing(24)

        temp_layout = QHBoxLayout()
        temp_layout.setSpacing(6)
        temp_label = QLabel("Temperature")
        temp_label.setFixedWidth(100)
        temp_label.setStyleSheet("color: #a6adc8; font-size: 13px;")
        temp_layout.addWidget(temp_label)
        self.ai_temp_spin = QDoubleSpinBox()
        self.ai_temp_spin.setRange(0.0, 2.0)
        self.ai_temp_spin.setSingleStep(0.1)
        self.ai_temp_spin.setDecimals(1)
        self.ai_temp_spin.setValue(1.0)
        self.ai_temp_spin.valueChanged.connect(self._auto_save)
        temp_layout.addWidget(self.ai_temp_spin)
        temp_tokens_row.addLayout(temp_layout)

        tokens_layout = QHBoxLayout()
        tokens_layout.setSpacing(6)
        tokens_label = QLabel("Max Tokens")
        tokens_label.setFixedWidth(100)
        tokens_label.setStyleSheet("color: #a6adc8; font-size: 13px;")
        tokens_layout.addWidget(tokens_label)
        self.ai_tokens_spin = QSpinBox()
        self.ai_tokens_spin.setRange(1, 131072)
        self.ai_tokens_spin.setSingleStep(256)
        self.ai_tokens_spin.setValue(4096)
        self.ai_tokens_spin.valueChanged.connect(self._auto_save)
        tokens_layout.addWidget(self.ai_tokens_spin)
        temp_tokens_row.addLayout(tokens_layout)

        temp_tokens_row.addStretch()
        ai_layout.addLayout(temp_tokens_row)

        # System Prompt
        sp_label = QLabel("System Prompt")
        sp_label.setObjectName("sectionTitle")
        ai_layout.addWidget(sp_label)
        self.ai_system_edit = QTextEdit()
        self.ai_system_edit.setMaximumHeight(64)
        self.ai_system_edit.setPlaceholderText("可选，设定 AI 的角色和行为...")
        self.ai_system_edit.textChanged.connect(self._auto_save)
        ai_layout.addWidget(self.ai_system_edit)

        layout.addWidget(ai_card)

        layout.addStretch()
        root.addWidget(scroll, 1)

    def _section_label(self, text):
        label = QLabel(text)
        label.setObjectName("myCardTitle")
        label.setContentsMargins(4, 8, 0, 0)
        return label

    def _card(self):
        card = QWidget()
        card.setObjectName("settingsPanel")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return card

    def _form_row(self, parent_layout, label_text, hint_text=None):
        label = QLabel(label_text)
        label.setObjectName("sectionTitle")
        parent_layout.addWidget(label)

        if hint_text:
            hint = QLabel(hint_text)
            hint.setObjectName("myCardHint")
            parent_layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        edit = QLineEdit()
        row.addWidget(edit)
        parent_layout.addLayout(row)
        return edit

    def _combo_row(self, parent_layout, label_text, items):
        label = QLabel(label_text)
        label.setObjectName("sectionTitle")
        parent_layout.addWidget(label)

        row = QHBoxLayout()
        row.setSpacing(8)
        combo = QComboBox()
        combo.addItems(items)
        combo.setStyleSheet(
            "QComboBox { background: #313244; color: #e8e8f0; border: 1px solid #45475a;"
            "  border-radius: 4px; padding: 6px 10px; font-size: 13px; }"
            "QComboBox:hover { border-color: #89b4fa; }"
            "QComboBox QAbstractItemView { background: #313244; color: #e8e8f0;"
            "  border: 1px solid #45475a; selection-background-color: #45475a; }"
        )
        row.addWidget(combo)
        row.addStretch()
        parent_layout.addLayout(row)
        return combo

    def _load_settings(self):
        self.name_edit.setText(settings.username if settings.username != socket.gethostname() else "")
        self.name_edit.setPlaceholderText(socket.gethostname())
        idx = self.theme_combo.findText(settings.theme_name)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        self.path_edit.setText(settings.get("default_receive_path"))
        self.pwd_edit.setText(settings.get("default_password"))
        self.port_spin.setValue(settings.get("lan_default_port"))
        self.auto_accept_check.setChecked(settings.get("auto_accept"))
        self.room_pwd_edit.setText(settings.get("room_default_password"))
        self.voice_check.setChecked(settings.get("room_voice_enabled"))
        self.delay_spin.setValue(int(settings.get("crawler_default_delay")))
        self.timeout_spin.setValue(settings.get("crawler_default_timeout"))
        self.retry_spin.setValue(settings.get("crawler_max_retries"))
        self.ua_edit.setPlainText(settings.get("crawler_default_ua"))
        self.export_edit.setText(settings.get("crawler_export_path"))
        cfg = settings.get_provider_config()
        self.ai_url_edit.setText(cfg.get("api_url", ""))
        self.ai_key_edit.setText(cfg.get("api_key", ""))
        self.ai_model_edit.setText(cfg.get("model", ""))
        self.ai_temp_spin.setValue(cfg.get("temperature", 1.0))
        self.ai_tokens_spin.setValue(cfg.get("max_tokens", 4096))
        self.ai_system_edit.setPlainText(cfg.get("system_prompt", ""))
        self.ai_provider_combo.setCurrentText(settings.get_active_provider())

    def _auto_save(self, *args):
        """控件变更 → 更新内存设置，并把落盘与 UI 刷新推迟到防抖窗口之后。"""
        if self._loading:
            return
        self._apply_widget_values()
        # 每次变更都重置计时器：连续输入期间不落盘、不重建样式表
        self._save_timer.start()

    def _apply_widget_values(self):
        """把当前控件值写入内存中的 settings（不落盘、不发信号）。"""
        name = self.name_edit.text().strip() or socket.gethostname()
        path = self.path_edit.text() or os.path.join(os.path.expanduser("~"), "Downloads")
        pwd = self.pwd_edit.text()
        auto = self.auto_accept_check.isChecked()

        settings.set("username", name)
        settings.set("default_receive_path", path)
        settings.set("default_password", pwd)
        settings.set("auto_accept", auto)
        settings.set("lan_default_port", self.port_spin.value())
        settings.set("room_default_password", self.room_pwd_edit.text())
        settings.set("room_voice_enabled", self.voice_check.isChecked())
        settings.set("crawler_default_delay", self.delay_spin.value())
        settings.set("crawler_default_timeout", self.timeout_spin.value())
        settings.set("crawler_max_retries", self.retry_spin.value())
        settings.set("crawler_default_ua", self.ua_edit.toPlainText())
        settings.set("crawler_export_path",
                     self.export_edit.text() or os.path.join(os.path.expanduser("~"), "Downloads"))
        provider_name = self.ai_provider_combo.currentText()
        cfg = {
            "api_url": self.ai_url_edit.text(),
            "api_key": self.ai_key_edit.text(),
            "model": self.ai_model_edit.text(),
            "temperature": self.ai_temp_spin.value(),
            "max_tokens": self.ai_tokens_spin.value(),
            "system_prompt": self.ai_system_edit.toPlainText(),
        }
        settings.set_provider_config(provider_name, cfg)
        settings.set_active_provider(provider_name)

    def _on_theme_changed(self, theme_name):
        if self._loading:
            return
        settings.set("theme", theme_name)
        settings.save()
        # 主题需要即时生效，不走防抖
        self.settings_changed.emit()

    def _on_ai_provider_changed(self, name):
        if self._loading:
            return
        cfg = settings.get_provider_config(name)
        if cfg.get("api_url"):
            self.ai_url_edit.setText(cfg["api_url"])
        else:
            prov = PRESET_PROVIDERS.get(name)
            if prov and prov.base_url:
                self.ai_url_edit.setText(prov.base_url)
        self.ai_key_edit.setText(cfg.get("api_key", ""))
        if cfg.get("model"):
            self.ai_model_edit.setText(cfg["model"])
        else:
            prov = PRESET_PROVIDERS.get(name)
            if prov and prov.default_model:
                self.ai_model_edit.setText(prov.default_model)
        self.ai_temp_spin.setValue(cfg.get("temperature", 1.0))
        self.ai_tokens_spin.setValue(cfg.get("max_tokens", 4096))
        self.ai_system_edit.setPlainText(cfg.get("system_prompt", ""))
        self._auto_save()

    def _browse_path(self):
        folder = QFileDialog.getExistingDirectory(self, "选择默认接收目录")
        if folder:
            self.path_edit.setText(folder)
            self._auto_save()

    def _browse_export_path(self):
        folder = QFileDialog.getExistingDirectory(self, "选择数据导出目录")
        if folder:
            self.export_edit.setText(folder)
            self._auto_save()

    def get_default_password(self):
        return settings.get("default_password") or None

    def get_default_receive_path(self):
        return settings.get("default_receive_path")
