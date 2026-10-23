# AGENTS.md

Guidance for AI agents working in this repository.
Deeper detail lives in `CLAUDE.md` (engineering conventions + stability invariants) and
`obj/开发文档.md` / `obj/内容文档.md` — read those before touching sensitive areas
(networking, workers/threads, build, installers).

## Project

PyQt6 cross-platform (Windows/macOS/Linux) desktop tool: file compression/decompression
(ZIP/7z/TAR family/SHISHAN/ZHANS), LAN file transfer, LAN room-based multiplayer
(discovery / join / roster / voice / in-room file transfer), visual web crawler,
HTTP client, AI chat, reverse proxy (HTTP/1.1, rules-based, no MITM), disk space analyzer,
diagnostics + standalone repair tool.
Dark Catppuccin Mocha theme. CLI and GUI share the same core logic.

Naming — all defined in `code/utils/constants.py`, never hard-code elsewhere:
- 中文界面名 `APP_NAME_ZH` = 帽兜工具; English name `APP_NAME_EN` = "LIWEN Tool"
- Filesystem/build slug `APP_NAME_SLUG` = `LIWENTool` (no spaces); install dir `APP_INSTALL_SLUG` = `LIWEN`
- Legacy name `ArchiveTool` survives only in `LEGACY_APP_SLUG*` for upgrade compatibility — do not use in new code

## Layout

All source is under `code/` (nothing executable at repo root):
`main.py` (GUI+CLI entry) · `build.py` · `repair_tool.py` ·
`ui/` (Qt pages) · `core/` (formats/, crawler/, file_index, workers.py) ·
`network/` (transfer, discovery, room_protocol, voice_protocol, interfaces, reverse_proxy) ·
`utils/` (constants, paths, settings, logger, jsonstore) · `cli/` (argparse commands) ·
`installer/` (Windows setup + right-click menu) · `tools/` (verify/fuzz scripts, mock server).

`static/` icons · `obj/` docs · `data/` runtime artifacts (log.txt, space_index_*.db — generated, never committed) ·
`dist/` `build/` `*.spec` build outputs.

## Commands

```bash
pip install -r requirements.txt
python code/main.py                     # GUI (run from repo root)
python code/main.py --help              # CLI mode: any argv → CLI, no Qt loaded

python code/build.py app|repair         # onedir builds → dist/LIWENTool(_Repair)/
python code/build.py installer          # Windows setup.exe only
python code/build.py dmg                # macOS only
python code/build.py tarball            # Linux
python code/build.py clean

python code/tools/verify_multiplayer.py            # 36-check multiplayer integration suite
python code/tools/verify_multiplayer.py --gui      # +7 offscreen Qt lobby checks
python code/tools/verify_multiplayer.py --audio    # +3 real audio-device checks
python code/tools/fuzz_multiplayer.py              # 15 adversarial protocol checks (A–I); can pass section letters
# NOTE: verify and fuzz both bind UDP 12347 — never run them concurrently.

python code/tools/verify_reverse_proxy.py          # 100-check reverse-proxy suite (self-hosted upstream, no network)
python code/tools/fuzz_reverse_proxy.py            # 41 adversarial reverse-proxy checks (+200 random mutations)
# NOTE: the reverse-proxy suites use ephemeral 127.0.0.1 ports only, so they may run alongside
# the multiplayer suites (which own 12345/12346/12347).

python code/tools/fix_venv_paths.py --apply        # after moving/renaming the project dir (repairs .venv absolute paths)
```

There is no pytest/lint setup; the verify/fuzz scripts are the test suite.

## Architecture rules

- Layers: `ui/` → `core/` + `network/` → `utils/`. Core/network modules are Qt-free except
  `core/workers.py` and `core/file_index.py` (QThread wrappers).
- Long-running work always runs on a `QThread` subclass in `core/workers.py` (or a page-local
  worker); never block the GUI thread.
- New archive format: add a file in `core/formats/`, subclass `ArchiveHandler`, decorate with
  `@register_handler`, import it in `core/formats/__init__.py` — `FormatRegistry` auto-discovers;
  no worker/UI changes needed.
- Settings load before QApplication in `main.py` (theme accent comes from settings).
- Settings/rooms persist via `utils/settings.py` / `utils/room_store.py`; all JSON writes go
  through `utils/jsonstore.py` (atomic mkstemp→fsync→replace), never `open()+json.dump`.
- Every feature must log to `data/log.txt` via `utils/logger.py` (`function_scope()` for
  entry/exit); crash handler is installed in `main.py`.

## Hard invariants (violating these reintroduces known bugs)

