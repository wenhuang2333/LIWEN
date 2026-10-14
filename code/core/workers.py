import os
import time
import traceback
from PyQt6.QtCore import QThread, pyqtSignal

from utils.logger import error, warn


class CompressWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, file_paths, output_path, fmt="zip", password=None,
                 volume_size=None, seed_code=None, compress_level=0):
        super().__init__()
        self.file_paths = file_paths
        self.output_path = output_path
        self.fmt = fmt
        self.password = password
        self.volume_size = volume_size
        self.seed_code = seed_code
        self.compress_level = compress_level

    def run(self):
        try:
            from core.formats import FormatRegistry

            handler = FormatRegistry.get_by_name(self.fmt)
            if handler is None or not handler.can_compress():
                self.error.emit(f"不支持的格式: {self.fmt}")
                return

            self.status.emit("正在压缩...")
            kwargs = {
                "password": self.password,
                "volume_size": self.volume_size,
                "progress_callback": self.progress.emit,
                "compress_level": self.compress_level,
            }
            if self.seed_code:
                kwargs["seed_code"] = self.seed_code
            result = handler.compress(
                self.file_paths, self.output_path, **kwargs)
            self.status.emit("压缩完成")
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class DecompressWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, archive_path, dest_dir, password=None, seed_code=None):
        super().__init__()
        self.archive_path = archive_path
        self.dest_dir = dest_dir
        self.password = password
        self.seed_code = seed_code

    def run(self):
        try:
            from core.formats import FormatRegistry

            handler = FormatRegistry.detect(self.archive_path)
            if handler is None or not handler.can_decompress():
                self.error.emit(f"不支持的压缩格式，请使用 {', '.join(FormatRegistry.compressible_formats())}")
                return

            self.status.emit("正在解压...")
            kwargs = {
                "password": self.password,
                "progress_callback": self.progress.emit,
            }
            if self.seed_code:
                kwargs["seed_code"] = self.seed_code
            result = handler.decompress(
                self.archive_path, self.dest_dir, **kwargs)
            self.status.emit("解压完成")
            self.finished.emit(result)
        except ValueError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(f"解压失败: {e}")


class SendWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    status = pyqtSignal(str)
    speed = pyqtSignal(str)

    def __init__(self, file_paths, host, port, compress_first=True, from_name="", send_request=True):
        super().__init__()
        self.file_paths = file_paths
        self.host = host
        self.port = port
        self.compress_first = compress_first
        self.from_name = from_name
        self.send_request_flag = send_request
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from network.transfer import send_files
            send_files(
                self.file_paths, self.host, self.port,
                compress_first=self.compress_first,
                progress_callback=self.progress.emit,
                speed_callback=self.speed.emit,
                status_callback=self.status.emit,
                send_request=self.send_request_flag,
                from_name=self.from_name,
                cancel_check=lambda: self._cancel,
            )
            self.status.emit("发送完成")
            self.finished.emit({"status": "ok", "host": self.host})
        except PermissionError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


class ReceiveWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    status = pyqtSignal(str)
    speed = pyqtSignal(str)
    request_decision = pyqtSignal(dict, object, object)  # info, decision_dict, event

    def __init__(self, port, save_dir, auto_accept=False):
        super().__init__()
        self.port = port
        self.save_dir = save_dir
        self.auto_accept = auto_accept
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from network.transfer import receive_files
            receive_files(
                self.port, self.save_dir,
                progress_callback=self.progress.emit,
                speed_callback=self.speed.emit,
                status_callback=self.status.emit,
                request_callback=self._on_request if not self.auto_accept else None,
                cancel_check=lambda: self._cancel,
                auto_accept=self.auto_accept,
            )
            self.status.emit("接收完成")
            self.finished.emit({"status": "ok"})
        except Exception as e:
            self.error.emit(str(e))

    def _on_request(self, info, decision, event):
        self.request_decision.emit(info, decision, event)


class PingWorker(QThread):
    finished = pyqtSignal(bool, str)
    error = pyqtSignal(str)

    def __init__(self, host, port, timeout=3):
        super().__init__()
        self.host = host
        self.port = port
        self.timeout = timeout

    def run(self):
        try:
            from network.transfer import ping_host
            ok, msg = ping_host(self.host, self.port, self.timeout)
            self.finished.emit(ok, msg)
        except Exception as e:
            self.error.emit(str(e))


class RoomHostWorker(QThread):
    status = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, room_info: dict, username: str = ""):
        super().__init__()
        self._room_info = room_info
        self._username = username
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from network.room_protocol import RoomHost
            self._host = RoomHost(self._room_info, self._username)
            self._host.start()
            self.status.emit("房间广播已启动")
            while not self._cancel:
                self.msleep(500)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if hasattr(self, "_host") and self._host:
                self._host.stop()


