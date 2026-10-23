"""故障检测页面 — 诊断网络端口、局域网发现、房间协议、配置文件、空间索引、AI等模块的健康状态"""
import os
import sys
import json
import glob
import locale
import sqlite3
import socket
import subprocess
import time
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QScrollArea,
    QPushButton, QTextEdit, QFileDialog, QSizePolicy, QFrame,
    QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QFont

from utils.settings import SETTINGS_DIR, SETTINGS_FILE
from utils.room_store import ROOMS_FILE
from utils.paths import data_dir
from ui.styles import MONO_FONT_FAMILY
from utils.logger import info as log_info, error as log_error, warn as log_warn
from utils.repair_shared import serialize_check_results
from utils.repair_helpers import find_repair_exe

STATUS_COLORS = {
    "pass": "#a6e3a1",
    "fail": "#f38ba8",
    "warning": "#f9e2af",
    "running": "#89b4fa",
    "pending": "#6c7086",
}

STATUS_ICONS = {
    "pass": "●",
    "fail": "●",
    "warning": "●",
    "running": "◉",
    "pending": "○",
}

STATUS_LABELS = {
    "pass": "通过",
    "fail": "失败",
    "warning": "警告",
    "running": "检测中…",
    "pending": "待检测",
}


@dataclass
class CheckResult:
    check_id: str = ""
    category: str = ""
    name: str = ""
    status: str = "pending"
    detail: str = ""
    timestamp: str = ""
    suggestions: list = field(default_factory=list)
    repair_id: str = ""


# ═══════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════

def _bind_tcp(port: int) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1)
    try:
        sock.bind(("0.0.0.0", port))
        sock.close()
        return True, ""
    except OSError as e:
        sock.close()
        return False, str(e)


def _bind_udp(port: int) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1)
    try:
        sock.bind(("0.0.0.0", port))
        sock.close()
        return True, ""
    except OSError as e:
        sock.close()
        return False, str(e)




# ═══════════════════════════════════════════════
# Check functions — each returns CheckResult
# ═══════════════════════════════════════════════

def _chk_network_interfaces(log) -> CheckResult:
    """Check LAN interfaces and their broadcast capabilities."""
    try:
        from network.interfaces import get_lan_interfaces
        ifaces = get_lan_interfaces()
        if not ifaces:
            return CheckResult(
                status="warning",
                detail="未检测到有效的局域网接口",
                suggestions=["检查网卡是否启用", "确认已连接到 WiFi 或路由器"])
        lines = []
        for iface in ifaces:
            lines.append(
                f"  {iface['name']}: {iface['ip']}/{iface['prefix']} "
                f"(广播: {iface['broadcast']})")
        return CheckResult(
            status="pass",
            detail=f"发现 {len(ifaces)} 个局域网接口:\n" + "\n".join(lines))
    except Exception as e:
        return CheckResult(status="warning", detail=f"网卡检测异常: {e}")


def _chk_subnet_match(log) -> CheckResult:
    """Check if multiple interfaces exist and whether they're on different subnets."""
    try:
        from network.interfaces import get_lan_interfaces
        ifaces = get_lan_interfaces()
        if len(ifaces) <= 1:
            return CheckResult(status="pass", detail="单网卡环境，无子网冲突风险")
        subnets = set(iface["network"] for iface in ifaces)
        if len(subnets) > 1:
            return CheckResult(
                status="warning",
                detail=f"检测到 {len(subnets)} 个不同子网: {', '.join(subnets)}\n"
                       "多个子网可能导致发现混乱，建议禁用不使用的网卡",
                suggestions=["在 Windows 网络设置中禁用不使用的网卡（如 VMware、VirtualBox）",
                            "确保所有联机玩家在同一子网"])
        return CheckResult(
            status="pass",
            detail=f"所有 {len(ifaces)} 个接口在同一子网: {subnets.pop()}")
    except Exception as e:
        return CheckResult(status="warning", detail=f"子网检测异常: {e}")


def _chk_tcp_12345(log) -> CheckResult:
    ok, err = _bind_tcp(12345)
    if ok:
        return CheckResult(status="pass", detail="TCP 12345 可用（文件传输端口）")
    return CheckResult(status="warning", detail=f"TCP 12345 被占用: {err}",
                       suggestions=["netstat -ano | findstr :12345 查看占用进程"])


def _chk_udp_12346(log) -> CheckResult:
    ok, err = _bind_udp(12346)
    if ok:
        return CheckResult(status="pass", detail="UDP 12346 可用（设备发现端口）")
    return CheckResult(status="warning", detail=f"UDP 12346 被占用: {err}")


def _chk_udp_12347(log) -> CheckResult:
    ok, err = _bind_udp(12347)
    if ok:
        return CheckResult(status="pass", detail="UDP 12347 可用（房间发现端口）")
    return CheckResult(status="warning", detail=f"UDP 12347 被占用: {err}")


def _chk_udp_broadcast(log) -> CheckResult:
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 55555))
        sock.sendto(b"DIAG_BCAST", ("255.255.255.255", 55555))
        sock.settimeout(1.5)
        try:
            data, addr = sock.recvfrom(1024)
            return CheckResult(status="pass", detail=f"UDP广播正常（收到 {addr} 回环）")
        except socket.timeout:
            return CheckResult(status="pass", detail="UDP广播已发送（无回环属正常）")
    except Exception as e:
        return CheckResult(status="fail", detail=f"UDP广播失败: {e}",
                           suggestions=["检查网络适配器是否启用", "检查防火墙设置"])
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _chk_device_discovery(log) -> CheckResult:
    try:
        # 注意：模块只导出 DiscoveryService / get_discovery_service()，
        # 旧代码里的 DeviceDiscovery 早已不存在 —— 每次调用都会 ImportError，
        # 设备发现检查因此永远走 warning 分支。
        from network.discovery import get_discovery_service
        log("扫描设备发现 (UDP 12346, 3s)…")
        d = get_discovery_service()
        found = d.scan_now(3.0)
        if found:
            lines = [f"发现 {len(found)} 个设备:"]
            for did, info in list(found.items())[:5]:
                name = info.get("username", info.get("ip", did))
                lines.append(f"  {name} @ {info.get('ip', did)}")
            return CheckResult(status="pass", detail="\n".join(lines))
        return CheckResult(status="pass", detail="未发现其他设备（需同一局域网内有其他设备运行本程序）")
    except Exception as e:
        return CheckResult(status="warning", detail=f"设备发现异常: {e}")


def _chk_room_scan(log) -> CheckResult:
    try:
        from network.room_protocol import RoomDiscoveryClient
        log("扫描局域网房间…")
        client = RoomDiscoveryClient()
        rooms = client.scan(timeout=3.0)
        if rooms:
            lines = [f"扫描到 {len(rooms)} 个房间:"]
            for rid, info in list(rooms.items())[:5]:
                lines.append(f"  {info.get('room_name', rid)} @ {info.get('host_ip', '?')}")
            return CheckResult(status="pass", detail="\n".join(lines))
        return CheckResult(status="pass", detail="未扫描到房间（正常，除非有其他用户正在创建房间）")
    except Exception as e:
        return CheckResult(status="warning", detail=f"房间扫描异常: {e}",
                           suggestions=["确认 UDP 12347 未被防火墙阻止"])


def _chk_audio_backend(log) -> CheckResult:
    """音频后端检测：sounddevice（推荐）或 PyAudio（回退）。

    后端库只是"能不能用"的一半，另一半是设备是否真的能打开 —— 这里顺带
    报告设备数量，让"库在但没设备"这种情况也能被看出来。
    """
    try:
        from network.voice_protocol import (
            is_voice_available, voice_backend_name, INSTALL_HINT)
        backend = voice_backend_name()
        if not is_voice_available():
            return CheckResult(
                status="warning",
                detail="音频后端未安装，房间语音不可用",
                suggestions=[INSTALL_HINT])
        count = _count_audio_devices(backend)
        if count < 0:
            # 设备枚举失败 ≠ 通过：不能报「可用，-1 个设备」这种自相矛盾的结论
            return CheckResult(
                status="warning",
                detail=f"{backend} 已安装，但无法枚举音频设备",
                suggestions=["确认系统允许本程序访问麦克风/扬声器",
                             "重新插拔或重启音频设备后重试"])
        if count == 0:
            return CheckResult(
                status="warning",
                detail=f"{backend} 已安装，但未检测到音频设备",
                suggestions=["检查系统音频设备/麦克风是否被禁用"])
        return CheckResult(
            status="pass", detail=f"{backend} 可用，{count} 个音频设备")
    except Exception as e:
        return CheckResult(status="warning", detail=f"音频后端异常: {e}")


