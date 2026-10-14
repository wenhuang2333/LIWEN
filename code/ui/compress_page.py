import hashlib
import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
    QLineEdit, QComboBox, QLabel, QFileDialog, QCheckBox, QSpinBox,
    QAbstractItemView, QListWidgetItem, QSlider, QMessageBox,
)
from PyQt6.QtCore import Qt, pyqtSignal

from utils.helpers import is_archive_file, get_output_archive_name
from core.formats import FormatRegistry

# Map internal format names to user-facing display names
_FORMAT_DISPLAY = {
    "zip": "ZIP",
    "7z": "7Z",
    "tar": "TAR",
    "tar.gz": "TAR.GZ",
    "tar.bz2": "TAR.BZ2",
    "custom": "自定义格式",
}
_FORMAT_INTERNAL = {v: k for k, v in _FORMAT_DISPLAY.items()}


def _display_name(internal: str) -> str:
    return _FORMAT_DISPLAY.get(internal, internal.upper())


def _internal_name(display: str) -> str:
    return _FORMAT_INTERNAL.get(display, display)


class CompressPage(QWidget):
    compress_requested = pyqtSignal(list, str, str, str, object, str, int)
    decompress_requested = pyqtSignal(str, str, str, str)

    MODE_COMPRESS = 0
    MODE_DECOMPRESS = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._file_settings = {}   # filepath -> {fmt, output, password, volume}
        self._archive_path = ""
        self._mode = self.MODE_COMPRESS
        self._current_file = None
        self._suppress_change = False
        self._setup_ui()
        self._set_mode(self.MODE_COMPRESS)

    @property
    def _files(self):
        return list(self._file_settings.keys())

    def _default_settings(self):
        return {"fmt": "zip", "output": "", "password": "", "volume": 0,
                "seed_code": "", "compress_level": 0}

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # Top row: mode toggle + preview button
        top_row = QHBoxLayout()

        mode_widget = QWidget()
        mode_widget.setObjectName("modeToggle")
        mode_layout = QHBoxLayout(mode_widget)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(0)

        self.btn_compress = QPushButton(" 压缩")
        self.btn_compress.setCheckable(True)
        self.btn_compress.clicked.connect(lambda: self._set_mode(self.MODE_COMPRESS))
        self.btn_decompress = QPushButton(" 解压")
        self.btn_decompress.setCheckable(True)
        self.btn_decompress.clicked.connect(lambda: self._set_mode(self.MODE_DECOMPRESS))

        mode_layout.addWidget(self.btn_compress)
        mode_layout.addWidget(self.btn_decompress)
        top_row.addWidget(mode_widget)
        top_row.addStretch()

        self.preview_btn = QPushButton("预览压缩包内容")
        self.preview_btn.setObjectName("secondaryBtn")
        top_row.addWidget(self.preview_btn)

        root.addLayout(top_row)

        # Main content: left (drop zone + list) | right (settings)
        content_row = QHBoxLayout()
        content_row.setSpacing(16)

        # === LEFT SIDE ===
        left = QVBoxLayout()
        left.setSpacing(8)

        self.drop_zone = QWidget()
        self.drop_zone.setObjectName("dropZone")
        dz_layout = QVBoxLayout(self.drop_zone)
        dz_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.drop_hint = QLabel("拖拽文件或文件夹到此处")
        self.drop_hint.setObjectName("dropZoneHint")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz_layout.addWidget(self.drop_hint)

        self.drop_sub = QLabel("或点击下方按钮添加")
        self.drop_sub.setObjectName("pageHint")
        self.drop_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz_layout.addWidget(self.drop_sub)

        left.addWidget(self.drop_zone, 1)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setMaximumHeight(140)
        self.file_list.itemSelectionChanged.connect(self._on_file_selection_changed)
        left.addWidget(self.file_list)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("添加文件")
        add_btn.clicked.connect(self._add_files)
        add_folder_btn = QPushButton("添加文件夹")
        add_folder_btn.setObjectName("secondaryBtn")
        add_folder_btn.clicked.connect(self._add_folder)
        remove_btn = QPushButton("移除")
        remove_btn.setObjectName("dangerBtn")
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("smallBtn")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(add_folder_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        left.addLayout(btn_row)

        content_row.addLayout(left, 2)

        # === RIGHT SIDE ===
        right = QWidget()
        right.setObjectName("settingsPanel")
        right.setFixedWidth(280)
        right_layout = QVBoxLayout(right)
        right_layout.setSpacing(12)

        self.settings_title = QLabel("压缩设置")
        self.settings_title.setObjectName("sectionTitle")
        right_layout.addWidget(self.settings_title)

        # -- Selected file indicator --
        self.selected_file_label = QLabel("")
        self.selected_file_label.setObjectName("fileSelectedHint")
        self.selected_file_label.setWordWrap(True)
        right_layout.addWidget(self.selected_file_label)

        # -- compress-specific widgets --
        self._compress_widgets = []

        fmt_label = QLabel("压缩格式")
        right_layout.addWidget(fmt_label)
        self._compress_widgets.append(fmt_label)

        self.format_combo = QComboBox()
        self.format_combo.addItems([_display_name(n) for n in FormatRegistry.compressible_formats()])
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        right_layout.addWidget(self.format_combo)
        self._compress_widgets.append(self.format_combo)

        # — custom format widgets (hidden unless "自定义格式" is selected) —
        self._custom_widgets = []

        self.seed_code_label = QLabel("自定义种子码 (ASCII字符串，任意长度)")
        right_layout.addWidget(self.seed_code_label)
        self._compress_widgets.append(self.seed_code_label)
        self._custom_widgets.append(self.seed_code_label)

        self.seed_code_edit = QLineEdit()
        self.seed_code_edit.setPlaceholderText("输入ASCII字符串, 如: myKey2024")
        self.seed_code_edit.textChanged.connect(self._on_seed_code_changed)
        right_layout.addWidget(self.seed_code_edit)
        self._compress_widgets.append(self.seed_code_edit)
        self._custom_widgets.append(self.seed_code_edit)

        self.seed_code_warning = QLabel("")
        self.seed_code_warning.setObjectName("pwdHint")
        self.seed_code_warning.setWordWrap(True)
        right_layout.addWidget(self.seed_code_warning)
        self._compress_widgets.append(self.seed_code_warning)
        self._custom_widgets.append(self.seed_code_warning)

        self.seed_code_preview = QLabel("")
        self.seed_code_preview.setStyleSheet("color: #a6e3a1; font-size: 11px;")
        self.seed_code_preview.setWordWrap(True)
        right_layout.addWidget(self.seed_code_preview)
        self._compress_widgets.append(self.seed_code_preview)
        self._custom_widgets.append(self.seed_code_preview)

        out_label = QLabel("输出路径")
        right_layout.addWidget(out_label)
        self._compress_widgets.append(out_label)

        out_row = QHBoxLayout()
        out_row.setSpacing(4)
        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("自动生成...")
        out_row.addWidget(self.output_edit)
        browse_btn = QPushButton("...")
        browse_btn.setObjectName("secondaryBtn")
        browse_btn.setFixedWidth(36)
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(browse_btn)
        right_layout.addLayout(out_row)
        self._compress_widgets.append(self.output_edit)
        self._compress_widgets.append(browse_btn)

        pwd_label = QLabel("密码（可选）")
        right_layout.addWidget(pwd_label)
        self._compress_widgets.append(pwd_label)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("留空则不加密码")
        right_layout.addWidget(self.password_edit)
        self._compress_widgets.append(self.password_edit)

        vol_formats = FormatRegistry.volume_formats()
        vol_label = f"分卷压缩 (仅 {', '.join(vol_formats).upper()})" if vol_formats else "分卷压缩"
        self.volume_check = QCheckBox(vol_label)
        self.volume_check.setVisible(False)
        self.volume_check.toggled.connect(lambda v: self.volume_spin.setVisible(v))
        right_layout.addWidget(self.volume_check)
        self._compress_widgets.append(self.volume_check)

        self.volume_spin = QSpinBox()
        self.volume_spin.setRange(1, 4096)
        self.volume_spin.setSuffix(" MB")
        self.volume_spin.setValue(100)
        self.volume_spin.setVisible(False)
        right_layout.addWidget(self.volume_spin)
        self._compress_widgets.append(self.volume_spin)

        # — compression level slider —
        level_label = QLabel("压缩刻度")
        right_layout.addWidget(level_label)
        self._compress_widgets.append(level_label)

        level_row = QHBoxLayout()
        self._level_slider = QSlider(Qt.Orientation.Horizontal)
        self._level_slider.setRange(0, 10)
        self._level_slider.setValue(0)
        self._level_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._level_slider.setTickInterval(1)
        self._level_slider.valueChanged.connect(self._on_level_changed)
        level_row.addWidget(self._level_slider)

        self._level_value_label = QLabel("0")
        self._level_value_label.setFixedWidth(24)
        self._level_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        level_row.addWidget(self._level_value_label)
        right_layout.addLayout(level_row)
        self._compress_widgets.append(self._level_slider)
        self._compress_widgets.append(self._level_value_label)

        self._level_hint_label = QLabel("0 = 通用压缩")
        self._level_hint_label.setStyleSheet("color: #9498a4; font-size: 10px;")
        right_layout.addWidget(self._level_hint_label)
        self._compress_widgets.append(self._level_hint_label)

        self._level_warning_label = QLabel("")
        self._level_warning_label.setWordWrap(True)
        self._level_warning_label.setVisible(False)
        right_layout.addWidget(self._level_warning_label)
        self._compress_widgets.append(self._level_warning_label)

        # -- decompress-specific widgets --
        self._decompress_widgets = []

        d_label = QLabel("解压到")
        right_layout.addWidget(d_label)
        self._decompress_widgets.append(d_label)

        d_row = QHBoxLayout()
        d_row.setSpacing(4)
        self.decompress_dest_edit = QLineEdit()
        self.decompress_dest_edit.setReadOnly(True)
        self.decompress_dest_edit.setPlaceholderText("压缩包所在目录...")
        d_row.addWidget(self.decompress_dest_edit)
        d_browse = QPushButton("...")
        d_browse.setObjectName("secondaryBtn")
        d_browse.setFixedWidth(36)
        d_browse.clicked.connect(self._browse_dest)
        d_row.addWidget(d_browse)
        right_layout.addLayout(d_row)
        self._decompress_widgets.append(self.decompress_dest_edit)
        self._decompress_widgets.append(d_browse)

        dp_label = QLabel("密码")
        right_layout.addWidget(dp_label)
        self._decompress_widgets.append(dp_label)

        self.decompress_pwd_edit = QLineEdit()
        self.decompress_pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.decompress_pwd_edit.setPlaceholderText("压缩包有密码时填写")
        right_layout.addWidget(self.decompress_pwd_edit)
        self._decompress_widgets.append(self.decompress_pwd_edit)

        self.decompress_pwd_hint = QLabel("")
        self.decompress_pwd_hint.setObjectName("pwdHint")
        right_layout.addWidget(self.decompress_pwd_hint)
        self._decompress_widgets.append(self.decompress_pwd_hint)

        # — seed code input for decompressing custom V2 archives —
        self.decompress_seed_label = QLabel("种子码（自定义格式需要）")
        right_layout.addWidget(self.decompress_seed_label)
        self._decompress_widgets.append(self.decompress_seed_label)

        self._decompress_seed_edit = QLineEdit()
        self._decompress_seed_edit.setPlaceholderText("输入创建时的种子码以验证")
        right_layout.addWidget(self._decompress_seed_edit)
        self._decompress_widgets.append(self._decompress_seed_edit)

        self.decompress_seed_hint = QLabel("")
        self.decompress_seed_hint.setObjectName("pwdHint")
        self.decompress_seed_hint.setWordWrap(True)
        right_layout.addWidget(self.decompress_seed_hint)
        self._decompress_widgets.append(self.decompress_seed_hint)

        self.origin_label = QLabel("")
        self.origin_label.setWordWrap(True)
        self.origin_label.setVisible(False)
        right_layout.addWidget(self.origin_label)
        self._decompress_widgets.append(self.origin_label)

        right_layout.addStretch()

        self.action_btn = QPushButton("开始压缩")
        self.action_btn.setObjectName("actionBtn")
        self.action_btn.setEnabled(False)
        self.action_btn.clicked.connect(self._on_action)
        right_layout.addWidget(self.action_btn)

        content_row.addWidget(right)
        root.addLayout(content_row)

    # ---- settings management ----

    def _save_current_settings(self):
        if self._mode != self.MODE_COMPRESS:
            return
        if self._current_file and self._current_file in self._file_settings:
            fmt_display = self.format_combo.currentText()
            self._file_settings[self._current_file] = {
                "fmt": _internal_name(fmt_display),
                "output": self.output_edit.text(),
                "password": self.password_edit.text(),
                "volume": self.volume_spin.value() * 1024 * 1024 if self.volume_check.isChecked() else 0,
                "seed_code": self.seed_code_edit.text().strip(),
                "compress_level": self._level_slider.value(),
            }

    def _load_settings_for(self, filepath):
        self._suppress_change = True
        if filepath and filepath in self._file_settings:
            s = self._file_settings[filepath]
            display = _display_name(s.get("fmt", "zip"))
            idx = self.format_combo.findText(display)
            if idx >= 0:
                self.format_combo.setCurrentIndex(idx)
            self.output_edit.setText(s.get("output", ""))
            internal = s.get("fmt", "zip")
            if not self.output_edit.text():
                self.output_edit.setPlaceholderText(get_output_archive_name([filepath], internal))
            self.password_edit.setText(s.get("password", ""))
            vol = s.get("volume", 0)
            if vol > 0:
                self.volume_check.setChecked(True)
                self.volume_spin.setValue(vol // (1024 * 1024))
            else:
                self.volume_check.setChecked(False)
                self.volume_spin.setValue(100)
            self.seed_code_edit.setText(s.get("seed_code", ""))
            lv = s.get("compress_level", 0)
            self._level_slider.setValue(lv)
            self._level_value_label.setText(str(lv))
            self._current_file = filepath
            fname = os.path.basename(filepath)
            self.selected_file_label.setText(f"当前: {fname}")
        else:
            self._reset_to_defaults()
            self._current_file = None
            self.selected_file_label.setText("")
        self._suppress_change = False

    def _reset_to_defaults(self):
        self.format_combo.setCurrentIndex(0)
        self.output_edit.clear()
        self.output_edit.setPlaceholderText("自动生成...")
        self.password_edit.clear()
        self.volume_check.setChecked(False)
        self.volume_spin.setValue(100)
        self.seed_code_edit.clear()
        self.seed_code_warning.setText("")
        self.seed_code_preview.setText("")
        self._level_slider.setValue(0)
        self._level_value_label.setText("0")
        self._level_hint_label.setText("0 = 通用压缩")
        self._level_warning_label.setVisible(False)

    def _on_file_selection_changed(self):
        if self._mode != self.MODE_COMPRESS:
            return
        self._save_current_settings()
        selected = self.file_list.selectedItems()
        if selected:
            fp = selected[0].text()
            if fp in self._file_settings:
                self._load_settings_for(fp)
        else:
            self._reset_to_defaults()
            self._current_file = None
            self.selected_file_label.setText("")

    # ---- mode toggle ----

    def _on_format_changed(self, display_name):
        internal = _internal_name(display_name)
        if self._mode == self.MODE_COMPRESS:
            handler = FormatRegistry.get_by_name(internal)
            supports_vol = handler.supports_volumes() if handler else False
            self.volume_check.setVisible(supports_vol)
            if not supports_vol:
                self.volume_check.setChecked(False)
            # Show/hide custom format widgets
            is_custom = (internal == "custom")
            for w in self._custom_widgets:
                w.setVisible(is_custom)
            if not is_custom:
                self.seed_code_edit.clear()
                self.seed_code_warning.setText("")
                self.seed_code_preview.setText("")
            if not self._suppress_change and self._current_file:
                self._save_current_settings()
            self._on_level_changed(self._level_slider.value())

    def _on_seed_code_changed(self, text):
        """Validate ASCII and show preview when user types a seed code."""
        if not text:
            self.seed_code_warning.setText("")
            self.seed_code_preview.setText("")
            return
        non_ascii = [c for c in text if ord(c) > 127]
        if non_ascii:
            shown = "".join(non_ascii[:3])
            self.seed_code_warning.setText(
                f"⚠ 包含非ASCII字符: {shown}，请只使用ASCII字符"
            )
            self.seed_code_warning.setStyleSheet("color: #fab387; font-size: 11px;")
        else:
            short = hashlib.sha256(f"magic:{text}".encode()).digest()[:4].hex()
            self.seed_code_warning.setText(
                f"✓ {len(text)}个ASCII字符 — 派生魔数: {short}"
            )
            self.seed_code_warning.setStyleSheet("color: #a6e3a1; font-size: 11px;")
        if self._current_file:
            base = Path(self._current_file).stem
        else:
            base = "archive"
        self.seed_code_preview.setText(f"压缩包: {base}.zhans")

    def _on_level_changed(self, value):
        self._level_value_label.setText(str(value))
        is_custom = _internal_name(self.format_combo.currentText()) == "custom"
        if value == 0:
            self._level_hint_label.setText("0 = 通用压缩")
            self._level_warning_label.setVisible(False)
        elif value <= 3:
            self._level_hint_label.setText(f"{value} = 轻度专有压缩")
        elif value <= 6:
            self._level_hint_label.setText(f"{value} = 中度专有压缩")
        else:
            self._level_hint_label.setText(f"{value} = 极限专有压缩")
        if value > 0 and not is_custom:
            self._level_warning_label.setText(
                "⚠ 此级别使用本程序专有压缩技术，压缩包仅本程序可解压")
            self._level_warning_label.setStyleSheet(
                "color: #fab387; font-size: 10px; padding: 2px 0;")
            self._level_warning_label.setVisible(True)
        elif value > 0 and is_custom:
            self._level_warning_label.setText(
                "自定义格式始终使用专有技术，刻度控制压缩激进程度")
            self._level_warning_label.setStyleSheet(
                "color: #89b4fa; font-size: 10px; padding: 2px 0;")
            self._level_warning_label.setVisible(True)
        else:
            self._level_warning_label.setVisible(False)

    def _set_mode(self, mode):
        self._save_current_settings()
        self._mode = mode
        self.btn_compress.setChecked(mode == self.MODE_COMPRESS)
        self.btn_decompress.setChecked(mode == self.MODE_DECOMPRESS)

        is_compress = mode == self.MODE_COMPRESS
        self.settings_title.setText("压缩设置" if is_compress else "解压设置")
        self.action_btn.setText("开始压缩" if is_compress else "开始解压")
        self.drop_hint.setText(
            "拖拽文件或文件夹到此处" if is_compress else "拖拽压缩包到此处"
        )
        self.selected_file_label.setVisible(is_compress)

        for w in self._compress_widgets:
            w.setVisible(is_compress)
        for w in self._decompress_widgets:
            w.setVisible(not is_compress)

        if is_compress:
            self._on_format_changed(self.format_combo.currentText())

        self._clear()

    # ---- file management ----

    def _add_files(self):
        if self._mode == self.MODE_DECOMPRESS:
            filepath, _ = QFileDialog.getOpenFileName(
                self, "选择压缩包", "",
                FormatRegistry.file_filter_string()
            )
            if filepath:
                self._set_archive(filepath)
                self._update_action_btn()
            return

        files, _ = QFileDialog.getOpenFileNames(self, "选择文件")
        for f in files:
            if f not in self._file_settings:
                self._file_settings[f] = self._default_settings()
                item = QListWidgetItem(f)
                self.file_list.addItem(item)
                # auto-select the newly added file
                self.file_list.setCurrentItem(item)
        self._update_action_btn()

    def _add_folder(self):
        if self._mode == self.MODE_DECOMPRESS:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder and folder not in self._file_settings:
            self._file_settings[folder] = self._default_settings()
            item = QListWidgetItem(folder)
            self.file_list.addItem(item)
            self.file_list.setCurrentItem(item)
            self._update_action_btn()

    def _remove_selected(self):
        self._save_current_settings()
        for item in self.file_list.selectedItems():
            fp = item.text()
            if fp in self._file_settings:
                del self._file_settings[fp]
            self.file_list.takeItem(self.file_list.row(item))
        self._current_file = None
        self._reset_to_defaults()
        self.selected_file_label.setText("")
        self._update_action_btn()

    def _clear(self):
        self._file_settings.clear()
        self._archive_path = ""
        self.file_list.clear()
        self._current_file = None
        self._reset_to_defaults()
        self.output_edit.clear()
        self.decompress_dest_edit.clear()
        self.decompress_pwd_hint.setVisible(False)
        self.decompress_seed_label.setVisible(False)
        self._decompress_seed_edit.setVisible(False)
        self._decompress_seed_edit.clear()
        self.decompress_seed_hint.setVisible(False)
        self.origin_label.setVisible(False)
        self.selected_file_label.setText("")
        self._update_action_btn()

    def _browse_output(self):
        self._save_current_settings()
        cur = self._current_file
        s = self._file_settings.get(cur, self._default_settings()) if cur else self._default_settings()
        fmt = s.get("fmt", _internal_name(self.format_combo.currentText()))
        if fmt == "custom":
            ext = "zhans"
            filter_label = "自定义格式 (*.zhans)"
        else:
            handler = FormatRegistry.get_by_name(fmt)
            ext = handler.extensions()[0].lstrip(".") if handler and handler.extensions() else fmt
            filter_label = f"{fmt.upper()} 文件 (*.{ext})"
        name = get_output_archive_name([cur], fmt) if cur else f"archive.{ext}"
        filepath, _ = QFileDialog.getSaveFileName(
            self, "保存压缩包", name, filter_label
        )
        if filepath:
            self.output_edit.setText(filepath)
            if cur and cur in self._file_settings:
                self._file_settings[cur]["output"] = filepath

    def _browse_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "选择解压目标文件夹")
        if folder:
            self.decompress_dest_edit.setText(folder)

    def _set_archive(self, filepath):
        self._archive_path = filepath
        self.file_list.clear()
        self.file_list.addItem(filepath)
        # Reset decompress seed fields
        self.decompress_seed_label.setVisible(False)
        self._decompress_seed_edit.setVisible(False)
        self._decompress_seed_edit.clear()
        self.decompress_seed_hint.setVisible(False)
        try:
            handler = FormatRegistry.detect(filepath)
            if handler:
                if handler.is_encrypted(filepath):
                    self.decompress_pwd_hint.setText("需要密码")
                    self.decompress_pwd_hint.setVisible(True)
                else:
                    self.decompress_pwd_hint.setVisible(False)

                fmt_name = handler.format_name()
                if fmt_name == "custom":
                    # Enhanced origin detection via get_origin_info()
                    origin = handler.get_origin_info(filepath)
                    has_seed = origin.get("has_seed", False)
                    version = origin.get("version", 1)

                    if origin.get("from_this_app"):
                        ulv = origin.get("user_compress_level", 0)
                        lv_note = f", 压缩 Lv.{ulv}" if ulv > 0 else ""
                        self.origin_label.setText(
                            f"✓ 本程序同源压缩包 ({origin['format_display']}{lv_note})")
                        self.origin_label.setStyleSheet(
                            "color: #a6e3a1; font-weight: bold; font-size: 12px; padding-top: 4px;")
                        if has_seed:
                            self.decompress_seed_label.setVisible(True)
                            self._decompress_seed_edit.setVisible(True)
                            self.decompress_seed_hint.setText(
                                f"此压缩包使用了种子码 (V{version})，请输入以验证")
                            self.decompress_seed_hint.setStyleSheet(
                                "color: #89b4fa; font-size: 11px;")
                            self.decompress_seed_hint.setVisible(True)
                    else:
                        self.origin_label.setText(
                            f"✗ {origin.get('format_display', '无法识别')}")
                        self.origin_label.setStyleSheet(
                            "color: #f38ba8; font-weight: bold; font-size: 12px; padding-top: 4px;")
                    self.origin_label.setVisible(True)

                elif fmt_name == "shishan":
                    self.origin_label.setText("✓ 本程序同源压缩包 (石山格式)")
                    self.origin_label.setStyleSheet(
                        "color: #a6e3a1; font-weight: bold; font-size: 12px; padding-top: 4px;")
                    self.origin_label.setVisible(True)

                else:
                    self.origin_label.setText("✗ 非本程序压缩包 (第三方格式，可解压)")
                    self.origin_label.setStyleSheet(
                        "color: #fab387; font-weight: bold; font-size: 12px; padding-top: 4px;")
                    self.origin_label.setVisible(True)
            else:
                self.origin_label.setText("✗ 无法识别的文件格式 — 可能无法解压")
                self.origin_label.setStyleSheet(
                    "color: #f38ba8; font-weight: bold; font-size: 12px; padding-top: 4px;")
                self.origin_label.setVisible(True)
                self.decompress_pwd_hint.setVisible(False)
        except Exception:
            pass
        self._file_settings.clear()
        self._update_action_btn()

    # ---- action ----

    def _on_action(self):
        if self._mode == self.MODE_COMPRESS:
            self._save_current_settings()
            if not self._file_settings:
                return
            for fp, settings in self._file_settings.items():
                fmt = settings["fmt"]
                seed_code = settings.get("seed_code", "").strip()
                if fmt == "custom" and not seed_code:
                    QMessageBox.warning(self, "提示", "请先输入自定义种子码")
                    return
                output = settings["output"]
                if not output:
                    output = os.path.join(
                        os.path.expanduser("~"), "Desktop",
                        get_output_archive_name([fp], fmt)
                    )
                pw = settings.get("password") or None
                vol = settings.get("volume", 0)
                compress_lv = settings.get("compress_level", 0)
                self.compress_requested.emit(
                    [fp], output, fmt, pw or "", vol, seed_code, compress_lv)
        else:
            dest = self.decompress_dest_edit.text()
            if not dest and self._archive_path:
                dest = os.path.dirname(self._archive_path)
            pw = self.decompress_pwd_edit.text() or None
            seed_code = getattr(self, "_decompress_seed_edit", None)
            seed_code = seed_code.text().strip() if seed_code else ""
            self.decompress_requested.emit(self._archive_path, dest or "", pw or "", seed_code)

    def handle_drop(self, paths):
        if self._mode == self.MODE_DECOMPRESS:
            archives = [p for p in paths if is_archive_file(p)]
            if archives:
                self._set_archive(archives[0])
            return

        for p in paths:
            if not is_archive_file(p) and p not in self._file_settings:
                self._file_settings[p] = self._default_settings()
                item = QListWidgetItem(p)
                self.file_list.addItem(item)
                self.file_list.setCurrentItem(item)
        self._update_action_btn()

    def set_busy(self, busy):
        if busy:
            self.action_btn.setEnabled(False)
        else:
            self._update_action_btn()
        self.btn_compress.setEnabled(not busy)
        self.btn_decompress.setEnabled(not busy)

    def _update_action_btn(self):
        """Enable action button only when there are items to process."""
        if self._mode == self.MODE_COMPRESS:
            self.action_btn.setEnabled(len(self._file_settings) > 0)
        else:
            self.action_btn.setEnabled(bool(self._archive_path))
