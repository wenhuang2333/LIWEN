# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
Before starting to write code, organize your thoughts and explain how you plan to implement it.
## Project overview

A PyQt6-based cross-platform desktop app for file compression/decompression (ZIP, 7z, TAR, TAR.GZ, TAR.BZ2, TAR.XZ, SHISHAN, ZHANS), LAN file transfer, LAN room-based multiplayer over **application-layer networking** (room discovery / join / roster / voice / in-room file transfer — no virtual adapter, no driver, no admin), and visual web crawler with embedded Chromium browser. Built with a dark Catppuccin Mocha-inspired theme, packaged into a standalone .exe via PyInstaller. Chinese name "帽兜工具" (window title); English name **LIWEN Tool** (repo, docs, releases). Filesystem/build identifiers use the space-free slug **`LIWENTool`** — see the naming rules below.

> **联机（房间）不走 VPN。** v4.18.0 重构已彻底删除 TUN/虚拟网卡方案（原 `ui/vpn_page.py`
> + Rust `lan_vpn` + `wintun.dll` + `diagnose_vpn.py` + `tools/fetch_wintun.py` 全部移除）。
> 联机协议层天生跨平台，Windows/macOS/Linux 混连共用同一套代码。**不要重新引入任何
> 平台分支、虚拟网卡、TUN 驱动、UAC 提权或 `sys.platform` 判断到联机路径中。**

## Directory layout

**All source code lives under `code/`.** Nothing executable sits in the project root.

```
LIWEN/
├── code/                      # 所有源代码
│   ├── main.py                # 入口 (GUI + CLI 双模式)
│   ├── build.py               # 构建脚本
│   ├── repair_tool.py         # 独立修复工具入口
│   ├── ui/ core/ network/ utils/ cli/ installer/
│   └── tools/                  # server.js 本地测试服务器 + 联机/反向代理的 verify_*.py / fuzz_*.py
├── static/                    # 静态资源: logo.jpg/.ico, head.jpg, AI.png, user.png
├── obj/                       # 文档
│   ├── 开发文档.md  内容文档.md  README.md
│   ├── legacy/                # 历史设计笔记
│   └── assets/                # 文档配图
├── data/                      # 运行时可写数据: log.txt, space_index_*.db  (自动生成)
├── dist/  build/              # 构建产物 (自动生成)
├── .venv/ .idea/ .claude/     # 系统/工具目录, 不可移动
├── CLAUDE.md  requirements.txt
```

> 仓库根目录**不再有任何 Windows 专有二进制**：`wintun.dll` 已随 VPN 方案一并删除。

**Path resolution rule — 禁止手写相对层级。** Never use
`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` to guess a project
path: the depth silently changes when files move. Use `utils/paths.py` instead:

| Function | Returns |
|---|---|
| `project_root()` / `app_dir()` | 项目根 (源码) 或 exe 所在目录 (冻结) |
| `static_dir()` / `static_path(*p)` | `static/` 目录 (冻结时 `sys._MEIPASS/static`) |
| `data_dir(create=False)` | `data/` 运行时可写目录 |
| `dist_dir()` | `dist/` 构建产物目录 |

> **`.venv/` 是唯一"移动后需要修"的目录**：`virtualenv` 把创建时的绝对路径写进了
> `bin/*` 的 shebang、`bin/activate*` 的 `VIRTUAL_ENV` 与 `pyvenv.cfg`。移动项目后
> Python 本身仍可用（`bin/python` 只是指向系统解释器的符号链接），但 `pip`、`pyinstaller`
> 会失效，IDE 也会记住旧路径。修法：`python code/tools/fix_venv_paths.py --apply`
> ＋ 在 IDE 里重新选一次解释器。**源码路径一律走 `utils/paths.py`，不要学 venv 的写法。**

## Commands

```bash
# Run the app  (从项目根目录执行)
python code/main.py

# CLI mode — 任何参数都会走 CLI, 不加载 Qt
python code/main.py --help
python code/main.py compress <paths...> -o out.zip -f zip -l 0
python code/main.py decompress out.zip -d ./out
python code/main.py settings-list

# Build standalone packages
python code/build.py app         # onedir build (output: dist/LIWENTool/)
python code/build.py repair      # onedir build of repair tool (output: dist/LIWENTool_Repair/)
python code/build.py installer   # package app + repair into setup.exe  (Windows only)
python code/build.py dmg         # macOS .dmg
python code/build.py tarball     # Linux portable .tar.gz
python code/build.py all         # current platform's full package
python code/build.py clean       # remove build artifacts (dist/ build/ *.spec)

# Install Windows right-click context menu (requires admin)
python code/installer/right_menu.py install
python code/installer/right_menu.py uninstall

# Verify the multiplayer stack end-to-end (cross-platform, no admin needed)
python code/tools/verify_multiplayer.py          # 36 项：发现/直连探测/畸形主机/凭证泄漏/加入/名单/文件传输/会话失效/音频后端/语音关闭时序/协议边界
python code/tools/verify_multiplayer.py --gui    # 追加 7 项（共 43）：离屏 Qt 跑通联机大厅「直连加入」整条链路
python code/tools/verify_multiplayer.py --audio  # 额外 3 项（共 39）实测音频设备可打开（会触发系统麦克风授权）

# 对抗性/不变量测试（畸形报文、端口推导穷举、人数上限、资源泄漏）
# 与上面的验证脚本**不能同时跑**：两者都要占 UDP 12347
python code/tools/fuzz_multiplayer.py            # 15 项（A-I 九节）；可只跑某几节：fuzz_multiplayer.py A B D

# 反向代理（v4.19.0）：只用 127.0.0.1 临时端口，可与联机套件同时跑
python code/tools/verify_reverse_proxy.py        # 100 项：规则校验/转发语义/chunked/大body/gzip/100-continue/
                                                 #   keep-alive/204/304/HEAD/EOF/502/504/400/431/403/
                                                 #   Location 回写/WebSocket/CONNECT/上游HTTPS/入口TLS/
                                                 #   并发20/热增删/stop 无残留/环形日志/持久化/无 Qt
python code/tools/fuzz_reverse_proxy.py          # 41 项对抗：随机字节/slowloris/头炸弹/裸CR/CL冲突/
                                                 #   CL+TE/非法chunk/CONNECT越权/Upgrade伪装/200轮变异

# Install dependencies
pip install -r requirements.txt

# venv 自检/修复：项目整体被移动过之后，.venv 内部仍记录着「创建时」的绝对路径
# （pip/pyinstaller/pyuic6 的 shebang、activate 的 VIRTUAL_ENV、pyvenv.cfg），
# 表现为 pip 报 No such file or directory、IDE 报 Cannot run program "<旧路径>/.venv/bin/python"
python code/tools/fix_venv_paths.py            # 检查（有残留 → 退出码 1）
python code/tools/fix_venv_paths.py --apply    # 就地改写为当前真实路径
```

