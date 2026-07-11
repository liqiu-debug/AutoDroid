"""
iOS MJPEG 实时画面流管理。

基于 WebDriverAgent 内置 MJPEG server（设备端默认端口 9100）：

- 通过 `tidevice relay` 将设备端 MJPEG 端口映射到本地 9300-9399 端口段
  （复用 backend/wda_port_manager.py 的 relay 生命周期管理）；
- 每台设备维持一条上游连接，增量解析 multipart JPEG 流并广播给多个客户端
  （广播/清理模式对齐 device_stream/manager.py 的 scrcpy 实现）；
- 客户端全部断开或上游（WDA）崩溃时，释放上游连接并回收 relay。

配置项（SystemSetting，解析优先级对齐 ios_wda_url）：

- 设备端 MJPEG 端口：`ios_mjpeg_port.{serial}` -> `ios_mjpeg_port_map`(JSON)
  -> `ios_mjpeg_port` -> 默认 9100；
- 帧率：`ios_mjpeg_framerate.{serial}` -> `ios_mjpeg_framerate` -> 默认 15；
- 质量：`ios_mjpeg_quality.{serial}` -> `ios_mjpeg_quality` -> 默认 50。
"""
from __future__ import annotations

import atexit
import json
import logging
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Generator, List, Optional
from urllib.parse import urlparse

from sqlmodel import Session

from backend.feature_flags import get_setting_value
from backend.wda_port_manager import ios_mjpeg_relay_manager

logger = logging.getLogger(__name__)

# 设备端 WDA MJPEG server 默认端口（WDA mjpegServerPort）
DEFAULT_MJPEG_DEVICE_PORT = 9100
# 默认推流参数（通过 WDA /appium/settings 尽力设置，避免带宽打满）
DEFAULT_MJPEG_FRAMERATE = 15
DEFAULT_MJPEG_QUALITY = 50

# 上游连接参数
UPSTREAM_CONNECT_TIMEOUT_SEC = 5.0
UPSTREAM_RECV_TIMEOUT_SEC = 5.0
# 上游持续无数据视为 WDA 已挂死（正常情况下 WDA 会按帧率持续推帧）
UPSTREAM_IDLE_TIMEOUT_SEC = 15.0
UPSTREAM_RECV_CHUNK = 65536

# 单帧 JPEG 上限；解析缓冲超过 2 倍上限视为流损坏
MAX_FRAME_BYTES = 8 * 1024 * 1024
# 客户端队列长度：MJPEG 帧相互独立，拥塞时丢旧帧保实时性
CLIENT_QUEUE_MAXSIZE = 3
# 最后一个客户端断开后的延迟回收宽限期：吸收前端刷新/重连的毫秒级竞态，
# 避免"断开即回收 relay"与"新客户端接入"互相踩踏（宽限期内新客户端直接复用上游）。
IDLE_SHUTDOWN_GRACE_SEC = 2.0

WDA_UNAVAILABLE_CODE = "P1005_WDA_UNAVAILABLE"

_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"
_CONTENT_LENGTH_RE = re.compile(rb"content-length\s*:\s*(\d+)", re.IGNORECASE)


class IOSMjpegStreamError(RuntimeError):
    """MJPEG 流建立/维持失败（错误码语义对齐 P1005_WDA_UNAVAILABLE）。"""

    def __init__(self, message: str, code: str = WDA_UNAVAILABLE_CODE):
        prefix = f"{code}: " if code and not message.startswith(code) else ""
        super().__init__(f"{prefix}{message}")
        self.code = code


# ==================== 上游地址与推流参数解析 ====================


@dataclass
class MjpegUpstream:
    """一台设备的 MJPEG 上游连接信息。"""

    serial: str
    host: str
    port: int
    via_relay: bool
    wda_url: str
    device_port: int


