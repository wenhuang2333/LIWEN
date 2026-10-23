"""Full-duplex voice over LAN using UDP unicast.

Each peer gets a dedicated UDP unicast stream.  This avoids flooding the
WiFi with broadcast audio, which is especially important on mobile hotspots.

Audio format: 16-bit mono PCM @ 22050 Hz, 20ms frames.
Frame header: 4-byte sender_hash + 4-byte sequence number.

音频后端选型（跨平台关键）
--------------------------
优先 **sounddevice**（PortAudio 官方 Python 绑定）：

* 发布 ``py3-none-any`` + macOS universal2 + win32/win_amd64/win_arm64 wheel，
  Linux 与任意 Python 版本都能直接装；
* 自带 PortAudio 二进制（macOS/Windows），Linux 只需系统 ``libportaudio2``。

缺失时回退 **PyAudio**（老依赖，保留兼容）：

* PyPI 上只有 Windows wheel（cp38–cp313），macOS/Linux 必须本地编译
  PortAudio，Python 3.14 无 wheel —— 这正是此前"语音不可用"的原因。

后端选择只看"哪个库能 import 成功"，**不含任何 ``sys.platform`` 分支**。
"""

import atexit
import collections
import socket
import struct
import threading
import time
import weakref

from utils.constants import (
    VOICE_SAMPLE_RATE, VOICE_CHUNK_MS,
    VOICE_CHANNELS, VOICE_SAMPLE_WIDTH, VOICE_JITTER_MS,
    is_valid_voice_port,
)
from utils.logger import error as log_error, warn as log_warn

FRAME_SAMPLES = VOICE_SAMPLE_RATE * VOICE_CHUNK_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * VOICE_CHANNELS * VOICE_SAMPLE_WIDTH
MAX_JITTER_FRAMES = VOICE_JITTER_MS // VOICE_CHUNK_MS

# 单次循环最多补播的帧数（防止系统挂起后一次性写入大量音频）
MAX_CATCHUP_FRAMES = 5
# 同时跟踪的对端上限：无认证的 UDP 端口，必须设上限防止内存被撑爆
MAX_TRACKED_SENDERS = 32

_BACKEND_NAME: str | None = None
_BACKEND_PROBED = False
_BACKEND_LOCK = threading.Lock()

# 安装提示：两个库任选其一即可，sounddevice 是推荐项
INSTALL_HINT = "pip install sounddevice（推荐）或 pip install pyaudio"

# 存活的 VoiceClient（弱引用，不阻止回收）；解释器退出时兜底关闭。
# 为什么必须有：PortAudio 的流如果在解释器最终化阶段才被 GC（从未 close），
# 进程会 **段错误（exit 139）** 而不是干净退出 —— 实测 `VoiceClient.start()`
# 后既不 stop 也不 close 就是必崩。任何漏掉 stop() 的路径（异常、提前 return、
# 第三方调用）都不能让整个程序崩溃。
_LIVE_CLIENTS: "weakref.WeakSet" = weakref.WeakSet()
_LIVE_LOCK = threading.Lock()
_ATEXIT_ARMED = False


def stop_all_clients():
    """关闭所有还活着的语音客户端（幂等，退出兜底也用它）。"""
    with _LIVE_LOCK:
        clients = list(_LIVE_CLIENTS)
    for client in clients:
        try:
            client.stop()
        except Exception as e:
            log_warn(f"退出时关闭语音客户端失败: {e}")


def _register_client(client):
    global _ATEXIT_ARMED
    with _LIVE_LOCK:
        try:
            _LIVE_CLIENTS.add(client)
        except TypeError:
            return
        if _ATEXIT_ARMED:
            return
        _ATEXIT_ARMED = True
    # 关键：必须在「解释器开始终结线程」之前把音频流关掉。
    # 实测（macOS + Python 3.14 + PortAudio）：已启动但从未 stop() 的客户端，
    # 若把 PortAudio 流留到线程终结阶段，进程直接段错误（exit 139）—— 而且那时
    # 连普通 atexit 回调都还没轮到执行。threading._register_atexit 正好在
    # 「解释器开始 join/终结线程」之前运行（concurrent.futures 也用这个钩子）。
    if hasattr(threading, "_register_atexit"):
        try:
            threading._register_atexit(stop_all_clients)
        except Exception:
            pass
    try:
        atexit.register(stop_all_clients)      # 兜底：普通 return 退出路径
    except Exception:
        pass


