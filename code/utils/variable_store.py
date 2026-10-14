"""Variable store for HTTP request template substitution.

Supports global variables and named environments. Variables are referenced
in URLs, headers, params, and bodies as {{variable_name}}.
"""
import os
import re

from utils import jsonstore

from utils.settings import SETTINGS_DIR as STORE_DIR
STORE_FILE = os.path.join(STORE_DIR, "http_variables.json")

VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def _default_data():
    return {
        "global": {"baseUrl": "http://127.0.0.1:8700"},
        "environments": {
            "本地测试": {"baseUrl": "http://127.0.0.1:8700"},
            "开发环境": {"baseUrl": "http://dev.example.com"},
            "正式环境": {"baseUrl": "https://api.example.com"},
        },
        "active_environment": None,
    }


class VariableStore:
    def __init__(self):
        self._data = _default_data()
        self.load()

    def load(self):
        loaded = jsonstore.load_json(STORE_FILE, default=None, expect_type=dict)
        if loaded:
            self._data.update(loaded)

    def save(self) -> bool:
        return jsonstore.save_json(STORE_FILE, self._data)

    def get_global(self, key: str, default: str = "") -> str:
        return self._data.get("global", {}).get(key, default)

    def set_global(self, key: str, value: str):
        self._data.setdefault("global", {})[key] = value
        self.save()

    def delete_global(self, key: str):
        self._data.get("global", {}).pop(key, None)
        self.save()

    def all_global(self) -> dict:
        return dict(self._data.get("global", {}))

    def environments(self) -> list:
        return list(self._data.get("environments", {}).keys())

    def get_environment(self, name: str) -> dict:
        return self._data.get("environments", {}).get(name, {})

    def create_environment(self, name: str, vars_dict: dict = None):
        self._data.setdefault("environments", {})[name] = vars_dict or {}
        self.save()

    def delete_environment(self, name: str):
        self._data.get("environments", {}).pop(name, None)
        if self._data.get("active_environment") == name:
            self._data["active_environment"] = None
        self.save()

    def set_env_var(self, env_name: str, key: str, value: str):
        env = self._data.setdefault("environments", {}).setdefault(env_name, {})
        env[key] = value
        self.save()

    def delete_env_var(self, env_name: str, key: str):
        self._data.get("environments", {}).get(env_name, {}).pop(key, None)
        self.save()

    def active_environment(self) -> str | None:
        return self._data.get("active_environment")

    def set_active_environment(self, name: str | None):
        self._data["active_environment"] = name
        self.save()

    def resolve(self, text: str) -> str:
        """Replace {{var}} placeholders with values.
        Environment vars override globals of the same name.
        """
        if not text or "{{" not in text:
            return text

        global_vars = self._data.get("global", {})
        env_vars = {}
        active = self._data.get("active_environment")
        if active:
            env_vars = self._data.get("environments", {}).get(active, {})

        def _replacer(match):
            name = match.group(1)
            if name in env_vars:
                return env_vars[name]
            if name in global_vars:
                return global_vars[name]
            return match.group(0)

        return VAR_PATTERN.sub(_replacer, text)

    def resolve_dict(self, d: dict) -> dict:
        return {self.resolve(k): self.resolve(v) for k, v in d.items()}

    def resolve_form_data(self, rows: list) -> list:
        for row in rows:
            row["key"] = self.resolve(row.get("key", ""))
            row["value"] = self.resolve(row.get("value", ""))
        return rows

    def collect_var_names(self, text: str) -> list:
        return VAR_PATTERN.findall(text)


variable_store = VariableStore()
