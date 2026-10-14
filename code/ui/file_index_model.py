"""
Custom QAbstractItemModel backed by IndexDatabase (SQLite).
Queries children on demand, caches expanded node children.
"""

from PyQt6.QtCore import (
    Qt, QAbstractItemModel, QModelIndex, QSortFilterProxyModel,
)
from PyQt6.QtGui import QStandardItem

from utils.helpers import format_size
from utils.logger import info


class FileIndexModel(QAbstractItemModel):
    """Tree model reading directly from IndexDatabase via queries."""

    COLUMNS = ["名称", "大小", "占比", "项目数"]

    def __init__(self, db, root_path="", parent=None):
        super().__init__(parent)
        self._db = db
        self._root_path = root_path
        self._root_id = None
        self._root_size = 0
        self._populated = False

    def set_root(self, root_path):
        self.beginResetModel()
        self._root_path = root_path
        self._root_id = None
        self._root_size = 0
        self._populated = False
        self.endResetModel()

    def populate(self):
        """Called after scan completes — loads root info from DB."""
        self.beginResetModel()
        root_info = self._db.get_by_path(self._root_path)
        if root_info:
            self._root_id = root_info['id']
            self._root_size = root_info['size']
        self._populated = True
        self.endResetModel()

    def refresh_incremental(self):
        """Called during scanning to show partial results without full reset."""
        if not self._populated:
            self.populate()
            return
        root_info = self._db.get_by_path(self._root_path)
        if root_info:
            self._root_id = root_info['id']
            self._root_size = root_info['size']
        self.beginResetModel()
        self.endResetModel()

    # ── QAbstractItemModel interface ───────────────────────────

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            # Top-level: children of root
            children = self._get_child_ids(None)
            if row < len(children):
                return self.createIndex(row, column, children[row])
            return QModelIndex()

        parent_id = parent.internalId()
        children = self._get_child_ids(parent_id)
        if row < len(children):
            return self.createIndex(row, column, children[row])
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()

        child_id = index.internalId()
        parent_id = self._db.get_parent_id(child_id)
        if parent_id is None:
            return QModelIndex()

        # Find parent's row within its own parent
        grandparent_id = self._db.get_parent_id(parent_id)
        siblings = self._get_child_ids(grandparent_id)
        try:
            row = siblings.index(parent_id)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, parent_id)

    def rowCount(self, parent=QModelIndex()):
        if not self._populated:
            return 0
        if not parent.isValid():
            return len(self._get_child_ids(None))
        return len(self._get_child_ids(parent.internalId()))

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLUMNS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row_id = index.internalId()
        col = index.column()
        entry = self._db.get_by_id(row_id)
        if entry is None:
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return entry['name']
            elif col == 1:
                return format_size(entry['size'])
            elif col == 2:
                if entry['is_dir']:
                    pct = (entry['size'] / self._root_size * 100) if self._root_size > 0 else 0
                    return f"{pct:.1f}%"
                return "-"
            elif col == 3:
                if entry['is_dir']:
                    return str(entry['file_count'] + entry['dir_count'])
                return "-"

        elif role == Qt.ItemDataRole.UserRole:
            if col == 1:
                return entry['size']
            elif col == 2:
                if entry['is_dir']:
                    return (entry['size'] / self._root_size * 100) if self._root_size > 0 else 0
                return 0
            elif col == 3:
                if entry['is_dir']:
                    return entry['file_count'] + entry['dir_count']
                return 0
            elif col == 0:
                return entry['path']

        elif role == Qt.ItemDataRole.UserRole + 1:
            return entry['is_dir']

        elif role == Qt.ItemDataRole.UserRole + 2:
            return entry['id']

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

    def hasChildren(self, parent=QModelIndex()):
        if not self._populated:
            return False
        if not parent.isValid():
            return len(self._get_child_ids(None)) > 0
        entry = self._db.get_by_id(parent.internalId())
        if entry and entry['is_dir']:
            return (entry['file_count'] + entry['dir_count']) > 0
        return False

    # ── Internal helpers ───────────────────────────────────────

    def _get_child_ids(self, parent_db_id):
        """Get sorted list of child DB IDs for a parent."""
        if parent_db_id is None and self._root_id is not None:
            parent_db_id = self._root_id
        if parent_db_id is None:
            return []

        children = self._db.get_children(parent_db_id)
        return [c['id'] for c in children]

    def get_item_path(self, index):
        if not index.isValid():
            return None
        entry = self._db.get_by_id(index.internalId())
        return entry['path'] if entry else None

    def is_dir(self, index):
        if not index.isValid():
            return False
        entry = self._db.get_by_id(index.internalId())
        return bool(entry['is_dir']) if entry else False
