import copy
import os
import sys
import socket

from utils import jsonstore

APP_DIR_NAME = "ArchiveTool"


def _get_app_data_dir() -> str:
    """Cross-platform app data directory (follows OS conventions)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.join(base, APP_DIR_NAME)


def _is_writable_dir(path: str) -> bool:
    """目录可否创建并写入。用于选择可用的配置目录，绝不抛异常。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _resolve_settings_dir() -> tuple:
    """挑一个真正可写的配置目录，返回 ``(目录, 是否有降级)``。

    原先这里直接采用 OS 约定目录并在 import 阶段就对它写入，一旦该目录不可写
    （只读漫游配置、被 OneDrive/组策略重定向的家目录、磁盘满、权限受限），
    ``import utils.settings`` 就会抛 ``PermissionError``，**整个程序连界面都
    起不来**。现在改为：按优先级探测，全部失败则退到临时目录，最差情况也只
    是"设置不持久化"，而不是崩掉。
    """
    candidates = [
        _get_app_data_dir(),
        os.path.join(os.path.expanduser("~"), f".{APP_DIR_NAME.lower()}"),
        os.path.join(os.path.abspath(os.sep), "tmp", APP_DIR_NAME),
    ]
    for i, candidate in enumerate(candidates):
        if _is_writable_dir(candidate):
            return candidate, i > 0
    import tempfile
    return os.path.join(tempfile.gettempdir(), APP_DIR_NAME), True


SETTINGS_DIR, _SETTINGS_DIR_DEGRADED = _resolve_settings_dir()
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

