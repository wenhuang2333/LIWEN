import os
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QFileDialog, QMessageBox,
    QAbstractItemView,
)
from PyQt6.QtCore import Qt
from core.formats import FormatRegistry
from utils.helpers import format_size


class ArchivePreviewDialog(QDialog):
    def __init__(self, archive_path, parent=None):
        super().__init__(parent)
        self.archive_path = archive_path
        self._contents = []
        self._handler = FormatRegistry.detect(archive_path)
        self.setWindowTitle(f"预览: {os.path.basename(archive_path)}")
        self.resize(700, 450)
        self.setMinimumSize(500, 300)
        self._setup_ui()
        self._load_contents()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(f"压缩包: {self.archive_path}")
        info.setStyleSheet("color: #c8ccd4; font-size: 12px;")
        layout.addWidget(info)

        encrypted = self._handler.is_encrypted(self.archive_path) if self._handler else False
        if encrypted:
            hint = QLabel("此压缩包已加密，预览可能受限")
            hint.setStyleSheet("color: #fab387; font-size: 12px;")
            layout.addWidget(hint)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["文件名", "大小", "类型"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        extract_btn = QPushButton("解压选中文件")
        extract_btn.clicked.connect(self._extract_selected)
        extract_all_btn = QPushButton("解压全部")
        extract_all_btn.clicked.connect(self._extract_all)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("secondaryBtn")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(extract_btn)
        btn_row.addWidget(extract_all_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _load_contents(self):
        # list_contents() 现在会为损坏的压缩包抛出清晰的 ValueError（旧实现
        # 返回空列表，用户看到一张空表却不知道出了什么事）。这里必须捕获，
        # 否则异常会从 __init__ 冒到 sys.excepthook，弹"程序异常崩溃"。
        try:
            if self._handler and self._handler.can_preview():
                self._contents = self._handler.list_contents(self.archive_path)
            else:
                self._contents = []
        except Exception as e:
            self._contents = []
            self._show_load_error(str(e))
            return
        self.table.setRowCount(len(self._contents))
        for i, item in enumerate(self._contents):
            name_item = QTableWidgetItem(item["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, item["name"])
            self.table.setItem(i, 0, name_item)

            size_str = format_size(item["size"]) if item["size"] else "-"
            self.table.setItem(i, 1, QTableWidgetItem(size_str))

            type_str = "文件夹" if item.get("is_dir") else "文件"
            self.table.setItem(i, 2, QTableWidgetItem(type_str))

    def _show_load_error(self, message: str):
        """列表读取失败：在表内给出提示，同时禁用解压按钮。"""
        try:
            self.table.setRowCount(1)
            item = QTableWidgetItem(f"无法读取压缩包内容: {message}")
            item.setForeground(Qt.GlobalColor.red)
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, 3)
        except Exception:
            pass
        for btn in self.findChildren(QPushButton):
            if "解压" in btn.text():
                btn.setEnabled(False)

    def _extract_selected(self):
        rows = set()
        for item in self.table.selectedItems():
            rows.add(item.row())

        if not rows:
            QMessageBox.warning(self, "提示", "请先选择要解压的文件")
            return

        dest = QFileDialog.getExistingDirectory(self, "选择解压目标")
        if not dest:
            return

        if not self._handler:
            QMessageBox.warning(self, "提示", "不支持的格式")
            return

        for row in rows:
            member_name = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            try:
                self._handler.extract_single(self.archive_path, member_name, dest)
            except Exception as e:
                QMessageBox.critical(self, "错误", f"解压失败: {e}")
                return

        QMessageBox.information(self, "完成", f"已解压 {len(rows)} 个文件到 {dest}")

    def _extract_all(self):
        dest = QFileDialog.getExistingDirectory(self, "选择解压目标")
        if not dest:
            return

        if not self._handler or not self._handler.can_decompress():
            QMessageBox.warning(self, "提示", "不支持的格式")
            return

        try:
            self._handler.decompress(self.archive_path, dest)
            QMessageBox.information(self, "完成", f"已解压全部文件到 {dest}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"解压失败: {e}")