> Build outputs are written to the **project root** (`dist/`, `build/`, `*.spec`),
> not into `code/` — `build.py` passes explicit `--paths/--distpath/--workpath/--specpath`.

## Architecture

Paths below are relative to `code/` unless stated otherwise.

**Entry point:** `main.py` — loads persisted settings via `settings.load()`, then creates QApplication, sets Fusion style + Catppuccin Mocha theme, shows `MainWindow`. Settings must load before the UI because the theme accent color comes from settings.

**UI layer** (`ui/`):
- `main_window.py` — `MainWindow` (QMainWindow): custom title bar with window controls, **5-category sidebar** (本机, 局域网, 互联网, 设置, 关于) with sub-tab navigation via `_make_category_with_sub_tabs()`. Categories with multiple pages use a sub-tab bar + `QStackedWidget`; single-page categories show the widget directly. Bottom status/progress bar, drag-drop routing (routes based on `_current_category` + `_current_sub_pages`), worker lifecycle management. Also contains `TitleBar` widget class and `_DiscoveryScanThread` (one-shot UDP scan for manual device discovery).
  - Cat 0 本机 → sub-tabs: 压缩解压 (`CompressPage`), 空间索引 (`SpaceIndexPage`)
  - Cat 1 局域网 → sub-tabs: 局域网传输 (`LanPage`), 联机大厅 (`RoomLobbyPage`)
  - Cat 2 互联网 → sub-tabs: 网页爬虫 (`CrawlerPage`), 请求发送 (`HttpRequestPage`), 生成式AI (`AiPage`), 反向代理 (`ReverseProxyPage`)
  - Cat 3 设置 → sub-tabs: 设置 (`MyPage`), 故障检测 (`DiagnosticPage`)
  - Cat 4 关于 → directly shows `AboutPage`
- `compress_page.py` — `CompressPage`: dual-mode widget (compress/decompress toggle) with per-file settings (format, output path, password, volume), drop zone, file list
- `lan_page.py` — `LanPage`: dual-mode widget (send/receive toggle) with device discovery UI, ping/link check, send file list, listen status display
- `room_lobby_page.py` — `RoomLobbyPage`: wraps a `QStackedWidget` with two sub-views:
  - **LobbyView**: create room (name, optional password, 通信端口), scan LAN for rooms, saved rooms list, and a **直连加入** row (host IP + port). Emits `create_room`/`join_room`/`direct_join` signals.
  - **RoomPage** (`ui/room_page.py`): the **only** in-room view — pure application-layer networking, no virtual adapter. Displays room info, the live member list pushed by the host, voice controls over real LAN IP addresses, and **房间内文件传输** (target picker + 发送文件, reusing `SendWorker`/`ReceiveWorker` on `room_data_port_for_room(room_port)`).
  - 加入房间有两条路径，走的是同一套 TCP 加入流程：**广播发现**（同局域网）与**直连探测**（`RoomProbeWorker` → `MSG_ROOM_PROBE` 拿 room_id，跨网段/广播被拦截时使用）。
  - `RoomLobbyPage.room_page` 暴露 `RoomPage`；**没有** `vpn_page`（VPN 方案已删除，不要再加回来）。
  - `RoomLobbyPage` 自己管理 `RoomHostWorker`/`RoomScanWorker`/`RoomJoinWorker`/`RoomSessionWorker`/`RoomProbeWorker` 的生命周期；`cleanup()` 会依次停止会话与探测线程。
- `my_page.py` — `MyPage`: settings/profile page with username, theme/accent color, default receive path, default password, auto-accept toggle. Auto-saves on every field change via `settings.set()` + `settings.save()`.
- `about_page.py` — `AboutPage`: displays app name (Chinese + English), version, author, and description. Re-exports `APP_NAME` (中文名), `APP_NAME_EN`, `APP_VERSION`, `APP_AUTHOR`, `APP_EMAIL` from `utils/constants.py`, plus its own `APP_DESCRIPTION`. **Change the version in `utils/constants.py`, not here.**
- `styles.py` — `get_main_style(accent)` function that generates the full QSS stylesheet dynamically from an accent color hex. Also exports `MAIN_STYLE` (default blue accent).
- `archive_preview_dialog.py` — `ArchivePreviewDialog`: table-based archive contents viewer with selective extraction
- `crawler_page.py` — `CrawlerPage`: visual web crawler with embedded Chromium browser (QWebEngineView). Features: URL navigation, element inspection (hover highlight + click to capture CSS/XPath selectors), field extraction rules with live preview, pagination workflow config, anti-crawl options (delay, UA, proxy, retries), site feasibility checking (robots.txt, JS requirement, CAPTCHA detection), debug/full crawl modes, data preview table, config save/load, and data export. Uses QWebChannel for JS→Python bridge (injected inspector JavaScript communicates element info back to Python). Manages its own `SiteCheckWorker` and `CrawlWorker` internally.
- `space_index_page.py` — `SpaceIndexPage`: disk space analyzer with **cross-platform** drive selector (`_get_available_drives()`: Win32 `GetLogicalDrives`+`GetDriveTypeW` on Windows, `/` + `/Volumes/*` mounts on macOS, `/` + `psutil.disk_partitions()` real mounts on Linux with pseudo-fs filtering), `BarChartWidget` (horizontal bar chart showing top items by size), `FileIndexModel`-backed `QTreeView` with sortable columns (名称, 占用, 占比, 项目数), scan progress bar, and hover tooltips. **占用 = 磁盘实际分配块** (see `file_index.py`).
- `file_index_model.py` — `FileIndexModel` (`QAbstractItemModel`): reads directly from `IndexDatabase` via SQLite queries, lazily loads children on demand. Columns: 名称, 占用, 占比, 项目数. Uses `internalId()` to store DB row IDs.
- `diagnostic_page.py` — `DiagnosticPage`: comprehensive health check system with two detection modes (局域网检测 ids: `network_interfaces`/`subnet_match`/`tcp_12345`/`udp_12346`/`udp_12347`/`udp_broadcast`/`device_discovery`/`room_scan`/`audio_backend`/`voice_protocol`; 基础检测: compression modules, config integrity). Features `DiagnosticWorker` (QThread-based check runner), `RepairWorker` (runs repair functions), "全局检查" chains both modes, "一键修复" auto-repairs issues, "进行修复" serializes issues to `repair_task.json` and launches external `repair_tool.py` EXE. Also contains `CheckResult` dataclass, repair registries (`REPAIR_REGISTRY`, `LAN_CHECKS`, `BASIC_CHECKS`), and `_CategoryCard` widget for result display.
- `http_request_page.py` — `HttpRequestPage`: Postman-like HTTP client supporting all HTTP methods (GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS/TRACE). Features: URL input with auto http:// prepend, Params tab (query string key-value table), Headers tab (key-value table + batch paste), Body tab with 5 modes (none/form-data/x-www-form-urlencoded/raw/binary), JSON editor with format/validate buttons, response display with status/time/size, response headers/cookies sub-tabs. Uses `HttpRequestWorker` for async requests via `requests` library.
- `reverse_proxy_page.py` — `ReverseProxyPage`（互联网分类第 4 个子标签）: 反向代理规则表（启用/名称/监听/上游/TLS/状态）+ 新建/编辑/删除/启动/停止/重启、规则编辑对话框（含两个头改写键值表与「生成自签证书」）、访问日志表（最多 2000 行）、统计页（总请求/错误/流入/流出/活动连接 + 每规则细分）、日志过滤与清空。启停在页面内 `_ServerOpWorker(QThread)` 上执行，`QTimer(300ms)` 轮询 `drain_events()/stats()`；`cleanup()` 幂等且绝不 `terminate()`。规则经 `utils/proxy_store.py` 落盘。
- `ai_page.py` — `AiPage`: multi-provider AI chat UI (provider/model picker, chat bubbles with cached avatars via `static/AI.png` + `static/user.png`, streaming and non-streaming replies, per-provider config dialog). Sits under the 互联网 category as the 生成式AI sub-tab.
- `compress_card.py`, `decompress_card.py`, `lan_card.py` — Legacy QGroupBox-based widgets, not used by the current `MainWindow`

