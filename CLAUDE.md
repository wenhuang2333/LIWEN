# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
Before starting to write code, organize your thoughts and explain how you plan to implement it.
## Project overview

A PyQt6-based Windows desktop app for file compression/decompression (ZIP, 7z, TAR, TAR.GZ, TAR.BZ2, TAR.XZ, SHISHAN, ZHANS), LAN file transfer, LAN room-based multiplayer VPN tunneling with voice chat, and visual web crawler with embedded Chromium browser. Built with a dark Catppuccin Mocha-inspired theme, packaged into a standalone .exe via PyInstaller. Chinese name "帽兜工具" (window title); English name **LIWEN Tool** (repo, docs, releases). Filesystem/build identifiers use the space-free slug **`LIWENTool`** — see the naming rules below.

## Directory layout

**All source code lives under `code/`.** Nothing executable sits in the project root.

```
LIWEN/
├── code/                      # 所有源代码
│   ├── main.py                # 入口 (GUI + CLI 双模式)
│   ├── build.py               # 构建脚本
│   ├── repair_tool.py         # 独立修复工具入口
│   ├── diagnose_vpn.py        # VPN 诊断脚本 (仅 Windows)
│   ├── ui/ core/ network/ utils/ cli/ installer/
│   └── tools/                  # server.js 本地测试服务器 + fetch_wintun.py 驱动获取/校验
├── static/                    # 静态资源: logo.jpg/.ico, head.jpg, AI.png, user.png
├── obj/                       # 文档
│   ├── 开发文档.md  内容文档.md  README.md
│   ├── legacy/                # 历史设计笔记
│   └── assets/                # 文档配图
├── data/                      # 运行时可写数据: log.txt, space_index_*.db  (自动生成)
├── dist/  build/              # 构建产物 (自动生成)
├── .venv/ .idea/ .claude/     # 系统/工具目录, 不可移动
├── CLAUDE.md  requirements.txt
└── wintun.dll                 # Windows TUN 驱动, 必须位于项目根目录
```

**Path resolution rule — 禁止手写相对层级。** Never use
`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` to guess a project
path: the depth silently changes when files move. Use `utils/paths.py` instead:

| Function | Returns |
|---|---|
| `project_root()` / `app_dir()` | 项目根 (源码) 或 exe 所在目录 (冻结) |
| `static_dir()` / `static_path(*p)` | `static/` 目录 (冻结时 `sys._MEIPASS/static`) |
| `data_dir(create=False)` | `data/` 运行时可写目录 |
| `dist_dir()` | `dist/` 构建产物目录 |

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

# VPN diagnostic (run as admin, Windows only)
python code/diagnose_vpn.py

# Install dependencies
pip install -r requirements.txt
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
  - Cat 2 互联网 → sub-tabs: 网页爬虫 (`CrawlerPage`), 请求发送 (`HttpRequestPage`)
  - Cat 3 设置 → sub-tabs: 设置 (`MyPage`), 故障检测 (`DiagnosticPage`)
  - Cat 4 关于 → directly shows `AboutPage`
- `compress_page.py` — `CompressPage`: dual-mode widget (compress/decompress toggle) with per-file settings (format, output path, password, volume), drop zone, file list
- `lan_page.py` — `LanPage`: dual-mode widget (send/receive toggle) with device discovery UI, ping/link check, send file list, listen status display
- `room_lobby_page.py` — `RoomLobbyPage`: wraps a `QStackedWidget` with two sub-views:
  - **LobbyView**: create room (name, password, virtual subnet), scan LAN for rooms, saved rooms list. Emits `create_room`/`join_room` signals.
  - **RoomPage** (`ui/room_page.py`): Simple in-room view — no VPN/virtual adapter. Displays room info, online peers, voice controls over real LAN IP addresses. Used when VPN module is unavailable.
  - **VpnPage** (`ui/vpn_page.py`): Full VPN tunneling page (virtual IP on 10.99.0.0/16 subnet, optional signaling server mode). Depends on optional Rust `lan_vpn.pyd` + `wintun.dll`. Falls back to install-guide message if unavailable. Polls Rust core at 200ms (5 Hz) for peer list + traffic stats. Supports UAC elevation via `_request_admin_relaunch()`. Has a 1.5s stop cooldown to let Windows tear down the TUN adapter.
