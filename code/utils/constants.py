DEFAULT_TCP_PORT = 12345
DISCOVERY_PORT = 12346
BUFFER_SIZE = 65536
DISCOVERY_INTERVAL = 5

MSG_TRANSFER_REQUEST = "transfer_request"
MSG_TRANSFER_ACCEPT = "transfer_accept"
MSG_TRANSFER_REJECT = "transfer_reject"
MSG_DISCOVERY = "discovery"
MSG_HERE = "here"
MSG_PING = "ping"
MSG_PONG = "pong"

# Room protocol message types (UDP discovery + TCP join)
ROOM_DISCOVERY_PORT = 12347
MSG_ROOM_ANNOUNCE = "room_announce"
MSG_ROOM_DISCOVERY = "room_discovery"
MSG_ROOM_INFO = "room_info"
MSG_ROOM_JOIN_REQUEST = "room_join_req"
MSG_ROOM_JOIN_CHALLENGE = "room_join_chal"
MSG_ROOM_JOIN_RESPONSE = "room_join_resp"
MSG_ROOM_JOIN_ACCEPT = "room_join_accept"
MSG_ROOM_JOIN_REJECT = "room_join_reject"
MSG_HEARTBEAT = "heartbeat"
MSG_HEARTBEAT_ACK = "heartbeat_ack"

# Session management
HEARTBEAT_INTERVAL = 3      # seconds between heartbeats
HEARTBEAT_TIMEOUT = 10      # seconds before peer is considered disconnected

# Voice call over LAN (UDP)
VOICE_PORT_OFFSET = 2        # voice port = room_port + VOICE_PORT_OFFSET
VOICE_SAMPLE_RATE = 22050    # Hz
VOICE_CHUNK_MS = 20          # ms per audio frame
VOICE_CHANNELS = 1           # mono
VOICE_SAMPLE_WIDTH = 2       # 16-bit = 2 bytes
VOICE_JITTER_MS = 200        # max buffered audio per peer before dropping


def voice_port_for_room(room_port: int) -> int:
    """由房间端口推出语音 UDP 端口。

    原实现直接取 ``room_port + 2``。默认房间端口 12345 时得到 12347 —— 恰好
    与 ``ROOM_DISCOVERY_PORT`` 相同，而房主的 RoomHost 已经占用了该 UDP 端口，
    于是**房主一侧的语音永远无法启动**（播放套接字 bind 失败），却仍然提示
    "语音已连接"。

    这里改成从专用区间取端口：避开房间发现端口和探测端口，只依赖房间端口本身
    （所有参与者算出的结果一致，无需额外协商）。
    """
    try:
        room_port = int(room_port)
    except (TypeError, ValueError):
        room_port = DEFAULT_TCP_PORT

    reserved = {DISCOVERY_PORT, ROOM_DISCOVERY_PORT, DEFAULT_TCP_PORT}
    # 12400-12799 区间：按房间端口散列，保证同一房间的所有参与者一致
    base = 12400 + (room_port * 8) % 400
    port = base
    for _ in range(8):
        if port not in reserved and port <= 65535:
            return port
        port += 1
    return 12500


def is_valid_voice_port(port: int) -> bool:
    try:
        return 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        return False


def get_archive_extensions():
    from core.formats import FormatRegistry
    return FormatRegistry.all_extensions()


def get_compressible_formats():
    from core.formats import FormatRegistry
    return FormatRegistry.compressible_formats()

VOLUME_SUFFIX_PATTERNS = [".7z.001", ".zip.001", ".part01.rar", ".r01"]


# ── 应用标识（命名单一来源）────────────────────────────────────
# 中文名「帽兜工具」用于界面显示；英文名「LIWEN Tool」用于仓库、文档与发行物。
# 可执行文件/构建产物/目录名去掉空格，避免命令行与路径必须加引号。
APP_NAME_ZH = "帽兜工具"
APP_NAME_EN = "LIWEN Tool"
APP_VERSION = "4.16.1"
APP_VENDOR_SLUG = "ShiShangYue"      # ASCII 厂商标识，用于 AppUserModelID / Bundle ID
APP_AUTHOR = "十山月"
APP_EMAIL = "1275679733@qq.com"
APP_NAME_SLUG = "LIWENTool"          # 无空格写法：exe / onedir / 数据目录
APP_REPAIR_SLUG = "LIWENTool_Repair"  # 修复工具
APP_SETUP_SLUG = "LIWENTool_Setup"    # 安装程序

# 上一代英文名，仅用于兼容旧数据目录与旧注册表项，不要在新代码中引用
LEGACY_APP_SLUG = "ArchiveTool"
LEGACY_APP_REPAIR_SLUG = "ArchiveTool_Repair"
