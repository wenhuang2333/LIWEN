import os
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QPushButton,
    QLineEdit, QLabel, QFileDialog, QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal
from core.archive_utils import is_encrypted


class DecompressCard(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("解压文件", parent)
        self._archive_path = ""
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        hint = QLabel("拖拽压缩包到此处，或点击下方按钮选择")
        hint.setObjectName("dropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        arc_row = QHBoxLayout()
        arc_row.addWidget(QLabel("压缩包:"))
        self.archive_edit = QLineEdit()
        self.archive_edit.setReadOnly(True)
        self.archive_edit.setPlaceholderText("选择压缩包文件...")
        arc_row.addWidget(self.archive_edit)
        browse_btn = QPushButton("选择压缩包")
        browse_btn.clicked.connect(self._browse_archive)
        arc_row.addWidget(browse_btn)
        layout.addLayout(arc_row)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("解压到:"))
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("默认为压缩包所在目录")
        dest_row.addWidget(self.dest_edit)
        dest_browse_btn = QPushButton("选择目标")
        dest_browse_btn.clicked.connect(self._browse_dest)
        dest_row.addWidget(dest_browse_btn)
        layout.addLayout(dest_row)

        pwd_row = QHBoxLayout()
        pwd_row.addWidget(QLabel("密  码:"))
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("压缩包有密码时填写")
        pwd_row.addWidget(self.password_edit)

        self.pwd_hint = QLabel("")
        self.pwd_hint.setStyleSheet("color: #fab387; font-size: 11px;")
        self.pwd_hint.setVisible(False)
        pwd_row.addWidget(self.pwd_hint)
        layout.addLayout(pwd_row)

        self.decompress_btn = QPushButton("开始解压")
        self.decompress_btn.setMinimumHeight(36)
        self.decompress_btn.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.decompress_btn)

    def _browse_archive(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "选择压缩包", "",
            "压缩文件 (*.zip *.7z *.rar *.tar *.gz);;所有文件 (*.*)"
        )
        if filepath:
            self.set_archive(filepath)

    def _browse_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "选择解压目标文件夹")
        if folder:
            self.dest_edit.setText(folder)

    def set_archive(self, filepath):
        self._archive_path = filepath
        self.archive_edit.setText(filepath)
        if not self.dest_edit.text():
            dest = os.path.dirname(filepath)
            self.dest_edit.setPlaceholderText(f"默认: {dest}")
        if is_encrypted(filepath):
            self.pwd_hint.setText("检测到加密压缩包，需要密码")
            self.pwd_hint.setVisible(True)
        else:
            self.pwd_hint.setVisible(False)

    def get_archive_path(self):
        return self._archive_path

    def get_dest_dir(self):
        text = self.dest_edit.text()
        if text:
            return text
        if self._archive_path:
            return os.path.dirname(self._archive_path)
        return ""

    def get_password(self):
        return self.password_edit.text() or None

    def has_archive(self):
        return bool(self._archive_path)