- `my_page.py` — `MyPage`: settings/profile page with username, theme/accent color, default receive path, default password, auto-accept toggle. Auto-saves on every field change via `settings.set()` + `settings.save()`.
- `about_page.py` — `AboutPage`: displays app name (Chinese + English), version, author, and description. Re-exports `APP_NAME` (中文名), `APP_NAME_EN`, `APP_VERSION`, `APP_AUTHOR`, `APP_EMAIL` from `utils/constants.py`, plus its own `APP_DESCRIPTION`. **Change the version in `utils/constants.py`, not here.**
- `styles.py` — `get_main_style(accent)` function that generates the full QSS stylesheet dynamically from an accent color hex. Also exports `MAIN_STYLE` (default blue accent).
- `archive_preview_dialog.py` — `ArchivePreviewDialog`: table-based archive contents viewer with selective extraction
- `crawler_page.py` — `CrawlerPage`: visual web crawler with embedded Chromium browser (QWebEngineView). Features: URL navigation, element inspection (hover highlight + click to capture CSS/XPath selectors), field extraction rules with live preview, pagination workflow config, anti-crawl options (delay, UA, proxy, retries), site feasibility checking (robots.txt, JS requirement, CAPTCHA detection), debug/full crawl modes, data preview table, config save/load, and data export. Uses QWebChannel for JS→Python bridge (injected inspector JavaScript communicates element info back to Python). Manages its own `SiteCheckWorker` and `CrawlWorker` internally.
- `space_index_page.py` — `SpaceIndexPage`: disk space analyzer with drive selector (Win32 GetLogicalDrives), `BarChartWidget` (horizontal bar chart showing top items by size), `FileIndexModel`-backed `QTreeView` with sortable columns (名称, 大小, 占比, 项目数), scan progress bar, and hover tooltips.
- `file_index_model.py` — `FileIndexModel` (`QAbstractItemModel`): reads directly from `IndexDatabase` via SQLite queries, lazily loads children on demand. Columns: 名称, 大小, 占比, 项目数. Uses `internalId()` to store DB row IDs.
- `diagnostic_page.py` — `DiagnosticPage`: comprehensive health check system with two detection modes (局域网检测: network interfaces, ports, discovery, voice; 基础检测: compression modules, config integrity). Features `DiagnosticWorker` (QThread-based check runner), `RepairWorker` (runs repair functions), "全局检查" chains both modes, "一键修复" auto-repairs issues, "进行修复" serializes issues to `repair_task.json` and launches external `repair_tool.py` EXE. Also contains `CheckResult` dataclass, repair registries (`REPAIR_REGISTRY`, `LAN_CHECKS`, `BASIC_CHECKS`), and `_CategoryCard` widget for result display.
- `http_request_page.py` — `HttpRequestPage`: Postman-like HTTP client supporting all HTTP methods (GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS/TRACE). Features: URL input with auto http:// prepend, Params tab (query string key-value table), Headers tab (key-value table + batch paste), Body tab with 5 modes (none/form-data/x-www-form-urlencoded/raw/binary), JSON editor with format/validate buttons, response display with status/time/size, response headers/cookies sub-tabs. Uses `HttpRequestWorker` for async requests via `requests` library.
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
- `file_index.py` — SQLite-backed file system index with two classes: `IndexDatabase` (thread-safe SQLite with WAL mode, schema for `files` table with parent_id foreign key, batched writes) and `ScanEngine` (`QThread` that walks directory tree via `os.scandir` + `deque`, 3-phase: collect files/dirs, bottom-up size aggregation, DB write). Used by `SpaceIndexPage` to populate `FileIndexModel`.
- `compress.py` / `decompress.py` / `archive_utils.py` — Thin compatibility wrappers that delegate to `FormatRegistry`. New code should use `FormatRegistry` directly.
- `ai_client.py` — `AiClient`: OpenAI-compatible chat-completions client (streaming + non-streaming) plus `PRESET_PROVIDERS`, `build_messages()`, `get_provider_default_config()`. Pure Python, no Qt.
- `ai_connection_test.py` — Standalone connectivity probe used by the diagnostic page / CLI (`ai-test-connection`).
- `http_client.py` — Shared low-level HTTP request helper backing `HttpRequestWorker` and the `http-request` CLI command.
- `workers.py` — QThread subclasses (each `run()` must be wrapped in try/except that emits its `error` signal; an exception escaping `run()` leaves the UI without any terminal signal and hangs the progress bar): `CompressWorker`, `DecompressWorker`, `SendWorker`, `ReceiveWorker`, `PingWorker`, plus room-related: `RoomHostWorker`, `RoomScanWorker`, `RoomJoinWorker`, `VoiceWorker`, plus crawler-related: `CrawlWorker` (runs `CrawlerEngine.run_crawl()` with cancellation support), `SiteCheckWorker` (runs `SiteChecker.check_site_feasibility()`), plus `SpaceScanWorker` (wraps `ScanEngine` for the space index page), plus `HttpRequestWorker` (async HTTP requests via `requests` library with method/params/headers/body config and all body types including file upload)

