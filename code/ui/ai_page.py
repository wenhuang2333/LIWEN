import os
import json
import re
import uuid
import time
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QLabel, QScrollArea, QTextEdit, QDoubleSpinBox,
    QSpinBox, QSizePolicy, QFrame, QMessageBox, QSplitter,
    QDialog, QDialogButtonBox, QComboBox, QMenu, QInputDialog,
    QApplication, QListWidget, QListWidgetItem, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPoint
from PyQt6.QtGui import QAction, QFont, QKeyEvent, QPixmap

from ui.styles import MONO_FONT_FAMILY
from utils.paths import static_path
from utils.settings import settings, SETTINGS_DIR
from utils.logger import info, error as log_error, warn as log_warn
from core.ai_client import AiClient, PRESET_PROVIDERS, build_messages, get_provider_default_config
from core.workers import AiChatWorker

CONVERSATIONS_DIR = os.path.join(SETTINGS_DIR, "ai_conversations")
CONVERSATIONS_INDEX = os.path.join(CONVERSATIONS_DIR, "index.json")


# ──────────────────────────────────────────────
#  AI Config Dialog
# ──────────────────────────────────────────────

class AiConfigDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 配置")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._loading = True
        self._setup_ui()
        self._load_config()
        self._loading = False

    def _setup_ui(self):
        self.setStyleSheet(
            "AiConfigDialog { background-color: #1e1e2e; }"
            "QLabel { color: #cdd6f4; font-size: 13px; background: transparent; }"
            "QLineEdit, QDoubleSpinBox, QSpinBox, QTextEdit, QComboBox {"
            "  background: #313244; color: #cdd6f4; border: 1px solid #45475a;"
            "  border-radius: 6px; padding: 6px 10px; font-size: 13px; }"
            "QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus,"
            "QTextEdit:focus, QComboBox:focus { border-color: #89b4fa; }"
            "QComboBox QAbstractItemView { background: #313244; color: #cdd6f4;"
            "  border: 1px solid #45475a; selection-background-color: #45475a; }"
            "QPushButton { border-radius: 6px; padding: 8px 20px; font-size: 13px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        title = QLabel("API 配置")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #cdd6f4;")
        layout.addWidget(title)

        hint = QLabel("请填写完整的 API 端点地址。预设地址已包含路径，自定义地址将原样使用。")
        hint.setStyleSheet("color: #a6adc8; font-size: 12px; margin-bottom: 6px;")
        layout.addWidget(hint)

        # Provider preset
        preset_row = QHBoxLayout()
        preset_row.setSpacing(10)
        preset_row.addWidget(QLabel("服务商预设"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(PRESET_PROVIDERS.keys()))
        self.preset_combo.currentTextChanged.connect(self._on_preset)
        preset_row.addWidget(self.preset_combo, 1)
        layout.addLayout(preset_row)

        # API URL
        url_row = QHBoxLayout()
        url_row.setSpacing(10)
        url_row.addWidget(QLabel("API 地址"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://api.openai.com/v1/chat/completions")
        url_row.addWidget(self.url_edit, 1)
        layout.addLayout(url_row)

        # API Key
        key_row = QHBoxLayout()
        key_row.setSpacing(10)
        key_row.addWidget(QLabel("API Key"))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-...")
        key_row.addWidget(self.key_edit, 1)
        layout.addLayout(key_row)

        # Model
        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        model_row.addWidget(QLabel("模型名称"))
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("gpt-4o")
        model_row.addWidget(self.model_edit, 1)
        layout.addLayout(model_row)

        # Temperature + Max Tokens
        param_row = QHBoxLayout()
        param_row.setSpacing(20)
        t_layout = QHBoxLayout()
        t_layout.setSpacing(6)
        t_layout.addWidget(QLabel("Temperature"))
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setDecimals(1)
        t_layout.addWidget(self.temp_spin)
        param_row.addLayout(t_layout)

        mt_layout = QHBoxLayout()
        mt_layout.setSpacing(6)
        mt_layout.addWidget(QLabel("Max Tokens"))
        self.tokens_spin = QSpinBox()
        self.tokens_spin.setRange(1, 131072)
        self.tokens_spin.setSingleStep(256)
        mt_layout.addWidget(self.tokens_spin)
        param_row.addLayout(mt_layout)
        param_row.addStretch()
        layout.addLayout(param_row)

        # System Prompt
        layout.addWidget(QLabel("System Prompt"))
        self.system_edit = QTextEdit()
        self.system_edit.setMaximumHeight(72)
        self.system_edit.setPlaceholderText("可选，设定 AI 的角色和行为...")
        layout.addWidget(self.system_edit)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #cdd6f4; border: none; }"
            "QPushButton:hover { background: #585b70; }"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        test_btn = QPushButton("测试连接")
        test_btn.setStyleSheet(
            "QPushButton { background: #a6e3a1; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #b5eeb5; }"
        )
        test_btn.clicked.connect(self._test_connection)
        btn_layout.addWidget(test_btn)

        apply_btn = QPushButton("应用")
        apply_btn.setStyleSheet(
            "QPushButton { background: #89b4fa; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #b4d0fb; }"
        )
        apply_btn.clicked.connect(self._apply)
        btn_layout.addWidget(apply_btn)
        layout.addLayout(btn_layout)

    def _load_config(self):
        self._current_provider = settings.get_active_provider()
        cfg = settings.get_provider_config(self._current_provider)
        self.url_edit.setText(cfg.get("api_url", ""))
        self.key_edit.setText(cfg.get("api_key", ""))
        self.model_edit.setText(cfg.get("model", ""))
        self.temp_spin.setValue(cfg.get("temperature", 1.0))
        self.tokens_spin.setValue(cfg.get("max_tokens", 4096))
        self.system_edit.setPlainText(cfg.get("system_prompt", ""))
        self.preset_combo.setCurrentText(self._current_provider)

    def _on_preset(self, name):
        if self._loading:
            return
        cfg = settings.get_provider_config(name)
        if cfg.get("api_url"):
            self.url_edit.setText(cfg["api_url"])
        else:
            prov = PRESET_PROVIDERS.get(name)
            if prov and prov.base_url:
                self.url_edit.setText(prov.base_url)
        self.key_edit.setText(cfg.get("api_key", ""))
        if cfg.get("model"):
            self.model_edit.setText(cfg["model"])
        else:
            prov = PRESET_PROVIDERS.get(name)
            if prov and prov.default_model:
                self.model_edit.setText(prov.default_model)
        self.temp_spin.setValue(cfg.get("temperature", 1.0))
        self.tokens_spin.setValue(cfg.get("max_tokens", 4096))
        self.system_edit.setPlainText(cfg.get("system_prompt", ""))

    def _get_config(self) -> dict:
        return {
            "api_url": self.url_edit.text().strip(),
            "api_key": self.key_edit.text().strip(),
            "model": self.model_edit.text().strip(),
            "temperature": self.temp_spin.value(),
            "max_tokens": self.tokens_spin.value(),
            "system_prompt": self.system_edit.toPlainText(),
        }

    def _apply(self):
        provider_name = self.preset_combo.currentText()
        cfg = self._get_config()
        # Show connection test dialog
        dlg = AiConnectionTestDialog(cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            settings.set_provider_config(provider_name, cfg)
            settings.set_active_provider(provider_name)
            settings.save()
            self.accept()
        # If rejected, stay in config dialog so user can fix issues

    def _test_connection(self):
        """Test connection without saving — standalone test button."""
        cfg = self._get_config()
        dlg = AiConnectionTestDialog(cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            QMessageBox.information(self, "连接测试", "连接成功！API 配置有效。\n请点击「应用」保存配置。")


# ──────────────────────────────────────────────
#  AI Connection Test Dialog
# ──────────────────────────────────────────────

class AiConnectionTestDialog(QDialog):
    """Modal dialog that tests API connectivity with real-time status updates."""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._result = None  # (success, message)
        self.setWindowTitle("连接测试")
        self.setMinimumWidth(460)
        self.setModal(True)
        self._setup_ui()
        # Start test after UI is shown
        QTimer.singleShot(200, self._run_test)

    def _setup_ui(self):
        self.setStyleSheet(
            "AiConnectionTestDialog { background-color: #1e1e2e; }"
            "QLabel { color: #cdd6f4; font-size: 13px; background: transparent; }"
            "QTextEdit { background: #181825; color: #cdd6f4; border: 1px solid #313244;"
            f"  border-radius: 6px; font-family: {MONO_FONT_FAMILY};"
            "  font-size: 12px; padding: 10px; }"
            "QPushButton { border-radius: 6px; padding: 8px 20px; font-size: 13px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # Title
        title = QLabel("正在测试 API 连接…")
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #89b4fa;")
        self._title_label = title
        layout.addWidget(title)

        # Info
        info_text = (
            f"地址: {self._config.get('api_url', '(未配置)')}\n"
            f"模型: {self._config.get('model', '(未配置)')}"
        )
        info_lbl = QLabel(info_text)
        info_lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        # Log area
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumHeight(180)
        layout.addWidget(self._log_view, 1)

        # Status
        self._status_label = QLabel("待连接…")
        self._status_label.setStyleSheet("color: #f9e2af; font-size: 13px; font-weight: 600;")
        layout.addWidget(self._status_label)

        # Buttons — only cancel during test
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._cancel_btn = QPushButton("取消测试")
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #cdd6f4; border: none; }"
            "QPushButton:hover { background: #585b70; }"
        )
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)

        self._force_apply_btn = QPushButton("仍然应用")
        self._force_apply_btn.setStyleSheet(
            "QPushButton { background: #f38ba8; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #f2cdcd; }"
        )
        self._force_apply_btn.clicked.connect(self.accept)
        self._force_apply_btn.setVisible(False)
        btn_row.addWidget(self._force_apply_btn)

        self._retry_btn = QPushButton("重试")
        self._retry_btn.setStyleSheet(
            "QPushButton { background: #89b4fa; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #b4d0fb; }"
        )
        self._retry_btn.clicked.connect(self._retry)
        self._retry_btn.setVisible(False)
        btn_row.addWidget(self._retry_btn)

        layout.addLayout(btn_row)

    def _log(self, msg):
        self._log_view.append(msg)
        sb = self._log_view.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _run_test(self):
        """Run connectivity test in background thread."""
        from core.workers import AiConnectionTestWorker

        self._test_worker = AiConnectionTestWorker(self._config)
        self._test_worker.log_message.connect(self._log)
        self._test_worker.status_update.connect(self._on_status)
        self._test_worker.finished.connect(self._on_test_finished)
        self._test_worker.start()

    def _retry(self):
        """Retry the connection test."""
        self._log_view.clear()
        self._force_apply_btn.setVisible(False)
        self._retry_btn.setVisible(False)
        self._cancel_btn.setVisible(True)
        self._status_label.setText("重试连接…")
        self._status_label.setStyleSheet("color: #f9e2af; font-size: 13px; font-weight: 600;")
        self._title_label.setText("正在测试 API 连接…")
        self._title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #89b4fa;")
        self._run_test()

    def _on_status(self, msg):
        self._status_label.setText(msg)

    def _on_test_finished(self, success, message):
        if success:
            self._title_label.setText("✓ 连接成功")
            self._title_label.setStyleSheet(
                "font-size: 15px; font-weight: bold; color: #a6e3a1;")
            self._status_label.setText("API 配置有效，连接测试通过")
            self._status_label.setStyleSheet(
                "color: #a6e3a1; font-size: 13px; font-weight: 600;")
            self._cancel_btn.setVisible(False)
            self._retry_btn.setVisible(False)
            self._force_apply_btn.setText("完成")
            self._force_apply_btn.setStyleSheet(
                "QPushButton { background: #a6e3a1; color: #1e1e2e; border: none;"
                "  font-weight: bold; }"
                "QPushButton:hover { background: #b5eeb5; }"
            )
            self._force_apply_btn.setVisible(True)
        else:
            self._title_label.setText("✗ 连接失败")
            self._title_label.setStyleSheet(
                "font-size: 15px; font-weight: bold; color: #f38ba8;")
            self._status_label.setText(message[:150])
            self._status_label.setStyleSheet(
                "color: #f38ba8; font-size: 13px; font-weight: 600;")
            self._cancel_btn.setVisible(False)
            self._retry_btn.setVisible(True)
            self._force_apply_btn.setVisible(True)


# ──────────────────────────────────────────────
#  Model Selector Menu
# ──────────────────────────────────────────────

class ModelSelectorMenu(QMenu):

    model_selected = pyqtSignal(str, str)  # provider_name, model_name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(420)
        self.setStyleSheet(
            "QMenu { background: #1e1e2e; color: #cdd6f4; border: 1px solid #45475a;"
            "  border-radius: 10px; padding: 8px 4px; }"
            "QMenu::item { padding: 10px 16px; border-radius: 6px; margin: 2px 4px; }"
            "QMenu::item:selected { background: #313244; }"
            "QMenu::separator { height: 1px; background: #45475a; margin: 6px 12px; }"
        )
        self.aboutToShow.connect(self._populate)

    def _populate(self):
        self.clear()
        active_provider = settings.get_active_provider()

        # Header
        header = QAction("选择模型", self)
        header.setEnabled(False)
        header_font = header.font()
        header_font.setBold(True)
        header_font.setPointSize(11)
        header.setFont(header_font)
        self.addAction(header)
        self.addSeparator()

        # Preset providers
        for name, prov in PRESET_PROVIDERS.items():
            if not prov.base_url:  # skip "自定义" entry
                continue
            cfg = settings.get_provider_config(name)
            is_current = (active_provider == name)
            has_key = bool(cfg.get("api_key", ""))
            prefix = "● " if is_current else "   "
            key_mark = " [已配置]" if has_key else ""
            label = f"{prefix}{name}  —  {cfg.get('model', prov.default_model)}{key_mark}"
            action = QAction(label, self)
            action.setFont(self._item_font())
            action.triggered.connect(
                lambda checked, n=name, p=prov: self._select_preset(n, p)
            )
            self.addAction(action)

        self.addSeparator()

        # Custom models section
        custom_header = QAction("自定义模型", self)
        custom_header.setEnabled(False)
        custom_header.setFont(self._item_font())
        self.addAction(custom_header)

        custom_models = settings.get_custom_models()
        for model_id, cfg in custom_models.items():
            if not isinstance(cfg, dict):
                continue
            model = cfg.get("model", model_id)
            url = cfg.get("api_url", "")
            has_key = bool(cfg.get("api_key", ""))
            is_custom_active = (active_provider == "自定义"
                                and settings.get_provider_config("自定义").get("model", "") == model)
            prefix = "● " if is_custom_active else "   "
            key_mark = " [已配置]" if has_key else ""
            url_short = (url[:35] + "…") if len(url) > 35 else (url or "无地址")
            label = f"✎ {prefix}{model}  —  {url_short}{key_mark}"
            action = QAction(label, self)
            action.setFont(self._item_font())
            action.triggered.connect(
                lambda checked, mid=model_id: self._select_custom_model(mid)
            )
            self.addAction(action)

        # Manage custom models
        manage_action = QAction("⚙ 管理自定义模型…", self)
        manage_action.setFont(self._item_font())
        manage_action.triggered.connect(self._custom_model)
        self.addAction(manage_action)

        self.addSeparator()

        # Config & Clear
        config_action = QAction("⚙ 配置 API...", self)
        config_action.setFont(self._item_font())
        config_action.triggered.connect(self._open_config)
        self.addAction(config_action)

        clear_action = QAction("🗑 新建对话", self)
        clear_action.setFont(self._item_font())
        clear_action.triggered.connect(self._new_chat)
        self.addAction(clear_action)

    def _item_font(self):
        f = QFont()
        f.setPointSize(11)
        return f

    def _select_preset(self, name, prov):
        # Load existing provider config or create default
        cfg = settings.get_provider_config(name)
        if not cfg.get("api_url") and prov.base_url:
            cfg["api_url"] = prov.base_url
        if not cfg.get("model") and prov.default_model:
            cfg["model"] = prov.default_model
        settings.set_provider_config(name, cfg)
        settings.set_active_provider(name)
        settings.save()

        # Open config dialog pre-filled for this provider
        dlg = AiConfigDialog(self.parent())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.model_selected.emit(name, settings.get_provider_config(name).get("model", ""))
        else:
            self.model_selected.emit(name, cfg.get("model", ""))

    def _custom_model(self):
        dlg = CustomModelDialog(self.parent())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            model = settings.get_provider_config("自定义").get("model", "")
            self.model_selected.emit("自定义", model)

    def _select_custom_model(self, model_id):
        """Directly activate a saved custom model from the menu."""
        cfg = settings.get_custom_model_config(model_id)
        if not cfg:
            return
        # Load full config into "自定义" provider and activate
        settings.set_provider_config("自定义", dict(cfg))
        settings.set_active_provider("自定义")
        settings.save()
        model = cfg.get("model", model_id)
        info(f"AI 菜单切换自定义模型: id={model_id}, model={model}")
        self.model_selected.emit("自定义", model)

    def _open_config(self):
        dlg = AiConfigDialog(self.parent())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            provider = settings.get_active_provider()
            model = settings.get_provider_config().get("model", "")
            self.model_selected.emit(provider, model)

    def _new_chat(self):
        page = self._find_ai_page()
        if page:
            page._new_conversation()

    def _find_ai_page(self):
        p = self.parent()
        while p:
            if hasattr(p, '_new_conversation') and hasattr(p, '_messages'):
                return p
            p = p.parent()
        return None


# ──────────────────────────────────────────────
#  Custom Model Dialog
# ──────────────────────────────────────────────

class CustomModelDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自定义模型管理")
        self.setMinimumWidth(560)
        self.setModal(True)
        self._setup_ui()
        self._refresh_list()

    def _setup_ui(self):
        self.setStyleSheet(
            "CustomModelDialog { background-color: #1e1e2e; }"
            "QLabel { color: #cdd6f4; font-size: 13px; background: transparent; }"
            "QListWidget { background: #313244; color: #cdd6f4; border: 1px solid #45475a;"
            "  border-radius: 6px; font-size: 13px; outline: none; }"
            "QListWidget::item { padding: 10px 12px; border-radius: 4px;"
            "  border-bottom: 1px solid #252536; }"
            "QListWidget::item:selected { background: #45475a; }"
            "QPushButton { border-radius: 6px; padding: 8px 18px; font-size: 13px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel("自定义模型管理")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #cdd6f4;")
        layout.addWidget(title)

        hint = QLabel("管理已保存的自定义模型配置。每个模型包含完整的 API 地址、密钥和参数。")
        hint.setStyleSheet("color: #a6adc8; font-size: 12px; margin-bottom: 4px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list_widget = QListWidget()
        self.list_widget.setMinimumHeight(180)
        layout.addWidget(self.list_widget, 1)

        # Detail preview
        self._detail_label = QLabel("")
        self._detail_label.setStyleSheet(
            "QLabel { color: #a6adc8; font-size: 12px;"
            "  background: #181825; border: 1px solid #313244;"
            "  border-radius: 4px; padding: 8px 12px; min-height: 40px; }")
        self._detail_label.setWordWrap(True)
        layout.addWidget(self._detail_label)

        self.list_widget.currentItemChanged.connect(self._on_item_changed)

        # Action buttons row 1
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        add_btn = QPushButton("添加模型")
        add_btn.setStyleSheet(
            "QPushButton { background: #a6e3a1; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #b5eeb5; }"
        )
        add_btn.clicked.connect(self._add_model)
        btn_row.addWidget(add_btn)

        edit_btn = QPushButton("编辑配置")
        edit_btn.setStyleSheet(
            "QPushButton { background: #89b4fa; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #b4d0fb; }"
        )
        edit_btn.clicked.connect(self._edit_model)
        btn_row.addWidget(edit_btn)

        del_btn = QPushButton("删除模型")
        del_btn.setStyleSheet(
            "QPushButton { background: #f38ba8; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #f2cdcd; }"
        )
        del_btn.clicked.connect(self._delete_model)
        btn_row.addWidget(del_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Dialog buttons row
        dlg_btns = QHBoxLayout()
        dlg_btns.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #cdd6f4; border: none; }"
            "QPushButton:hover { background: #585b70; }"
        )
        cancel_btn.clicked.connect(self.reject)
        dlg_btns.addWidget(cancel_btn)

        use_btn = QPushButton("使用所选模型")
        use_btn.setStyleSheet(
            "QPushButton { background: #89b4fa; color: #1e1e2e; border: none;"
            "  font-weight: bold; }"
            "QPushButton:hover { background: #b4d0fb; }"
        )
        use_btn.clicked.connect(self._use_model)
        dlg_btns.addWidget(use_btn)
        layout.addLayout(dlg_btns)

    def _refresh_list(self):
        self.list_widget.currentItemChanged.disconnect(self._on_item_changed)
        self.list_widget.clear()

        custom_models = settings.get_custom_models()
        active_model = settings.get_provider_config("自定义").get("model", "")
        active_provider = settings.get_active_provider()

        for model_id, cfg in custom_models.items():
            if not isinstance(cfg, dict):
                continue
            model = cfg.get("model", model_id)
            url = cfg.get("api_url", "")
            has_key = bool(cfg.get("api_key", ""))
            key_mark = " [已配置]" if has_key else " [未配置密钥]"
            url_short = (url[:45] + "…") if len(url) > 45 else (url or "无API地址")

            is_active = (active_provider == "自定义" and model == active_model)
            prefix = "● " if is_active else "   "
            item_text = f"{prefix}{model}  —  {url_short}{key_mark}"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, model_id)
            item.setToolTip(
                f"模型: {model}\n"
                f"地址: {url}\n"
                f"温度: {cfg.get('temperature', 1.0)}\n"
                f"最大Token: {cfg.get('max_tokens', 4096)}\n"
                f"密钥: {'已配置' if has_key else '未配置'}"
            )
            self.list_widget.addItem(item)

        self.list_widget.currentItemChanged.connect(self._on_item_changed)
        self._detail_label.setText(
            f"共 {len(custom_models)} 个自定义模型" if custom_models
            else "尚未保存自定义模型 — 点击「添加模型」配置新的 API 和模型"
        )

    def _on_item_changed(self, current, previous):
        if current is None:
            self._detail_label.setText("")
            return
        model_id = current.data(Qt.ItemDataRole.UserRole)
        if not model_id:
            return
        cfg = settings.get_custom_model_config(model_id)
        model = cfg.get("model", model_id)
        url = cfg.get("api_url", "未配置")
        key_ok = "已配置" if cfg.get("api_key", "") else "未配置"
        self._detail_label.setText(
            f"  模型: {model}\n"
            f"  地址: {url}\n"
            f"  密钥: {key_ok}  |  温度: {cfg.get('temperature', 1.0)}  |  最大Token: {cfg.get('max_tokens', 4096)}"
        )

    def _add_model(self):
        """Open AiConfigDialog for a new custom model."""
        # Pre-set to "自定义" with empty config
        old_provider = settings.get_active_provider()
        settings.set_active_provider("自定义")
        settings.set_provider_config("自定义", {
            "api_url": "", "api_key": "", "model": "",
            "temperature": 1.0, "max_tokens": 4096, "system_prompt": "",
        })
        settings.save()

        dlg = AiConfigDialog(self)
        dlg.setWindowTitle("添加自定义模型")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Read the config the user just applied
            cfg = settings.get_provider_config("自定义")
            model_id = cfg.get("model", "").strip()
            if not model_id:
                QMessageBox.warning(self, "提示",
                    "模型名称不能为空。请重新添加并填写模型名称。")
                settings.set_active_provider(old_provider)
                return

            # Check for duplicate
            existing = settings.get_custom_models()
            if model_id in existing:
                reply = QMessageBox.question(
                    self, "模型已存在",
                    f"模型「{model_id}」已存在。是否覆盖？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    settings.set_active_provider(old_provider)
                    return

            settings.add_custom_model(model_id, cfg)
            self._refresh_list()
            info(f"AI 添加自定义模型: id={model_id}, url={cfg.get('api_url', '')}")
        else:
            # User cancelled — restore old provider
            settings.set_active_provider(old_provider)

    def _edit_model(self):
        """Open AiConfigDialog to edit the selected custom model."""
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择一个模型")
            return
        model_id = item.data(Qt.ItemDataRole.UserRole)
        cfg = settings.get_custom_model_config(model_id)
        if not cfg:
            QMessageBox.warning(self, "错误", f"未找到模型「{model_id}」的配置")
            return

        # Load the config into "自定义" provider temporarily for editing
        old_provider = settings.get_active_provider()
        old_cfg = settings.get_provider_config("自定义")
        settings.set_active_provider("自定义")
        settings.set_provider_config("自定义", dict(cfg))
        settings.save()

        dlg = AiConfigDialog(self)
        dlg.setWindowTitle(f"编辑自定义模型 — {model_id}")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cfg = settings.get_provider_config("自定义")
            new_model_id = new_cfg.get("model", "").strip()
            if not new_model_id:
                QMessageBox.warning(self, "提示", "模型名称不能为空。")
                # Restore
                settings.set_active_provider(old_provider)
                settings.set_provider_config("自定义", old_cfg)
                return

            # If model name changed, remove old entry
            if new_model_id != model_id:
                settings.remove_custom_model(model_id)
            settings.add_custom_model(new_model_id, new_cfg)
            self._refresh_list()
            info(f"AI 编辑自定义模型: {model_id} → {new_model_id}")
        else:
            # User cancelled — restore
            settings.set_active_provider(old_provider)
            settings.set_provider_config("自定义", old_cfg)

    def _delete_model(self):
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择一个模型")
            return
        model_id = item.data(Qt.ItemDataRole.UserRole)
        cfg = settings.get_custom_model_config(model_id)
        model = cfg.get("model", model_id)

        reply = QMessageBox.question(
            self, "删除模型",
            f"确定要删除模型「{model}」吗？\n\n"
            f"地址: {cfg.get('api_url', '未配置')}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            settings.remove_custom_model(model_id)
            self._refresh_list()
            info(f"AI 删除自定义模型: id={model_id}")

    def _use_model(self):
        """Load the selected model's full config and activate it."""
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择一个模型再使用")
            return
        model_id = item.data(Qt.ItemDataRole.UserRole)
        cfg = settings.get_custom_model_config(model_id)
        if not cfg:
            QMessageBox.warning(self, "错误", f"未找到模型「{model_id}」的配置")
            return

        # Write full config to "自定义" provider and activate it
        settings.set_provider_config("自定义", dict(cfg))
        settings.set_active_provider("自定义")
        settings.save()

        info(f"AI 激活自定义模型: id={model_id}, url={cfg.get('api_url', '')}")
        self.accept()


# ──────────────────────────────────────────────
#  Main AI Chat Page
# ──────────────────────────────────────────────

class AiPage(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages = []
        self._worker = None
        self._current_ai_bubble = None
        self._current_ai_text = ""
        self._generating = False
        self._current_conv_id = None       # None = new unsaved conversation
        self._conversations = []           # [{id, title, created_at, updated_at}]
        self._conv_saved_on_disk = False   # whether current conv has been persisted
        self._thinking_bubble = None       # "正在思考..." animation bubble
        self._thinking_timer = None        # QTimer for dot animation
        self._thinking_dots = 0            # 0–5, number of dots to show
        # Persistent bubble pool — widgets are reused, never destroyed
        self._pool_user: list[QFrame] = []     # idle user-style bubbles
        self._pool_ai: list[QFrame] = []       # idle AI-style bubbles
        self._active_bubbles: list[tuple] = []  # [(bubble, role, content_label), ...] in display order
        self._setup_ui()
        self._load_index()
        self._refresh_sidebar()
        self._update_ui_state()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ==== Horizontal Splitter: sidebar | chat area ====
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(3)
        self.main_splitter.setStyleSheet(
            "QSplitter::handle { background: #313244; }"
            "QSplitter::handle:hover { background: #89b4fa; }"
        )

        # ---- Sidebar ----
        sidebar = QWidget()
        sidebar.setObjectName("aiSidebar")
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(
            "#aiSidebar { background-color: #181825; border-right: 1px solid #313244; }"
        )
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 12, 8, 12)
        sidebar_layout.setSpacing(8)

        # New conversation button
        self.new_conv_btn = QPushButton("+ 新建对话")
        self.new_conv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_conv_btn.setStyleSheet(
            "QPushButton { background: #313244; color: #cdd6f4; border: 1px solid #45475a;"
            "  border-radius: 8px; padding: 10px 14px; font-size: 13px; font-weight: bold;"
            "  text-align: left; }"
            "QPushButton:hover { background: #45475a; border-color: #89b4fa; }"
        )
        self.new_conv_btn.clicked.connect(self._new_conversation)
        sidebar_layout.addWidget(self.new_conv_btn)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("QFrame { color: #313244; max-height: 1px; }")
        sidebar_layout.addWidget(sep)

        # Conversation list
        conv_label = QLabel("历史对话")
        conv_label.setStyleSheet("color: #6c7086; font-size: 11px; font-weight: bold;"
                                 "  padding: 4px 6px; background: transparent;")
        sidebar_layout.addWidget(conv_label)

        self.conv_list = QListWidget()
        self.conv_list.setStyleSheet(
            "QListWidget { background: transparent; color: #cdd6f4; border: none;"
            "  outline: none; font-size: 12px; }"
            "QListWidget::item { padding: 8px 10px; border-radius: 6px;"
            "  margin: 1px 0; }"
            "QListWidget::item:selected { background: #313244; color: #cdd6f4; }"
            "QListWidget::item:hover { background: #1e1e2e; }"
        )
        self.conv_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.conv_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.conv_list.customContextMenuRequested.connect(self._on_conv_context_menu)
        self.conv_list.currentItemChanged.connect(self._on_conv_selected)
        sidebar_layout.addWidget(self.conv_list, 1)

        self.main_splitter.addWidget(sidebar)

        # ---- Right Side: Chat Area ----
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Chat scroll area
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_scroll.setStyleSheet(
            "QScrollArea { background-color: transparent; }"
            "QScrollArea > QWidget > QWidget { background-color: transparent; }"
        )

        self.chat_content = QWidget()
        self.chat_content.setStyleSheet("background-color: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_content)
        self.chat_layout.setContentsMargins(20, 16, 20, 16)
        self.chat_layout.setSpacing(14)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_content)

        # Input area
        input_container = QWidget()
        input_container.setObjectName("aiInputContainer")
        input_container.setStyleSheet(
            "#aiInputContainer { background-color: #1e1e2e; border-top: 1px solid #313244; }"
        )
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(16, 12, 16, 12)
        input_layout.setSpacing(8)

        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText("输入消息... (Enter 发送, Shift+Enter 换行)")
        self.input_edit.setStyleSheet(
            "QTextEdit { background: #181825; color: #cdd6f4; border: 1px solid #313244;"
            "  border-radius: 8px; padding: 10px 14px; font-size: 13px; }"
            "QTextEdit:focus { border-color: #89b4fa; }"
        )
        self.input_edit.installEventFilter(self)
        input_layout.addWidget(self.input_edit, 1)

        # Bottom bar
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(10)

        self.model_btn = QPushButton("选择模型")
        self.model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.model_btn.setStyleSheet(
            "QPushButton { background: #313244; color: #a6adc8; border: 1px solid #45475a;"
            "  border-radius: 6px; padding: 6px 14px; font-size: 12px; }"
            "QPushButton:hover { border-color: #89b4fa; color: #cdd6f4; }"
        )
        self.model_btn.clicked.connect(self._show_model_menu)
        bottom_bar.addWidget(self.model_btn)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #a6adc8; font-size: 12px; background: transparent;")
        bottom_bar.addWidget(self.status_label, 1)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setFixedHeight(32)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: #f38ba8; color: #1e1e2e; border: none;"
            "  border-radius: 6px; padding: 4px 16px; font-size: 12px; font-weight: bold; }"
            "QPushButton:hover { background: #f2cdcd; }"
        )
        self.stop_btn.clicked.connect(self._stop_generation)
        self.stop_btn.setVisible(False)
        bottom_bar.addWidget(self.stop_btn)

        self.send_btn = QPushButton("发送")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setFixedSize(80, 36)
        self.send_btn.setStyleSheet(
            "QPushButton { background: #89b4fa; color: #1e1e2e; border: none;"
            "  border-radius: 8px; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background: #b4d0fb; }"
            "QPushButton:disabled { background: #45475a; color: #6c7086; }"
        )
        self.send_btn.clicked.connect(self._send_message)
        bottom_bar.addWidget(self.send_btn)

        input_layout.addLayout(bottom_bar)

        # Vertical splitter: chat / input
        chat_splitter = QSplitter(Qt.Orientation.Vertical)
        chat_splitter.setHandleWidth(4)
        chat_splitter.setStyleSheet(
            "QSplitter::handle { background: #313244; }"
            "QSplitter::handle:hover { background: #89b4fa; }"
        )
        chat_splitter.addWidget(self.chat_scroll)
        chat_splitter.addWidget(input_container)
        chat_splitter.setStretchFactor(0, 1)
        chat_splitter.setStretchFactor(1, 0)
        chat_splitter.setSizes([500, 160])

        right_layout.addWidget(chat_splitter)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([220, 600])

        root.addWidget(self.main_splitter)

        self._refresh_model_label()

    # ── Conversation Management ──

    def _load_index(self):
        """Load the conversation index from disk."""
        try:
            os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
            if os.path.exists(CONVERSATIONS_INDEX):
                with open(CONVERSATIONS_INDEX, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._conversations = data.get("conversations", [])
                info(f"AI 加载对话索引: {len(self._conversations)} 个对话")
            else:
                self._conversations = []
        except (json.JSONDecodeError, OSError) as e:
            log_error(f"AI 加载对话索引失败: {e}")
            self._conversations = []

    def _save_index(self):
        """Save the conversation index to disk."""
        try:
            os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
            data = {"conversations": self._conversations}
            with open(CONVERSATIONS_INDEX, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log_error(f"AI 保存对话索引失败: {e}")

    def _refresh_sidebar(self):
        """Refresh the conversation list in the sidebar."""
        self.conv_list.blockSignals(True)
        self.conv_list.clear()

        # Sort by updated_at descending
        sorted_convs = sorted(
            self._conversations,
            key=lambda c: c.get("updated_at", ""),
            reverse=True,
        )

        for conv in sorted_convs:
            title = conv.get("title", "未命名对话")
            updated = conv.get("updated_at", "")
            # Format date nicely
            try:
                dt = datetime.fromisoformat(updated)
                date_str = dt.strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                date_str = ""

            item = QListWidgetItem()
            item.setText(f"{title}\n{date_str}")
            item.setData(Qt.ItemDataRole.UserRole, conv.get("id", ""))
            item.setToolTip(f"{title}\n{date_str}")

            # Highlight current conversation
            if conv.get("id") == self._current_conv_id:
                item.setSelected(True)

            self.conv_list.addItem(item)

        self.conv_list.blockSignals(False)

    def _new_conversation(self):
        """Start a new conversation. Saves current one if it has messages."""
        if self._generating:
            return

        if self._messages and self._current_conv_id:
            self._save_current_conversation()

        self.chat_scroll.setUpdatesEnabled(False)
        self._release_all_bubbles()

        self._messages = []
        self._current_conv_id = None
        self._conv_saved_on_disk = False
        self._current_ai_bubble = None
        self._current_ai_text = ""

        self.chat_scroll.setUpdatesEnabled(True)
        self._refresh_sidebar()
        self.input_edit.clear()
        self.input_edit.setFocus()
        print("[AI DEBUG] 新建对话")
        info("AI 新建对话")

    def _switch_conversation(self, conv_id):
        """Switch to a different conversation."""
        if self._generating:
            return
        if conv_id == self._current_conv_id:
            return
        if getattr(self, '_loading_conv', False):
            return  # prevent re-entrant switches

        self._loading_conv = True
        try:
            # Save current conversation first
            if self._messages and self._current_conv_id:
                self._save_current_conversation()
            elif self._messages and not self._current_conv_id:
                # Current is an unsaved new conversation — save it now
                self._create_and_save_conversation()

            # Load the selected conversation
            self._load_conversation_from_file(conv_id)
            self._refresh_sidebar()
        finally:
            self._loading_conv = False

    def _on_conv_selected(self, current, previous):
        """Handle conversation list item click."""
        if current is None:
            return
        conv_id = current.data(Qt.ItemDataRole.UserRole)
        if conv_id and conv_id != self._current_conv_id:
            self._switch_conversation(conv_id)

    def _on_conv_context_menu(self, pos):
        """Right-click context menu on conversation list."""
        item = self.conv_list.itemAt(pos)
        if not item:
            return
        conv_id = item.data(Qt.ItemDataRole.UserRole)
        if not conv_id:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #1e1e2e; color: #cdd6f4; border: 1px solid #45475a;"
            "  border-radius: 8px; padding: 4px; }"
            "QMenu::item { padding: 8px 16px; border-radius: 4px; }"
            "QMenu::item:selected { background: #313244; }"
        )
        delete_action = menu.addAction("删除对话")
        delete_action.triggered.connect(lambda: self._delete_conversation(conv_id))
        menu.exec(self.conv_list.mapToGlobal(pos))

    def _delete_conversation(self, conv_id):
        """Delete a conversation and its file."""
        conv = next((c for c in self._conversations if c.get("id") == conv_id), None)
        title = conv.get("title", "未命名") if conv else "未命名"
        reply = QMessageBox.question(
            self, "删除对话", f"确定要删除对话「{title}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Remove from index
        self._conversations = [c for c in self._conversations if c.get("id") != conv_id]
        self._save_index()

        # Delete the conversation file
        conv_file = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")
        try:
            if os.path.exists(conv_file):
                os.remove(conv_file)
        except OSError as e:
            log_warn(f"AI 删除对话文件失败: {e}")

        # If the deleted conversation is the current one, start new
        if conv_id == self._current_conv_id:
            self.chat_scroll.setUpdatesEnabled(False)
            self._release_all_bubbles()
            self._messages = []
            self._current_conv_id = None
            self._conv_saved_on_disk = False
            self.chat_scroll.setUpdatesEnabled(True)
            self.input_edit.clear()

        print(f"[AI DEBUG] 删除对话: {title}")
        info(f"AI 删除对话: {title} ({conv_id})")
        self._refresh_sidebar()

    def _create_and_save_conversation(self):
        """Create a new conversation entry and persist it. Called on first user message."""
        if not self._messages:
            return

        conv_id = uuid.uuid4().hex[:12]
        # Use first 6 chars of the first user message as title
        first_user_msg = ""
        for msg in self._messages:
            if msg.get("role") == "user":
                first_user_msg = msg.get("content", "")
                break
        title = first_user_msg[:6] if first_user_msg else "新对话"
        now = datetime.now().isoformat()

        conv_meta = {
            "id": conv_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
        }
        self._current_conv_id = conv_id
        self._conversations.insert(0, conv_meta)
        self._save_index()
        self._save_current_conversation()
        # Note: _refresh_sidebar is called by _switch_conversation after loading

        print(f"[AI DEBUG] 创建并保存对话: {title} ({conv_id})")
        info(f"AI 创建对话: title={title}, id={conv_id}")

    def _save_current_conversation(self):
        """Persist the current conversation to its file."""
        if not self._current_conv_id or not self._messages:
            return
        try:
            conv_file = os.path.join(CONVERSATIONS_DIR, f"{self._current_conv_id}.json")
            now = datetime.now().isoformat()
            data = {
                "id": self._current_conv_id,
                "title": next(
                    (c.get("title", "") for c in self._conversations
                     if c.get("id") == self._current_conv_id), ""
                ),
                "messages": self._messages,
                "created_at": next(
                    (c.get("created_at", now) for c in self._conversations
                     if c.get("id") == self._current_conv_id), now
                ),
                "updated_at": now,
            }
            with open(conv_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            # Update index timestamp
            for c in self._conversations:
                if c.get("id") == self._current_conv_id:
                    c["updated_at"] = now
                    break
            self._save_index()
            self._conv_saved_on_disk = True
        except OSError as e:
            log_error(f"AI 保存对话失败: {e}")

    def _load_conversation_from_file(self, conv_id):
        """Load a conversation — reuses persisted bubble widgets, no destroy/create."""
        conv_file = os.path.join(CONVERSATIONS_DIR, f"{conv_id}.json")

        # Freeze scroll area, release all bubbles to pool
        self.chat_scroll.setUpdatesEnabled(False)
        self._release_all_bubbles()

        self._messages = []
        self._current_conv_id = conv_id
        self._conv_saved_on_disk = True

        try:
            with open(conv_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log_error(f"AI 加载对话文件失败: {conv_id} — {e}")
            self.chat_scroll.setUpdatesEnabled(True)
            self._add_system_message(f"⚠ 对话文件无法加载\n{conv_file}\n{e}")
            self._refresh_sidebar()
            return

        # Display messages — reuses bubbles from pool
        for msg in data.get("messages", []):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content:
                self._add_message(role, content)

        self.chat_scroll.setUpdatesEnabled(True)
        self.chat_scroll.update()
        QTimer.singleShot(30, self._scroll_to_bottom)

        print(f"[AI DEBUG] 加载对话: {data.get('title', '')} ({conv_id}), {len(self._messages)} 条消息")
        info(f"AI 加载对话: id={conv_id}, messages={len(self._messages)}")

    def _add_system_message(self, text):
        """Render a system-level message (error, warning, etc.) in the chat."""
        if self.chat_layout.count() > 0:
            item = self.chat_layout.itemAt(self.chat_layout.count() - 1)
            if item.spacerItem():
                self.chat_layout.removeItem(item)

        bubble = QFrame()
        layout = QVBoxLayout(bubble)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.setContentsMargins(16, 4, 16, 4)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            "QLabel { color: #f38ba8; font-size: 12px;"
            "  background: rgba(243,139,168,0.1); border: 1px solid #f38ba8;"
            "  border-radius: 8px; padding: 8px 14px; }"
        )
        row.addWidget(label)
        row.addStretch()
        layout.addLayout(row)
        self.chat_layout.addWidget(bubble)
        self.chat_layout.addStretch()
        QTimer.singleShot(50, self._scroll_to_bottom)

    # ── Model / UI helpers ──

    def _refresh_model_label(self):
        provider = settings.get_active_provider()
        cfg = settings.get_provider_config()
        model = cfg.get("model", "")
        if model:
            self.model_btn.setText(f"{provider}: {model}")
        else:
            self.model_btn.setText("选择模型")

    def _show_model_menu(self):
        menu = ModelSelectorMenu(self)
        menu.model_selected.connect(self._on_model_changed)
        pos = self.model_btn.mapToGlobal(QPoint(0, self.model_btn.height() + 4))
        menu.exec(pos)

    def _on_model_changed(self, provider_name, model_name):
        self._refresh_model_label()
        self._update_ui_state()

    def eventFilter(self, obj, event):
        if obj is self.input_edit and event.type() == event.Type.KeyPress:
            key_event = event
            if key_event.key() == Qt.Key.Key_Return or key_event.key() == Qt.Key.Key_Enter:
                if not (key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self._send_message()
                    return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_model_label()
        self._update_ui_state()

    # ── UI State ──

    def _update_ui_state(self):
        _, api_key, model, _, _, _ = settings.get_active_ai_config()
        has_key = bool(api_key.strip())
        has_model = bool(model.strip())
        can_send = has_key and has_model and not self._generating

        self.send_btn.setEnabled(can_send)
        self.input_edit.setEnabled(not self._generating)

        if not has_key:
            self.send_btn.setToolTip("请先配置 API Key")
            self.input_edit.setPlaceholderText("请先配置 API Key 后再发送消息...")
        elif not has_model:
            self.send_btn.setToolTip("请先选择模型")
            self.input_edit.setPlaceholderText("请先选择一个模型...")
        else:
            self.send_btn.setToolTip("")
            self.input_edit.setPlaceholderText("输入消息... (Enter 发送, Shift+Enter 换行)")

    # ── Thinking Animation ──

    def _show_thinking(self):
        """Show the '正在思考...' animated bubble before the first token arrives."""
        self._hide_thinking()

        if self.chat_layout.count() > 0:
            item = self.chat_layout.itemAt(self.chat_layout.count() - 1)
            if item.spacerItem():
                self.chat_layout.removeItem(item)

        bubble = QFrame()
        bubble.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        outer = QVBoxLayout(bubble)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(8)

        ai_avatar = self._make_avatar("assistant")
        content_row.addWidget(ai_avatar, 0, Qt.AlignmentFlag.AlignTop)

        self._thinking_label = QLabel("正在思考")
        self._thinking_label.setStyleSheet(
            "QLabel { background-color: #313244; color: #cdd6f4;"
            "  border-radius: 12px; padding: 10px 16px; font-size: 13px; }"
        )
        content_row.addWidget(self._thinking_label)
        content_row.addStretch()
        outer.addLayout(content_row)

        self._thinking_bubble = bubble
        self._thinking_dots = 0
        self._update_thinking_dots()

        self.chat_layout.addWidget(bubble)
        self.chat_layout.addStretch()
        QTimer.singleShot(50, self._scroll_to_bottom)

        self._thinking_timer = QTimer(self)
        self._thinking_timer.timeout.connect(self._tick_thinking)
        self._thinking_timer.start(400)

    def _tick_thinking(self):
        """Advance the dot animation: 1→2→3→4→5→6→1…"""
        self._thinking_dots = (self._thinking_dots + 1) % 7
        if self._thinking_dots == 0:
            self._thinking_dots = 1
        self._update_thinking_dots()

    def _update_thinking_dots(self):
        """Render the current dot count on the thinking label."""
        if self._thinking_label:
            dots = "·" * self._thinking_dots
            self._thinking_label.setText(f"正在思考{dots}")

    def _hide_thinking(self):
        """Remove the thinking bubble and stop the animation timer."""
        if self._thinking_timer:
            self._thinking_timer.stop()
            self._thinking_timer = None
        if self._thinking_bubble:
            self._thinking_bubble.hide()
            self._thinking_bubble.deleteLater()
            self._thinking_bubble = None
        self._thinking_label = None

    # ── Message Sending ──

    def _send_message(self):
        if self._generating:
            return
        text = self.input_edit.toPlainText().strip()
        if not text:
            return

        api_url, api_key, model, temperature, max_tokens, system_prompt = \
            settings.get_active_ai_config()
        api_key = api_key.strip()
        api_url = api_url.strip()
        model = model.strip()
        if not api_key or not model:
            self._update_ui_state()
            if not api_key:
                self._prompt_config()
            return

        print(f"[AI DEBUG] 发送消息: {text[:60]}...")
        print(f"[AI DEBUG] API: {api_url}, 模型: {model}")
        info(f"AI 发送消息: text_len={len(text)}, model={model}, url={api_url}")

        self.input_edit.clear()

        # When the user's message contains quoted content, add a contextual hint
        if "> [" in text:
            self._add_message("user", text)
            if self._messages and self._messages[-1]["role"] == "user":
                self._messages[-1]["content"] = (
                    "请针对以下对话引用内容进行回复：\n\n" + text
                )
        else:
            self._add_message("user", text)

        # Auto-create conversation on first user message
        if self._current_conv_id is None:
            self._create_and_save_conversation()

        self._generating = True
        self._update_ui_state()

        client = AiClient(api_url, api_key, model, temperature, max_tokens)
        messages = build_messages(system_prompt, self._messages)

        self._current_ai_bubble = None
        self._current_ai_text = ""

        self._worker = AiChatWorker(client, messages)
        self._worker.token.connect(self._on_token)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.status.connect(self._on_status)
        self._worker.start()

        self._show_thinking()

    def _on_token(self, token):
        self._current_ai_text += token
        if self._current_ai_bubble is None:
            print(f"[AI DEBUG] 收到首个 token")
            info("AI 收到首个 token")
            self._hide_thinking()
            self._current_ai_bubble = self._add_message("assistant", self._current_ai_text, incomplete=True)
        else:
            self._update_bubble_text(self._current_ai_bubble, self._current_ai_text)

    def _on_finished(self):
        if self._current_ai_bubble:
            self._finalize_bubble(self._current_ai_bubble, self._current_ai_text)
        if self._current_ai_text.strip():
            self._messages.append({"role": "assistant", "content": self._current_ai_text})

        print(f"[AI DEBUG] 对话完成, AI 回复 {len(self._current_ai_text)} 字符, 总计 {len(self._messages)} 条消息")
        info(f"AI 对话完成: ai_len={len(self._current_ai_text)}, total_messages={len(self._messages)}")

        self._save_current_conversation()
        self._refresh_sidebar()
        self._generating = False
        self._current_ai_bubble = None
        self._current_ai_text = ""
        self.status_label.clear()
        self.stop_btn.setVisible(False)
        self._update_ui_state()
        self._worker = None

    def _on_error(self, err_msg):
        print(f"[AI DEBUG] 对话错误: {err_msg}")
        log_error(f"AI 对话错误: {err_msg}")
        self._hide_thinking()
        # If the AI bubble was already receiving tokens, keep the partial text
        if self._current_ai_bubble is not None and self._current_ai_text.strip():
            self._finalize_bubble(self._current_ai_bubble, self._current_ai_text)
            self._messages.append({"role": "assistant", "content": self._current_ai_text})
        elif self._current_ai_bubble is not None:
            self._discard_ai_bubble()
        self._add_error_bubble(err_msg)
        self._generating = False
        self._current_ai_bubble = None
        self._current_ai_text = ""
        self.status_label.clear()
        self.stop_btn.setVisible(False)
        self._update_ui_state()
        self._worker = None

    def _on_status(self, msg):
        print(f"[AI DEBUG] 状态: {msg}")
        self.status_label.setText(msg)
        self.stop_btn.setVisible(True)

    def _stop_generation(self):
        print(f"[AI DEBUG] 用户停止生成")
        info("AI 用户停止生成")
        self._hide_thinking()
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        self._generating = False
        if self._current_ai_bubble is not None:
            if self._current_ai_text.strip():
                self._finalize_bubble(self._current_ai_bubble, self._current_ai_text)
                self._messages.append({"role": "assistant", "content": self._current_ai_text})
            else:
                self._discard_ai_bubble()
        self._current_ai_bubble = None
        self._current_ai_text = ""
        self.status_label.clear()
        self.stop_btn.setVisible(False)
        self._update_ui_state()

    def _prompt_config(self):
        dlg = AiConfigDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_model_label()
            self._update_ui_state()

    # ═══════════════════════════════════════════════════════════════
    #  Bubble Pool — widgets persist, only content text changes
    # ═══════════════════════════════════════════════════════════════

    _avatar_cache = {}  # class-level avatar pixmap cache

    def _make_avatar(self, role):
        """Create a 32x32 rounded avatar QLabel for the given role. Uses cache."""
        if role in self._avatar_cache:
            pixmap = self._avatar_cache[role]
        else:
            filename = "user.png" if role == "user" else "AI.png"
            path = static_path(filename)
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    32, 32,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._avatar_cache[role] = pixmap

        avatar = QLabel()
        avatar.setFixedSize(32, 32)
        if not pixmap.isNull():
            avatar.setPixmap(pixmap)
        else:
            avatar.setStyleSheet(
                "QLabel { border-radius: 16px; background-color: #45475a; }"
            )
        return avatar

    # ── Bubble Pool ─────────────────────────────────────────────

    def _build_bubble(self, role: str) -> tuple:
        """Build a new bubble widget tree from scratch. Returns (bubble, label, actions)."""
        is_user = (role == "user")
        bubble = QFrame()
        bubble.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bubble.setProperty("role", role)

        outer = QVBoxLayout(bubble)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)

        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse |
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        label.setStyleSheet(self._bubble_style(is_user))

        avatar = self._make_avatar(role)

        if is_user:
            content_row.addStretch()
            content_row.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)
            content_row.addSpacing(8)
            content_row.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        else:
            content_row.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
            content_row.addSpacing(8)
            content_row.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)
            content_row.addStretch()

        outer.addLayout(content_row)

        # Action bar
        actions = QWidget()
        actions.setObjectName("bubble_actions")
        actions.setVisible(False)
        al = QHBoxLayout(actions)
        al.setContentsMargins(0, 1, 0, 0)
        al.setSpacing(4)

        if is_user:
            al.addStretch()

        copy_btn = QPushButton("复制")
        copy_btn.setFixedSize(40, 22)
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #a6adc8; border: none;"
            "  border-radius: 4px; font-size: 11px; padding: 0; }"
            "QPushButton:hover { background: #585b70; color: #cdd6f4; }")
        copy_btn.clicked.connect(lambda: self._copy_message(bubble))
        al.addWidget(copy_btn)

        quote_btn = QPushButton("引用")
        quote_btn.setFixedSize(40, 22)
        quote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quote_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #a6adc8; border: none;"
            "  border-radius: 4px; font-size: 11px; padding: 0; }"
            "QPushButton:hover { background: #585b70; color: #cdd6f4; }")
        quote_btn.clicked.connect(lambda: self._quote_message(bubble))
        al.addWidget(quote_btn)

        if is_user:
            retry_btn = QPushButton("重试")
            retry_btn.setFixedSize(40, 22)
            retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            retry_btn.setStyleSheet(
                "QPushButton { background: #45475a; color: #a6adc8; border: none;"
                "  border-radius: 4px; font-size: 11px; padding: 0; }"
                "QPushButton:hover { background: #585b70; color: #cdd6f4; }")
            retry_btn.clicked.connect(lambda: self._retry_message(bubble))
            al.addWidget(retry_btn)

        if not is_user:
            al.addStretch()

        outer.addWidget(actions)
        bubble.hide()  # start hidden until used

        return bubble, label, actions

    def _acquire_bubble(self, role: str) -> tuple:
        """Get a bubble from the matching pool, or build a new one. Returns (bubble, label, actions)."""
        pool = self._pool_user if role == "user" else self._pool_ai
        if pool:
            bubble, label, actions = pool.pop()
        else:
            bubble, label, actions = self._build_bubble(role)
        return bubble, label, actions

    def _release_all_bubbles(self):
        """Return all active bubbles to their idle pools (hide, don't destroy)."""
        for bubble, role, label, actions in self._active_bubbles:
            # Remove from layout
            self.chat_layout.removeWidget(bubble)
            bubble.hide()
            bubble.setProperty("raw_content", "")
            label.clear()
            actions.setVisible(False)
            # Return to pool
            if role == "user":
                self._pool_user.append((bubble, label, actions))
            else:
                self._pool_ai.append((bubble, label, actions))
        self._active_bubbles.clear()

    def _discard_ai_bubble(self):
        """If the current streaming AI bubble has no content, return it to pool."""
        if self._current_ai_bubble is not None:
            bubble = self._current_ai_bubble
            # Find it in active list
            for i, (b, r, l, a) in enumerate(self._active_bubbles):
                if b is bubble:
                    self.chat_layout.removeWidget(b)
                    b.hide()
                    b.setProperty("raw_content", "")
                    l.clear()
                    a.setVisible(False)
                    self._pool_ai.append((b, l, a))
                    del self._active_bubbles[i]
                    break
            self._current_ai_bubble = None

    def _insert_bubble_in_layout(self, bubble):
        """Insert a bubble before the trailing stretch."""
        idx = self.chat_layout.count() - 1  # before stretch
        if idx < 0:
            idx = 0
        self.chat_layout.insertWidget(idx, bubble)

    def _add_message(self, role, content, incomplete=False):
        """Add a message using the bubble pool. Returns the bubble frame."""
        bubble, label, actions = self._acquire_bubble(role)
        label.setText(self._render_content(content, role == "user"))
        bubble.setProperty("raw_content", content)
        actions.setVisible(not incomplete)
        bubble.show()
        self._insert_bubble_in_layout(bubble)
        self._active_bubbles.append((bubble, role, label, actions))
        if not incomplete:
            self._messages.append({"role": role, "content": content})
        return bubble

    # ── Streaming: update / finalize ────────────────────────────

    def _update_bubble_text(self, bubble, text):
        label = bubble.findChild(QLabel)
        if label:
            label.setText(self._render_content(text, False))

    def _finalize_bubble(self, bubble, text):
        label = bubble.findChild(QLabel)
        if label:
            label.setText(self._render_content(text, False))
        bubble.setProperty("raw_content", text)
        # Show action buttons
        actions = bubble.findChild(QWidget, "bubble_actions")
        if actions:
            actions.setVisible(True)
        # Scroll to latest
        QTimer.singleShot(30, self._scroll_to_bottom)

    def _render_content(self, text, is_user):
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def replace_code_block(m):
            code = m.group(2) or ""
            code = code.replace("\n", "<br>")
            code = code.replace("  ", "&nbsp;&nbsp;")
            return (
                f'<div style="background-color: #11111b; border: 1px solid #45475a;'
                f'  border-radius: 6px; padding: 10px 14px; margin: 8px 0;'
                f'  font-family: {MONO_FONT_FAMILY};'
                f'  font-size: 12px; color: #a6e3a1; white-space: pre-wrap;'
                f'  overflow-x: auto;">{code}</div>'
            )
        text = re.sub(r"```(\w*)\n?(.*?)```", replace_code_block, text, flags=re.DOTALL)

        text = re.sub(
            r"`([^`]+)`",
            '<code style="background-color: #11111b; color: #fab387;'
            f'  padding: 1px 5px; border-radius: 3px; font-family: {MONO_FONT_FAMILY};'
            '  font-size: 12px;">\\1</code>',
            text
        )

        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
        text = text.replace("\n", "<br>")

        return text

    def _add_error_bubble(self, err_msg):
        if self.chat_layout.count() > 0:
            item = self.chat_layout.itemAt(self.chat_layout.count() - 1)
            if item.spacerItem():
                self.chat_layout.removeItem(item)

        bubble = QFrame()
        bubble.setProperty("raw_content", err_msg)
        bubble.setProperty("role", "error")

        outer = QVBoxLayout(bubble)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(8)

        ai_avatar = self._make_avatar("assistant")
        content_row.addWidget(ai_avatar, 0, Qt.AlignmentFlag.AlignTop)

        label = QLabel(f'<span style="color: #f38ba8;">错误: {err_msg.replace(chr(10), "<br>")}</span>')
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setStyleSheet(
            "QLabel { background-color: #351a1a; border: 1px solid #f38ba8;"
            "  border-radius: 8px; padding: 8px 14px; font-size: 13px;"
            "  color: #f38ba8; }"
        )
        content_row.addWidget(label)
        content_row.addStretch()
        outer.addLayout(content_row)

        # Copy button for error message
        actions = QWidget()
        actions.setObjectName("bubble_actions")
        acts_layout = QHBoxLayout(actions)
        acts_layout.setContentsMargins(0, 1, 0, 0)
        acts_layout.setSpacing(4)

        copy_btn = QPushButton("复制")
        copy_btn.setFixedSize(40, 22)
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #a6adc8; border: none;"
            "  border-radius: 4px; font-size: 11px; padding: 0; }"
            "QPushButton:hover { background: #585b70; color: #cdd6f4; }"
        )
        copy_btn.clicked.connect(lambda: self._copy_message(bubble))
        acts_layout.addWidget(copy_btn)
        acts_layout.addStretch()
        outer.addWidget(actions)

        self.chat_layout.addWidget(bubble)
        self.chat_layout.addStretch()
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _copy_message(self, bubble):
        content = bubble.property("raw_content") or ""
        if content:
            QApplication.clipboard().setText(content)

    def _quote_message(self, bubble):
        content = bubble.property("raw_content") or ""
        if content:
            role = bubble.property("role") or ""
            speaker = "用户" if role == "user" else "AI"
            # Add speaker tag before the first quoted line
            first_line = content.splitlines()[0] if content else ""
            if len(first_line) > 60:
                first_line = first_line[:60] + "…"
            quoted = "\n".join(f"> {line}" for line in content.splitlines())
            header = f">> [{speaker}]: {first_line}\n"
            current = self.input_edit.toPlainText()
            if current and not current.endswith("\n"):
                current += "\n"
            self.input_edit.setText(f"{current}{header}{quoted}\n\n")
            cursor = self.input_edit.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.input_edit.setTextCursor(cursor)
            self.input_edit.setFocus()

    def _retry_message(self, bubble):
        if self._generating:
            return
        content = bubble.property("raw_content") or ""
        if content:
            self.input_edit.setText(content)
            self._send_message()

    def _bubble_style(self, is_user):
        if is_user:
            return (
                "QLabel { background-color: #89b4fa; color: #1e1e2e;"
                "  border-radius: 12px; padding: 10px 16px; font-size: 13px;"
                "  max-width: 540px; }"
            )
        else:
            return (
                "QLabel { background-color: #313244; color: #cdd6f4;"
                "  border-radius: 12px; padding: 10px 16px; font-size: 13px;"
                "  max-width: 540px; }"
            )

    def _scroll_to_bottom(self):
        sb = self.chat_scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def cleanup(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
