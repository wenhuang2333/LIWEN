"""项目路径解析 —— 全项目唯一的路径来源。

目录约定（见 obj/开发文档.md）：

    <项目根>/
    ├── code/      所有源代码
    │   └── utils/paths.py   ← 本文件
    ├── static/    静态资源（图标、头像）
    ├── obj/       文档
    ├── data/      运行时可写数据（log.txt、空间索引缓存）
    └── dist/      构建产物

本模块存在的意义：**禁止**其它模块再用
``os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`` 这类
脆弱的相对层级去猜项目根目录——源码目录一旦调整就会静默失效。
所有需要项目路径的代码统一调用本模块。

同时兼容两种运行形态：

* **源码运行** —— 路径基于本文件位置推导（项目根 = code/ 的上一级）。
* **PyInstaller 冻结运行** —— 静态资源在 ``sys._MEIPASS`` 下，
  其余可写路径基于可执行文件所在目录。
"""

import os
import sys

# ── 源码布局常量（冻结运行时依然有意义：指向打包前的源码树） ──
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(_CODE_DIR)
CODE_DIR = _CODE_DIR
STATIC_SRC_DIR = os.path.join(PROJECT_ROOT, "static")


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    """应用（可执行文件）所在目录。

    源码运行 → 项目根目录；冻结运行 → 可执行文件所在目录。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return PROJECT_ROOT


def project_root() -> str:
    """项目根目录（与 :func:`app_dir` 同义，语义更明确的别名）。"""
    return app_dir()


def static_dir() -> str:
    """静态资源目录 static/。

    冻结运行时为 ``sys._MEIPASS/static``（PyInstaller 解包目录）。
    """
    if is_frozen():
        return os.path.join(getattr(sys, "_MEIPASS", app_dir()), "static")
    return STATIC_SRC_DIR


def data_dir(create: bool = False) -> str:
    """运行时可写数据目录 data/（日志、空间索引缓存等）。"""
    path = os.path.join(app_dir(), "data")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def dist_dir() -> str:
    """构建产物目录 dist/。"""
    return os.path.join(project_root(), "dist")


def static_path(*parts: str) -> str:
    """拼出 static/ 下某个资源的绝对路径，例如 ``static_path("logo.jpg")``。"""
    return os.path.join(static_dir(), *parts)


def project_path(*parts: str) -> str:
    """拼出项目根目录下某个路径的绝对路径。"""
    return os.path.join(project_root(), *parts)
