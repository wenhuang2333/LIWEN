"""Request history and saved-collections manager.

Auto-saves recent requests (ring buffer) and supports named saved requests
with import/export.
"""
import json
import os
from datetime import datetime

from utils import jsonstore
from utils.logger import info, error

from utils.settings import SETTINGS_DIR as STORE_DIR
HISTORY_FILE = os.path.join(STORE_DIR, "http_request_history.json")
COLLECTIONS_FILE = os.path.join(STORE_DIR, "http_request_collections.json")

MAX_HISTORY = 200


class RequestHistory:
    def __init__(self):
        self._history: list[dict] = []
        self._collections: dict[str, list[dict]] = {}
        self.load()

    def load(self):
        loaded = jsonstore.load_json(HISTORY_FILE, default=[], expect_type=list)
        if isinstance(loaded, list):
            self._history = loaded
        loaded = jsonstore.load_json(COLLECTIONS_FILE, default={}, expect_type=dict)
        if isinstance(loaded, dict):
            self._collections = loaded

    def save(self) -> bool:
        return jsonstore.save_json(HISTORY_FILE, self._history)

    def _save_collections(self) -> bool:
        return jsonstore.save_json(COLLECTIONS_FILE, self._collections)

    def add(self, method: str, url: str, params: dict, headers: dict,
            body_config: dict, auth_config: dict, settings_config: dict,
            response_status: int = 0, response_time_ms: int = 0,
            response_size: int = 0):
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "url": url,
            "params": params,
            "headers": headers,
            "body_config": body_config,
            "auth_config": auth_config or {},
            "settings_config": settings_config or {},
            "response_status": response_status,
            "response_time_ms": response_time_ms,
            "response_size": response_size,
        }
        self._history.insert(0, entry)
        if len(self._history) > MAX_HISTORY:
            self._history = self._history[:MAX_HISTORY]
        self.save()

    def get_all(self) -> list[dict]:
        return list(self._history)

    def get(self, index: int) -> dict | None:
        if 0 <= index < len(self._history):
            return dict(self._history[index])
        return None

    def clear(self):
        self._history.clear()
        self.save()

    def search(self, text: str) -> list[dict]:
        t = text.lower()
        return [e for e in self._history
                if t in e.get("url", "").lower() or t in e.get("method", "").lower()]

    # ── Collections (saved requests) ────────────────────────────

    def collections(self) -> list[str]:
        return sorted(self._collections.keys())

    def save_to_collection(self, name: str, request_data: dict):
        req = dict(request_data)
        req["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._collections.setdefault(name, []).append(req)
        self._save_collections()
        info(f"RequestHistory: saved to collection '{name}'")

    def get_collection(self, name: str) -> list[dict]:
        return list(self._collections.get(name, []))

    def delete_collection(self, name: str):
        self._collections.pop(name, None)
        self._save_collections()

    def delete_collection_item(self, name: str, index: int):
        items = self._collections.get(name, [])
        if 0 <= index < len(items):
            items.pop(index)
            if not items:
                self._collections.pop(name, None)
            self._save_collections()

    def rename_collection(self, old_name: str, new_name: str):
        if old_name in self._collections and new_name != old_name:
            self._collections[new_name] = self._collections.pop(old_name)
            self._save_collections()

    # ── Export / Import ─────────────────────────────────────────

    def export_to_file(self, filepath: str) -> bool:
        """导出收藏集到用户指定路径。失败只记日志并返回 False（UI 未做异常处理）。"""
        data = {
            "version": 1,
            "exported_at": datetime.now().isoformat(),
            "collections": self._collections,
        }
        ok = jsonstore.save_json(filepath, data)
        if ok:
            info(f"RequestHistory: exported to {filepath}")
        else:
            error(f"RequestHistory: 导出失败 {filepath}")
        return ok

    def import_from_file(self, filepath: str) -> int:
        """从文件导入收藏集。文件损坏时抛 ``ValueError``，由 UI 提示用户。"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"文件不是合法的 JSON: {e}") from e
        except OSError as e:
            raise ValueError(f"无法读取文件: {e}") from e

        if not isinstance(data, dict):
            raise ValueError("文件格式不正确：顶层应为 JSON 对象")

        imported = 0
        for name, items in data.get("collections", {}).items():
            if not isinstance(items, list):
                continue
            existing = self._collections.get(name, [])
            existing.extend(items)
            self._collections[name] = existing
            imported += len(items)
        self._save_collections()
        info(f"RequestHistory: imported {imported} requests from {filepath}")
        return imported


request_history = RequestHistory()