**Core layer** (`core/`):
- `formats/` — **Archive format abstraction layer** (the central paradigm for compression/decompression):
  - `_base.py` — `ArchiveHandler` ABC with `@register_handler` decorator for auto-registration
  - `_registry.py` — `FormatRegistry`: central dispatch — `detect()`, `get_by_name()`, `compressible_formats()`, `file_filter_string()`. Handlers auto-discovered on `import core.formats`.
  - `zip_handler.py` — ZIP via `zipfile`/`pyzipper` (AES encryption)
  - `seven_z_handler.py` — 7z via `py7zr`, only format with `supports_volumes=True`
  - `tar_handler.py` — .tar via stdlib `tarfile`, no password support. **Use `core.formats._base.tar_extract(tar, member, dest_dir)` instead of `tar.extract(..., filter="data")`** — the `filter` kwarg is Python 3.12+ only and raises `TypeError` on 3.10/3.11 (which broke tar extraction entirely); the helper version-guards and applies an equivalent cross-platform traversal check.
  - `tar_gz_handler.py` — .tar.gz/.tgz via `tarfile:gz`
  - `tar_bz2_handler.py` — .tar.bz2/.tbz2 via `tarfile:bz2`
  - `tar_xz_handler.py` — .tar.xz/.txz decompress-only via `tarfile:xz`
  - `shishan_handler.py` — `ShishanHandler`: .shishan format, **decompression-only** now (custom zlib container with optional AES-256-GCM). Compression use has been superseded by CustomHandler.
  - `custom_handler.py` — `CustomHandler`: .zhans format (阵格式) — a superset of shishan with extended features: deduplication (content-hash-based skipping via XOR filter), per-file zlib strategy selection, deduplication-aware compression level tuning. Supports compression and decompression.
- `crawler/` — **Web crawler engine package** (pure Python, no Qt imports):
  - `engine.py` — `CrawlerEngine`: HTTP fetching via `requests`, data extraction via `DataExtractor`, multi-page crawl with pagination, progress/log/cancel callbacks
  - `extractor.py` — `DataExtractor`: CSS/XPath-based field extraction from HTML using `lxml`, selector generation from element info, selector validation/preview
  - `robot_checker.py` — `SiteChecker`: robots.txt analysis, anti-crawl pattern detection (Cloudflare, CAPTCHA), CMS/framework detection, site feasibility reports
- `file_index.py` — SQLite-backed file system index with two classes: `IndexDatabase` (thread-safe SQLite with WAL mode, schema for `files` table with parent_id foreign key, bulk `insert_rows()`/`update_dir_stats()` via `executemany`, `wal_autocheckpoint=8000`) and `ScanEngine` (`QThread` wrapping `scan_directory()`). Used by `SpaceIndexPage` to populate `FileIndexModel`.
  - **Scan algorithm (v4.17.0 rewrite)**: level-synchronous BFS with a `ThreadPoolExecutor` (`_SCAN_WORKERS` = 4) — each level's directories are stat'd in parallel by `_scan_one_dir()`, results merged in the main thread into in-memory parallel lists indexed by BFS order (so parent index < child index, which makes Phase-2 aggregation a plain reverse `range()` walk with no sort). Files and dirs share one id counter; rows flush in batches of `_DB_BATCH_ROWS` (8000) throttled to ≤1 commit/s. `/Applications` went 19.4 s → 9.7 s (≈2×, matching the pure-walk floor); full `/` = 77 s for 3.45 M files / 774 k dirs.
  - **Directory inode dedup** (`deduped_dirs` in the result dict): on macOS, `/` is a firmlink volume, so `/Users` and `/System/Volumes/Data/Users` are the *same* directory (`(st_dev, st_ino)` identical) and were counted twice — 168 150 duplicate directories. Dedup happens in the **main thread** (not in workers) so the kept path is deterministic: level-synchronous BFS always reaches the shallow path (`/Users`) first. Files are **not** deduped (hardlinks stay listed per path — a file browser must show every path, and the byte inflation is negligible).
  - **`size` = 磁盘实际分配块 (allocated bytes)**, not apparent size: `_disk_bytes(st)` returns `st_size` on Windows, else `st_blocks * 512`. Before this, sparse files (Docker.raw 994 GB apparent / 14.5 GB allocated, Xcode CompilationCache 17 GB / 0) made a 994 GB disk report 1896 GB scanned. Now full `/` = 851 GB vs `os_used` 831 GB (ratio 1.024 — the remainder is APFS metadata + snapshots + directory blocks) and `sum(children) == root` exactly. Matches `du`, Finder 「占用空间」, Explorer 「磁盘上的大小」. **Windows limitation**: Python exposes no `st_blocks`, so sparse files still overcount there (documented, not fixable without Win32 `GetCompressedFileSize`).
