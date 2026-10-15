"""
Flat QAbstractTableModel for file-browser-style directory listing.
Shows children of a single directory — replaces the tree model for SPaceIndexPage.
"""

import os
import datetime

from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex

from utils.helpers import format_size


class FileListModel(QAbstractTableModel):
    """Flat table model showing children of a single directory."""

    COLUMNS = ["名称", "占用", "占比", "项目数", "创建时间"]

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self._current_dir_id = None
        self._current_dir_name = ""
        self._children = []   # list of dicts from DB get_children
        self._root_size = 0

    # ── Public API ──────────────────────────────────────────────

    def set_directory(self, dir_id: int):
        """Navigate to a directory, loading its children from the DB."""
        self.beginResetModel()
        self._current_dir_id = dir_id
        if dir_id is not None:
            self._children = self._db.get_children(dir_id)
        else:
            self._children = []
        self.endResetModel()

    def set_root_size(self, size: int):
        self._root_size = size

    @property
    def current_dir_id(self):
        return self._current_dir_id

    def get_dir_name(self, dir_id):
        """Return the name of a directory by its DB id."""
        entry = self._db.get_by_id(dir_id)
        return entry['name'] if entry else ""

    def get_child_info(self, row):
        """Return the full DB dict for the child at row, or None."""
        if 0 <= row < len(self._children):
            return self._children[row]
        return None

    def get_child_path(self, row):
        if 0 <= row < len(self._children):
            return self._children[row]['path']
        return ""

    def is_dir(self, row):
        if 0 <= row < len(self._children):
            return bool(self._children[row]['is_dir'])
        return False

    def get_child_id(self, row):
        if 0 <= row < len(self._children):
            return self._children[row]['id']
        return None

    # ── QAbstractTableModel interface ───────────────────────────

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._children)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLUMNS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row >= len(self._children):
            return None

        child = self._children[row]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return child['name']
            elif col == 1:
                return format_size(child['size'])
            elif col == 2:
                if child['is_dir']:
                    pct = (child['size'] / self._root_size * 100) if self._root_size > 0 else 0
                    return f"{pct:.1f}%"
                return "-"
            elif col == 3:
                if child['is_dir']:
                    return str(child['file_count'] + child['dir_count'])
                return "-"
            elif col == 4:
                try:
                    ctime_ts = os.path.getctime(child['path'])
                    return datetime.datetime.fromtimestamp(ctime_ts).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    return "-"

        elif role == Qt.ItemDataRole.UserRole:
            if col == 1:
                return child['size']
            elif col == 2:
                if child['is_dir']:
                    return (child['size'] / self._root_size * 100) if self._root_size > 0 else 0
                return 0
            elif col == 3:
                if child['is_dir']:
                    return child['file_count'] + child['dir_count']
                return 0
            elif col == 4:
                try:
                    return os.path.getctime(child['path'])
                except OSError:
                    return 0
            elif col == 0:
                return child['path']

        elif role == Qt.ItemDataRole.UserRole + 1:
            return child['is_dir']

        elif role == Qt.ItemDataRole.UserRole + 2:
            return child['id']

        elif role == Qt.ItemDataRole.DecorationRole:
            if col == 0:
                return "📁" if child['is_dir'] else "📄"
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if section < len(self.COLUMNS):
                return self.COLUMNS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