def _resolve_scoped_setting(
    session: Session,
    serial: str,
    base_key: str,
    map_key: Optional[str] = None,
) -> Optional[str]:
    """按 scoped -> map -> global 的优先级解析设置值（对齐 ios_wda_url 风格）。"""
    scoped = get_setting_value(session, f"{base_key}.{serial}")
    if scoped and str(scoped).strip():
        return str(scoped).strip()

    if map_key:
        mapping_raw = get_setting_value(session, map_key)
        if mapping_raw:
            try:
                mapping = json.loads(mapping_raw)
                if isinstance(mapping, dict):
                    mapped = mapping.get(serial)
                    if mapped is not None and str(mapped).strip():
                        return str(mapped).strip()
            except Exception:
                logger.warning("invalid JSON in setting %s, ignored", map_key)

    global_value = get_setting_value(session, base_key)
    if global_value and str(global_value).strip():
        return str(global_value).strip()
    return None


def _resolve_int_setting(
    session: Session,
    serial: str,
    base_key: str,
    default: int,
    *,
    map_key: Optional[str] = None,
    minimum: int = 1,
    maximum: int = 65535,
) -> int:
    raw = _resolve_scoped_setting(session, serial, base_key, map_key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid integer setting %s=%r, fallback to %s", base_key, raw, default)
        return default
    if not (minimum <= value <= maximum):
        logger.warning(
            "setting %s=%s out of range [%s, %s], fallback to %s",
            base_key, value, minimum, maximum, default,
        )
        return default
    return value


def resolve_ios_mjpeg_device_port(session: Session, serial: str) -> int:
    """解析设备端 MJPEG 端口：scoped -> map -> global -> 默认 9100。"""
    return _resolve_int_setting(
        session,
        serial,
        "ios_mjpeg_port",
        DEFAULT_MJPEG_DEVICE_PORT,
        map_key="ios_mjpeg_port_map",
    )


def resolve_ios_mjpeg_stream_settings(session: Session, serial: str) -> Dict[str, int]:
    """解析推流帧率/质量，返回可直接提交给 WDA /appium/settings 的字典。"""
    framerate = _resolve_int_setting(
        session, serial, "ios_mjpeg_framerate", DEFAULT_MJPEG_FRAMERATE, minimum=1, maximum=60,
    )
    quality = _resolve_int_setting(
        session, serial, "ios_mjpeg_quality", DEFAULT_MJPEG_QUALITY, minimum=1, maximum=100,
    )
    return {
        "mjpegServerFramerate": framerate,
        "mjpegServerScreenshotQuality": quality,
    }


def resolve_ios_mjpeg_upstream(session: Session, serial: str) -> MjpegUpstream:
    """
    解析设备的 MJPEG 上游连接地址。

    - WDA URL 解析为本机（127.0.0.1/localhost，含自动 relay）时，
      为设备端 MJPEG 端口建立本地 tidevice relay（9300-9399）；
    - WDA URL 指向远端主机时，直接连接同主机的 MJPEG 端口。
    """
    device_id = str(serial or "").strip()
    if not device_id:
        raise IOSMjpegStreamError("invalid device serial")

    device_port = resolve_ios_mjpeg_device_port(session, device_id)

    try:
        from backend.cross_platform_execution import resolve_ios_wda_url

        wda_url = resolve_ios_wda_url(session, device_id)
    except Exception as exc:
        raise IOSMjpegStreamError(
            f"failed to resolve WDA endpoint for device {device_id}: {exc}"
        ) from exc

    host = ""
    try:
        host = (urlparse(wda_url).hostname or "").strip().lower()
    except Exception:
        host = ""

    if host and host not in ("127.0.0.1", "localhost"):
        # 远程 WDA：假定 MJPEG server 与 WDA 同主机可达
        return MjpegUpstream(
            serial=device_id,
            host=host,
            port=device_port,
            via_relay=False,
            wda_url=wda_url,
            device_port=device_port,
        )

    try:
        local_port = ios_mjpeg_relay_manager.ensure_relay(device_id, remote_port=device_port)
    except Exception as exc:
        raise IOSMjpegStreamError(
            f"failed to establish MJPEG relay for device {device_id} "
            f"(device port {device_port}): {exc}"
        ) from exc

    return MjpegUpstream(
        serial=device_id,
        host="127.0.0.1",
        port=local_port,
        via_relay=True,
        wda_url=wda_url,
        device_port=device_port,
    )


def apply_wda_mjpeg_settings(wda_url: str, settings: Dict[str, int]) -> bool:
    """
    尽力而为地通过 WDA /appium/settings 设置 MJPEG 帧率/质量。

    失败仅记录日志，不阻断推流（WDA 会使用其默认参数）。
    """
    import requests

    base = str(wda_url or "").rstrip("/")
    if not base or not settings:
        return False
    try:
        resp = requests.get(f"{base}/status", timeout=3)
        resp.raise_for_status()
        payload = resp.json() or {}
        session_id = payload.get("sessionId") or (payload.get("value") or {}).get("sessionId")

        if not session_id:
            # 无会话时创建空会话（不启动任何 App，仅附着当前前台应用）
            created = requests.post(f"{base}/session", json={"capabilities": {}}, timeout=5)
            created.raise_for_status()
            created_payload = created.json() or {}
            session_id = created_payload.get("sessionId") or (
                created_payload.get("value") or {}
            ).get("sessionId")

        if not session_id:
            logger.info("skip WDA mjpeg settings: no sessionId available url=%s", base)
            return False

        resp = requests.post(
            f"{base}/session/{session_id}/appium/settings",
            json={"settings": dict(settings)},
            timeout=5,
        )
        resp.raise_for_status()
        logger.info("applied WDA mjpeg settings: url=%s settings=%s", base, settings)
        return True
    except Exception as exc:
        logger.warning("apply WDA mjpeg settings failed (ignored): url=%s error=%s", base, exc)
        return False


# ==================== multipart MJPEG 解析 ====================


class MjpegStreamParser:
    """
    增量解析 multipart MJPEG 字节流，提取完整 JPEG 帧。

    兼容两种格式：
    - part header 携带 Content-Length（WDA FBMjpegServer 格式）：按长度精确截取；
    - 无 Content-Length：按 JPEG SOI/EOI 标记扫描。

    HTTP 响应头、boundary 行等非 JPEG 前缀会被自动跳过，
    因此无需依赖具体 boundary 字符串（WDA 的 boundary 声明与实际分隔行不一致）。
    """

    def __init__(self, max_buffer: int = MAX_FRAME_BYTES * 2):
        self._buffer = bytearray()
        self._max_buffer = max_buffer

    def feed(self, data: bytes) -> List[bytes]:
        """喂入新数据，返回本次解析出的完整 JPEG 帧列表。"""
        if data:
            self._buffer.extend(data)

        frames: List[bytes] = []
        while True:
            frame = self._extract_one()
            if frame is None:
                break
            frames.append(frame)

        if len(self._buffer) > self._max_buffer:
            raise IOSMjpegStreamError(
                f"MJPEG parse buffer overflow ({len(self._buffer)} bytes), stream corrupted"
            )
        return frames

    def _extract_one(self) -> Optional[bytes]:
        soi = self._buffer.find(_JPEG_SOI)
        if soi < 0:
            return None

        # 优先使用 SOI 前 part header 中的 Content-Length 精确截取
        header_blob = bytes(self._buffer[:soi])
        matches = _CONTENT_LENGTH_RE.findall(header_blob)
        if matches:
            try:
                frame_len = int(matches[-1])
            except ValueError:
                frame_len = 0
            if 0 < frame_len <= MAX_FRAME_BYTES:
                end = soi + frame_len
                if len(self._buffer) < end:
                    return None
                frame = bytes(self._buffer[soi:end])
                del self._buffer[:end]
                return frame

        # 回退：按 JPEG EOI 标记扫描
        eoi = self._buffer.find(_JPEG_EOI, soi + len(_JPEG_SOI))
        if eoi < 0:
            return None
        end = eoi + len(_JPEG_EOI)
        frame = bytes(self._buffer[soi:end])
        del self._buffer[:end]
        return frame


# ==================== 广播流与客户端 ====================


def _offer_latest(client_queue: "queue.Queue", item) -> None:
    """将元素放入客户端队列；拥塞时丢弃旧帧，保证客户端看到的是最新画面。"""
    while True:
        try:
            client_queue.put_nowait(item)
            return
        except queue.Full:
            try:
                client_queue.get_nowait()
            except queue.Empty:
                pass


class MjpegClient:
    """单个下游客户端：持有独立帧队列，由所属流广播填充。"""

    def __init__(self, stream: "_DeviceMjpegStream"):
        self._stream = stream
        self.queue: "queue.Queue" = queue.Queue(maxsize=CLIENT_QUEUE_MAXSIZE)

    @property
    def serial(self) -> str:
        return self._stream.serial

    def next_frame(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """阻塞获取下一帧 JPEG；流结束（或超时）返回 None。"""
        deadline = time.time() + timeout if timeout is not None else None
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                if not self._stream.is_active():
                    return None
                if deadline is not None and time.time() >= deadline:
                    return None
                continue
            return item

    def frames(self) -> Generator[bytes, None, None]:
        """帧生成器：直到流结束。"""
        while True:
            frame = self.next_frame(timeout=UPSTREAM_IDLE_TIMEOUT_SEC * 2)
            if frame is None:
                return
            yield frame

    def close(self) -> None:
        """注销客户端；若为最后一个客户端则触发上游释放。"""
        self._stream.remove_client(self)


class _DeviceMjpegStream:
    """
    单台设备的 MJPEG 上游连接与广播器。

    生命周期：
    1. 首个客户端接入 -> 连接上游 -> 启动读取线程
    2. 读取线程解析出 JPEG 帧 -> 广播给所有客户端队列
    3. 客户端全部断开 / 上游断开 / 解析异常 -> 结束并回调 on_stopped
    """

    def __init__(
        self,
        upstream: MjpegUpstream,
        on_stopped: Callable[["_DeviceMjpegStream", Optional[str]], None],
    ):
        self.upstream = upstream
        self.serial = upstream.serial
        self._on_stopped = on_stopped

        self._clients: List[MjpegClient] = []
        self._clients_lock = threading.Lock()
        self._start_lock = threading.Lock()

        self._socket: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._idle_timer: Optional[threading.Timer] = None
        self._started = False
        self._stop_requested = False
        self._finished = False
        self._last_frame: Optional[bytes] = None
        self.error: Optional[str] = None

    # ---------- 客户端管理 ----------

    def add_client(self) -> Optional[MjpegClient]:
        """注册新客户端；流已结束时返回 None（由管理器重建流）。"""
        with self._clients_lock:
            if self._finished or self._stop_requested:
                return None
            client = MjpegClient(self)
            self._clients.append(client)
            last_frame = self._last_frame
            idle_timer = self._idle_timer
            self._idle_timer = None
        if idle_timer is not None:
            # 宽限期内新客户端接入：取消延迟回收，直接复用现有上游
            idle_timer.cancel()
        if last_frame is not None:
            # 预置最近一帧，保证新客户端秒出画面
            _offer_latest(client.queue, last_frame)
        return client

    def remove_client(self, client: MjpegClient) -> None:
        with self._clients_lock:
            if client in self._clients:
                self._clients.remove(client)
            has_clients = bool(self._clients)
        if not has_clients:
            self._schedule_idle_shutdown()

    def _schedule_idle_shutdown(self) -> None:
        """最后一个客户端断开后延迟回收上游/relay；宽限期内接入的新客户端可复用。"""
        grace = max(0.0, float(IDLE_SHUTDOWN_GRACE_SEC))
        if grace <= 0:
            self.shutdown()
            return
        with self._clients_lock:
            if self._finished or self._stop_requested or self._clients:
                return
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            timer = threading.Timer(grace, self._idle_shutdown_due)
            timer.daemon = True
            self._idle_timer = timer
        timer.start()

    def _idle_shutdown_due(self) -> None:
        with self._clients_lock:
            self._idle_timer = None
        # shutdown 内部会再次确认当前确实没有客户端
        self.shutdown()

    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def is_active(self) -> bool:
        return not self._finished

    def accepts_clients(self) -> bool:
        """是否可继续接收新客户端（未结束且未在停止流程中）。"""
        return not self._finished and not self._stop_requested

    # ---------- 启动与停止 ----------

    def ensure_started(self) -> None:
        """确保上游连接与读取线程已启动（幂等；并发时其余调用方等待结果）。"""
        with self._start_lock:
            if self._started:
                if self._finished:
                    raise IOSMjpegStreamError(
                        self.error or f"MJPEG stream for device {self.serial} already closed"
                    )
                return
            if self._finished or self._stop_requested:
                raise IOSMjpegStreamError(
                    self.error or f"MJPEG stream for device {self.serial} already closed"
                )
            self._connect_and_start()
            self._started = True

    def _connect_and_start(self) -> None:
        host, port = self.upstream.host, self.upstream.port
        try:
            sock = socket.create_connection((host, port), timeout=UPSTREAM_CONNECT_TIMEOUT_SEC)
        except Exception as exc:
            message = (
                f"failed to connect MJPEG upstream {host}:{port} for device {self.serial}: {exc}"
            )
            self._finish(message)
            raise IOSMjpegStreamError(message) from exc

        try:
            sock.settimeout(UPSTREAM_RECV_TIMEOUT_SEC)
            # WDA 的 MJPEG server 在 TCP 连接后即推流；发送 GET 兼容需要请求行的实现
            request = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Accept: multipart/x-mixed-replace, image/jpeg\r\n"
                f"Connection: keep-alive\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
        except Exception as exc:
            try:
                sock.close()
            except Exception:
                pass
            message = (
                f"failed to handshake MJPEG upstream {host}:{port} for device {self.serial}: {exc}"
            )
            self._finish(message)
            raise IOSMjpegStreamError(message) from exc

        self._socket = sock
        thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"IOSMjpegReader-{self.serial}",
        )
        self._reader_thread = thread
        thread.start()
        logger.info(
            "iOS MJPEG 上游已连接: serial=%s upstream=%s:%s via_relay=%s",
            self.serial, host, port, self.upstream.via_relay,
        )

    def shutdown(self, force: bool = False) -> None:
        """请求停止；默认仅在无客户端时生效，force=True 用于服务退出强制停止。"""
        with self._clients_lock:
            if self._finished:
                return
            if self._clients and not force:
                return
            self._stop_requested = True
            sock = self._socket
            idle_timer = self._idle_timer
            self._idle_timer = None
        if idle_timer is not None:
            idle_timer.cancel()
        if sock is not None:
            try:
                sock.close()  # 唤醒阻塞在 recv 的读取线程
            except Exception:
                pass
        if not self._started:
            # 读取线程从未启动，直接结束
            self._finish(None)

    def _reader_loop(self) -> None:
        parser = MjpegStreamParser()
        sock = self._socket
        error: Optional[str] = None
        last_data_at = time.time()

        logger.info(
            "iOS MJPEG 读取线程启动: serial=%s upstream=%s:%s",
            self.serial, self.upstream.host, self.upstream.port,
        )
        while not self._stop_requested:
            try:
                chunk = sock.recv(UPSTREAM_RECV_CHUNK)
            except socket.timeout:
                if time.time() - last_data_at > UPSTREAM_IDLE_TIMEOUT_SEC:
                    error = (
                        f"MJPEG upstream idle for {UPSTREAM_IDLE_TIMEOUT_SEC:.0f}s, "
                        f"WDA may have crashed"
                    )
                    break
                continue
            except OSError as exc:
                if not self._stop_requested:
                    error = f"MJPEG upstream read failed: {exc}"
                break

            if not chunk:
                if not self._stop_requested:
                    error = "MJPEG upstream closed connection (WDA stopped?)"
                break

            last_data_at = time.time()
            try:
                frames = parser.feed(chunk)
            except IOSMjpegStreamError as exc:
                error = str(exc)
                break
            for frame in frames:
                self._broadcast(frame)

        logger.info("iOS MJPEG 读取线程结束: serial=%s error=%s", self.serial, error)
        self._finish(error)

    def _broadcast(self, frame: bytes) -> None:
        with self._clients_lock:
            self._last_frame = frame
            clients = list(self._clients)
        for client in clients:
            _offer_latest(client.queue, frame)

    def _finish(self, error: Optional[str]) -> None:
        with self._clients_lock:
            if self._finished:
                return
            self._finished = True
            self.error = error
            clients = list(self._clients)

        sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        # 发送结束哨兵，唤醒所有等待帧的客户端
        for client in clients:
            _offer_latest(client.queue, None)

        try:
            self._on_stopped(self, error)
        except Exception:
            logger.exception("iOS MJPEG 流停止回调异常: serial=%s", self.serial)


class IOSMjpegStreamManager:
    """
    iOS MJPEG 流管理器（单例）。

    - 每台设备复用同一条上游连接，多客户端广播；
    - 客户端全部断开或上游异常时，关闭上游连接并回收对应 relay。
    """

    def __init__(self, relay_manager=ios_mjpeg_relay_manager):
        self._streams: Dict[str, _DeviceMjpegStream] = {}
        self._lock = threading.Lock()
        self._relay_manager = relay_manager

    def open_client(
        self,
        upstream: MjpegUpstream,
        configure: Optional[Callable[[], None]] = None,
    ) -> MjpegClient:
        """
        为设备注册一个流客户端；必要时建立上游连接。

        configure 仅在新建上游流时调用（如设置 WDA 帧率/质量），失败不阻断。
        Raises:
            IOSMjpegStreamError: 上游连接失败。
        """
        serial = upstream.serial
        for _ in range(3):
            with self._lock:
                stream = self._streams.get(serial)
                is_new = stream is None or not stream.accepts_clients()
                if is_new:
                    stream = _DeviceMjpegStream(upstream, on_stopped=self._handle_stream_stopped)
                    self._streams[serial] = stream
                client = stream.add_client()

            if client is None:
                # 拿到的流恰好刚结束，重试创建新流
                continue

            if is_new and configure is not None:
                try:
                    configure()
                except Exception as exc:
                    logger.warning(
                        "iOS MJPEG 流参数配置失败（忽略）: serial=%s error=%s", serial, exc
                    )

            try:
                stream.ensure_started()
            except Exception:
                client.close()
                raise
            return client

        raise IOSMjpegStreamError(f"failed to open MJPEG stream for device {serial}: retry exhausted")

    def describe(self) -> List[dict]:
        """返回当前活跃流的概览（调试用）。"""
        with self._lock:
            streams = list(self._streams.values())
        return [
            {
                "serial": stream.serial,
                "upstream": f"{stream.upstream.host}:{stream.upstream.port}",
                "via_relay": stream.upstream.via_relay,
                "clients": stream.client_count(),
                "active": stream.is_active(),
            }
            for stream in streams
        ]

    def shutdown(self) -> None:
        """停止所有流并回收 relay（进程退出时调用）。"""
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            try:
                stream.shutdown(force=True)
            except Exception:
                logger.exception("关闭 iOS MJPEG 流失败: serial=%s", stream.serial)

    def _handle_stream_stopped(self, stream: _DeviceMjpegStream, error: Optional[str]) -> None:
        with self._lock:
            if self._streams.get(stream.serial) is stream:
                self._streams.pop(stream.serial, None)
            # 新流已接管同一设备（快速重连），不回收其正在使用的 relay
            replaced = self._streams.get(stream.serial) is not None

        if stream.upstream.via_relay and not replaced:
            try:
                self._relay_manager.stop_relay(stream.serial)
            except Exception as exc:
                logger.warning(
                    "回收 iOS MJPEG relay 失败（忽略）: serial=%s error=%s", stream.serial, exc
                )

        if error:
            logger.warning("iOS MJPEG 流异常结束: serial=%s error=%s", stream.serial, error)
        else:
            logger.info("iOS MJPEG 流已关闭: serial=%s", stream.serial)


ios_mjpeg_stream_manager = IOSMjpegStreamManager()


def _shutdown_at_exit() -> None:
    # main.py 的 on_shutdown 钩子会显式调用清理；这里通过 atexit 兜底，
    # 覆盖非 FastAPI 生命周期的退出路径，避免 tidevice relay 子进程残留。
    try:
        ios_mjpeg_stream_manager.shutdown()
    except Exception:
        pass
    try:
        ios_mjpeg_relay_manager.stop_all()
    except Exception:
        pass


atexit.register(_shutdown_at_exit)