class RoomScanWorker(QThread):
    finished = pyqtSignal(dict)

    def __init__(self, username: str = ""):
        super().__init__()
        self._username = username

    def run(self):
        try:
            from network.room_protocol import RoomDiscoveryClient
            client = RoomDiscoveryClient(self._username)
            rooms = client.scan()
            self.finished.emit(rooms)
        except Exception:
            self.finished.emit({})


class RoomJoinWorker(QThread):
    finished = pyqtSignal(bool, str, dict)
    error = pyqtSignal(str)

    def __init__(self, host_ip: str, port: int, room_id: str, password: str = ""):
        super().__init__()
        self._host_ip = host_ip
        self._port = port
        self._room_id = room_id
        self._password = password
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from network.room_protocol import RoomJoinClient
            ok, msg, config = RoomJoinClient.join_room(
                self._host_ip, self._port, self._room_id, self._password)
            self.finished.emit(ok, msg, config)
        except Exception as e:
            self.error.emit(str(e))
            self.finished.emit(False, str(e), {})


class VoiceWorker(QThread):
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(self, sender_id: str, voice_port: int):
        super().__init__()
        self._sender_id = sender_id
        self._voice_port = voice_port
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        ok = False
        try:
            from network.voice_protocol import VoiceClient
            self._client = VoiceClient(self._sender_id, self._voice_port)
            if self._client.start():
                self.status.emit("语音已连接")
                ok = True
                while not self._cancel:
                    self.msleep(200)
            else:
                self.error.emit("无法初始化音频设备 — 请检查麦克风是否可用")
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if hasattr(self, "_client") and self._client:
                self._client.stop()
            self.finished.emit(ok)


class SiteCheckWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self._url = url
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from core.crawler.robot_checker import SiteChecker
            import requests

            html = ""
            headers = {}
            status = 0
            if not self._cancel:
                try:
                    resp = requests.get(self._url, headers={
                        "User-Agent": SiteChecker._UA,
                    }, timeout=15, allow_redirects=True)
                    html = resp.text[:500000]
                    headers = dict(resp.headers)
                    status = resp.status_code
                except Exception:
                    pass

            report = SiteChecker.check_site_feasibility(
                self._url, html, headers, status)
            if not self._cancel:
                self.finished.emit(report)
        except Exception as e:
            if not self._cancel:
                self.error.emit(str(e))


class SpaceScanWorker(QThread):
    """Thin wrapper that delegates to core.file_index.ScanEngine."""

    progress = pyqtSignal(object, object, float, float)
    scanning_path = pyqtSignal(str)
    status = pyqtSignal(str)
    finished = pyqtSignal(object, object, object, object)
    error = pyqtSignal(str)
    dirs_updated = pyqtSignal()

    def __init__(self, root_path: str, db):
        super().__init__()
        self._root_path = root_path
        self._db = db
        self._engine = None

    def cancel(self):
        # _engine 在 run() 里才创建，取消可能早于它 —— 原实现直接
        # `if self._engine:` 其实是安全的，但一旦 run() 抛异常导致
        # _engine 半初始化，这里也要能容忍。
        try:
            engine = self._engine
            if engine is not None:
                engine.cancel()
        except Exception as e:
            warn(f"SpaceScanWorker.cancel 异常: {e}")

    def run(self):
        from core.file_index import ScanEngine
        try:
            self._engine = ScanEngine(self._root_path, self._db)
            self._engine.progress.connect(self.progress.emit)
            self._engine.scanning_path.connect(self.scanning_path.emit)
            self._engine.status.connect(self.status.emit)
            self._engine.finished.connect(self.finished.emit)
            self._engine.error.connect(self.error.emit)
            self._engine.dirs_updated.connect(self.dirs_updated.emit)
            self._engine.run()
        except Exception as e:
            # 原实现没有 try/except：任何异常（例如数据库连接被上层提前关闭
            # 后继续写入 → sqlite3.ProgrammingError）都会逃出 run()，页面永远
            # 收不到 finished/error，进度条卡死，异常还会被全局钩子当成崩溃弹窗。
            tb = traceback.format_exc()
            error(f"SpaceScanWorker.run 异常: {e}\n{tb}")
            self.error.emit(f"扫描失败: {e}")
        finally:
            engine = self._engine
            if engine is not None:
                # 逐个断开，避免无参 disconnect() 触发
                # "wildcard call disconnects from destroyed signal" 噪音
                for sig in ("progress", "scanning_path", "status",
                            "finished", "error", "dirs_updated"):
                    s = getattr(engine, sig, None)
                    if s is not None:
                        try:
                            s.disconnect()
                        except (TypeError, RuntimeError):
                            pass
                self._engine = None


class CrawlWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    status = pyqtSignal(str)
    log = pyqtSignal(str)
    data_row = pyqtSignal(dict)

    def __init__(self, config: dict):
        super().__init__()
        self._config = config
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from core.crawler.engine import CrawlerEngine
            result = CrawlerEngine.run_crawl(
                start_url=self._config.get("start_url", ""),
                fields=self._config.get("fields", []),
                pagination=self._config.get("pagination"),
                options=self._config.get("options"),
                progress_cb=self.progress.emit,
                log_cb=self.log.emit,
                data_row_cb=self.data_row.emit,
                cancel_check=lambda: self._cancel,
            )
            self.status.emit(f"完成 — {len(result)} 条数据")
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class HttpRequestWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, method: str, url: str, params: dict = None,
                 headers: dict = None, body_config: dict = None, timeout: int = 30,
                 allow_redirects: bool = True, verify_ssl: bool = True,
                 proxy: str = None):
        super().__init__()
        self._method = method.upper()
        self._url = url
        self._params = params or {}
        self._headers = headers or {}
        self._body_config = body_config or {"type": "none"}
        self._timeout = timeout
        self._allow_redirects = allow_redirects
        self._verify_ssl = verify_ssl
        self._proxy = proxy
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        import requests
        try:
            from core.http_client import send_http_request

            if self._cancel:
                return

            self.status.emit("正在发送请求...")
            result = send_http_request(
                method=self._method,
                url=self._url,
                params=self._params,
                headers=self._headers,
                body_config=self._body_config,
                timeout=self._timeout,
                allow_redirects=self._allow_redirects,
                verify_ssl=self._verify_ssl,
                proxy=self._proxy,
                cancel_check=lambda: self._cancel,
                status_callback=self.status.emit,
            )
            if not self._cancel and result:
                self.finished.emit(result)

        except requests.exceptions.Timeout:
            self.error.emit("请求超时")
        except requests.exceptions.ConnectionError:
            self.error.emit("连接失败 — 请检查 URL 和网络")
        except requests.exceptions.TooManyRedirects:
            self.error.emit("重定向次数过多")
        except Exception as e:
            if not self._cancel:
                self.error.emit(f"请求异常: {type(e).__name__}: {e}")


class AiChatWorker(QThread):
    token = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, client, messages, parent=None):
        super().__init__(parent)
        self.client = client
        self.messages = messages
        self._cancel = False

    def run(self):
        import time
        from utils.logger import info, error as log_error
        t0 = time.time()
        print(f"[AI DEBUG] AiChatWorker 线程开始, 消息数: {len(self.messages)}")
        info(f"AiChatWorker 线程开始: messages={len(self.messages)}")
        try:
            self.status.emit("正在生成...")
            token_count = 0
            for token in self.client.chat_stream(self.messages):
                if self._cancel:
                    print(f"[AI DEBUG] AiChatWorker 被取消, 已收到 {token_count} tokens")
                    info(f"AiChatWorker 被取消: tokens={token_count}")
                    break
                token_count += 1
                self.token.emit(token)
            self.finished.emit()
            elapsed = time.time() - t0
            print(f"[AI DEBUG] AiChatWorker 线程正常结束: {token_count} tokens, 耗时 {elapsed:.2f}s")
            info(f"AiChatWorker 线程正常结束: tokens={token_count}, elapsed={elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[AI DEBUG] AiChatWorker 线程异常: {e}, 耗时 {elapsed:.2f}s")
            log_error(f"AiChatWorker 线程异常: {e}, elapsed={elapsed:.2f}s")
            if not self._cancel:
                self.error.emit(str(e))

    def cancel(self):
        self._cancel = True


class AiConnectionTestWorker(QThread):
    """Test AI API connectivity in background — resolves DNS, connects, sends minimal request."""
    log_message = pyqtSignal(str)
    status_update = pyqtSignal(str)
    finished = pyqtSignal(bool, str)  # success, message

    def __init__(self, config: dict):
        super().__init__()
        self._config = config
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from core.ai_connection_test import test_ai_connection

            if self._cancel:
                return

            success, message = test_ai_connection(
                self._config,
                cancel_check=lambda: self._cancel,
                log_callback=self.log_message.emit,
                status_callback=self.status_update.emit,
            )
            if not self._cancel:
                self.finished.emit(success, message)
        except Exception as e:
            if not self._cancel:
                self.finished.emit(False, f"测试异常: {e}")