**Network layer** (`network/`):
- `transfer.py` — TCP-based file transfer with a header protocol: 4-byte big-endian length prefix + JSON metadata + file data. Three message flows: ping/pong, transfer request/accept/reject, and legacy direct. `send_files()` auto-compresses to a temp ZIP before sending; `receive_files()` auto-decompresses and deletes the temp.
- `discovery.py` — `DiscoveryService` (**singleton via `get_discovery_service()`**; there is no `DeviceDiscovery` class): continuous UDP broadcast (~every 2s) on port 12346, collecting `MSG_HERE` replies. Used while listening for transfers. `reply_listener()` is **blocking** (default 3 s) — never call it from the GUI thread; `ui/main_window.py` runs it on `_ReplyListenerThread`. `scan_now(timeout)` does a one-shot scan (used by manual scan and the diagnostic page). `set_listen_port(port)` controls the port advertised in replies.
- `interfaces.py` — LAN interface enumeration via `psutil` (with socket-based fallback). Filters out virtual adapters (VMware, Hyper-V, Docker, WSL, VPN, etc.) and invalid IPs (loopback, APIPA). Returns IP, netmask, prefix, network CIDR, and directed broadcast address. Exports `get_lan_interfaces()`, `get_primary_interface()`, `get_local_ip()`, `get_all_broadcast_addresses()`.
- `room_protocol.py` — `RoomHost`: hosts a room on LAN with periodic UDP announcements (port 12347) + TCP join server with challenge-response auth (SHA-256 password hashing). Also provides `RoomDiscoveryClient.scan_rooms()` (broadcast discover + collect announcements) and `join_room()` (TCP connect + challenge-response).
- `voice_protocol.py` — `VoiceClient`: full-duplex voice over LAN using UDP **unicast** + PyAudio. 16-bit mono PCM at 22050 Hz, 20ms frames. One client per room participant. Optional — reported unavailable if PyAudio is missing. **The voice port must come from `utils.constants.voice_port_for_room(room_port)`** — never `room_port + 2`, which collides with `ROOM_DISCOVERY_PORT` (12347) at the default port and kills voice on the host side. `start()` binds the playback socket and opens the audio device **synchronously** and returns `False` on failure.

**Utils** (`utils/`):
- `constants.py` — **single source of truth for app identity** (`APP_NAME_ZH` 帽兜工具, `APP_NAME_EN` "LIWEN Tool", `APP_NAME_SLUG` `LIWENTool`, `APP_REPAIR_SLUG`, `APP_SETUP_SLUG`, `APP_VERSION`, `APP_AUTHOR`, `APP_EMAIL`, plus `LEGACY_APP_SLUG` for upgrade compatibility), port defaults (TCP 12345, UDP discovery 12346, room discovery 12347), `voice_port_for_room()`, buffer size, message protocol type strings (transfer, room, voice), file extension sets, volume suffix patterns
- `helpers.py` — `format_size()`, `format_speed()`, `hash_file()`, `get_common_parent()`, `get_temp_archive_path()`, `is_archive_file()`, `get_output_archive_name()`, `count_files()`
- `settings.py` — `Settings` class: persists to `%APPDATA%/LIWENTool/settings.json`. Keys: `username`, `theme`, `default_receive_path`, `default_password`, `auto_accept`, `lan_default_port`, `room_default_password`, `room_voice_enabled`, `crawler_default_delay`, `crawler_default_timeout`, `crawler_max_retries`, `crawler_default_ua`, `crawler_export_path`. Exposes module-level `settings` singleton. Also defines `ACCENT_COLORS` dict and computed properties `accent_color`, `theme_name`.
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
- `setup.py` — GUI installer wizard (PyQt6) that extracts the embedded zip into a user-chosen directory, creates shortcuts, and optionally adds right-click menu
- `setup_tk.py` — Alternative tkinter-based installer (simpler, no PyQt6 dependency)
- `shortcut.py` — Creates Windows `.lnk` shortcuts (used by setup.py)

