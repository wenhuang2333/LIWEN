"""Full-duplex voice over LAN using UDP unicast + PyAudio.

Each peer gets a dedicated UDP unicast stream.  This avoids flooding the
WiFi with broadcast audio, which is especially important on mobile hotspots.

Audio format: 16-bit mono PCM @ 22050 Hz, 20ms frames.
Frame header: 4-byte sender_hash + 4-byte sequence number.
"""

import collections
import socket
import struct
import threading
import time

from utils.constants import (
    VOICE_PORT_OFFSET, VOICE_SAMPLE_RATE, VOICE_CHUNK_MS,
    VOICE_CHANNELS, VOICE_SAMPLE_WIDTH, VOICE_JITTER_MS,
    is_valid_voice_port,
)
from utils.logger import error as log_error

FRAME_SAMPLES = VOICE_SAMPLE_RATE * VOICE_CHUNK_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * VOICE_CHANNELS * VOICE_SAMPLE_WIDTH
MAX_JITTER_FRAMES = VOICE_JITTER_MS // VOICE_CHUNK_MS

# 单次循环最多补播的帧数（防止系统挂起后一次性写入大量音频）
MAX_CATCHUP_FRAMES = 5
# 同时跟踪的对端上限：无认证的 UDP 端口，必须设上限防止内存被撑爆
MAX_TRACKED_SENDERS = 32

_PYAUDIO = None


def _get_pyaudio():
    global _PYAUDIO
    if _PYAUDIO is None:
        try:
            import pyaudio
            _PYAUDIO = pyaudio
        except ImportError:
            pass
    return _PYAUDIO


def is_voice_available() -> bool:
    return _get_pyaudio() is not None


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
        self._capture_thread = None
        self._playback_thread = None
        self._input_stream = None
        self._output_stream = None
        self._pa = None
        self._sequence = 0

        # Peer IPs to send audio to — updated by the room session
        self._peer_ips: list[str] = []
        self._peer_ips_lock = threading.Lock()

        # Per-peer jitter queues: sender_hash_bytes -> deque of (seq, pcm_bytes)
        self._jitter: dict[bytes, collections.deque] = {}
        self._peer_last_seq: dict[bytes, int] = {}

    # ── Public API ──────────────────────────────────────────

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

        关键：**播放套接字的 bind 必须在这里同步完成**。原实现把 bind 放在
        播放线程里，bind 失败只把 ``_running`` 置 False 就返回了，而 ``start()``
        早已返回 True —— 于是 UI 提示"语音已连接"，实际完全没声音（房主侧
        100% 复现：房间发现服务已占用该 UDP 端口）。
        """
        pyaudio = _get_pyaudio()
        if not pyaudio or self._running:
            return False

        if not is_valid_voice_port(self._voice_port):
            log_error(f"语音端口无效: {self._voice_port}")
            return False

        # 先把播放侧资源准备好，失败就直接返回 False
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self._voice_port))
        except OSError as e:
            log_error(f"语音端口 {self._voice_port} 绑定失败: {e}")
            sock.close()
            return False
        sock.setblocking(False)

        self._pa = pyaudio.PyAudio()
        try:
            self._output_stream = self._pa.open(
                format=self._pa.get_format_from_width(VOICE_SAMPLE_WIDTH),
                channels=VOICE_CHANNELS,
                rate=VOICE_SAMPLE_RATE,
                output=True,
                frames_per_buffer=FRAME_SAMPLES,
            )
        except Exception as e:
            log_error(f"无法打开音频输出设备: {e}")
            sock.close()
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
            return False

        self._running = True
        self._sequence = 0

        self._playback_thread = threading.Thread(
            target=self._playback_loop, args=(sock,), daemon=True)
        self._playback_thread.start()

        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        return True

    def stop(self):
        self._running = False
        for stream in [self._input_stream, self._output_stream]:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        self._input_stream = None
        self._output_stream = None

    # ── Capture (mic → UDP unicast to each peer) ───────────

    def _capture_loop(self):
        try:
            self._input_stream = self._pa.open(
                format=self._pa.get_format_from_width(VOICE_SAMPLE_WIDTH),
                channels=VOICE_CHANNELS,
                rate=VOICE_SAMPLE_RATE,
                input=True,
                frames_per_buffer=FRAME_SAMPLES,
            )
        except Exception:
            self._running = False
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        while self._running:
            try:
                pcm = self._input_stream.read(
                    FRAME_SAMPLES, exception_on_overflow=False)
            except Exception:
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

        sock.close()

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
                    self._output_stream.write(mixed)
                except Exception:
                    self._running = False
                    break
            if not self._running:
                break

        try:
            sock.close()
        except OSError:
            pass
        try:
            if self._output_stream is not None:
                self._output_stream.stop_stream()
                self._output_stream.close()
        except Exception:
            pass
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