def _count_audio_devices(backend: str) -> int:
    """枚举音频设备数量；后端查询失败时返回 -1（不阻塞检测流程）。"""
    try:
        if backend == "sounddevice":
            import sounddevice as sd
            return len(sd.query_devices())
        if backend == "pyaudio":
            import pyaudio
            pa = pyaudio.PyAudio()
            try:
                return pa.get_device_count()
            finally:
                pa.terminate()
    except Exception as e:
        log_warn(f"枚举音频设备失败: {e}")
    return -1


def _chk_voice_protocol(log) -> CheckResult:
    try:
        from network.voice_protocol import VoiceClient
        return CheckResult(status="pass", detail="voice_protocol 模块正常")
    except Exception as e:
        return CheckResult(status="warning", detail=f"语音协议模块异常: {e}")


def _chk_config_integrity(log) -> CheckResult:
    issues = []
    for label, path in [
        ("settings.json", SETTINGS_FILE),
        ("rooms.json", ROOMS_FILE),
    ]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
                issues.append(f"{label}: 正常")
            except (json.JSONDecodeError, OSError) as e:
                issues.append(f"{label}: 损坏 — {e}")
        else:
            issues.append(f"{label}: 不存在（首次运行自动创建）")

    status = "warning" if any("损坏" in i for i in issues) else "pass"
    return CheckResult(status=status, detail="\n".join(issues),
                       suggestions=[f"配置目录: {SETTINGS_DIR}"] if status == "warning" else [])


def _chk_compress_modules(log) -> CheckResult:
    """Check that core compression modules are available."""
    modules_ok = []
    modules_bad = []
    for mod_name, display in [
        ("zipfile", "ZIP"),
        ("tarfile", "TAR"),
        ("py7zr", "7z"),
    ]:
        try:
            __import__(mod_name)
            modules_ok.append(display)
        except ImportError:
            modules_bad.append(display)
        except Exception:
            modules_bad.append(display)

    if modules_bad:
        return CheckResult(
            status="warning",
            detail=f"以下压缩模块不可用: {', '.join(modules_bad)}\n可用: {', '.join(modules_ok)}",
            suggestions=[f"pip install {' '.join(m.lower() for m in modules_bad)}"])
    return CheckResult(status="pass", detail=f"压缩模块全部可用: {', '.join(modules_ok)}")


# ── Space Index checks ─────────────────────────────────────────

def _chk_si_sqlite_avail(log) -> CheckResult:
    try:
        import sqlite3 as _sql
        conn = _sql.connect(":memory:")
        conn.execute("CREATE TABLE test(x INTEGER)")
        conn.execute("INSERT INTO test VALUES (1)")
        row = conn.execute("SELECT x FROM test").fetchone()
        conn.close()
        ver = _sql.sqlite_version
        return CheckResult(status="pass",
                          detail=f"sqlite3 可用 (版本 {ver})，内存数据库读写正常")
    except ImportError:
        return CheckResult(status="fail", detail="sqlite3 模块不可用",
                          suggestions=["Python 安装可能不完整，请重新安装 Python"])
    except Exception as e:
        return CheckResult(status="fail", detail=f"sqlite3 异常: {e}")


def _chk_si_db_writable(log) -> CheckResult:
    try:
        from core.file_index import _index_db_path
        test_path = _index_db_path("C:\\")
        test_path = test_path.replace("space_index_C.db", "_diag_test.db")
        from core.file_index import IndexDatabase
        db = IndexDatabase(test_path)
        db.set_meta("test_key", "test_value")
        val = db.get_meta("test_key")
        db.close()
        try:
            os.remove(test_path)
            os.remove(test_path + "-wal")
        except OSError:
            pass
        try:
            os.remove(test_path + "-shm")
        except OSError:
            pass
        if val == "test_value":
            return CheckResult(status="pass",
                              detail=f"索引数据库可读写，WAL 模式正常\n路径: {os.path.dirname(test_path)}")
        return CheckResult(status="warning", detail="索引数据库写入验证失败（读取值与写入值不匹配）")
    except PermissionError as e:
        return CheckResult(status="warning",
                          detail=f"索引数据库目录无写权限: {e}",
                          suggestions=["以管理员身份运行程序", "检查目录权限设置"])
    except Exception as e:
        return CheckResult(status="warning", detail=f"索引数据库可写性检测异常: {e}")


def _chk_si_scan_engine(log) -> CheckResult:
    errors = []
    try:
        from core.file_index import IndexDatabase, ScanEngine
        log("  IndexDatabase + ScanEngine 导入成功")
    except ImportError as e:
        errors.append(f"core.file_index: {e}")
    try:
        from core.workers import SpaceScanWorker
        log("  SpaceScanWorker 导入成功")
    except ImportError as e:
        errors.append(f"core.workers.SpaceScanWorker: {e}")
    try:
        from ui.file_list_model import FileListModel
        log("  FileListModel 导入成功")
    except ImportError as e:
        errors.append(f"ui.file_list_model: {e}")
    if errors:
        return CheckResult(status="fail",
                          detail="扫描引擎模块导入失败:\n" + "\n".join(errors),
                          suggestions=["检查程序文件完整性", "重新安装程序"])
    return CheckResult(status="pass", detail="扫描引擎模块全部可用 (IndexDatabase, ScanEngine, SpaceScanWorker, FileListModel)")