**Repair tool** (`repair_tool.py`):
- Standalone PyQt6 EXE entry point (`LIWENTool_Repair.exe`). Reads `repair_task.json` (written by `DiagnosticPage`), runs targeted repairs via `RepairThread`, then restarts the main `LIWENTool.exe`. Falls back to global repair mode (run all checks → fix what's broken) if no task file exists. Uses file-based locking to prevent concurrent instances. Shares check/repair logic with `DiagnosticPage` by importing from `ui/diagnostic_page.py`.

**CLI layer** (`cli/`):
- `__init__.py` — `run_cli(argv)`: builds the argparse tree from the command modules and dispatches. Reached from `main.py` whenever `len(sys.argv) > 1`, so CLI mode never imports Qt.
- `commands/` — one module per command group, each exposing `register(subparsers)`: `compress.py` (compress/decompress/preview/extract/check), `transfer.py` (lan-send/lan-receive/lan-scan/lan-ping), `room.py` (room-scan/room-create/room-join), `crawler.py` (crawl/site-check), `http_cmd.py` (http-request), `ai_cmd.py` (ai-chat/ai-test-connection), `space.py` (space-scan/space-query), `diagnostic.py` (diagnostic-check), `settings_cmd.py` (settings-get/set/list).

**Build** (`build.py`):
- Subcommands: `app`/`repair` (onedir), `setup`, `installer` (onedir → tar.xz → setup.exe, Windows), `dmg` (macOS), `tarball` (Linux), `all`, `clean`
- Auto-generates `logo.ico` (Windows) / `logo.png` (macOS/Linux) from `static/logo.jpg` via Pillow if needed
- Passes `--paths code/`, `--distpath dist/`, `--workpath build/`, `--specpath <root>` so build artifacts never land inside `code/`
- `wintun.dll` **is now bundled** on Windows builds: `build.py` calls `code/tools/fetch_wintun.py::ensure_wintun()`, which verifies the file against the official WireGuard SHA-256 and re-downloads it if missing/tampered, then passes it via `--add-binary`. Non-Windows builds skip it (no Windows DLL in a mac/linux package). Fetch manually with `python code/tools/fetch_wintun.py`.

**VPN / LAN联机 dependencies:**
- `lan_vpn` — Optional Rust-based VPN core, imported as `lan_vpn.pyd`. Provides `VpnCore` and `PeerInfo`. Built from a sibling `rust_/` directory via `maturin develop --release`.
- `wintun.dll` — Windows TUN driver, must be present in the project root for VPN functionality (source runs) or next to the .exe (frozen).

**Docs & assets:**
- `obj/开发文档.md` — architecture, directory layout, path rules, build/release workflow, conventions
- `obj/内容文档.md` — user-facing feature inventory, formats, protocols, ports, settings
- `obj/legacy/` — historical design notes (`plan.md`, `order.md`, `thing.md`, `ai.md`)
- `obj/assets/` — screenshots/images referenced by the docs
- `code/tools/server.js` — local Node.js mock API server (port 8700) used to exercise the HTTP request page
- `code/tools/fetch_wintun.py` — downloads / verifies `wintun.dll` against pinned official SHA-256 hashes; used by `build.py` and runnable standalone (`--check`, `--force`, `--arch`)

## Key patterns

- **App naming (单一来源)**: all names live in `utils/constants.py` — never hard-code them again.
  | 用途 | 值 |
  |---|---|
  | 中文名（界面、窗口标题） | `APP_NAME_ZH` = `帽兜工具` |
  | 英文名（仓库、文档、发行说明） | `APP_NAME_EN` = `LIWEN Tool` |
  | 可执行文件 / onedir / 数据目录 | `APP_NAME_SLUG` = `LIWENTool`（**无空格**，避免路径必须加引号） |
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
- `VoiceClient` uses raw UDP frames with an 8-byte header (4-byte sender_id hash + 4-byte sequence number), with a jitter buffer of 200ms
- `RoomLobbyPage` is a `QStackedWidget` that switches between `LobbyView` (room list/create), `RoomPage` (simple in-room, no VPN), and `VpnPage` (full VPN tunneling). When `lan_vpn.pyd` is unavailable, joining a room shows `RoomPage` instead of `VpnPage`. Access via `room_lobby_page.room_page` and `room_lobby_page.vpn_page`.
- `CrawlerPage` uses `QWebChannel` to bridge JavaScript ↔ Python: a `_BridgeObject` (QObject) is registered as `__crawler_bridge` and injected JS calls `onElementClicked(json)` via `pyqtSlot`. The page manages its own `CrawlWorker`/`SiteCheckWorker` internally (MainWindow does not manage crawler worker lifecycle).
- Crawler element inspection uses injected JavaScript (see `INSPECTOR_JS` in `crawler_page.py`): hover shows blue highlight, click captures CSS selector + XPath + text, shows green highlight on selected + yellow on all matching elements. Press Escape to exit selection mode.
- The embedded QWebEngineView is only for visual configuration; actual crawling uses `requests` + `lxml` in `CrawlerEngine` for speed.
- **Sidebar navigation**: `MainWindow` has 5 sidebar categories (本机, 局域网, 互联网, 设置, 关于). Categories with multiple sub-pages use `_make_category_with_sub_tabs()` which creates a sub-tab bar + inner `QStackedWidget`. Categories with a single page show the widget directly. `MainWindow._switch_category(idx)` switches the outer `main_stack`; `_switch_sub_page(cat_idx, sub_idx)` switches the inner stack and updates button states.
- **Diagnostic → Repair workflow**: `DiagnosticPage` runs checks via `DiagnosticWorker`. If issues found, user can: (a) "一键修复" — run repairs in-process via `RepairWorker`, then auto re-run checks to verify; or (b) "进行修复" — serialize issues to `repair_task.json`, launch `repair_tool.py` as a detached process, and quit the main app. The repair EXE reads the task file, runs `RepairThread`, restarts `LIWENTool.exe` on completion.
- **Space index pipeline**: `SpaceIndexPage` → user selects drive → `ScanEngine` (QThread) walks dir tree via `os.scandir` → writes to `IndexDatabase` (SQLite WAL) → `FileIndexModel` (QAbstractItemModel) provides lazy-loaded tree data → `QTreeView` with `QSortFilterProxyModel` → `BarChartWidget` renders top 80 items visually. The scan has 3 phases: (1) collect files + dir placeholders with batch DB writes, (2) bottom-up directory size aggregation, (3) write final directory entries to DB.
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
   `ROOM_DISCOVERY_PORT`. Any new port needs to be checked against 12345/12346/12347.
9. **Cap UI work driven by data volume**: bound visible table rows and throttle progress
   callbacks to integer-percentage changes at most every ~0.1 s (always emit a final 100).
10. **A feature that cannot work must say so.** `VoiceClient.start()` returns `False` when
    bind/device-open fails; a compress of a nonexistent path raises; `supports_volumes()`
    must be `False` unless volumes are genuinely implemented. No silent fake success.

## Cross-Platform Development Rule

**以后进行代码修改，不论是检查bug还是新增功能，都要考虑到各个操作系统的适配性，并且每次进行更新的时候都要顾及到安装包的跟进的优化更新。**

具体要求：

1. **所有新增代码必须在 Windows、macOS、Linux 上正常工作**，除非功能本身属于特定平台（VPN 隧道、右键菜单、Windows 安装程序）。
2. **系统调用使用 `sys.platform` 守卫**：`"win32"` 表示 Windows，`"darwin"` 表示 macOS，`"linux"` 或其他表示 Linux。
3. **使用跨平台帮助函数**：
   - `utils.helpers.open_file_or_dir(path)` 替代 `os.startfile()`
   - `utils.helpers.spawn_detached(args, cwd)` 替代原始 `subprocess.CREATE_NO_WINDOW`/`DETACHED_PROCESS`/`CREATE_NEW_PROCESS_GROUP`
   - `utils.repair_helpers._find_executable(dir, name)` 用于跨平台可执行文件发现
4. **禁止硬编码路径**：不要使用 `C:\`、`Program Files`、`%APPDATA%` 等。使用 `utils.settings._get_app_data_dir()` 获取应用数据目录。
5. **禁止硬编码扩展名**：`.exe` 仅在 Windows 上使用。使用 `utils.repair_helpers._EXE_SUFFIX` 常量。
6. **禁止硬编码字体**：QSS 中的等宽字体使用 `ui.styles.MONO_FONT_FAMILY` 常量。
7. **禁止使用 `encoding="gbk"`**：子进程输出编码使用 `locale.getpreferredencoding()`。
8. **平台特定的功能必须优雅降级**：VPN 隧道 (`ui/vpn_page.py`)、右键菜单 (`installer/right_menu.py`)、Windows 安装程序 (`installer/`) 在其他平台上必须显示清晰的"当前平台不支持"提示，不得崩溃。
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
