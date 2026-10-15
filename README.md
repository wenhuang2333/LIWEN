# 帽兜工具 · LIWEN Tool

一款集 **文件压缩/解压、局域网文件传输、局域网联机大厅、VPN 隧道联机、可视化网页爬虫、
HTTP 请求发送、AI 对话、磁盘空间分析、故障检测与修复** 于一体的跨平台桌面工具。

界面基于 PyQt6，采用 Catppuccin Mocha 暗色主题；CLI 与 GUI 共用同一套核心逻辑。

**当前版本：4.17.0**

---

## 功能一览

| 模块 | 能力 |
|---|---|
| **压缩 / 解压** | ZIP / 7z / TAR / TAR.GZ / TAR.BZ2 / TAR.XZ / SHISHAN / ZHANS；0–10 级专有压缩刻度；AES 加密；7z 分卷；归档内容预览与选择性提取 |
| **局域网传输** | TCP 直传，自动压缩后发送、接收端自动解压；设备发现（UDP 广播）；请求确认 / 自动接收 |
| **联机大厅** | 房间创建与发现、挑战-应答口令认证、实时成员名单、实时语音（UDP，22050 Hz 单声道）、**房间内文件互传（全部成员 / 指定成员）** |
| **VPN 隧道** | 基于 TUN 虚拟网卡组建虚拟局域网（10.99.0.0/16），依赖可选 Rust 核心 `lan_vpn` + `wintun.dll`（仅 Windows） |
| **网页爬虫** | 内嵌 Chromium 可视化拾取元素、CSS/XPath 规则、分页、反爬选项、站点可行性检测、导出 |
| **HTTP 请求** | Postman 风格，支持全部方法、Params/Headers/Body（5 种）、响应头与 Cookie |
| **AI 对话** | OpenAI 兼容多provider，流式回复，自定义模型 |
| **空间索引** | 扫描磁盘建立 SQLite 索引，树形浏览 + 占比条形图，快速定位大文件；按**实际占用空间**统计（与访达 / `du` 同口径），跨平台挂载点 |
| **故障检测** | 网络 / 压缩 / 配置 / 索引 / AI 多项自检，一键修复，或交给独立修复程序 |

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动图形界面
python code/main.py

# 3. 命令行模式（任何参数都会走 CLI，不加载 Qt，启动更快）
python code/main.py --help
python code/main.py compress ./some_dir -o out.7z -f 7z -l 0
python code/main.py decompress out.7z -d ./restored
python code/main.py preview out.7z
```

> 入口必须在 `code/` 内执行，即从项目根目录运行 `python code/main.py`。

### 构建安装包

```bash
python code/build.py app         # onedir 构建 → dist/LIWENTool/
python code/build.py repair      # 修复工具 → dist/LIWENTool_Repair/
python code/build.py installer   # 打包为 setup.exe（仅 Windows）
python code/build.py dmg         # macOS .dmg
python code/build.py tarball     # Linux 便携包
python code/build.py clean       # 清理构建产物
```

Windows 构建会自动校验并打包 `wintun.dll`；若源码运行时缺少该驱动，可执行：

```bash
python code/tools/fetch_wintun.py          # 自动获取（带 SHA-256 校验）
python code/tools/fetch_wintun.py --check  # 只校验
```

---

## 目录结构

```
LIWEN/
├── code/              # 全部源代码（ui/ core/ network/ utils/ cli/ installer/ tools/）
├── static/            # 图标与头像
├── obj/               # 文档
├── data/              # 运行时数据（日志、空间索引），自动生成，可整体删除
├── dist/  build/      # 构建产物
├── wintun.dll         # Windows TUN 驱动（VPN 隧道必需）
├── requirements.txt
└── CLAUDE.md          # 面向 AI 助手的工程说明
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [开发文档](obj/开发文档.md) | 架构分层、目录约定、路径规范、扩展指南、线程模型、网络协议、构建发布、编码规范、**稳定性优化记录** |
| [内容文档](obj/内容文档.md) | 功能总览、界面地图、功能详解、命令行完整参考、端口速查、已知限制 |
| [文档索引](obj/README.md) | `obj/` 目录说明与历史笔记 |
| [CLAUDE.md](CLAUDE.md) | 工程约定：路径解析、日志、跨平台规则、版本管理、**必须保持的稳定性不变量** |

---

## 端口约定

| 用途 | 端口 | 协议 |
|---|---|---|
| 文件传输 | 12345 | TCP |
| 设备发现广播 | 12346 | UDP |
| 房间公告 / 加入房间 | 12347 | UDP / TCP |
| 房间语音 | 12400–12799（由房间端口推导） | UDP |
| 房间内文件传输 | 12800–13199（由房间端口推导） | TCP |
| 本地 Mock API（开发用） | 8700 | HTTP |

---

## 已知限制

1. 压缩等级 > 0 时产物会被封装进 `.zhans` 容器，通用解压工具无法打开；需要标准格式请用等级 0。
2. `TAR.XZ` 与 `SHISHAN` 仅支持解压。
3. TAR 系列不支持密码；仅 ZIP / 7z / SHISHAN / ZHANS 支持。
4. 分卷压缩仅 7z 支持（产物为 `<名称>.7z.001`、`.002` …）。
5. VPN 隧道仅 Windows，需 `lan_vpn` 核心 + `wintun.dll` + 管理员权限。
6. 空间索引缓存可能较大，建议定期清理或在故障检测页重置。

---

## 第三方组件

- [wintun.dll](https://www.wintun.net/) — WireGuard LLC 发布的 Windows TUN 驱动，
  MIT / Apache-2.0 双许可。仓库内为官方 `wintun-0.14.1` 原版文件，未经修改
  （SHA-256 `e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce`）。
