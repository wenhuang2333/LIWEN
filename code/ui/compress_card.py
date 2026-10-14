import os
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
    QLineEdit, QComboBox, QLabel, QFileDialog, QCheckBox, QSpinBox,
    QAbstractItemView, QWidget, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal
from utils.helpers import get_output_archive_name


class CompressCard(QGroupBox):
    files_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("压缩文件", parent)
        self._files = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        hint = QLabel("拖拽文件/文件夹到此处，或点击下方按钮添加")
        hint.setObjectName("dropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setMaximumHeight(120)
        self.file_list.setDragEnabled(False)
        layout.addWidget(self.file_list)

        btn_row = QHBoxLayout()
        add_files_btn = QPushButton("添加文件")
        add_files_btn.clicked.connect(self._add_files)
        add_folder_btn = QPushButton("添加文件夹")
        add_folder_btn.setObjectName("secondaryBtn")
        add_folder_btn.clicked.connect(self._add_folder)
        remove_btn = QPushButton("移除选中")
        remove_btn.setObjectName("dangerBtn")
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("secondaryBtn")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(add_files_btn)
        btn_row.addWidget(add_folder_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("压缩格式:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["zip", "7z"])
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        fmt_row.addWidget(self.format_combo)
        fmt_row.addStretch()

        self.volume_check = QCheckBox("分卷压缩")
        self.volume_check.setVisible(False)
        self.volume_check.toggled.connect(self._on_volume_toggled)
        fmt_row.addWidget(self.volume_check)

        self.volume_spin = QSpinBox()
        self.volume_spin.setRange(1, 4096)
        self.volume_spin.setSuffix(" MB")
        self.volume_spin.setValue(100)
        self.volume_spin.setVisible(False)
        fmt_row.addWidget(self.volume_spin)
        layout.addLayout(fmt_row)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("输出路径:"))
        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("选择输出文件路径...")
        out_row.addWidget(self.output_edit)
        browse_btn = QPushButton("浏览")
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(browse_btn)
        layout.addLayout(out_row)

        pwd_row = QHBoxLayout()
        pwd_row.addWidget(QLabel("密  码:"))
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("留空则不加密")
        pwd_row.addWidget(self.password_edit)
        layout.addLayout(pwd_row)

        self.compress_btn = QPushButton("开始压缩")
        self.compress_btn.setMinimumHeight(36)
        self.compress_btn.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.compress_btn)

    def _on_format_changed(self, fmt):
        self.volume_check.setVisible(fmt == "7z")
        if fmt != "7z":
            self.volume_check.setChecked(False)
            self.volume_spin.setVisible(False)

    def _on_volume_toggled(self, checked):
        self.volume_spin.setVisible(checked)

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择文件")
        for f in files:
            if f not in self._files:
                self._files.append(f)
                self.file_list.addItem(f)
        self._update_output_name()
        self.files_changed.emit()

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder and folder not in self._files:
            self._files.append(folder)
            self.file_list.addItem(folder)
            self._update_output_name()
            self.files_changed.emit()

    def _remove_selected(self):
        for item in self.file_list.selectedItems():
            self._files.remove(item.text())
            self.file_list.takeItem(self.file_list.row(item))
        self._update_output_name()
        self.files_changed.emit()

    def _clear(self):
        self._files.clear()
        self.file_list.clear()
        self.output_edit.clear()
        self.files_changed.emit()

    def _browse_output(self):
        fmt = self.format_combo.currentText()
        default_name = get_output_archive_name(self._files, fmt)
        filepath, _ = QFileDialog.getSaveFileName(
            self, "保存压缩包", default_name,
            f"{fmt.upper()} 文件 (*.{fmt})"
        )
        if filepath:
            self.output_edit.setText(filepath)

    def _update_output_name(self):
        if self._files:
            fmt = self.format_combo.currentText()
            default_name = get_output_archive_name(self._files, fmt)
            if not self.output_edit.text():
                self.output_edit.setPlaceholderText(f"默认: {default_name}")

    def add_files(self, paths):
        for p in paths:
            if p not in self._files:
                self._files.append(p)
                self.file_list.addItem(p)
        self._update_output_name()
        self.files_changed.emit()

    def get_files(self):
        return list(self._files)

    def get_output_path(self):
        return self.output_edit.text()

    def get_format(self):
        return self.format_combo.currentText()

    def get_password(self):
        return self.password_edit.text() or None

    def get_volume_size(self):
        if self.volume_check.isChecked() and self.format_combo.currentText() == "7z":
            return self.volume_spin.value() * 1024 * 1024
        return None

    def has_files(self):
        return len(self._files) > 0