- `compress.py` / `decompress.py` / `archive_utils.py` — Thin compatibility wrappers that delegate to `FormatRegistry`. New code should use `FormatRegistry` directly.
- `ai_client.py` — `AiClient`: OpenAI-compatible chat-completions client (streaming + non-streaming) plus `PRESET_PROVIDERS`, `build_messages()`, `get_provider_default_config()`. Pure Python, no Qt.
- `ai_connection_test.py` — Standalone connectivity probe used by the diagnostic page / CLI (`ai-test-connection`).
- `http_client.py` — Shared low-level HTTP request helper backing `HttpRequestWorker` and the `http-request` CLI command.
- `workers.py` — QThread subclasses (each `run()` must be wrapped in try/except that emits its `error` signal; an exception escaping `run()` leaves the UI without any terminal signal and hangs the progress bar): `CompressWorker`, `DecompressWorker`, `SendWorker`, `ReceiveWorker`, `PingWorker`, plus room-related: `RoomHostWorker` (polls `RoomHost.build_roster()` every 500 ms and emits `roster_updated` so the **host's own** member list is live), `RoomScanWorker`, `RoomJoinWorker` (takes `username`/`peer_id`), **`RoomProbeWorker`** (直连探测 `RoomJoinClient.probe_room()`，`finished(bool, str, dict)`), **`RoomSessionWorker`** (runs `RoomSession` — heartbeat keep-alive + host-pushed roster; emits `roster_updated`/`session_lost`), `VoiceWorker` (exposes `set_peers()` — without it room voice transmitted to an empty peer list), plus crawler-related: `CrawlWorker` (runs `CrawlerEngine.run_crawl()` with cancellation support), `SiteCheckWorker` (runs `SiteChecker.check_site_feasibility()`), plus `SpaceScanWorker` (wraps `ScanEngine` for the space index page), plus `HttpRequestWorker` (async HTTP requests via `requests` library with method/params/headers/body config and all body types including file upload)

**Network layer** (`network/`):
- `transfer.py` — TCP-based file transfer with a header protocol: 4-byte big-endian length prefix + JSON metadata + file data. Three message flows: ping/pong, transfer request/accept/reject, and legacy direct. `send_files()` auto-compresses to a temp ZIP before sending; `receive_files()` auto-decompresses and deletes the temp.
- `discovery.py` — `DiscoveryService` (**singleton via `get_discovery_service()`**; there is no `DeviceDiscovery` class): continuous UDP broadcast (~every 2s) on port 12346, collecting `MSG_HERE` replies. Used while listening for transfers. `reply_listener()` is **blocking** (default 3 s) — never call it from the GUI thread; `ui/main_window.py` runs it on `_ReplyListenerThread`. `scan_now(timeout)` does a one-shot scan (used by manual scan and the diagnostic page). `set_listen_port(port)` controls the port advertised in replies.
- `interfaces.py` — LAN interface enumeration via `psutil` (with socket-based fallback). Filters out virtual adapters (VMware, Hyper-V, Docker, WSL, VPN, etc.) and invalid IPs (loopback, APIPA). Returns IP, netmask, prefix, network CIDR, and directed broadcast address. Exports `get_lan_interfaces()`, `get_primary_interface()`, `get_local_ip()`, `get_all_broadcast_addresses()`.
- `room_protocol.py` — `RoomHost`: hosts a room on LAN with periodic UDP announcements (port 12347) + TCP join server with challenge-response auth (SHA-256 password hashing). Also provides `RoomDiscoveryClient.scan_rooms()` (broadcast discover + collect announcements) and `RoomJoinClient.join_room()` (TCP connect + challenge-response, returns `roster`/`data_port`/`peer_id`).
  - **直连探测 (`MSG_ROOM_PROBE`)**: `RoomJoinClient.probe_room(host_ip, port, timeout=5.0, should_cancel=None)` 直连房间端口取一份「房间名片」，返回字段与 UDP 广播公告**逐项相同（同为 8 项：room_id/room_name/has_password/host_name/host_ip/port/player_count/max_players）**，`build_roster()` 里**绝不含 `session_id`**，也**不含 `data_port`**（它是推导值，加入应答里给）——多回一个字段就等于多一条可被探测的信息。`host_ip` 回填**用户填写的地址**（刚刚连通并验证过的那个），主机自报的 `_get_local_ip()` 在多网卡机器上可能是另一个网段，拿它去加入会莫名失败。`join_room` 与 `probe_room` 共用 `_join_target()` 做端口推导。连接阶段是**非阻塞 connect + `select` 分片等待**，因此 `should_cancel` 回调能真正打断探测（`RoomProbeWorker.cancel()` 透传它）。用途：广播被 AP 隔离 / 跨网段时仍能凭 IP:port 加入；CLI `room-join --room-id` 缺省时自动探测。
  - **Roster propagation (v4.17.0)**: the member list is piggybacked on the existing 3 s heartbeat ack (`MSG_HEARTBEAT_ACK.roster`) and on `MSG_ROOM_JOIN_ACCEPT` — no new connection or protocol rewrite. `RoomHost.build_roster()` returns `[{peer_id, username, ip, is_host}]`, host first. **`session_id` must never appear in the roster** — it is the host-issued session credential, and leaking it to co-members would allow impersonation.
  - **`RoomSession`** (client side) sends the first heartbeat immediately, then every `HEARTBEAT_INTERVAL`, parses `roster` into `on_roster`, and sleeps in 0.1 s shards so `stop()` is prompt. On `known: False` it stops and fires `on_session_lost` (surfaced as 「会话已失效」) instead of silently re-registering — silent re-registration would weaken the room password against LAN sniffers.
  - **Room data transfer**: `room_data_port_for_room(room_port)` (12800–13199, 12345→13160) is the per-room file-transfer port. All members listen on it; `RoomPage` drives `network/transfer.py`'s `send_files()`/`receive_files()` through `SendWorker`/`ReceiveWorker`. Derivation skips every reserved port (12345/12346/12347, `room_port`, `room_port+1`, `voice_port_for_room(room_port)`).
- `reverse_proxy.py` — **反向代理引擎**（v4.19.0，纯标准库、无 Qt、无 requests）：`normalize_rule()` 边界校验、`ProxyRuleStore` 规则持久化（`%APPDATA%/LIWENTool/proxy_rules.json`，经 `utils/jsonstore`）、`ReverseProxyServer` 多监听器（`start/stop/add_rule/remove_rule/replace_rules/listeners/stats/drain_events`）。HTTP/1.1 双向转发：keep-alive、chunked 双向（重新声明 `Transfer-Encoding`）、大 body、`100-continue`、hop-by-hop 与 `Connection` 点名头剥离、Host 改写、XFF/XFP/Via、Location 回写、`502/504/400/431/403/503` 语义、WebSocket/Upgrade 101 隧道、`CONNECT`（**仅限本规则上游**，其余 403）、上游 HTTPS（`verify_tls` 开关）与入口 TLS 终止（`generate_self_signed_cert()` 走系统 `openssl`，缺失时如实报错）。环形访问日志（`MAX_LOG_EVENTS=500`）+ 统计。**硬规则**：拒绝保留端口 12345/12346/12347；`CL+TE`、重复冲突 `CL`、裸 `CR`/裸 `LF` 一律 400（走私防护）；空闲等待用 0.5 s 分片 `select` 轮询 `closed` 事件收线程，**不依赖"关 fd 唤醒阻塞 recv"**（macOS 上 `shutdown()` 可能抛 `ENOTCONN`，线程会永久泄漏）。详见 `obj/开发文档.md` §19。
- `voice_protocol.py` — `VoiceClient`: full-duplex voice over LAN using UDP **unicast**. 16-bit mono PCM at 22050 Hz, 20ms frames. One client per room participant.
  - **音频后端（跨平台关键）**: 优先 `sounddevice`（PortAudio 官方绑定，macOS/Windows/Linux + 任意 Python 版本都有 wheel），缺失时回退 `pyaudio`。`is_voice_available()` / `voice_backend_name()` 只按"哪个库能 import"决定，**没有 `sys.platform` 分支**。`INSTALL_HINT` 是统一的安装提示文案。**不要再把 PyAudio 写成唯一/必需依赖**——它在 PyPI 上只有 Windows wheel（cp38–cp313），正是此前"语音永远不可用"的根因。
  - **The voice port must come from `utils.constants.voice_port_for_room(room_port)`** — never `room_port + 2`, which collides with `ROOM_DISCOVERY_PORT` (12347) at the default port and kills voice on the host side. `start()` binds the playback socket and opens the audio device **synchronously** and returns `False` on failure.

**Utils** (`utils/`):
- `constants.py` — **single source of truth for app identity** (`APP_NAME_ZH` 帽兜工具, `APP_NAME_EN` "LIWEN Tool", `APP_NAME_SLUG` `LIWENTool`, **`APP_INSTALL_SLUG` `LIWEN`** = 安装目录名 (both installers use it for `DEFAULT_INSTALL_DIR`; the display name in shortcuts stays `APP_NAME`), `APP_REPAIR_SLUG`, `APP_SETUP_SLUG`, `APP_VERSION`, `APP_AUTHOR`, `APP_EMAIL`, plus `LEGACY_APP_SLUG` for upgrade compatibility), port defaults (TCP 12345, UDP discovery 12346, room discovery 12347), `voice_port_for_room()` (12400–12799), `room_data_port_for_room()` (12800–13199), buffer size, message protocol type strings (transfer, room, voice, **`MSG_ROOM_PROBE`**), file extension sets, volume suffix patterns. **`VOICE_PORT_OFFSET` 已删除** —— 语音端口一律用 `voice_port_for_room()`，不要再引入裸偏移量。
- `helpers.py` — `format_size()`, `format_speed()`, `hash_file()`, `get_common_parent()`, `get_temp_archive_path()`, `is_archive_file()`, `get_output_archive_name()`, `count_files()`
- `settings.py` — `Settings` class: persists to `%APPDATA%/LIWENTool/settings.json`. Keys: `username`, `theme`, `default_receive_path`, `default_password`, `auto_accept`, `lan_default_port`, `room_default_password`, `room_voice_enabled`, `crawler_default_delay`, `crawler_default_timeout`, `crawler_max_retries`, `crawler_default_ua`, `crawler_export_path`. Exposes module-level `settings` singleton. Also defines `ACCENT_COLORS` dict and computed properties `accent_color`, `theme_name`.
- `proxy_store.py` — 反向代理规则存储门面：转出 `network.reverse_proxy` 的 `ProxyRuleStore` / `RuleError` / `normalize_rule`，并提供模块级单例 `proxy_rule_store`（与 `settings` / `room_store` 同风格）。
- `room_store.py` — `RoomStore`: persists saved rooms to `%APPDATA%/LIWENTool/rooms.json`. CRUD operations for room dicts keyed by `room_id`. Exposes module-level `room_store` singleton.
- `logger.py` — Application-wide file logger writing to `data/log.txt` (see `utils.paths.data_dir()`). Overwrites on each startup. Provides `info()`, `warn()`, `error()`, `function_scope(name)` context manager for function entry/exit logging, `install_crash_handler()` (global `sys.excepthook` that logs traceback + shows QMessageBox popup), and `setup_logging()` (initializes the log file with header info). Called early in `main.py` before QApplication, and also in `repair_tool.py`.
- `jsonstore.py` — `load_json()` / `save_json()` / `ensure_dir()`: atomic JSON persistence (`mkstemp` → `fsync` → `os.replace`, module lock, never raises). **Use this for every persisted JSON file** instead of `open() + json.dump`.
- `paths.py` — **Single source of truth for filesystem paths** (`project_root()`, `static_dir()`, `data_dir()`, `dist_dir()`, `static_path()`). Use this instead of hand-rolled `dirname()` chains; it transparently handles the frozen/source split.
- `repair_shared.py` — Serialization contract between main app and standalone repair EXE: `serialize_check_results()` writes `repair_task.json`, `load_check_results()` reads it, plus file-based lock (`acquire_lock`/`release_lock`) to prevent concurrent repair instances.
- `repair_helpers.py` — Path resolution helpers: `find_repair_exe()` and `find_main_exe()` resolve paths relative to install dir (frozen) or `dist/` (source).
- `request_history.py` — Persists HTTP request history for `HttpRequestPage`.
- `variable_store.py` — Key/value variable storage used by the HTTP request page for `{{variable}}` substitution.

**Installer** (`installer/`):
- `right_menu.py` — Registers/unregisters Windows Explorer context menu entries via `winreg`
- `setup.py` — GUI installer wizard (PyQt6) that extracts the embedded zip into a user-chosen directory (**默认 `%ProgramFiles%/LIWEN`**, via `APP_INSTALL_SLUG`), creates shortcuts, and optionally adds right-click menu
- `setup_tk.py` — Alternative tkinter-based installer (simpler, no PyQt6 dependency)
- `shortcut.py` — Creates Windows `.lnk` shortcuts (used by setup.py)

**Repair tool** (`repair_tool.py`):
- Standalone PyQt6 EXE entry point (`LIWENTool_Repair.exe`). Reads `repair_task.json` (written by `DiagnosticPage`), runs targeted repairs via `RepairThread`, then restarts the main `LIWENTool.exe`. Falls back to global repair mode (run all checks → fix what's broken) if no task file exists. Uses file-based locking to prevent concurrent instances. Shares check/repair logic with `DiagnosticPage` by importing from `ui/diagnostic_page.py`.

**CLI layer** (`cli/`):
- `__init__.py` — `run_cli(argv)`: builds the argparse tree from the command modules and dispatches. Reached from `main.py` whenever `len(sys.argv) > 1`, so CLI mode never imports Qt.
- `commands/` — one module per command group, each exposing `register(subparsers)`: `compress.py` (compress/decompress/preview/extract/check), `transfer.py` (lan-send/lan-receive/lan-scan/lan-ping), `room.py` (room-scan/room-create/room-join/room-probe), `crawler.py` (crawl/site-check), `http_cmd.py` (http-request), `ai_cmd.py` (ai-chat/ai-test-connection), `space.py` (space-scan/space-query), `diagnostic.py` (diagnostic-check), `settings_cmd.py` (settings-get/set/list).

**Build** (`build.py`):
- Subcommands: `app`/`repair` (onedir), `setup`, `installer` (onedir → tar.xz → setup.exe, Windows), `dmg` (macOS), `tarball` (Linux), `all`, `clean`
- Auto-generates `logo.ico` (Windows) / `logo.png` (macOS/Linux) from `static/logo.jpg` via Pillow if needed
- Passes `--paths code/`, `--distpath dist/`, `--workpath build/`, `--specpath <root>` so build artifacts never land inside `code/`
- **不打包任何平台专有二进制**：`--uac-admin` 已移除（应用本体不需要管理员权限），`wintun.dll` 打包逻辑随 VPN 方案一并删除。`hidden_imports` 里放的是 `sounddevice`（音频后端）。

**联机（房间）依赖：**
- **无**。纯 Python 标准库（`socket`/`threading`/`json`）+ PyQt6。联机路径上**没有**可选原生模块、没有驱动、没有平台判断。
- 语音是唯一的外部依赖：`sounddevice`（已写入 `requirements.txt`，缺失时自动回退 `pyaudio`，两者都没有则语音按钮禁用并提示安装，房间其余功能不受影响）。

**Docs & assets:**
- `obj/开发文档.md` — architecture, directory layout, path rules, build/release workflow, conventions
- `obj/内容文档.md` — user-facing feature inventory, formats, protocols, ports, settings
- `obj/legacy/` — historical design notes (`plan.md`, `order.md`, `thing.md`, `ai.md`)
- `obj/assets/` — screenshots/images referenced by the docs
- `code/tools/server.js` — local Node.js mock API server (port 8700) used to exercise the HTTP request page
- `code/tools/fix_venv_paths.py` — venv 迁移自检/修复：把 `.venv` 内部残留的旧绝对路径（shebang / VIRTUAL_ENV / pyvenv.cfg）改写为当前路径，默认 dry-run。**项目目录被移动或改名后先跑它**；IDE 的解释器路径需另行在 `Settings → Project → Python Interpreter` 重新选择
- `code/tools/verify_multiplayer.py` — 跨平台联机集成验证脚本，默认 36 项（发现/直连探测/畸形主机不崩/凭证不泄漏/加入/名单/房间内文件传输/会话失效/音频后端/语音关闭时序/协议边界），`--gui` 追加 7 项（共 43）离屏 Qt 大厅直连链路，`--audio` 追加 3 项（共 39）才实测音频设备
- `code/tools/fuzz_multiplayer.py` — 联机协议对抗性测试（A 端口推导穷举 / B 畸形 TCP 应答 / C 畸形 UDP 报文 / D 房主广播抗畸形包 / E 畸形心跳名单 / F 人数上限 / G ping_host / H 资源泄漏 / I `_recv_header` 边界），默认 15 项，`fuzz_multiplayer.py A B D` 可只跑部分节
- `code/tools/verify_reverse_proxy.py` — 反向代理验证套件（100 项，自带上游 HTTP/HTTPS/WebSocket 测试服务器，不依赖外网）
- `code/tools/fuzz_reverse_proxy.py` — 反向代理对抗性套件（41 项，含 200 轮随机变异；断言不崩、干净收场、攻击后仍能服务）

## Key patterns

- **App naming (单一来源)**: all names live in `utils/constants.py` — never hard-code them again.
  | 用途 | 值 |
  |---|---|
  | 中文名（界面、窗口标题） | `APP_NAME_ZH` = `帽兜工具` |
  | 英文名（仓库、文档、发行说明） | `APP_NAME_EN` = `LIWEN Tool` |
  | 可执行文件 / onedir / 数据目录 | `APP_NAME_SLUG` = `LIWENTool`（**无空格**，避免路径必须加引号） |
  | 安装目录 | `APP_INSTALL_SLUG` = `LIWEN`（`DEFAULT_INSTALL_DIR`） |
  | 修复工具 | `APP_REPAIR_SLUG` = `LIWENTool_Repair` |
  | 安装程序 | `APP_SETUP_SLUG` = `LIWENTool_Setup` |

  上一代英文名 `ArchiveTool` 只保留在 `LEGACY_APP_SLUG` / `LEGACY_APP_REPAIR_SLUG`，**仅用于
  升级兼容**（旧数据目录迁移、旧安装目录查找、旧注册表项清理），新代码不要引用。
- **Adding a new archive format**: Create a new file in `core/formats/`, subclass `ArchiveHandler`, implement all abstract methods, decorate with `@register_handler`. Import it in `core/formats/__init__.py`. No changes needed to workers, UI, or helpers — everything auto-discovers through `FormatRegistry`.
- All long-running operations run on `QThread` subclasses (in `core/workers.py`), never on the main thread
- Core compression/decompression functions are pure and don't import PyQt — they take callbacks for progress reporting
- The UI separates compress/decompress concerns per-file: each file in the list has its own settings dict (`{fmt, output, password, volume}`)
- Drag-and-drop is handled at `MainWindow` level and routed to the active page based on `_current_category` + `_current_sub_pages` (sidebar category + sub-tab index)
- LAN transfer always compresses to a temp ZIP before sending; the receiver auto-decompresses on arrival
- The legacy card widgets are retained but unused — new feature work should target `CompressPage` and `LanPage`
- Default TCP port is 12345, UDP discovery port is 12346, room discovery port is 12347
- Settings are loaded before QApplication creation (`settings.load()` in `main.py`) because the theme accent color is needed for the initial stylesheet
- `MyPage` auto-saves on every field change (uses a `_loading` guard to avoid saving during initial population). When settings change, it emits `settings_changed` → `MainWindow` re-applies the full QSS
- The transfer request/accept/reject flow uses `threading.Event` for cross-thread synchronization: `ReceiveWorker` emits `request_decision` signal, main thread shows QMessageBox, sets `decision["accepted"]` and calls `event.set()` to unblock the worker thread
- Room join uses a challenge-response handshake: server sends nonce, client responds with `SHA256(password_hash + nonce)`
- `VoiceClient` uses raw UDP frames with an 8-byte header (4-byte sender_id hash + 4-byte sequence number), with a jitter buffer of 200ms. 后端由 `voice_backend_name()` 报告（`sounddevice` 优先，`pyaudio` 回退）。
- `RoomLobbyPage` is a `QStackedWidget` with exactly **two** pages: `LobbyView` (room list/create/直连加入) and `RoomPage` (in-room). `RoomPage` is the only in-room view — **there is no `vpn_page`**. Access via `room_lobby_page.room_page`.
- **Room membership lifecycle**: the host runs `RoomHostWorker` (which also pushes its own roster to the UI); a joiner runs `RoomJoinWorker` then `RoomSessionWorker`. Both feed `RoomPage.update_peers(roster)`. Leaving/cleanup calls `_leave_room_ui()` → `_stop_session()` → `_room_view.shutdown()`. Room display names come from `settings.username` (`RoomLobbyPage._username()`), **not** `socket.gethostname()` — the LAN transfer page already used `settings.username`, and the two disagreed.
- `CrawlerPage` uses `QWebChannel` to bridge JavaScript ↔ Python: a `_BridgeObject` (QObject) is registered as `__crawler_bridge` and injected JS calls `onElementClicked(json)` via `pyqtSlot`. The page manages its own `CrawlWorker`/`SiteCheckWorker` internally (MainWindow does not manage crawler worker lifecycle).
- Crawler element inspection uses injected JavaScript (see `INSPECTOR_JS` in `crawler_page.py`): hover shows blue highlight, click captures CSS selector + XPath + text, shows green highlight on selected + yellow on all matching elements. Press Escape to exit selection mode.
- The embedded QWebEngineView is only for visual configuration; actual crawling uses `requests` + `lxml` in `CrawlerEngine` for speed.
- **Sidebar navigation**: `MainWindow` has 5 sidebar categories (本机, 局域网, 互联网, 设置, 关于). Categories with multiple sub-pages use `_make_category_with_sub_tabs()` which creates a sub-tab bar + inner `QStackedWidget`. Categories with a single page show the widget directly. `MainWindow._switch_category(idx)` switches the outer `main_stack`; `_switch_sub_page(cat_idx, sub_idx)` switches the inner stack and updates button states.
- **Diagnostic → Repair workflow**: `DiagnosticPage` runs checks via `DiagnosticWorker`. If issues found, user can: (a) "一键修复" — run repairs in-process via `RepairWorker`, then auto re-run checks to verify; or (b) "进行修复" — serialize issues to `repair_task.json`, launch `repair_tool.py` as a detached process, and quit the main app. The repair EXE reads the task file, runs `RepairThread`, restarts `LIWENTool.exe` on completion.
- **Space index pipeline**: `SpaceIndexPage` → user selects drive → `ScanEngine` (QThread) → `scan_directory()` level-synchronous parallel BFS (see `file_index.py`) → `IndexDatabase` (SQLite WAL, bulk `executemany`) → `FileIndexModel` (QAbstractItemModel) provides lazy-loaded tree data → `QTreeView` with `QSortFilterProxyModel` → `BarChartWidget` renders top 80 items visually. Two phases: (1) BFS walk, streaming rows to the DB in batches while collecting dir stat lists in BFS order; (2) reverse-`range()` aggregation (`update_dir_stats`) — no sort needed because BFS order guarantees children follow parents.
- **Logging requirement**: Every function or feature (including newly added ones) MUST log its execution to `data/log.txt` (`utils/logger.py`, path via `utils.paths.data_dir()`). Use `info()` for entry/exit, `error()` for failures, `warn()` for warnings. The log file is overwritten on each app startup. Use `utils.logger.function_scope(name)` context manager for function-level entry/exit logging. On unhandled exceptions, `install_crash_handler()` (called in `main.py` after QApplication creation) logs the full traceback and shows a QMessageBox popup with error details before the process exits.
- **Adding a new path**: never hardcode a directory relative to the current file. Add a helper to `utils/paths.py` (or reuse `static_path()` / `data_dir()`), so source and frozen runs both resolve correctly.
- **Runtime artifacts** (`data/log.txt`, `data/space_index_*.db`) are generated, never committed and never placed in the project root. `dist/`, `build/` and `*.spec` are build outputs.

## Stability / Security invariants (v4.16.1 hardening — keep these)

These were real, reproduced defects. Do not reintroduce the old patterns.

1. **Never block the GUI thread.** No `QThread.wait()` / blocking socket call in a slot or
   `QTimer` callback. `DiscoveryService.reply_listener()` (3 s) and crawler/HTTP cleanup are
   the classic offenders — run them on a worker thread or use a bounded wait only in
   `shutdown()`/`cleanup()`.
2. **Never drop a reference to a running QThread.** Always `cancel()` → `wait(timeout)` →
   disconnect **individual signals** (not bare `disconnect()`, which also detaches
   `destroyed` and prints a Qt warning) → *then* `= None`. If `wait()` times out, keep the
   reference and log it. Never `terminate()`.
3. **Every `QThread.run()` wraps its body in `try/except` and emits `error`.** An exception
   escaping `run()` means the page never receives its terminal signal: `_scanning` stays
   `True`, the progress bar sticks, and the crash dialog tries to build on the worker thread.
4. **Guard terminal slots by sender identity**: `if self.sender() is not self._worker: return`.
   Otherwise a cancelled run's late signal nulls the *new* worker and the UI desyncs.
5. **Atomic file writes.** Persisted JSON goes through `utils.jsonstore`. Archive output goes
   to `*.part` + `os.replace()`; nothing may overwrite a user's existing file before success,
   and a failed operation must leave no partial output.
6. **Validate everything that arrives from the network or from an archive**: metadata fields,
   declared sizes (`MAX_HEADER_SIZE` 64 KB, `MAX_RECV_BYTES` 64 GiB), entry names
   (`_safe_join`), and ports (1–65534). Bound concurrency (`BoundedSemaphore`) and
   per-peer state (`MAX_TRACKED_SENDERS`).
7. **Cap decompression output** with `zlib.decompressobj(..., max_length=size_hint + 1)` and
   stream to disk — never `zlib.decompress()` on untrusted data, and never allocate before
   validating the declared size.
8. **Ports are derived, not invented.** Voice must use
   `utils.constants.voice_port_for_room(room_port)`; `room_port + 2` collides with
   `ROOM_DISCOVERY_PORT`. Room file transfer must use
   `utils.constants.room_data_port_for_room(room_port)` (which itself skips 12345/12346/12347,
   `room_port`, `room_port+1`, and the room's voice port). Any new port needs to be checked
   against 12345/12346/12347.
9. **Cap UI work driven by data volume**: bound visible table rows and throttle progress
   callbacks to integer-percentage changes at most every ~0.1 s (always emit a final 100).
10. **A feature that cannot work must say so.** `VoiceClient.start()` returns `False` when
    bind/device-open fails; a compress of a nonexistent path raises; `supports_volumes()`
    must be `False` unless volumes are genuinely implemented. No silent fake success.
    A diagnostic check that cannot query the thing must **not** report `pass` (e.g.
    `_count_audio_devices()` returning `-1` → `warning`, not "可用，-1 个设备").
11. **A field's *value* is as hostile as its presence.** 同样地，**JSON 的"类型"
    也可以是恶意的**：`[1,2,3]` / `5` / `"x"` / `null` 都是合法 JSON 但不是对象。
    包头协议（`transfer._recv_header`）与 UDP 报文解析（`room_protocol.scan` /
    `reply_listener` / `RoomHost._broadcast_loop`、`discovery._parse_msg`）必须在
    边界上就拒绝非对象载荷（`_recv_header` 抛 `ValueError`、UDP 侧 `isinstance` 后
    `continue`、`_parse_msg` 只返回 dict/None）—— 否则 `.get()` 抛的是
    `AttributeError`，而循环只捕获了 `OSError/JSONDecodeError`，异常会**逃出监听循环**：
    局域网内任意主机发一个 5 字节包就能让"扫描房间/设备发现/房主广播"线程静默死亡
    （`_running` 仍为 True，没有任何机制会重启它）。回归检查见 `verify_multiplayer.py`
    的 4 项「非 JSON 对象」+ 2 项「畸形 UDP 后循环仍存活」，以及 `fuzz_multiplayer.py` C/D 节。
    Presence checks (`if not x`) are not
    validation. Any network-supplied value that becomes a **dict key** (`room_id` →
    `{rid: info}`, `rid in room_store`) must be type-checked (`room_protocol._clean_room_id`)
    — a list makes Python raise `TypeError: unhashable type`, and an exception escaping a Qt
    slot aborts the process (LAN-triggerable DoS). Same for numbers (`_clean_int`) and for
    roster entries (`_clean_roster`, `_clean_text`): coerce/limit/reject at the protocol
    boundary, and keep the UI-side guards as a second layer.
12. **The session credential must never leave the host.** `session_id` is the host-issued
    credential: it must not appear in `build_roster()`, in the probe reply, or in a roster
    pushed by the heartbeat ack — and it must never be *fallback material* for a display id
    (`info.get("peer_id") or sid` published the credential to the whole room). Derive a
    non-reversible id instead (`_fallback_peer_id`). Covered by the verifier check
    「空 peer_id 时名单不泄露 session_id」.
13. **音频流必须在解释器终结线程之前显式关闭；`stop()` 必须等工作线程退出。**
    实测段错误（macOS + Python 3.14 + PortAudio，`EXC_BAD_ACCESS / Instruction Abort`，
    exit 139）：`sounddevice` 用 CFFI `dlopen` 加载 `libportaudio.dylib`，而
    `VoiceClient.start()` 只**同步**打开输出流 —— 输入流是在 `_capture_loop` 线程里
    **异步**打开的。若进程此时退出，解释器终结阶段会释放 CFFI 句柄（`dlclose` 把代码页
    unmap），仍在 `Pa_OpenStream` 里的工作线程随即执行到未映射内存而崩溃（崩溃报告里
    `libportaudio` 已不在 Binary Images 中）。因此：`stop()` 必须 `abort()` → `join(timeout)`
    → 关流 → 释放后端（幂等、可从任意线程调用）；工作线程负责关闭自己打开的流（防止
    `stop()` 之后才创建出来的孤儿流）；退出兜底用 `threading._register_atexit`（在
    `atexit` 之前运行，即**早于** `sounddevice._exit_handler` 的 `Pa_Terminate()`，顺序
    反了就是在已终止的 PortAudio 上 `Pa_AbortStream`）。回归检查见验证脚本
    「语音关闭: …」5 项（用假后端把竞态窗口放大到 400ms，确定性失败于修复前语义）。

## Cross-Platform Development Rule

**以后进行代码修改，不论是检查bug还是新增功能，都要考虑到各个操作系统的适配性，并且每次进行更新的时候都要顾及到安装包的跟进的优化更新。**

具体要求：

1. **所有新增代码必须在 Windows、macOS、Linux 上正常工作**，除非功能本身属于特定平台（右键菜单、Windows 安装程序）。**联机（房间）不属于此类**——它必须三个平台完全一致。
2. **系统调用使用 `sys.platform` 守卫**：`"win32"` 表示 Windows，`"darwin"` 表示 macOS，`"linux"` 或其他表示 Linux。
3. **使用跨平台帮助函数**：
   - `utils.helpers.open_file_or_dir(path)` 替代 `os.startfile()`
   - `utils.helpers.spawn_detached(args, cwd)` 替代原始 `subprocess.CREATE_NO_WINDOW`/`DETACHED_PROCESS`/`CREATE_NEW_PROCESS_GROUP`
   - `utils.repair_helpers._find_executable(dir, name)` 用于跨平台可执行文件发现
4. **禁止硬编码路径**：不要使用 `C:\`、`Program Files`、`%APPDATA%` 等。使用 `utils.settings._get_app_data_dir()` 获取应用数据目录。
5. **禁止硬编码扩展名**：`.exe` 仅在 Windows 上使用。使用 `utils.repair_helpers._EXE_SUFFIX` 常量。
6. **禁止硬编码字体**：QSS 中的等宽字体使用 `ui.styles.MONO_FONT_FAMILY` 常量。
7. **禁止使用 `encoding="gbk"`**：子进程输出编码使用 `locale.getpreferredencoding()`。
8. **平台特定的功能必须优雅降级**：右键菜单 (`installer/right_menu.py`)、Windows 安装程序 (`installer/`)、空间索引的盘符枚举在其他平台上必须显示清晰的"当前平台不支持"提示，不得崩溃。
9. **构建脚本** (`code/build.py`)：
   - `python code/build.py app` — 所有平台通用
   - `python code/build.py repair` — 所有平台通用
   - `python code/build.py installer` — 仅 Windows
   - `python code/build.py dmg` — 仅 macOS
   - `python code/build.py tarball` — Linux 便携包
   - `python code/build.py all` — 自动选择平台对应的打包方式
10. **每次更新功能时同步检查构建配置是否需要跟进优化**。
11. **路径一律走 `utils/paths.py`**：新增文件读写时禁止使用 `os.path.dirname(__file__)` 推导项目路径——源码目录层级一旦调整就会静默失效（本次 `code/` 重构已因此修复 10 处隐患）。

## Version management

The current version is stored in **`utils/constants.py`** as `APP_VERSION` (re-exported by `ui/about_page.py` for backwards compatibility, and consumed by both installers so they can never drift out of sync again). When making changes, update the version according to these rules:

- **Bug fixes / code optimization / minor tweaks**: increment the patch version (e.g. 1.0.0 → 1.0.1)
- **Feature additions / mechanism upgrades / functional changes**: increment the minor version (e.g. 1.0.0 → 1.1.0)
- **Major changes / significant refactoring / major new features**: increment the major version (e.g. 1.0.0 → 2.0.0). **Requires explicit user consent before incrementing.**

When the user explicitly instructs a specific version change, their instruction takes priority over these rules.