def _chk_si_db_integrity(log) -> CheckResult:
    try:
        from core.file_index import _index_db_path
    except ImportError:
        return CheckResult(status="fail", detail="无法导入 core.file_index 模块")

    app_dir = data_dir()

    pattern = os.path.join(app_dir, "space_index_*.db")
    db_files = glob.glob(pattern)
    if not db_files:
        return CheckResult(status="pass", detail="未发现空间索引缓存文件（首次使用或已清理）")

    lines = []
    corrupted = 0
    ok_count = 0
    for db_path in sorted(db_files):
        drive = os.path.basename(db_path).replace("space_index_", "").replace(".db", "")
        # 连接必须在 finally 里关闭：原来的 conn.close() 在 try 内部，
        # 一旦 PRAGMA 或 sqlite_master 查询抛异常就会泄漏连接，
        # Windows 上文件被占用后"一键修复"删不掉这个损坏的索引库。
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=2)
            cur = conn.execute("PRAGMA integrity_check")
            integrity = cur.fetchone()[0]
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            table_names = {t[0] for t in tables}
            has_files = "files" in table_names
            has_meta = "scan_meta" in table_names
            if integrity == "ok" and has_files and has_meta:
                ok_count += 1
                lines.append(f"  {drive}盘: 正常")
            else:
                issues = []
                if integrity != "ok":
                    issues.append(f"完整性: {integrity}")
                if not has_files:
                    issues.append("缺少 files 表")
                if not has_meta:
                    issues.append("缺少 scan_meta 表")
                corrupted += 1
                lines.append(f"  {drive}盘: 异常 — {', '.join(issues)}")
        except sqlite3.DatabaseError as e:
            corrupted += 1
            lines.append(f"  {drive}盘: 数据库损坏 — {e}")
        except Exception as e:
            corrupted += 1
            lines.append(f"  {drive}盘: 无法打开 — {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    summary = f"发现 {len(db_files)} 个缓存文件: {ok_count} 正常"
    if corrupted > 0:
        summary += f", {corrupted} 异常"
    detail = summary + "\n" + "\n".join(lines)

    if corrupted > 0:
        return CheckResult(status="warning", detail=detail,
                          suggestions=["使用「一键修复」重置损坏的索引数据库",
                                      f"或手动删除: {app_dir}\\space_index_*.db"])
    return CheckResult(status="pass", detail=detail)


# ── AI checks ──────────────────────────────────────────────────

def _chk_ai_requests_lib(log) -> CheckResult:
    try:
        import requests
        ver = getattr(requests, "__version__", "未知")
        log(f"  requests 版本: {ver}")
        return CheckResult(status="pass", detail=f"requests 库可用 (版本 {ver})")
    except ImportError:
        return CheckResult(status="fail", detail="requests 库未安装",
                          suggestions=["pip install requests"])


def _chk_ai_api_config(log) -> CheckResult:
    try:
        from utils.settings import settings as _s
    except ImportError:
        return CheckResult(status="fail", detail="无法加载设置模块")

    provider = _s.get_active_provider()
    cfg = _s.get_provider_config()
    api_url = cfg.get("api_url", "").strip()
    api_key = cfg.get("api_key", "").strip()
    model = cfg.get("model", "").strip()

    issues = []
    is_ollama = ("ollama" in provider.lower() or "localhost" in api_url or
                 "127.0.0.1" in api_url)

    if not api_url:
        issues.append("API 地址未配置")
    elif not (api_url.startswith("http://") or api_url.startswith("https://")):
        issues.append(f"API 地址格式无效: {api_url}")
    else:
        try:
            parsed = urlparse(api_url)
            if not parsed.hostname:
                issues.append(f"无法解析 API 地址主机名: {api_url}")
        except Exception:
            issues.append(f"API 地址解析失败: {api_url}")

    if not api_key and not is_ollama:
        issues.append("API Key 未配置")
    elif api_key and len(api_key) < 3:
        issues.append("API Key 过短（可能无效）")

    if not model:
        issues.append("模型未选择")

    if issues:
        return CheckResult(
            status="warning",
            detail=f"当前服务商: {provider}\n" + "\n".join(f"  ✗ {i}" for i in issues),
            suggestions=["点击「选择模型」配置 API 参数", "在 AI 页面打开 API 配置对话框"])
    url_display = api_url[:60] + ("…" if len(api_url) > 60 else "")
    key_mask = api_key[:4] + "***" + api_key[-4:] if len(api_key) >= 8 else "***"
    return CheckResult(
        status="pass",
        detail=f"当前服务商: {provider}\n  地址: {url_display}\n  Key: {key_mask}\n  模型: {model}")


def _chk_ai_api_connectivity(log) -> CheckResult:
    try:
        from utils.settings import settings as _s
    except ImportError:
        return CheckResult(status="fail", detail="无法加载设置模块")

    cfg = _s.get_provider_config()
    api_url = cfg.get("api_url", "").strip()
    if not api_url:
        return CheckResult(status="pass", detail="API 地址未配置，跳过连通性检测")

    try:
        parsed = urlparse(api_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception:
        return CheckResult(status="warning", detail=f"无法解析 API 地址: {api_url}")

    if host in ("localhost", "127.0.0.1", "::1"):
        # Localhost: check TCP port
        log(f"  检测本地服务 {host}:{port}…")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            sock.connect((host, port))
            sock.close()
            return CheckResult(status="pass", detail=f"本地服务 {host}:{port} 可达")
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            return CheckResult(status="warning",
                              detail=f"本地服务 {host}:{port} 不可达: {e}",
                              suggestions=["确认 Ollama 或其他本地服务已启动",
                                          "检查本地服务端口配置"])
        finally:
            sock.close()

    # Remote: DNS resolution
    try:
        log(f"  解析 DNS: {host}…")
        addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        ips = list(set(a[4][0] for a in addrs))
        log(f"  解析结果: {ips}")
        return CheckResult(status="pass",
                          detail=f"API 端点 DNS 解析成功: {host} → {', '.join(ips[:4])}")
    except socket.gaierror as e:
        return CheckResult(status="warning",
                          detail=f"API 端点 DNS 解析失败: {host} — {e}",
                          suggestions=["检查网络连接", "确认 DNS 设置正确",
                                      "如使用代理，确认代理已配置"])
    except Exception as e:
        return CheckResult(status="warning", detail=f"API 连通性检测异常: {e}")


def _chk_ai_conv_storage(log) -> CheckResult:
    from utils.settings import SETTINGS_DIR
    conv_dir = os.path.join(SETTINGS_DIR, "ai_conversations")
    index_file = os.path.join(conv_dir, "index.json")

    if not os.path.exists(conv_dir):
        return CheckResult(status="pass", detail="对话存储目录不存在（未使用 AI 对话功能）")

    lines = []
    total_files = 0
    corrupted = 0

    # Check index.json
    conv_list = []
    if os.path.exists(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            conv_list = data.get("conversations", [])
            lines.append(f"  对话索引: {len(conv_list)} 个对话")
        except (json.JSONDecodeError, OSError) as e:
            corrupted += 1
            lines.append(f"  对话索引损坏: {e}")
    else:
        lines.append("  对话索引文件不存在")

    # Check individual conversation files
    for conv in conv_list:
        conv_id = conv.get("id", "")
        if not conv_id:
            continue
        conv_file = os.path.join(conv_dir, f"{conv_id}.json")
        total_files += 1
        if os.path.exists(conv_file):
            try:
                with open(conv_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                msg_count = len(cdata.get("messages", []))
                log(f"  {conv_id}: {msg_count} 条消息 — 正常")
            except (json.JSONDecodeError, OSError) as e:
                corrupted += 1
                lines.append(f"  {conv_id}: 文件损坏 — {e}")
        else:
            lines.append(f"  {conv_id}: 文件缺失")

    if corrupted > 0:
        status = "warning"
        suggestions = ["使用「一键修复」修复或清理损坏的对话文件"]
    else:
        status = "pass"

    detail = f"对话存储: {len(conv_list)} 个对话, {total_files} 个文件"
    if corrupted:
        detail += f", {corrupted} 个损坏"
    if lines:
        detail += "\n" + "\n".join(lines)

    return CheckResult(status=status, detail=detail, suggestions=(
        suggestions if corrupted > 0 else []))


# ── HTTP Request checks ─────────────────────────────────────────

def _chk_hr_requests_lib(log) -> CheckResult:
    try:
        import requests
        ver = getattr(requests, "__version__", "未知")
        log(f"  requests 版本: {ver}")
        return CheckResult(status="pass", detail=f"requests 库可用 (版本 {ver})")
    except ImportError:
        return CheckResult(status="fail", detail="requests 库未安装",
                          suggestions=["pip install requests"])
    except Exception as e:
        return CheckResult(status="fail", detail=f"requests 导入异常: {e}")


def _chk_hr_urllib_avail(log) -> CheckResult:
    try:
        import urllib.parse
        result = urllib.parse.urlencode({"test": "ok"})
        if result == "test=ok":
            return CheckResult(status="pass", detail="urllib.parse 可用，URL 编码正常")
        return CheckResult(status="warning", detail=f"urllib.parse 行为异常: urlencode({result})")
    except ImportError:
        return CheckResult(status="fail", detail="urllib 模块不可用")
    except Exception as e:
        return CheckResult(status="fail", detail=f"urllib 异常: {e}")


def _chk_hr_worker_import(log) -> CheckResult:
    try:
        from core.workers import HttpRequestWorker
        sigs = ["finished", "error", "status"]
        missing = [s for s in sigs if not hasattr(HttpRequestWorker, s)]
        if missing:
            return CheckResult(status="fail",
                              detail=f"HttpRequestWorker 缺少信号: {', '.join(missing)}")
        log("  HttpRequestWorker 导入成功")
        return CheckResult(status="pass", detail="HttpRequestWorker 模块可用 (finished, error, status)")
    except ImportError as e:
        return CheckResult(status="fail", detail=f"HttpRequestWorker 导入失败: {e}",
                          suggestions=["检查程序文件完整性", "重新安装程序"])


def _chk_hr_page_import(log) -> CheckResult:
    try:
        from ui.http_request_page import HttpRequestPage
        log("  HttpRequestPage 导入成功")
        return CheckResult(status="pass", detail="HttpRequestPage 模块可用")
    except ImportError as e:
        return CheckResult(status="fail", detail=f"HttpRequestPage 导入失败: {e}",
                          suggestions=["检查程序文件完整性", "检查依赖: pip install PyQt6 requests"])
    except Exception as e:
        return CheckResult(status="fail", detail=f"HttpRequestPage 导入异常: {e}")


def _chk_hr_history_storage(log) -> CheckResult:
    from utils.settings import SETTINGS_DIR
    history_file = os.path.join(SETTINGS_DIR, "http_request_history.json")
    collections_file = os.path.join(SETTINGS_DIR, "http_request_collections.json")

    lines = []
    has_issue = False

    for label, path, expected_type in [
        ("请求历史", history_file, "list"),
        ("请求收藏集", collections_file, "dict"),
    ]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if expected_type == "list" and not isinstance(data, list):
                    has_issue = True
                    lines.append(f"  {label}: 格式异常 (期望列表，实际 {type(data).__name__})")
                elif expected_type == "dict" and not isinstance(data, dict):
                    has_issue = True
                    lines.append(f"  {label}: 格式异常 (期望字典，实际 {type(data).__name__})")
                else:
                    count = len(data)
                    log(f"  {label}: {count} 条记录 — 正常")
                    lines.append(f"  {label}: {count} 条记录")
            except (json.JSONDecodeError, OSError) as e:
                has_issue = True
                lines.append(f"  {label}: 文件损坏 — {e}")
        else:
            lines.append(f"  {label}: 未创建（首次使用自动创建）")

    if has_issue:
        return CheckResult(status="warning",
                          detail="请求存储存在问题:\n" + "\n".join(lines),
                          suggestions=["使用「一键修复」修复或重置请求存储文件"])
    return CheckResult(status="pass", detail="请求存储正常:\n" + "\n".join(lines))


def _chk_hr_variable_storage(log) -> CheckResult:
    from utils.settings import SETTINGS_DIR
    var_file = os.path.join(SETTINGS_DIR, "http_variables.json")
    if not os.path.exists(var_file):
        return CheckResult(status="pass", detail="变量存储文件未创建（首次使用自动创建）")

    try:
        with open(var_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        keys_ok = []
        for key in ["global", "environments", "active_environment"]:
            if key in data:
                keys_ok.append(key)
        missing = set(["global", "environments", "active_environment"]) - set(keys_ok)
        if missing:
            return CheckResult(status="warning",
                              detail=f"变量存储格式异常，缺少字段: {', '.join(missing)}",
                              suggestions=["使用「一键修复」重置变量存储"])
        env_count = len(data.get("environments", {}))
        global_count = len(data.get("global", {}))
        active = data.get("active_environment", "(无)")
        log(f"  变量存储: {global_count} 全局变量, {env_count} 环境, 激活: {active}")
        return CheckResult(status="pass",
                          detail=f"变量存储正常: {global_count} 全局变量, {env_count} 环境\n  激活环境: {active}")
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(status="warning", detail=f"变量存储文件损坏: {e}",
                          suggestions=["使用「一键修复」重置变量存储"])


def _chk_hr_basic_connectivity(log) -> CheckResult:
    """Test basic internet connectivity via TCP to 8.8.8.8:443."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        log("  尝试连接 8.8.8.8:443…")
        sock.connect(("8.8.8.8", 443))
        sock.close()
        return CheckResult(status="pass", detail="基础网络连通性正常 (8.8.8.8:443 可达)")
    except socket.timeout:
        return CheckResult(status="warning", detail="基础网络连通性: 连接超时 (8.8.8.8:443 不可达)",
                          suggestions=["检查网络连接", "检查防火墙是否阻止出站连接",
                                      "如使用代理，确认代理已配置"])
    except OSError as e:
        return CheckResult(status="warning", detail=f"基础网络连通性: 网络错误 — {e}",
                          suggestions=["检查网卡是否启用", "确认已连接到互联网"])
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════
# Repair functions
# ═══════════════════════════════════════════════

def _subprocess_flags():
    """Return platform-appropriate subprocess creation flags."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


_SYSTEM_ENCODING = locale.getpreferredencoding(do_setlocale=False)


def _repair_free_port(log, port: int) -> tuple[bool, str]:
    """Find and kill the process occupying a specific TCP/UDP port."""
    if sys.platform != "win32":
        return _repair_free_port_unix(log, port)
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
            encoding=_SYSTEM_ENCODING, errors="replace",
            creationflags=_subprocess_flags())
        pid = None
        port_str = f":{port}"
        for line in result.stdout.splitlines():
            if port_str in line and ("LISTENING" in line or "ESTABLISHED" in line
                                      or "UDP" in line):
                parts = line.strip().split()
                if parts:
                    pid = parts[-1]
                    break
        if not pid:
            return True, f"端口 {port} 当前未被占用"
        if pid == "0":
            return False, f"端口 {port} 被系统进程占用，无法自动释放"

        log(f"端口 {port} 被 PID {pid} 占用，尝试终止…")
        subprocess.run(
            ["taskkill", "/F", "/PID", pid],
            capture_output=True, text=True, timeout=10,
            creationflags=_subprocess_flags())
        result2 = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
            encoding=_SYSTEM_ENCODING, errors="replace",
            creationflags=_subprocess_flags())
        for line in result2.stdout.splitlines():
            if port_str in line and ("LISTENING" in line or "UDP" in line):
                return False, f"尝试终止 PID {pid} 但端口 {port} 仍被占用"
        return True, f"已终止 PID {pid}，端口 {port} 已释放"
    except Exception as e:
        return False, f"释放端口 {port} 失败: {e}"


def _repair_free_port_unix(log, port: int) -> tuple[bool, str]:
    """Unix implementation: use lsof to find and kill port occupiers."""
    import signal
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=10)
        pids = result.stdout.strip().split()
        if not pids:
            return True, f"端口 {port} 当前未被占用"
        for pid_str in pids:
            try:
                pid = int(pid_str)
                log(f"端口 {port} 被 PID {pid} 占用，尝试终止…")
                os.kill(pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
        result2 = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5)
        if result2.stdout.strip():
            return False, f"尝试终止但端口 {port} 仍被占用"
        return True, f"已释放端口 {port}"
    except Exception as e:
        return False, f"释放端口 {port} 失败: {e}"


def _repair_install_audio(log) -> tuple[bool, str]:
    """通过 pip 安装推荐的音频后端 sounddevice。

    不再安装 PyAudio：它在 PyPI 上只有 Windows wheel，macOS/Linux 需要本地
    编译 PortAudio，Python 3.14 更是没有 wheel —— 这正是此前"语音永远装不上"
    的根因。sounddevice 是全平台 wheel，装完即用。
    """
    try:
        # 冻结（PyInstaller）后 sys.executable 是应用自己的 EXE，`-m pip`
        # 只会把程序再启动一遍 —— 与其假装安装成功，不如如实说明。
        if getattr(sys, "frozen", False):
            return False, (
                "打包版已内置音频后端，无法（也无需）通过 pip 安装。\n"
                "请确认系统允许本程序访问麦克风/扬声器；若后端确实缺失，"
                "请重新安装或更新本程序。")
        log("正在安装 sounddevice…")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "sounddevice"],
            capture_output=True, text=True, timeout=180,
            creationflags=_subprocess_flags())
        if result.returncode == 0:
            # 让 voice_protocol 重新探测后端（模块级缓存了上一次结果）
            try:
                import network.voice_protocol as vp
                vp._BACKEND_PROBED = False
                vp._BACKEND_NAME = None
            except Exception:
                pass
            from network.voice_protocol import voice_backend_name
            backend = voice_backend_name()
            if backend:
                return True, f"音频后端已就绪（{backend}）"
            return False, "pip 安装完成但仍未探测到可用后端，请重启程序后重试"
        else:
            detail = result.stderr.strip() or result.stdout.strip()
            return False, f"sounddevice 安装失败: {detail[:300]}"
    except Exception as e:
        return False, f"sounddevice 安装异常: {e}"