1. **联机不走 VPN/TUN** — rooms are pure application-layer networking. Never reintroduce
   `vpn_page`, `wintun.dll`, virtual adapters, UAC elevation, or `sys.platform` branches on the
   room code path. Win/macOS/Linux mix freely on the same code.
2. **Paths via `utils/paths.py` only** — never `os.path.dirname(__file__)` chains; they silently
   break when files move. Frozen (PyInstaller) and source runs must both resolve.
3. **Ports are derived, never invented**: voice uses `voice_port_for_room(room_port)` (12400–12799),
   room file transfer `room_data_port_for_room(room_port)` (12800–13199). Fixed ports:
   TCP transfer 12345, UDP discovery 12346, UDP room discovery 12347. `room_port + 2` collides
   with 12347 — a past bug.
4. **`session_id` never leaves the host** — must not appear in roster, probe reply, or heartbeat
   push; leaking it enables impersonation.
5. **QThread lifecycle**: `run()` bodies wrapped in try/except emitting `error` (an escaped
   exception hangs the page); never drop a running worker reference; `cancel()` → `wait()` →
   disconnect individual signals → `= None`; never `terminate()`; guard terminal slots with
   `if self.sender() is not self._worker: return`.
6. **Validate all network/archive input at the boundary**: JSON payloads must be type-checked
   as dicts (`[1,2,3]`/`5`/`null` are valid JSON but kill listen loops), dict keys like `room_id`
   must be hashable-type-checked, declared sizes/ports bounded (`MAX_HEADER_SIZE`, `MAX_RECV_BYTES`,
   `_safe_join` for entry names). Cap decompression with `decompressobj(max_length=...)`.
7. **Audio shutdown ordering matters**: `VoiceClient.stop()` must abort → join threads → close
   streams → release backend; `threading._register_atexit` runs before
   `sounddevice._exit_handler`. Wrong order = `EXP_BAD_ACCESS` on exit (macOS, reproduced).
8. **Voice backend**: `sounddevice` first, `pyaudio` fallback, decided by import — never make
   PyAudio a required dependency (Windows-only wheels caused "voice never available").
9. **Atomic output**: archives write `*.part` + `os.replace`; a failed operation leaves no
   partial file; never overwrite user files before success.
10. **No silent fake success**: unavailable features return `False` / disable with a message;
    diagnostics that can't query something must not report pass.

## Cross-platform requirements (every change)

All new code must work on Windows, macOS, and Linux (rooms must be identical across platforms).
Use `sys.platform` guards only for genuinely platform-specific features (right-click menu,
Windows installer, drive enumeration). Use helpers instead of raw calls:
`utils.helpers.open_file_or_dir` (not `os.startfile`), `spawn_detached`, `repair_helpers._EXE_SUFFIX`
(no hard-coded `.exe`), `ui.styles.MONO_FONT_FAMILY` (no hard-coded fonts), `locale.getpreferredencoding()`
(never `encoding="gbk"`), `utils.settings._get_app_data_dir()` (no `%APPDATA%`/`C:\` literals).
Check that `code/build.py` packaging still covers the change.

## Version management

`APP_VERSION` lives only in `code/utils/constants.py` (`ui/about_page.py` re-exports; installers
consume). Bug fix → patch; feature → minor; major requires explicit user consent.
Update installers/packaging in step with features.

## GitHub 备份与版本分支（每次更新必须执行）

- **每次更新都要同步 GitHub 备份**：完成任何提交后必须 `git push` 到远端，保持
  GitHub 上的备份与本地一致；不要让提交只留在本地。
- **每个大版本的 `x.0` 保存在独立分支里**：每当发布新的 major 版本（x.0.0）时，
  为该 `x.0` 系列创建并保留一个分支（如 `4.0`、`5.0`），分支内容为该大版本的
  `x.0.0` 快照，作为该大版本的基线存档，后续不在此分支上开发。
- **`main` 分支始终是最新的 `x.n.m`**：所有日常开发（bug 修复、功能更新）都进
  `main`，`main` 上永远是当前大版本的最新 `x.n.m` 版本。

## Docs to read first

- `CLAUDE.md` — full conventions, per-file architecture map, stability invariants (v4.16.1+ hardening)
- `obj/开发文档.md` — architecture, protocols, build/release workflow
- `obj/内容文档.md` — feature inventory, CLI reference, port table, known limitations
- `code/tools/verify_multiplayer.py` / `fuzz_multiplayer.py` — executable spec for the room protocol
- `code/tools/verify_reverse_proxy.py` / `fuzz_reverse_proxy.py` — executable spec for the reverse proxy
  (`obj/开发文档.md` §19 is the frozen design; §19.8 records the defects found while implementing it)