class _StreamCloser:
    """把「关闭音频流」做成**幂等且线程安全**的操作。

    关闭路径有两条：工作线程结束时会关自己打开的流，``stop()`` 也会兜底补关。
    PortAudio 对同一条流的重复/并发 close 属未定义行为，所以用 WeakSet 记住
    「已经关过的流」，谁先到谁关。
    """

    #: 各后端关闭一条流要依次调用的方法名
    _CLOSE_METHODS = ("stop", "close")

    def __init__(self):
        self._closed_lock = threading.Lock()
        self._closed_streams = weakref.WeakSet()

    def _mark_closed(self, stream) -> bool:
        """首次调用返回 True（由你负责关闭）；重复调用返回 False。"""
        if stream is None:
            return False
        try:
            with self._closed_lock:
                if stream in self._closed_streams:
                    return False
                self._closed_streams.add(stream)
                return True
        except TypeError:
            # 极少数不可弱引用的后端对象：保守放行（宁可重复关也不要漏关）
            return True

    def abort_stream(self, stream):
        """让阻塞中的 read()/write() 立刻返回。

        PortAudio 只有在流被 abort 后才会打断阻塞中的 ``Pa_ReadStream`` /
        ``Pa_WriteStream``；先 abort 再 join，工作线程才能自己把流关掉，
        避免主线程在对方读写时并发 close。
        """
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:
            pass

    def close_stream(self, stream):
        if not self._mark_closed(stream):
            return
        for name in self._CLOSE_METHODS:
            fn = getattr(stream, name, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                pass


def _probe_backend() -> str:
    """探测可用的音频后端，返回 ``"sounddevice"`` / ``"pyaudio"`` / ``""``。

    结果缓存：import 只做一次。``sounddevice`` 在缺 PortAudio 动态库时会抛
    ``OSError``，``pyaudio`` 缺失时抛 ``ImportError``，一并按"不可用"处理。
    """
    global _BACKEND_NAME, _BACKEND_PROBED
    with _BACKEND_LOCK:
        if _BACKEND_PROBED:
            return _BACKEND_NAME or ""
        _BACKEND_PROBED = True
        try:
            import sounddevice  # noqa: F401
            _BACKEND_NAME = "sounddevice"
            return _BACKEND_NAME
        except Exception as e:  # ImportError / OSError(缺 PortAudio)
            log_warn(f"sounddevice 不可用（{type(e).__name__}: {e}），尝试 PyAudio")
        try:
            import pyaudio  # noqa: F401
            _BACKEND_NAME = "pyaudio"
            return _BACKEND_NAME
        except Exception as e:
            log_warn(f"PyAudio 也不可用（{type(e).__name__}: {e}）")
        _BACKEND_NAME = ""
        return ""


def voice_backend_name() -> str:
    """当前使用的音频后端名（不可用时为空串）。"""
    return _probe_backend()


def is_voice_available() -> bool:
    """音频后端库是否可用。

    只代表"库能 import"；设备是否真的能打开由 :meth:`VoiceClient.start`
    的返回值决定（不假装成功）。
    """
    return bool(_probe_backend())


class _SoundDeviceBackend(_StreamCloser):
    """sounddevice 后端：用 Raw* 流直接收发 bytes，不依赖 numpy。"""

    name = "sounddevice"

    def __init__(self):
        super().__init__()
        import sounddevice as sd
        self._sd = sd
        self._input = None
        self._output = None

    def open_output(self):
        self._output = self._sd.RawOutputStream(
            samplerate=VOICE_SAMPLE_RATE,
            channels=VOICE_CHANNELS,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
        )
        return self._output

    def open_input(self):
        self._input = self._sd.RawInputStream(
            samplerate=VOICE_SAMPLE_RATE,
            channels=VOICE_CHANNELS,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
        )
        return self._input

    @staticmethod
    def read(stream, frames: int) -> bytes:
        # RawInputStream.read() 返回 (buffer, overflowed)
        data, _overflowed = stream.read(frames)
        return bytes(data)

    @staticmethod
    def write(stream, data: bytes):
        stream.write(data)

    def terminate(self):
        # 兜底：任何还挂在本后端上的流都必须显式关闭。
        # 只把引用置 None 会让 PortAudio 流在解释器最终化时才被 GC —— 段错误。
        for stream in (self._input, self._output):
            self.close_stream(stream)
        self._input = None
        self._output = None


class _PyAudioBackend(_StreamCloser):
    """PyAudio 后端（兼容保留）。"""

    name = "pyaudio"
    _CLOSE_METHODS = ("stop_stream", "close")

    def __init__(self):
        super().__init__()
        import pyaudio
        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        self._format = self._pa.get_format_from_width(VOICE_SAMPLE_WIDTH)
        self._input = None
        self._output = None

    def open_output(self):
        self._output = self._pa.open(
            format=self._format,
            channels=VOICE_CHANNELS,
            rate=VOICE_SAMPLE_RATE,
            output=True,
            frames_per_buffer=FRAME_SAMPLES,
        )
        return self._output

    def open_input(self):
        self._input = self._pa.open(
            format=self._format,
            channels=VOICE_CHANNELS,
            rate=VOICE_SAMPLE_RATE,
            input=True,
            frames_per_buffer=FRAME_SAMPLES,
        )
        return self._input

    @staticmethod
    def read(stream, frames: int) -> bytes:
        return stream.read(frames, exception_on_overflow=False)

    @staticmethod
    def write(stream, data: bytes):
        stream.write(data)

    def abort_stream(self, stream):
        if stream is None:
            return
        try:
            stream.stop_stream()
        except Exception:
            pass

    def terminate(self):
        for stream in (self._input, self._output):
            self.close_stream(stream)
        self._input = None
        self._output = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


_BACKENDS = {
    "sounddevice": _SoundDeviceBackend,
    "pyaudio": _PyAudioBackend,
}


def _make_backend():
    """按探测结果实例化后端；不可用或初始化失败时返回 None。"""
    name = _probe_backend()
    cls = _BACKENDS.get(name)
    if cls is None:
        return None
    try:
        return cls()
    except Exception as e:
        log_error(f"音频后端 {name} 初始化失败: {e}")
        return None


class VoiceClient:
    """Full-duplex voice over LAN using UDP unicast to each peer.

    One VoiceClient per room participant.  Capture runs on a daemon thread;
    receive+playback runs on another daemon thread.  The client sends audio
    via unicast to each known peer IP rather than broadcasting.
    """

    def __init__(self, sender_id: str, voice_port: int):
        self._sender_id = sender_id
        self._sender_hash = struct.pack("<I", hash(sender_id) & 0xFFFFFFFF)
        self._voice_port = voice_port
        self._running = False
        self._stopped = False
        self._capture_thread = None
        self._playback_thread = None
        self._input_stream = None
        self._output_stream = None
        self._backend = None
        self._sequence = 0
        self._stop_lock = threading.Lock()
        self._stream_lock = threading.Lock()

        # Peer IPs to send audio to — updated by the room session
        self._peer_ips: list[str] = []
        self._peer_ips_lock = threading.Lock()

        # Per-peer jitter queues: sender_hash_bytes -> deque of (seq, pcm_bytes)
        self._jitter: dict[bytes, collections.deque] = {}
        self._peer_last_seq: dict[bytes, int] = {}

        _register_client(self)

    # ── Public API ──────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        """实际使用的音频后端名（启动后才有值）。"""
        return self._backend.name if self._backend else ""

    def set_peers(self, peer_ips: list[str]):
        """Update the list of peer IPs to send voice to."""
        with self._peer_ips_lock:
            self._peer_ips = list(peer_ips)

    def add_peer(self, peer_ip: str):
        with self._peer_ips_lock:
            if peer_ip not in self._peer_ips:
                self._peer_ips.append(peer_ip)

    def remove_peer(self, peer_ip: str):
        with self._peer_ips_lock:
            if peer_ip in self._peer_ips:
                self._peer_ips.remove(peer_ip)

    def start(self):
        """启动收发线程。返回是否成功。

        关键：**播放套接字的 bind 与音频输出设备的打开都必须在这里同步完成**。
        原实现把 bind 放在播放线程里，bind 失败只把 ``_running`` 置 False 就
        返回了，而 ``start()`` 早已返回 True —— 于是 UI 提示"语音已连接"，实际
        完全没声音（房主侧 100% 复现：房间发现服务已占用该 UDP 端口）。
        """
        if self._running:
            return False

        if not is_valid_voice_port(self._voice_port):
            log_error(f"语音端口无效: {self._voice_port}")
            return False

        backend = _make_backend()
        if backend is None:
            log_error(f"没有可用的音频后端（{INSTALL_HINT}）")
            return False
        self._backend = backend

        # 先把播放侧资源准备好，失败就直接返回 False
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self._voice_port))
        except OSError as e:
            log_error(f"语音端口 {self._voice_port} 绑定失败: {e}")
            sock.close()
            self._release_backend()
            return False
        sock.setblocking(False)

        try:
            self._output_stream = backend.open_output()
        except Exception as e:
            log_error(f"无法打开音频输出设备: {e}")
            sock.close()
            self._release_backend()
            return False

        self._running = True
        self._stopped = False
        self._sequence = 0

        self._playback_thread = threading.Thread(
            target=self._playback_loop, args=(sock,), daemon=True)
        self._playback_thread.start()

        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        return True

    def stop(self, timeout: float = 1.0):
        """停止语音并**保证音频流被显式关闭**（幂等，可从任意线程调用）。

        关闭顺序是这次修复的核心：原来直接 ``close_stream()``，而采集/播放
        线程此时正阻塞在 ``read()``/``write()`` 里，主线程并发 close 会在
        PortAudio 内部崩掉；更糟的是采集线程如果在 ``stop()`` 之后才把输入流
        打开完，那条流就成了没人管的孤儿，直到解释器退出才被 GC —— 实测**必段
        错误（exit 139）**。现在：置位 → ``abort()`` 解除阻塞 → join（有界）
        → 线程自己关自己打开的流 → 兜底补关 → 释放后端。
        """
        with self._stop_lock:
            if self._stopped and self._backend is None:
                return
            self._stopped = True
            self._running = False

            backend = self._backend
            if backend is not None:
                with self._stream_lock:
                    streams = (self._input_stream, self._output_stream)
                for stream in streams:
                    backend.abort_stream(stream)

            current = threading.current_thread()
            for thread in (self._capture_thread, self._playback_thread):
                if thread is None or thread is current or not thread.is_alive():
                    continue
                thread.join(timeout)

            if backend is not None:
                # 兜底：线程超时未退出时仍有流未关。宁可冒小概率竞态，也不能
                # 把 PortAudio 流留到解释器退出（那是必崩）。
                with self._stream_lock:
                    leftovers = (self._input_stream, self._output_stream)
                for stream in leftovers:
                    backend.close_stream(stream)

            self._release_backend()

    def _release_backend(self):
        with self._stream_lock:
            backend = self._backend
            self._backend = None
            self._input_stream = None
            self._output_stream = None
        if backend is not None:
            try:
                backend.terminate()
            except Exception as e:
                log_warn(f"释放音频后端失败: {e}")

    # ── Capture (mic → UDP unicast to each peer) ───────────

    def _capture_loop(self):
        backend = self._backend
        if backend is None:
            self._running = False
            return
        sock = None
        stream = None
        try:
            try:
                stream = backend.open_input()
            except Exception as e:
                log_error(f"无法打开音频输入设备: {e}")
                self._running = False
                return

            with self._stream_lock:
                if not self._running:
                    # stop() 在设备打开期间就发生了：这条流绝不能留成孤儿，
                    # 否则它会在解释器退出时才被 GC → 段错误
                    backend.close_stream(stream)
                    return
                self._input_stream = stream

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            while self._running:
                try:
                    pcm = backend.read(stream, FRAME_SAMPLES)
                except Exception as e:
                    # 关闭时 abort() 会让阻塞中的 read 抛异常，这不算故障
                    if self._running:
                        log_error(f"采集音频失败，语音采集线程退出: {e}")
                    break

                self._sequence += 1
                header = self._sender_hash + struct.pack("<I", self._sequence)
                frame = header + pcm

                # Unicast to each known peer
                with self._peer_ips_lock:
                    targets = list(self._peer_ips)
                for peer_ip in targets:
                    try:
                        sock.sendto(frame, (peer_ip, self._voice_port))
                    except OSError:
                        pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._stream_lock:
                if self._input_stream is stream:
                    self._input_stream = None
            backend.close_stream(stream)

    # ── Playback (UDP receive → jitter buffer → mix → speaker) ──

    def _playback_loop(self, sock):
        """收包 + 播放。

        ``sock`` 与 ``_output_stream`` 已由 :meth:`start` 准备好，这里只负责
        循环，因此 start() 能真实反映"语音能否启动"。

        性能要点：原实现用 ``sock.settimeout(0.05)`` + 每次循环只播放**一帧**
        （20ms）。收包阶段必然等满 50ms 超时，于是播放速率被压到 20 帧/秒，
        而采集端是 50 帧/秒 —— 约 84% 的音频被丢弃，表现为断续/卡顿。
        现在改成非阻塞收包 + 固定 20ms 节拍播放，并对落后做补偿。
        """
        backend = self._backend
        if backend is None:
            self._running = False
            try:
                sock.close()
            except OSError:
                pass
            return

        frame_period = VOICE_CHUNK_MS / 1000.0
        next_tick = time.monotonic()

        while self._running:
            # 非阻塞地把当前已到达的包全部收进抖动缓冲
            try:
                while True:
                    data, addr = sock.recvfrom(65536)
                    if len(data) < 8:
                        continue
                    sender_hash = data[:4]
                    if sender_hash == self._sender_hash:
                        continue  # ignore own voice
                    seq = struct.unpack("<I", data[4:8])[0]
                    pcm = data[8:]

                    if sender_hash not in self._jitter:
                        if len(self._jitter) >= MAX_TRACKED_SENDERS:
                            continue
                        self._jitter[sender_hash] = collections.deque(
                            maxlen=MAX_JITTER_FRAMES)
                        self._peer_last_seq[sender_hash] = 0

                    prev = self._peer_last_seq.get(sender_hash, 0)
                    if seq > prev or prev - seq > 1000:
                        self._jitter[sender_hash].append(pcm)
                        self._peer_last_seq[sender_hash] = seq
            except (BlockingIOError, socket.timeout):
                pass
            except OSError:
                break

            # 按 20ms 节拍播放；落后较多时一次补齐（最多 MAX_CATCHUP 帧），
            # 避免长时间抖动后播放速度被永久拖慢
            now = time.monotonic()
            frames_due = 0
            while now >= next_tick and frames_due < MAX_CATCHUP_FRAMES:
                next_tick += frame_period
                frames_due += 1
            if frames_due == 0:
                # 还没到下一拍：睡到该时刻，避免空转吃 CPU
                time.sleep(max(0.0, next_tick - now))
                continue
            if next_tick < now - frame_period * MAX_CATCHUP_FRAMES:
                # 已经落后太多（例如系统挂起），重置节拍而不是补播全部
                next_tick = now + frame_period

            for _ in range(frames_due):
                mixed = self._mix_frame()
                try:
                    backend.write(self._output_stream, mixed)
                except Exception as e:
                    # abort() 之后 write 会抛异常，这是正常关闭路径，不该报错
                    if self._running:
                        log_warn(f"播放音频失败，语音播放线程退出: {e}")
                    self._running = False
                    break
            if not self._running:
                break

        try:
            sock.close()
        except OSError:
            pass
        try:
            backend.close_stream(self._output_stream)
        finally:
            with self._stream_lock:
                self._output_stream = None

    def _mix_frame(self):
        """Pull one frame from each peer's jitter queue and mix via averaging."""
        frames = []
        expired = []
        for sender_hash, q in self._jitter.items():
            if q:
                frames.append(q.popleft())
            else:
                expired.append(sender_hash)

        for sender_hash in expired:
            if sender_hash in self._jitter and not self._jitter[sender_hash]:
                del self._jitter[sender_hash]
                # 同时清掉序号表：否则每个伪造 sender_hash 都会永久占用一个
                # 条目，局域网内任意主机都能把它撑成内存泄漏。
                self._peer_last_seq.pop(sender_hash, None)

        if not frames:
            return b"\x00" * FRAME_BYTES

        if len(frames) == 1:
            return frames[0][:FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")

        count = len(frames)
        mixed = bytearray(FRAME_BYTES)
        for i in range(0, FRAME_BYTES, 2):
            total = 0
            for frm in frames:
                if i + 2 <= len(frm):
                    total += struct.unpack_from("<h", frm, i)[0]
            avg = max(-32768, min(32767, total // count))
            struct.pack_into("<h", mixed, i, avg)

        return bytes(mixed)
