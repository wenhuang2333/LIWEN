# obj/ — 文档目录

本目录集中存放 **帽兜工具**（英文名 **LIWEN Tool**）项目的全部文档。

> 目录约定：`code/` 放代码、`static/` 放静态资源、`obj/` 放文档、`data/` 放运行时数据。
> 详见 [开发文档.md](开发文档.md) 第 2 节。

---

## 文档索引

| 文档 | 内容 | 适合谁看 |
|---|---|---|
| [开发文档.md](开发文档.md) | 项目定位、目录结构、环境准备、架构分层、模块职责、**路径解析规范**、数据存储、扩展指南、线程模型、网络协议、构建发布、编码规范、整理记录、**稳定性与安全性优化记录** | 开发者、维护者、打包发布者 |
| [内容文档.md](内容文档.md) | 功能总览、界面地图、10 大功能详解、命令行完整参考、端口与协议速查、数据位置、跨平台支持、已知限制、**4.17.0 用户可感知改进**、快速上手、术语表 | 使用者、测试、产品/文档 |
| `legacy/` | 整理前的历史设计笔记（原始资料，未改动） | 需要追溯早期设计的人 |
| `assets/` | 文档配图 | — |

---

## legacy/ 历史笔记

这些是项目早期积累的设计笔记，整理时从根目录移入此处保留备查。
**内容可能已与当前实现不一致**，请以 `开发文档.md` 与代码为准。

| 文件 | 原始主题 |
|---|---|
| [plan.md](legacy/plan.md) | 核心必选功能规划（HTTP 请求等底层能力） |
| [order.md](legacy/order.md) | CLI 命令参考（早期版本） |
| [thing.md](legacy/thing.md) | 空间索引技术实现文档 |
| [ai.md](legacy/ai.md) | 生成式 AI 模块技术架构分析 |

---

## assets/ 配图

| 文件 | 说明 |
|---|---|
| `img.png` | 历史截图，代码中无引用 |
| `re.png` | 历史截图，代码中无引用 |

> 这两张图在重构前位于项目根目录，且全项目代码与文档均未引用。
> 暂时保留在 `assets/` 以备后续写入文档；若确认无用可直接删除。

---

## 项目根目录其余文件说明

| 路径 | 说明 |
|---|---|
| `code/` | **全部源代码**（`ui/ core/ network/ utils/ cli/ installer/ tools/` + 4 个入口脚本） |
| `static/` | 静态资源：应用图标、关于页头像、AI 对话头像 |
| `obj/` | 本目录，文档 |
| `data/` | 运行时数据：`log.txt`、空间索引缓存。**可整体删除**，程序会自动重建 |
| `dist/` | 构建产物（PyInstaller 输出） |
| `build/` | PyInstaller 中间产物 |
| `.venv/` | Python 虚拟环境（含绝对路径，**不可移动**） |
| `.idea/` | PyCharm 工程配置（**不可移动**） |
| `.claude/` | Claude Code 权限配置（**不可移动**） |
| `CLAUDE.md` | 面向 AI 编程助手的工程说明，必须在项目根目录 |
| `requirements.txt` | 依赖清单 |
| `wintun.dll` | Windows TUN 驱动，VPN 隧道功能必需，**必须在项目根目录** |

---

## 常用命令

```bash
# 运行（图形界面）
python code/main.py

# 运行（命令行）
python code/main.py --help

# 构建
python code/build.py app        # 主程序 onedir → dist/LIWENTool/
python code/build.py clean      # 清理构建产物

# 安装依赖
pip install -r requirements.txt
```