def _repair_repair_config(log) -> tuple[bool, str]:
    """Repair corrupted configuration files by resetting to defaults."""
    repaired = []
    for label, path, defaults in [
        ("settings.json", SETTINGS_FILE,
         {"username": socket.gethostname(), "theme": "catppuccin-mocha",
          "default_receive_path": os.path.join(os.path.expanduser("~"), "Downloads"),
          "default_password": "", "auto_accept": False}),
        ("rooms.json", ROOMS_FILE, []),
    ]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
            except (json.JSONDecodeError, OSError):
                backup = path + ".bak"
                try:
                    os.rename(path, backup)
                    log(f"已备份损坏文件: {backup}")
                except OSError:
                    os.remove(path)
                    log(f"已删除损坏文件: {path}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(defaults, f, ensure_ascii=False, indent=2)
                repaired.append(label)
                log(f"已重建: {path}")
    if repaired:
        return True, f"已修复 {len(repaired)} 个配置文件: {', '.join(repaired)}"
    return True, "配置文件状态正常，无需修复"


def _repair_reset_index_db(log) -> tuple[bool, str]:
    """Delete all corrupted space index database files."""
    app_dir = data_dir()

    pattern = os.path.join(app_dir, "space_index_*.db")
    db_files = glob.glob(pattern)
    # Also clean WAL/SHM files
    wal_pattern = os.path.join(app_dir, "space_index_*.db-*")
    db_files.extend(glob.glob(wal_pattern))

    if not db_files:
        return True, "没有需要清理的空间索引数据库文件"

    deleted = 0
    for db_path in sorted(set(db_files)):
        try:
            bak = db_path + ".diag_bak"
            os.rename(db_path, bak)
            os.remove(bak)
            deleted += 1
            log(f"  已删除: {os.path.basename(db_path)}")
        except OSError as e:
            try:
                os.remove(db_path)
                deleted += 1
                log(f"  已强制删除: {os.path.basename(db_path)}")
            except OSError:
                return False, f"无法删除 {os.path.basename(db_path)}: {e}"

    return True, f"已清理 {deleted} 个空间索引数据库文件"


def _repair_repair_ai_config(log) -> tuple[bool, str]:
    """Reset AI provider configs to defaults while preserving current provider choice."""
    try:
        from utils.settings import settings as _s, DEFAULT_SETTINGS
        current_provider = _s.get_active_provider()
        _s._data["ai_providers"] = dict(DEFAULT_SETTINGS["ai_providers"])
        _s._data["ai_active_provider"] = current_provider
        _s._data["ai_custom_models"] = dict(DEFAULT_SETTINGS.get("ai_custom_models", {}))
        _s.save()
        log(f"  AI 配置已重置为默认值，保留当前服务商: {current_provider}")
        return True, f"AI 配置已重置为默认值（保留当前服务商: {current_provider}）"
    except Exception as e:
        return False, f"重置 AI 配置失败: {e}"


def _repair_repair_conv_storage(log) -> tuple[bool, str]:
    """Repair corrupted conversation storage."""
    from utils.settings import SETTINGS_DIR
    conv_dir = os.path.join(SETTINGS_DIR, "ai_conversations")
    index_file = os.path.join(conv_dir, "index.json")

    try:
        os.makedirs(conv_dir, exist_ok=True)
    except OSError as e:
        return False, f"无法创建对话存储目录: {e}"

    repaired = 0
    deleted_count = 0

    # Rebuild index from existing conversation files
    valid_convs = []
    if os.path.exists(conv_dir):
        for fname in os.listdir(conv_dir):
            if not fname.endswith(".json") or fname == "index.json":
                continue
            fpath = os.path.join(conv_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cid = data.get("id", fname.replace(".json", ""))
                valid_convs.append({
                    "id": cid,
                    "title": data.get("title", "未命名对话"),
                    "created_at": data.get("created_at", datetime.now().isoformat()),
                    "updated_at": data.get("updated_at", datetime.now().isoformat()),
                })
            except (json.JSONDecodeError, OSError):
                # Corrupted — rename to .bak
                try:
                    bak_path = fpath + ".diag_bak"
                    os.rename(fpath, bak_path)
                    deleted_count += 1
                    log(f"  已备份损坏文件: {os.path.basename(fpath)}")
                except OSError:
                    try:
                        os.remove(fpath)
                        deleted_count += 1
                    except OSError:
                        pass

    # Write rebuilt index
    try:
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump({"conversations": valid_convs}, f, ensure_ascii=False, indent=2)
        repaired += 1
    except OSError as e:
        return False, f"写入对话索引失败: {e}"

    msg_parts = [f"对话索引已重建 ({len(valid_convs)} 个对话)"]
    if deleted_count:
        msg_parts.append(f"{deleted_count} 个损坏文件已备份")
    return True, "，".join(msg_parts)


def _repair_repair_http_history(log) -> tuple[bool, str]:
    """Repair corrupted HTTP request history and collections files."""
    from utils.settings import SETTINGS_DIR
    history_file = os.path.join(SETTINGS_DIR, "http_request_history.json")
    collections_file = os.path.join(SETTINGS_DIR, "http_request_collections.json")

    repaired = []
    for label, path, default in [
        ("请求历史", history_file, []),
        ("请求收藏集", collections_file, {}),
    ]:
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
        except OSError:
            pass

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
            except (json.JSONDecodeError, OSError):
                bak = path + ".diag_bak"
                try:
                    os.rename(path, bak)
                    log(f"  已备份: {os.path.basename(path)} → {os.path.basename(bak)}")
                except OSError:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(default, f, ensure_ascii=False, indent=2)
                    repaired.append(label)
                    log(f"  已重建: {os.path.basename(path)}")
                except OSError as e:
                    return False, f"重建 {label} 失败: {e}"
        else:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(default, f, ensure_ascii=False, indent=2)
                repaired.append(label)
                log(f"  已创建: {os.path.basename(path)}")
            except OSError as e:
                return False, f"创建 {label} 失败: {e}"

    if repaired:
        return True, f"已修复请求存储: {', '.join(repaired)}"
    return True, "请求存储文件状态正常，无需修复"


def _repair_repair_http_variables(log) -> tuple[bool, str]:
    """Reset HTTP variables file to defaults."""
    from utils.settings import SETTINGS_DIR
    from utils.variable_store import _default_data
    var_file = os.path.join(SETTINGS_DIR, "http_variables.json")

    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
    except OSError:
        pass

    if os.path.exists(var_file):
        try:
            with open(var_file, "r", encoding="utf-8") as f:
                json.load(f)
            return True, "HTTP 变量存储文件状态正常，无需修复"
        except (json.JSONDecodeError, OSError):
            bak = var_file + ".diag_bak"
            try:
                os.rename(var_file, bak)
                log(f"  已备份损坏文件: {os.path.basename(var_file)}")
            except OSError:
                try:
                    os.remove(var_file)
                except OSError:
                    pass

    try:
        defaults = _default_data()
        with open(var_file, "w", encoding="utf-8") as f:
            json.dump(defaults, f, ensure_ascii=False, indent=2)
        log(f"  HTTP 变量已重置为默认值")
        return True, "HTTP 变量存储已重置为默认值"
    except OSError as e:
        return False, f"重置 HTTP 变量失败: {e}"


# Registry: repair_id → (display_name, function, needs_admin, mode)
# mode: "lan" = only available in LAN detection mode, "basic" = only in basic mode
REPAIR_REGISTRY: dict[str, tuple[str, callable, bool, str]] = {
    "free_port":          ("释放被占端口",          None,                         False, "lan"),
    "install_audio":      ("安装音频后端",        _repair_install_audio,        False, "lan"),
    "repair_config":      ("修复配置文件",          _repair_repair_config,        False, "basic"),
    "reset_index_db":       ("重置空间索引数据库",    _repair_reset_index_db,         False, "basic"),
    "repair_ai_config":     ("重置AI配置",            _repair_repair_ai_config,       False, "basic"),
    "repair_conv_storage":  ("修复对话存储",          _repair_repair_conv_storage,    False, "basic"),
    "repair_http_history":  ("修复请求历史",          _repair_repair_http_history,    False, "basic"),
    "repair_http_variables":("重置HTTP变量",          _repair_repair_http_variables,  False, "basic"),
}


# ═══════════════════════════════════════════════
# Check registries — split by detection mode
# ═══════════════════════════════════════════════

# LAN detection: everything related to LAN transfer, rooms, and voice
# （联机已改为纯应用层组网，不再有 VPN/虚拟网卡检测项）
LAN_CHECKS = [
    # (check_id, category, name, function, repair_id)
    ("network_interfaces",   "局域网接口", "网卡接口检测",      _chk_network_interfaces,  ""),
    ("subnet_match",         "局域网接口", "子网冲突检测",      _chk_subnet_match,        ""),
    ("tcp_12345",            "网络端口",   "TCP 端口 12345",    _chk_tcp_12345,           "free_port"),
    ("udp_12346",            "网络端口",   "UDP 端口 12346",    _chk_udp_12346,           "free_port"),
    ("udp_12347",            "网络端口",   "UDP 端口 12347",    _chk_udp_12347,           "free_port"),
    ("udp_broadcast",        "局域网发现", "UDP 广播能力",      _chk_udp_broadcast,       ""),
    ("device_discovery",     "局域网发现", "设备发现检测",      _chk_device_discovery,    ""),
    ("room_scan",            "房间协议",   "局域网房间扫描",    _chk_room_scan,           ""),
    ("audio_backend",        "语音模块",   "音频后端可用性",    _chk_audio_backend,       "install_audio"),
    ("voice_protocol",       "语音模块",   "语音协议模块",      _chk_voice_protocol,      "install_audio"),
]

# Basic detection: configuration, compression modules
BASIC_CHECKS = [
    # (check_id, category, name, function, repair_id)
    ("compress_modules",     "压缩功能",   "压缩模块可用性",     _chk_compress_modules,    ""),
    ("config_integrity",     "配置文件",   "配置文件完整性",     _chk_config_integrity,    "repair_config"),
    ("si_sqlite_avail",      "空间索引",   "sqlite3 模块可用性", _chk_si_sqlite_avail,     ""),
    ("si_db_writable",       "空间索引",   "索引数据库可写性",   _chk_si_db_writable,      "reset_index_db"),
    ("si_scan_engine",       "空间索引",   "扫描引擎模块",       _chk_si_scan_engine,      ""),
    ("si_db_integrity",      "空间索引",   "缓存数据库完整性",   _chk_si_db_integrity,     "reset_index_db"),
    ("ai_requests_lib",      "AI功能",     "requests 库可用性",   _chk_ai_requests_lib,       ""),
    ("ai_api_config",        "AI功能",     "API 配置完整性",      _chk_ai_api_config,         "repair_ai_config"),
    ("ai_api_connectivity",  "AI功能",     "API 端点可达性",      _chk_ai_api_connectivity,   ""),
    ("ai_conv_storage",      "AI功能",     "对话存储完整性",      _chk_ai_conv_storage,       "repair_conv_storage"),
    ("hr_requests_lib",      "请求发送",   "requests 库可用性",   _chk_hr_requests_lib,       ""),
    ("hr_urllib_avail",      "请求发送",   "urllib 模块可用性",   _chk_hr_urllib_avail,       ""),
    ("hr_worker_import",     "请求发送",   "请求工作器模块",      _chk_hr_worker_import,      ""),
    ("hr_page_import",       "请求发送",   "请求页面模块",        _chk_hr_page_import,        ""),
    ("hr_history_storage",   "请求发送",   "请求历史存储完整性",  _chk_hr_history_storage,    "repair_http_history"),
    ("hr_variable_storage",  "请求发送",   "变量存储完整性",      _chk_hr_variable_storage,   "repair_http_variables"),
    ("hr_basic_connectivity","请求发送",   "基础网络连通性",      _chk_hr_basic_connectivity, ""),
]


# ═══════════════════════════════════════════════
# Diagnostic Worker
# ═══════════════════════════════════════════════

class DiagnosticWorker(QThread):
    check_started = pyqtSignal(str)
    check_completed = pyqtSignal(CheckResult)
    all_completed = pyqtSignal(list)
    log_message = pyqtSignal(str)

    def __init__(self, mode: str = "lan"):
        """mode: 'lan' or 'basic'"""
        super().__init__()
        self._cancel = False
        self._mode = mode

    def cancel(self):
        self._cancel = True

    def run(self):
        mode_map = {"lan": LAN_CHECKS, "basic": BASIC_CHECKS}
        checks = mode_map.get(self._mode, LAN_CHECKS)
        results = []
        for check_id, category, name, func, repair_id in checks:
            if self._cancel:
                break
            self.check_started.emit(check_id)
            t0 = time.time()
            try:
                result = func(self.log_message.emit)
            except Exception as e:
                result = CheckResult(
                    status="fail",
                    detail=f"检查执行异常: {type(e).__name__}: {e}",
                    suggestions=["诊断框架内部错误，请联系开发者"])
            result.check_id = check_id
            result.category = category
            result.name = name
            result.repair_id = repair_id
            result.timestamp = datetime.now().isoformat()
            elapsed = time.time() - t0
            self.log_message.emit(
                f"[{STATUS_LABELS.get(result.status, result.status)}] "
                f"{name} ({elapsed:.1f}s)")
            self.check_completed.emit(result)
            results.append(result)
        self.all_completed.emit(results)


# ═══════════════════════════════════════════════
# Repair Worker
# ═══════════════════════════════════════════════

class RepairWorker(QThread):
    repair_started = pyqtSignal(str)
    repair_completed = pyqtSignal(str, bool, str)
    all_completed = pyqtSignal(list)
    log_message = pyqtSignal(str)

    def __init__(self, repairs: list[tuple[str, str, int]]):
        """repairs: list of (repair_id, check_name, port_or_0)"""
        super().__init__()
        self._repairs = repairs
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        results = []
        for repair_id, check_name, port in self._repairs:
            if self._cancel:
                break
            self.repair_started.emit(repair_id)
            entry = REPAIR_REGISTRY.get(repair_id)
            if entry is None:
                self.repair_completed.emit(repair_id, False, f"未找到修复程序: {repair_id}")
                results.append((repair_id, False, f"未找到修复程序: {repair_id}"))
                continue
            display_name, func, _needs_admin, _mode = entry
            if func is None and repair_id == "free_port":
                func = lambda log, p=port: _repair_free_port(log, p)
            if func is None:
                self.repair_completed.emit(repair_id, False, f"修复程序未实现: {repair_id}")
                results.append((repair_id, False, f"修复程序未实现: {repair_id}"))
                continue
            self.log_message.emit(f"── 正在修复: {display_name} ({check_name}) ──")
            try:
                success, message = func(self.log_message.emit)
            except Exception as e:
                success = False
                message = f"修复异常: {type(e).__name__}: {e}"
            self.repair_completed.emit(repair_id, success, message)
            results.append((repair_id, success, message))
            self.log_message.emit(
                f"[{'完成' if success else '失败'}] {display_name}: {message}")
        self.all_completed.emit(results)


# ═══════════════════════════════════════════════
# Category Card
# ═══════════════════════════════════════════════

class _CategoryCard(QFrame):
    def __init__(self, category: str, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self._items: dict[str, QLabel] = {}
        self._status_labels: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        header = QLabel(category)
        header.setStyleSheet(
            "font-size: 14px; font-weight: 700; color: #e8e8f0;"
            "border-bottom: 1px solid #313244; padding-bottom: 6px; margin-bottom: 2px;")
        layout.addWidget(header)

        self._item_layout = QVBoxLayout()
        self._item_layout.setSpacing(4)
        layout.addLayout(self._item_layout)

    def add_item(self, check_id: str, name: str):
        row = QHBoxLayout()
        row.setSpacing(8)

        dot = QLabel(STATUS_ICONS["pending"])
        dot.setFixedWidth(20)
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setStyleSheet(
            f"font-size: 14px; color: {STATUS_COLORS['pending']}; background: transparent;")
        row.addWidget(dot)

        lbl = QLabel(name)
        lbl.setStyleSheet("font-size: 12px; color: #c8ccd4; background: transparent;")
        row.addWidget(lbl, 1)

        status = QLabel("待检测")
        status.setStyleSheet(
            "font-size: 11px; color: #6c7086; background: transparent;"
            "padding: 1px 8px; border-radius: 3px;")
        row.addWidget(status)

        self._item_layout.addLayout(row)
        self._status_labels[check_id] = status
        self._items[check_id] = dot

    def update_item(self, result: CheckResult):
        if result.check_id not in self._items:
            return
        color = STATUS_COLORS.get(result.status, "#6c7086")
        icon = STATUS_ICONS.get(result.status, "○")
        label = STATUS_LABELS.get(result.status, result.status)

        self._items[result.check_id].setText(icon)
        self._items[result.check_id].setStyleSheet(
            f"font-size: 14px; color: {color}; background: transparent;")
        self._status_labels[result.check_id].setText(label)
        bg_map = {"pass": "rgba(166,227,161,0.12)", "fail": "rgba(243,139,168,0.15)",
                   "warning": "rgba(249,226,175,0.12)", "running": "rgba(137,180,250,0.12)"}
        bg = bg_map.get(result.status, "transparent")
        self._status_labels[result.check_id].setStyleSheet(
            f"font-size: 11px; color: {color}; background: {bg};"
            f"padding: 1px 8px; border-radius: 3px;")


# ═══════════════════════════════════════════════
# Diagnostic Page
# ═══════════════════════════════════════════════

class DiagnosticPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: dict[str, CheckResult] = {}
        self._worker: DiagnosticWorker | None = None
        self._repair_worker: RepairWorker | None = None
        self._running = False
        self._last_mode: str = ""       # "lan", "basic", or "global"
        self._global_chain: list[str] = []  # remaining modes for global check
        self._was_global: bool = False  # True if last detection was a global check
        self._cards: dict[str, _CategoryCard] = {}
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # ── Title ──
        title = QLabel("故障检测")
        title.setObjectName("myCardTitle")
        root.addWidget(title)

        # ── Action Bar ──
        action_bar = QWidget()
        action_bar.setObjectName("settingsPanel")
        ab_layout = QHBoxLayout(action_bar)
        ab_layout.setContentsMargins(20, 14, 20, 14)
        ab_layout.setSpacing(12)

        # ── Row 1: Global Check ──
        row1 = QHBoxLayout()
        row1.setSpacing(12)

        self._global_btn = QPushButton("全局检查")
        self._global_btn.setObjectName("globalCheckBtn")
        self._global_btn.clicked.connect(self._run_global_check)
        row1.addWidget(self._global_btn)

        self._export_btn = QPushButton("导出诊断报告")
        self._export_btn.setObjectName("secondaryBtn")
        self._export_btn.clicked.connect(self._export_report)
        row1.addWidget(self._export_btn)

        self._repair_btn = QPushButton("一键修复")
        self._repair_btn.setObjectName("repairBtn")
        self._repair_btn.clicked.connect(self._run_repairs)
        self._repair_btn.setVisible(False)
        row1.addWidget(self._repair_btn)

        self._repair_external_btn = QPushButton("进行修复")
        self._repair_external_btn.setObjectName("repairExternalBtn")
        self._repair_external_btn.clicked.connect(self._launch_external_repair)
        self._repair_external_btn.setVisible(False)
        row1.addWidget(self._repair_external_btn)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet("color: #9498a4; font-size: 12px;")
        row1.addWidget(self._summary_label, 1)

        row1.addStretch()
        ab_layout.addLayout(row1)

        # ── Row 2: Individual Checks ──
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self._lan_btn = QPushButton("局域网检测")
        self._lan_btn.setObjectName("lanCheckBtn")
        self._lan_btn.clicked.connect(self._run_lan_check)
        row2.addWidget(self._lan_btn)

        self._basic_btn = QPushButton("基础检测")
        self._basic_btn.setObjectName("actionBtn")
        self._basic_btn.clicked.connect(self._run_basic_check)
        row2.addWidget(self._basic_btn)

        mode_hint = QLabel("局域网检测包含网卡、端口、发现、语音等模块")
        mode_hint.setStyleSheet("color: #6c7086; font-size: 11px;")
        row2.addWidget(mode_hint)

        row2.addStretch()
        ab_layout.addLayout(row2)

        root.addWidget(action_bar)

        # ── Results Grid (scrollable) ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")

        grid_content = QWidget()
        grid_content.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(grid_content)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(12)

        # Collect all unique categories from both check lists
        seen_categories = []
        for checks in [LAN_CHECKS, BASIC_CHECKS]:
            for check_id, category, name, _func, _r in checks:
                if category not in seen_categories:
                    seen_categories.append(category)

        for idx, cat in enumerate(seen_categories):
            card = _CategoryCard(cat)
            self._cards[cat] = card
            row, col = divmod(idx, 2)
            self._grid.addWidget(card, row, col)

        # Add items from both check lists
        for checks in [LAN_CHECKS, BASIC_CHECKS]:
            for check_id, category, name, _func, _r in checks:
                if category in self._cards:
                    self._cards[category].add_item(check_id, name)

        scroll.setWidget(grid_content)
        root.addWidget(scroll, 1)

        # ── Log area (collapsible) ──
        log_header_row = QHBoxLayout()
        log_header_row.setSpacing(8)
        self._log_toggle_btn = QPushButton("▾ 诊断日志")
        self._log_toggle_btn.setStyleSheet(
            "QPushButton { text-align: left; background: transparent; border: none;"
            " color: #9498a4; font-size: 13px; font-weight: 600; padding: 4px 0; }"
            "QPushButton:hover { color: #c8ccd4; }")
        self._log_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._log_toggle_btn.clicked.connect(self._toggle_log)
        log_header_row.addWidget(self._log_toggle_btn)
        log_header_row.addStretch()
        root.addLayout(log_header_row)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumHeight(250)
        self._log_view.setStyleSheet(
            "QTextEdit {"
            "  background-color: #1e1e2e; color: #c8ccd4;"
            f"  font-family: {MONO_FONT_FAMILY}; font-size: 12px;"
            "  border: 1px solid #313244; border-radius: 6px; padding: 10px; }")
        root.addWidget(self._log_view)

    def _toggle_log(self):
        visible = not self._log_view.isVisible()
        self._log_view.setVisible(visible)
        self._log_toggle_btn.setText("▾ 诊断日志" if visible else "▸ 诊断日志")

    _MODE_LABELS = {"lan": "局域网检测", "basic": "基础检测",
                     "global": "全局检查"}
    _MODE_BTN_MAP: dict[str, list] = {}  # populated in _setup_ui

    def _start_checks(self, mode: str):
        mode_label = self._MODE_LABELS.get(mode, mode)
        self._last_mode = mode
        # Track if this is part of a global check (for permissive repair filtering)
        if not self._global_chain:
            self._was_global = (mode == "global")
        self._running = True
        self._repair_btn.setVisible(False)
        self._repair_btn.setEnabled(True)
        self._repair_btn.setText("一键修复")
        self._repair_external_btn.setVisible(False)

        # Disable all check buttons
        self._global_btn.setEnabled(False)
        self._global_btn.setText("全局检查运行中…" if mode == "global" else "请等待…")
        self._lan_btn.setEnabled(False)
        self._lan_btn.setText("检测运行中…" if mode == "lan" else "请等待…")
        self._basic_btn.setEnabled(False)
        self._basic_btn.setText("检测运行中…" if mode == "basic" else "请等待…")

        self._summary_label.setText(f"⏳ {mode_label}进行中，请勿操作…")
        # Clear log & results only on first phase of global check
        if not self._global_chain or mode == self._global_chain[0]:
            self._log_view.clear()
            self._results.clear()

        mode_map = {"lan": LAN_CHECKS, "basic": BASIC_CHECKS}
        all_checks = mode_map.get(mode, LAN_CHECKS)
        current_ids = {c[0] for c in all_checks}
        for card in self._cards.values():
            for check_id in card._items:
                if check_id in current_ids:
                    card.update_item(CheckResult(
                        check_id=check_id, category="", name="", status="pending"))

        self._log("=" * 55)
        self._log(f"[{mode_label}] 开始 — {datetime.now().isoformat()}")
        self._log(f"检测模式: {mode_label}")
        self._log(f"Python: {sys.version[:60]}")
        self._log(f"可执行文件: {sys.executable}")
        self._log(f"工作目录: {os.getcwd()}")
        self._log(f"Frozen: {getattr(sys, 'frozen', False)}")
        self._log("=" * 55)
        log_info(f"诊断开始: {mode_label}")

        self._worker = DiagnosticWorker(mode=mode)
        self._worker.check_started.connect(self._on_check_started)
        self._worker.check_completed.connect(self._on_check_completed)
        self._worker.all_completed.connect(self._on_all_completed)
        self._worker.log_message.connect(self._log)
        self._worker.start()

    def _run_lan_check(self):
        if self._running:
            return
        self._was_global = False
        self._global_chain = []
        self._start_checks(mode="lan")

    def _run_basic_check(self):
        if self._running:
            return
        self._was_global = False
        self._global_chain = []
        self._start_checks(mode="basic")

    def _run_global_check(self):
        if self._running:
            return
        if not self._show_global_warning():
            return
        # Chain: LAN → Basic
        self._was_global = True
        self._global_chain = ["basic"]
        self._start_checks(mode="lan")

    def _show_global_warning(self) -> bool:
        """Show mandatory warning before global check."""
        msg = QMessageBox(self)
        msg.setWindowTitle("全局检查 — 重要提示")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            "全局检查将依次执行以下检测：\n\n"
            "  1. 局域网检测 — 网卡接口、传输端口、设备发现、房间扫描、语音模块\n"
            "  2. 基础检测 — 压缩模块、配置文件、空间索引、AI功能\n\n"
            "全程预计耗时 30–60 秒，期间请勿操作本应用。\n"
            "检测完成后将显示完整诊断报告。")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.button(QMessageBox.StandardButton.Yes).setText("开始全局检查")
        msg.button(QMessageBox.StandardButton.No).setText("取消")
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        msg.setMinimumWidth(520)
        log_info("显示全局检查提示弹窗")
        return msg.exec() == QMessageBox.StandardButton.Yes

    # ── Repair logic ──

    def _collect_fixable(self) -> list[tuple[str, str, int]]:
        """Collect fixable items from current mode's results.
        When _was_global, include repairs from all modes.
        Returns [(repair_id, check_name, port), ...]"""
        port_map = {"tcp_12345": 12345, "udp_12346": 12346, "udp_12347": 12347}
        repairs: list[tuple[str, str, int]] = []
        seen = set()
        for r in self._results.values():
            rid = r.repair_id
            if not rid or r.status not in ("fail", "warning"):
                continue
            # Mode filter — skip unless this is a global check
            if not self._was_global:
                entry = REPAIR_REGISTRY.get(rid)
                if entry and entry[3] != self._last_mode:
                    continue
            if rid != "free_port" and rid in seen:
                continue
            seen.add(rid)
            port = port_map.get(r.check_id, 0)
            repairs.append((rid, r.name, port))
        return repairs

    def _show_repair_warning(self, repairs: list[tuple[str, str, int]]) -> bool:
        """Show confirmation dialog before executing repairs."""
        unique_names = []
        seen = set()
        for rid, name, _port in repairs:
            entry = REPAIR_REGISTRY.get(rid)
            if entry and name not in seen:
                unique_names.append((name, entry[2]))
                seen.add(name)

        needs_admin_repairs = [n for n, a in unique_names if a]

        lines = ["即将执行以下修复操作：\n"]
        for name, needs_admin in unique_names:
            tag = "  [需管理员权限]" if needs_admin else ""
            lines.append(f"  • {name}{tag}")

        if needs_admin_repairs:
            lines.append(f"\n⚠ 以下修复需要管理员权限：")
            for n in needs_admin_repairs:
                lines.append(f"    - {n}")
            lines.append("   将以管理员身份启动修复进程。")

        lines.append("\n修复过程中请勿关闭程序。是否继续？")

        msg = QMessageBox(self)
        msg.setWindowTitle("一键修复 — 确认")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText("\n".join(lines))
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.button(QMessageBox.StandardButton.Yes).setText("开始修复")
        msg.button(QMessageBox.StandardButton.No).setText("取消")
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        return msg.exec() == QMessageBox.StandardButton.Yes

    def _run_repairs(self):
        if self._running:
            return
        repairs = self._collect_fixable()
        if not repairs:
            QMessageBox.information(self, "一键修复", "当前没有需要修复的问题。")
            return

        if not self._show_repair_warning(repairs):
            return

        self._running = True
        self._repair_btn.setEnabled(False)
        self._repair_btn.setText("修复中…")
        self._global_btn.setEnabled(False)
        self._lan_btn.setEnabled(False)
        self._basic_btn.setEnabled(False)
        self._summary_label.setText("⏳ 正在执行修复操作，请稍候…")
        self._log("=" * 55)
        self._log(f"一键修复开始 — {datetime.now().isoformat()}")
        log_info(f"一键修复开始 ({self._MODE_LABELS.get(self._last_mode, '?')})")

        self._repair_worker = RepairWorker(repairs)
        self._repair_worker.repair_started.connect(self._on_repair_started)
        self._repair_worker.repair_completed.connect(self._on_repair_completed)
        self._repair_worker.all_completed.connect(self._on_repairs_all_completed)
        self._repair_worker.log_message.connect(self._log)
        self._repair_worker.start()

    def _on_repair_started(self, repair_id: str):
        self._log(f"▶ 开始: {REPAIR_REGISTRY.get(repair_id, (repair_id,))[0]}")

    def _on_repair_completed(self, repair_id: str, success: bool, message: str):
        status = "✓ 成功" if success else "✗ 失败"
        self._log(f"  {status}: {message}")

    def _on_repairs_all_completed(self, results: list):
        self._running = False
        self._repair_btn.setEnabled(True)
        self._global_btn.setEnabled(True)
        self._global_btn.setText("全局检查")
        self._lan_btn.setEnabled(True)
        self._lan_btn.setText("局域网检测")
        self._basic_btn.setEnabled(True)
        self._basic_btn.setText("基础检测")
        self._repair_worker = None

        successes = sum(1 for _, ok, _ in results if ok)
        failures = sum(1 for _, ok, _ in results if not ok)
        self._summary_label.setText(
            f"修复完成: {successes} 成功, {failures} 失败")
        self._log("=" * 55)
        self._log(f"一键修复完成: {successes} 成功, {failures} 失败")
        log_info(f"一键修复完成: {successes}成功, {failures}失败")

        # Auto re-run checks to verify fixes
        if self._was_global:
            self._log("修复后自动重新全局检查以验证…")
            self._was_global = True
            self._global_chain = ["basic"]
            self._start_checks(mode="lan")
        else:
            mode_label = self._MODE_LABELS.get(self._last_mode, self._last_mode)
            self._log(f"修复后自动重新{mode_label}以验证…")
            self._start_checks(mode=self._last_mode)

    def _on_check_started(self, check_id: str):
        for card in self._cards.values():
            if check_id in card._items:
                card.update_item(CheckResult(
                    check_id=check_id, category="", name="", status="running"))

    def _on_check_completed(self, result: CheckResult):
        self._results[result.check_id] = result
        for card in self._cards.values():
            if result.check_id in card._items:
                card.update_item(result)

    def _on_all_completed(self, results: list):
        self._running = False
        self._worker = None

        passes = sum(1 for r in results if r.status == "pass")
        fails = sum(1 for r in results if r.status == "fail")
        warns = sum(1 for r in results if r.status == "warning")
        mode_label = self._MODE_LABELS.get(self._last_mode, self._last_mode)
        self._summary_label.setText(
            f"[{mode_label}] {passes} 通过  |  {fails} 失败  |  {warns} 警告  (共 {len(results)} 项)")
        self._log("=" * 55)
        self._log(f"[{mode_label}] 完成: {passes} 通过, {fails} 失败, {warns} 警告")
        log_info(f"诊断完成 ({mode_label}): {passes}通过, {fails}失败, {warns}警告")

        # ── Global check chain: auto-start next mode ──
        if self._global_chain:
            next_mode = self._global_chain.pop(0)
            self._log(f"\n>>> 全局检查：自动进入下一阶段 [{self._MODE_LABELS.get(next_mode, next_mode)}] <<<\n")
            self._start_checks(mode=next_mode)
            return

        # Reset buttons on global completion
        self._global_btn.setEnabled(True)
        self._global_btn.setText("全局检查")
        self._lan_btn.setEnabled(True)
        self._lan_btn.setText("局域网检测")
        self._basic_btn.setEnabled(True)
        self._basic_btn.setText("基础检测")

        # Show repair button if there are fixable issues in the current mode
        fixable = [r for r in results
                   if r.status in ("fail", "warning") and r.repair_id]
        if fixable:
            unique_repairs = set(r.repair_id for r in fixable)
            self._repair_btn.setVisible(True)
            self._repair_btn.setText(f"一键修复 ({len(unique_repairs)} 项)")
            self._repair_external_btn.setVisible(True)
            self._repair_external_btn.setText(f"进行修复 ({len(unique_repairs)} 项)")
            self._log(f"发现 {len(fixable)} 个可修复问题（{len(unique_repairs)} 种修复方案）")
        else:
            self._repair_btn.setVisible(False)
            self._repair_external_btn.setVisible(False)

    def _launch_external_repair(self):
        if self._running:
            return

        fixable = [r for r in self._results.values()
                   if r.status in ("fail", "warning") and r.repair_id]
        if not fixable:
            QMessageBox.information(self, "进行修复", "当前没有需要修复的问题。")
            return

        unique_repairs = set(r.repair_id for r in fixable)

        msg = QMessageBox(self)
        msg.setWindowTitle("进行修复 — 重要提示")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText(
            "即将关闭当前程序并启动独立修复工具。\n\n"
            "修复工具会自动处理检测到的问题，完成后将重新启动主程序。\n\n"
            "请勿手动关闭修复工具窗口。\n\n"
            f"待修复问题: {len(fixable)} 项\n"
            f"修复方案: {len(unique_repairs)} 种\n\n"
            "是否继续？"
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.button(QMessageBox.StandardButton.Yes).setText("关闭并修复")
        msg.button(QMessageBox.StandardButton.No).setText("取消")
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        repair_exe = find_repair_exe()
        if not os.path.exists(repair_exe):
            QMessageBox.critical(
                self, "错误",
                f"未找到修复工具。\n\n预期位置: {repair_exe}\n\n"
                "请重新安装程序或联系开发者。")
            return

        try:
            main_exe = sys.executable
            serialize_check_results(fixable, main_exe)
            log_info(f"修复任务已写入 ({len(fixable)} 项)")
        except Exception as e:
            log_error(f"写入修复任务失败: {e}")
            QMessageBox.critical(self, "错误", f"无法创建修复任务: {e}")
            return

        from utils.helpers import spawn_detached
        try:
            spawn_detached([repair_exe])
            log_info(f"修复工具已启动: {repair_exe}")
        except Exception as e:
            log_error(f"启动修复工具失败: {e}")
            QMessageBox.critical(self, "错误", f"无法启动修复工具: {e}")
            return

        QApplication.quit()

    def _log(self, message: str):
        self._log_view.append(message)
        sb = self._log_view.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _export_report(self):
        if not self._results:
            return
        filepath, _ = QFileDialog.getSaveFileName(
            self, "导出诊断报告",
            f"diagnostic_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "文本文件 (*.txt)")
        if not filepath:
            return
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("帽兜工具 诊断报告\n")
                f.write(f"生成时间: {datetime.now().isoformat()}\n")
                f.write(f"Python: {sys.version}\n")
                f.write(f"可执行文件: {sys.executable}\n")
                f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
                f.write("=" * 55 + "\n\n")

                all_checks = LAN_CHECKS + BASIC_CHECKS
                for check_id, cat, name, _func, _r in all_checks:
                    r = self._results.get(check_id)
                    if r is None:
                        f.write(f"[未运行] [{cat}] {name}\n\n")
                        continue
                    f.write(f"[{STATUS_LABELS.get(r.status, r.status)}] [{cat}] {name}\n")
                    f.write(f"  {r.detail}\n")
                    if r.suggestions:
                        for s in r.suggestions:
                            f.write(f"  建议: {s}\n")
                    f.write("\n")

                f.write("=" * 55 + "\n完整日志:\n" + "=" * 55 + "\n")
                f.write(self._log_view.toPlainText())
            self._log(f"报告已导出: {filepath}")
        except Exception as e:
            self._log(f"导出失败: {e}")

    def cleanup(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        if self._repair_worker and self._repair_worker.isRunning():
            self._repair_worker.cancel()
            self._repair_worker.wait(3000)