# Per-provider default configs
_DEFAULT_PROVIDER_CONFIGS = {
    "OpenAI": {
        "api_url": "https://api.openai.com/v1/chat/completions",
        "api_key": "",
        "model": "gpt-4o",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "DeepSeek": {
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-v4-pro",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "Kimi (月之暗面)": {
        "api_url": "https://api.moonshot.cn/v1/chat/completions",
        "api_key": "",
        "model": "kimi-k2-0711-preview",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "MiniMax (稀宇)": {
        "api_url": "https://api.minimax.io/v1/chat/completions",
        "api_key": "",
        "model": "MiniMax-M2.7",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "通义千问 (阿里)": {
        "api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "api_key": "",
        "model": "qwen-plus",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "讯飞星火": {
        "api_url": "https://spark-api-open.xf-yun.com/v1/chat/completions",
        "api_key": "",
        "model": "generalv3",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "Ollama": {
        "api_url": "http://localhost:11434/v1/chat/completions",
        "api_key": "",
        "model": "llama3",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "Groq": {
        "api_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key": "",
        "model": "llama-3.1-70b-versatile",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "Together AI": {
        "api_url": "https://api.together.xyz/v1/chat/completions",
        "api_key": "",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
    "自定义": {
        "api_url": "",
        "api_key": "",
        "model": "",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    },
}

DEFAULT_SETTINGS = {
    "username": socket.gethostname(),
    "theme": "默认蓝",
    "default_receive_path": os.path.join(os.path.expanduser("~"), "Downloads"),
    "default_password": "",
    "auto_accept": False,
    "lan_default_port": 12345,
    "room_default_password": "",
    "room_voice_enabled": True,
    "crawler_default_delay": 2.0,
    "crawler_default_timeout": 30,
    "crawler_max_retries": 3,
    "crawler_default_ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "crawler_export_path": os.path.join(os.path.expanduser("~"), "Downloads"),
    "ai_active_provider": "自定义",
    "ai_custom_models": {},
    "ai_providers": _DEFAULT_PROVIDER_CONFIGS,
}

ACCENT_COLORS = {
    "默认蓝": "#89b4fa",
    "紫": "#cba6f7",
    "绿": "#a6e3a1",
    "橙": "#fab387",
    "红": "#f38ba8",
}


class Settings:
    """应用设置（进程内单例）。

    设计约束：

    * ``__init__`` **绝不抛异常**。原实现在 import 阶段就调用 ``save()``，
      配置目录不可写时直接让整个程序起不来；现在即使无法落盘也只是退化为
      内存态设置。
    * 不在 import 阶段写盘。只有真正发生变更时才调用 ``save()``。
    * ``save()`` 走 :mod:`utils.jsonstore` 的原子写入，避免半截 JSON
      导致下次启动设置全部丢失。
    """

    def __init__(self):
        # 深拷贝，避免与 DEFAULT_SETTINGS / _DEFAULT_PROVIDER_CONFIGS
        # 共享嵌套字典——否则 set_provider_config() 会就地改写模块级默认值。
        self._data = copy.deepcopy(DEFAULT_SETTINGS)
        self._legacy_flat = {}
        self.load()
        if self._migrate_ai_settings():
            # 迁移结果需要落盘（老版本扁平键 → ai_providers 结构）
            self.save()

    def load(self):
        """读取配置文件。缺失/损坏/类型不符时保持默认值，永不抛异常。"""
        loaded = jsonstore.load_json(SETTINGS_FILE, default=None, expect_type=dict)
        if loaded is None:
            return
        for k in DEFAULT_SETTINGS:
            if k in loaded:
                self._data[k] = loaded[k]
        # 老版本的扁平 AI 配置键：load() 只搬运 DEFAULT_SETTINGS 里存在的键，
        # 所以这里必须单独记下来，否则下面的迁移分支永远进不去（旧配置被静默丢弃）。
        if "ai_providers" not in loaded:
            self._legacy_flat = {k: loaded[k] for k in self._LEGACY_FLAT_KEYS
                                 if k in loaded}

    _LEGACY_FLAT_KEYS = ("ai_api_url", "ai_api_key", "ai_model",
                         "ai_temperature", "ai_max_tokens", "ai_system_prompt")

    def _migrate_ai_settings(self) -> bool:
        """把各种历史设置结构迁移到当前结构。返回是否发生了变更（需落盘）。"""
        from core.ai_client import migrate_api_url, PRESET_PROVIDERS

        dirty = False

        # 已迁移（正常路径）：补齐缺失的 provider / 键，并转换旧的自定义模型结构
        if not self._legacy_flat:
            providers = self._data.get("ai_providers")
            if not isinstance(providers, dict):
                providers = {}
                self._data["ai_providers"] = providers
                dirty = True
            for name, cfg in DEFAULT_SETTINGS["ai_providers"].items():
                if name not in providers:
                    providers[name] = dict(cfg)
                    dirty = True
            for k in ("ai_active_provider", "ai_custom_models"):
                if k not in self._data:
                    self._data[k] = copy.deepcopy(DEFAULT_SETTINGS[k])
                    dirty = True
            # 自定义模型 list[str] → dict[str, dict]
            if isinstance(self._data.get("ai_custom_models"), list):
                self._data["ai_custom_models"] = self._convert_custom_models(
                    self._data["ai_custom_models"])
                dirty = True
            return dirty

        # 旧版扁平键迁移
        old_url = migrate_api_url(self._legacy_flat.get("ai_api_url", ""))
        old_key = self._legacy_flat.get("ai_api_key", "")
        old_model = self._legacy_flat.get("ai_model", "")
        old_temp = self._legacy_flat.get("ai_temperature", 1.0)
        old_tokens = self._legacy_flat.get("ai_max_tokens", 4096)
        old_system = self._legacy_flat.get("ai_system_prompt", "")

        matched = "自定义"
        for name, prov in PRESET_PROVIDERS.items():
            if prov.base_url and old_url == prov.base_url:
                matched = name
                break

        providers = {name: dict(cfg)
                     for name, cfg in DEFAULT_SETTINGS["ai_providers"].items()}
        providers[matched] = {
            "api_url": old_url,
            "api_key": old_key,
            "model": old_model,
            "temperature": old_temp,
            "max_tokens": old_tokens,
            "system_prompt": old_system,
        }
        self._data["ai_providers"] = providers
        self._data["ai_active_provider"] = matched
        self._data["ai_custom_models"] = copy.deepcopy(
            DEFAULT_SETTINGS["ai_custom_models"])
        self._legacy_flat = {}
        return True

    @staticmethod
    def _convert_custom_models(old_models) -> dict:
        """把旧版 list[str|dict] 形式的自定义模型转成 dict[str, dict]。"""
        new_models = {}
        for entry in old_models:
            if isinstance(entry, str):
                new_models[entry] = {
                    "api_url": "", "api_key": "", "model": entry,
                    "temperature": 1.0, "max_tokens": 4096,
                    "system_prompt": "",
                }
            elif isinstance(entry, dict):
                new_models[entry.get("model", "unknown")] = dict(entry)
        return new_models

    def save(self) -> bool:
        """原子写入配置文件。失败只记日志并返回 False，绝不抛异常。"""
        return jsonstore.save_json(SETTINGS_FILE, self._data)

    def get(self, key, default=None):
        return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key, value):
        self._data[key] = value

    def get_all(self):
        return copy.deepcopy(self._data)

    # ── AI Per-Provider Convenience Methods ──

    def get_active_provider(self):
        return self._data.get("ai_active_provider", "自定义")

    def get_provider_config(self, provider_name=None):
        if provider_name is None:
            provider_name = self.get_active_provider()
        providers = self._data.get("ai_providers", {})
        return dict(providers.get(provider_name, {}))

    def get_active_ai_config(self):
        """Return (api_url, api_key, model, temperature, max_tokens, system_prompt)."""
        cfg = self.get_provider_config()
        return (
            cfg.get("api_url", ""),
            cfg.get("api_key", ""),
            cfg.get("model", ""),
            cfg.get("temperature", 1.0),
            cfg.get("max_tokens", 4096),
            cfg.get("system_prompt", ""),
        )

    def set_provider_config(self, provider_name, cfg_dict):
        if "ai_providers" not in self._data:
            self._data["ai_providers"] = {}
        self._data["ai_providers"][provider_name] = dict(cfg_dict)

    def set_active_provider(self, provider_name):
        self._data["ai_active_provider"] = provider_name

    def get_custom_models(self):
        """Return dict of custom model configs: {model_id: config_dict}."""
        models = self._data.get("ai_custom_models", {})
        if isinstance(models, list):
            return {}
        return dict(models)

    def get_custom_model_config(self, model_id):
        """Return full config dict for a custom model, or empty dict."""
        models = self.get_custom_models()
        return dict(models.get(model_id, {}))

    def add_custom_model(self, model_id, config):
        """Add or update a custom model with full config."""
        if "ai_custom_models" not in self._data or isinstance(self._data["ai_custom_models"], list):
            self._data["ai_custom_models"] = {}
        self._data["ai_custom_models"][model_id] = dict(config)
        self.save()

    def remove_custom_model(self, model_id):
        """Remove a custom model by its model_id."""
        models = self._data.get("ai_custom_models", {})
        if isinstance(models, list):
            self._data["ai_custom_models"] = {}
            return
        if model_id in models:
            del models[model_id]
            self.save()

    # ── Legacy flat-key accessors (for backward compat) ──

    @property
    def username(self):
        return self._data.get("username", DEFAULT_SETTINGS["username"])

    @property
    def accent_color(self):
        theme_name = self._data.get("theme", "默认蓝")
        return ACCENT_COLORS.get(theme_name, ACCENT_COLORS["默认蓝"])

    @property
    def theme_name(self):
        return self._data.get("theme", "默认蓝")


settings = Settings()
