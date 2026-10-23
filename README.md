# 帽兜工具 · LIWEN Tool

一款集 **文件压缩/解压、局域网文件传输、局域网联机大厅、可视化网页爬虫、
HTTP 请求发送、反向代理、AI 对话、磁盘空间分析、故障检测与修复** 于一体的跨平台桌面工具。

界面基于 PyQt6，采用 Catppuccin Mocha 暗色主题；CLI 与 GUI 共用同一套核心逻辑。

**当前版本：4.19.0**

---

## 功能一览

| 模块 | 能力 |
|---|---|
| **压缩 / 解压** | ZIP / 7z / TAR / TAR.GZ / TAR.BZ2 / TAR.XZ / SHISHAN / ZHANS；0–10 级专有压缩刻度；AES 加密；7z 分卷；归档内容预览与选择性提取 |
| **局域网传输** | TCP 直传，自动压缩后发送、接收端自动解压；设备发现（UDP 广播）；请求确认 / 自动接收 |
| **联机大厅** | 房间创建与发现、**IP:端口 直连加入**、挑战-应答口令认证、实时成员名单、实时语音（UDP，22050 Hz 单声道）、**房间内文件互传（全部成员 / 指定成员）** |
| **网页爬虫** | 内嵌 Chromium 可视化拾取元素、CSS/XPath 规则、分页、反爬选项、站点可行性检测、导出 |
| **HTTP 请求** | Postman 风格，支持全部方法、Params/Headers/Body（5 种）、响应头与 Cookie |
| **反向代理** | 规则化反向代理：本机入口 → 固定上游，支持路径剥离/追加、Host 与 XFF 改写、Location 回写、WebSocket 隧道、上游 HTTPS 与入口 TLS 终止、热增删规则、访问日志与统计（**不做 MITM 解密**） |
| **AI 对话** | OpenAI 兼容多provider，流式回复，自定义模型 |
| **空间索引** | 扫描磁盘建立 SQLite 索引，树形浏览 + 占比条形图，快速定位大文件；按**实际占用空间**统计（与访达 / `du` 同口径），跨平台挂载点 |
| **故障检测** | 网络 / 压缩 / 配置 / 索引 / AI 多项自检，一键修复，或交给独立修复程序 |

### 反向代理是干什么的

客户端里的域名 / 端口写死在代码里、又不方便改的时候（只能装真机的 App、第三方回调），
建一条规则就能把「本机某个端口」整体转到「预先配置的上游」：

```bash
# 127.0.0.1:8888 → https://api.example.com，并剥掉 /api 前缀
python code/main.py proxy-add --name 联调 --listen 127.0.0.1:8888 \
    --target https://api.example.com --strip /api
python code/main.py proxy-serve --name 联调      # 前台运行，Ctrl+C 停止
```

它是**有规则的**反向代理，不是开放代理：`CONNECT` 只能打到该规则配置的上游（其余 403），
只支持 HTTP/1.1，也不做 HTTPS 中间人解密（那需要往客户端装根证书）。

### 联机是怎么组网的

联机是**纯应用层组网**：不创建虚拟网卡、不加载 TUN 驱动、不需要管理员权限，
也不会被 UAC 或杀毒软件拦截。协议层（UDP 广播发现 + TCP 挑战-应答加入 + 3 秒心跳名单）
天生跨平台，**Windows / macOS / Linux 混连使用同一套代码**。

| 场景 | 方式 |
|---|---|
| 同一局域网（同一路由器 / WiFi / 热点） | 「搜索房间」自动发现，选中后加入 |
| 跨网段、AP 隔离、广播被防火墙拦截 | 「直连加入」填对方 **IP + 通信端口**（走 TCP，不依赖广播） |

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

# 4. 命令行联机（跨平台，可混连）
python code/main.py room-create -n 我的房间 -p 12345        # 开房（Ctrl+C 关闭）
python code/main.py room-scan                               # 广播搜索房间
python code/main.py room-probe -H 192.168.1.20 -p 12345     # 直连探测房间
python code/main.py room-join  -H 192.168.1.20 -p 12345     # 直连加入（自动探测 room_id）
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

---

## 目录结构

```
LIWEN/
├── code/              # 全部源代码（ui/ core/ network/ utils/ cli/ installer/ tools/）
├── static/            # 图标与头像
├── obj/               # 文档
├── data/              # 运行时数据（日志、空间索引），自动生成，可整体删除
├── dist/  build/      # 构建产物
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
| 房间公告 / 房间发现 | 12347 | UDP |
| 房间加入服务 | 房间端口 + 1（如 12346） | TCP |
| 房间语音 | 12400–12799（由房间端口推导） | UDP |
| 房间内文件传输 | 12800–13199（由房间端口推导） | TCP |
| 本地 Mock API（开发用） | 8700 | HTTP |

> 房间端口合法范围 1024–65534（加入服务使用「端口 + 1」，65535+1 会越界）。
> 语音端口与房间数据端口一律由 `utils.constants` 推导，不要手写 `room_port + N`。

---

## 已知限制

1. 压缩等级 > 0 时产物会被封装进 `.zhans` 容器，通用解压工具无法打开；需要标准格式请用等级 0。
2. `TAR.XZ` 与 `SHISHAN` 仅支持解压。
3. TAR 系列不支持密码；仅 ZIP / 7z / SHISHAN / ZHANS 支持。
4. 分卷压缩仅 7z 支持（产物为 `<名称>.7z.001`、`.002` …）。
5. 联机本身不需要管理员权限；**仅右键菜单注册**（`code/installer/right_menu.py`）需要。
6. 房间语音需要音频后端（`sounddevice`，已列入 `requirements.txt`）；缺失时语音按钮禁用并给出安装提示，
   房间的成员名单与文件传输不受影响。
7. 空间索引缓存可能较大，建议定期清理或在故障检测页重置。

---

## 第三方组件

- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) — GUI 框架（GPLv3 / 商业双授权）
- [sounddevice](https://python-sounddevice.readthedocs.io/) — PortAudio 的 Python 绑定，房间语音的推荐后端
  （MIT；wheel 覆盖 macOS / Windows / Linux，跨平台可用）
- [py7zr](https://pypi.org/project/py7zr/)、[pyzipper](https://pypi.org/project/pyzipper/)、
  [pycryptodome](https://www.pycryptodome.org/) — 压缩与加密
