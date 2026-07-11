"""iOS MJPEG 实时画面流（WDA mjpegServer）单元测试。

覆盖：multipart 解析、配置解析优先级、relay 端口管理、
多客户端广播与断开清理、WDA 不可达错误路径、WS/HTTP 端点契约。
"""
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from backend.device_stream import ios_mjpeg
from backend.device_stream.ios_mjpeg import (
    DEFAULT_MJPEG_DEVICE_PORT,
    IOSMjpegStreamError,
    IOSMjpegStreamManager,
    MjpegStreamParser,
    MjpegUpstream,
    resolve_ios_mjpeg_device_port,
    resolve_ios_mjpeg_stream_settings,
    resolve_ios_mjpeg_upstream,
)
from backend.models import SystemSetting
from backend.wda_port_manager import (
    MJPEG_PORT_RANGE_END,
    MJPEG_PORT_RANGE_START,
    MJPEG_REMOTE_PORT,
    WDARelayManager,
    ios_mjpeg_relay_manager,
    wda_relay_manager,
)


def _jpeg(payload: bytes) -> bytes:
    return b"\xff\xd8\xff\xe0" + payload + b"\xff\xd9"


def _wda_multipart(frames, content_length: bool = True) -> bytes:
    """构造 WDA FBMjpegServer 风格的 MJPEG 字节流。"""
    out = bytearray(
        b"HTTP/1.0 200 OK\r\n"
        b"Server: WebDriverAgent\r\n"
        b"Content-Type: multipart/x-mixed-replace; boundary=--BoundaryString\r\n\r\n"
    )
    for frame in frames:
        out += b"--BoundaryString\r\n"
        out += b"Content-type: image/jpg\r\n"
        if content_length:
            out += b"Content-Length: " + str(len(frame)).encode("ascii") + b"\r\n"
        out += b"\r\n" + frame + b"\r\n\r\n"
    return bytes(out)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class _FakeUpstreamSocket:
    """脚本化的上游 socket：按序返回 chunks，之后 EOF 或挂起等待关闭。"""

    def __init__(self, chunks=(), eof: bool = True, gate: threading.Event = None):
        self._chunks = list(chunks)
        self._eof = eof
        self._gate = gate
        self.closed = threading.Event()
        self.sent = b""

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent += bytes(data)

    def recv(self, size):
        if self.closed.is_set():
            raise OSError("socket closed")
        if self._gate is not None and not self._gate.is_set():
            if self.closed.wait(0.02):
                raise OSError("socket closed")
            raise socket.timeout("timed out")
        if self._chunks:
            return self._chunks.pop(0)
        if self._eof:
            return b""
        if self.closed.wait(0.02):
            raise OSError("socket closed")
        raise socket.timeout("timed out")

    def close(self):
        self.closed.set()


class MjpegStreamParserTests(unittest.TestCase):
    def test_parses_wda_style_stream_with_content_length(self):
        frames = [_jpeg(b"frame-1" * 10), _jpeg(b"frame-2" * 20), _jpeg(b"f3")]
        parser = MjpegStreamParser()

        parsed = parser.feed(_wda_multipart(frames))

        self.assertEqual(parsed, frames)

    def test_parses_stream_fed_in_small_chunks(self):
        frames = [_jpeg(b"alpha" * 30), _jpeg(b"beta" * 7)]
        raw = _wda_multipart(frames)
        parser = MjpegStreamParser()

        parsed = []
        for i in range(0, len(raw), 7):
            parsed.extend(parser.feed(raw[i:i + 7]))

        self.assertEqual(parsed, frames)

    def test_content_length_wins_over_inner_eoi_marker(self):
        # JPEG 数据内部出现 EOI 标记（如内嵌缩略图）时，Content-Length 保证不截断
        tricky = b"\xff\xd8\xff\xe1" + b"\xff\xd9" + b"tail" * 10 + b"\xff\xd9"
        parser = MjpegStreamParser()

        parsed = parser.feed(_wda_multipart([tricky]))

        self.assertEqual(parsed, [tricky])

    def test_falls_back_to_soi_eoi_scan_without_content_length(self):
        frames = [_jpeg(b"plain-1" * 5), _jpeg(b"plain-2" * 5)]
        parser = MjpegStreamParser()

        parsed = parser.feed(_wda_multipart(frames, content_length=False))

        self.assertEqual(parsed, frames)

    def test_incomplete_frame_is_held_until_more_data(self):
        frame = _jpeg(b"partial" * 10)
        raw = _wda_multipart([frame])
        parser = MjpegStreamParser()

        self.assertEqual(parser.feed(raw[:-20]), [])
        self.assertEqual(parser.feed(raw[-20:]), [frame])

    def test_buffer_overflow_raises_stream_error(self):
        parser = MjpegStreamParser(max_buffer=1024)

        with self.assertRaises(IOSMjpegStreamError):
            parser.feed(b"\x00" * 4096)


class MjpegSettingResolutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()

    def _add_setting(self, key: str, value: str):
        self.session.add(SystemSetting(key=key, value=value))
        self.session.commit()

    def test_device_port_defaults_to_9100(self):
        self.assertEqual(resolve_ios_mjpeg_device_port(self.session, "ios-1"), DEFAULT_MJPEG_DEVICE_PORT)

    def test_device_port_global_setting(self):
        self._add_setting("ios_mjpeg_port", "9200")
        self.assertEqual(resolve_ios_mjpeg_device_port(self.session, "ios-1"), 9200)

    def test_device_port_map_overrides_global(self):
        self._add_setting("ios_mjpeg_port", "9200")
        self._add_setting("ios_mjpeg_port_map", '{"ios-1": 9201}')
        self.assertEqual(resolve_ios_mjpeg_device_port(self.session, "ios-1"), 9201)
        self.assertEqual(resolve_ios_mjpeg_device_port(self.session, "ios-2"), 9200)

    def test_device_port_scoped_overrides_map(self):
        self._add_setting("ios_mjpeg_port_map", '{"ios-1": 9201}')
        self._add_setting("ios_mjpeg_port.ios-1", "9202")
        self.assertEqual(resolve_ios_mjpeg_device_port(self.session, "ios-1"), 9202)

    def test_device_port_invalid_value_falls_back_to_default(self):
        self._add_setting("ios_mjpeg_port", "not-a-port")
        self.assertEqual(resolve_ios_mjpeg_device_port(self.session, "ios-1"), DEFAULT_MJPEG_DEVICE_PORT)

    def test_stream_settings_defaults(self):
        settings = resolve_ios_mjpeg_stream_settings(self.session, "ios-1")
        self.assertEqual(
            settings,
            {
                "mjpegServerFramerate": ios_mjpeg.DEFAULT_MJPEG_FRAMERATE,
                "mjpegServerScreenshotQuality": ios_mjpeg.DEFAULT_MJPEG_QUALITY,
            },
        )

    def test_stream_settings_scoped_override_and_range_guard(self):
        self._add_setting("ios_mjpeg_framerate", "30")
        self._add_setting("ios_mjpeg_framerate.ios-1", "10")
        self._add_setting("ios_mjpeg_quality", "999")  # 超出范围回退默认

        settings = resolve_ios_mjpeg_stream_settings(self.session, "ios-1")

        self.assertEqual(settings["mjpegServerFramerate"], 10)
        self.assertEqual(settings["mjpegServerScreenshotQuality"], ios_mjpeg.DEFAULT_MJPEG_QUALITY)
        other = resolve_ios_mjpeg_stream_settings(self.session, "ios-2")
        self.assertEqual(other["mjpegServerFramerate"], 30)


class MjpegUpstreamResolutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()

    def test_local_wda_url_uses_mjpeg_relay(self):
        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8233",
        ), patch.object(
            ios_mjpeg.ios_mjpeg_relay_manager, "ensure_relay", return_value=9305
        ) as ensure_mock:
            upstream = resolve_ios_mjpeg_upstream(self.session, "ios-1")

        self.assertEqual(upstream.host, "127.0.0.1")
        self.assertEqual(upstream.port, 9305)
        self.assertTrue(upstream.via_relay)
        self.assertEqual(upstream.device_port, DEFAULT_MJPEG_DEVICE_PORT)
        self.assertEqual(upstream.wda_url, "http://127.0.0.1:8233")
        ensure_mock.assert_called_once_with("ios-1", remote_port=DEFAULT_MJPEG_DEVICE_PORT)

    def test_configured_device_port_is_passed_to_relay(self):
        self.session.add(SystemSetting(key="ios_mjpeg_port.ios-1", value="9110"))
        self.session.commit()

        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://localhost:8200",
        ), patch.object(
            ios_mjpeg.ios_mjpeg_relay_manager, "ensure_relay", return_value=9300
        ) as ensure_mock:
            upstream = resolve_ios_mjpeg_upstream(self.session, "ios-1")

        self.assertEqual(upstream.device_port, 9110)
        ensure_mock.assert_called_once_with("ios-1", remote_port=9110)

    def test_remote_wda_url_connects_directly_without_relay(self):
        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://10.10.10.2:8100",
        ), patch.object(ios_mjpeg.ios_mjpeg_relay_manager, "ensure_relay") as ensure_mock:
            upstream = resolve_ios_mjpeg_upstream(self.session, "ios-1")

        self.assertEqual(upstream.host, "10.10.10.2")
        self.assertEqual(upstream.port, DEFAULT_MJPEG_DEVICE_PORT)
        self.assertFalse(upstream.via_relay)
        ensure_mock.assert_not_called()

    def test_wda_resolution_failure_raises_p1005_error(self):
        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            side_effect=RuntimeError("tidevice command not found"),
        ):
            with self.assertRaises(IOSMjpegStreamError) as ctx:
                resolve_ios_mjpeg_upstream(self.session, "ios-1")
        self.assertIn("P1005_WDA_UNAVAILABLE", str(ctx.exception))

    def test_relay_failure_raises_p1005_error(self):
        with patch(
            "backend.cross_platform_execution.resolve_ios_wda_url",
            return_value="http://127.0.0.1:8233",
        ), patch.object(
            ios_mjpeg.ios_mjpeg_relay_manager,
            "ensure_relay",
            side_effect=RuntimeError("no free WDA relay port in range 9300-9399"),
        ):
            with self.assertRaises(IOSMjpegStreamError) as ctx:
                resolve_ios_mjpeg_upstream(self.session, "ios-1")
        self.assertIn("P1005_WDA_UNAVAILABLE", str(ctx.exception))


class _FakeRelayProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 1 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class MjpegRelayManagerTests(unittest.TestCase):
    def test_mjpeg_relay_manager_uses_dedicated_port_range(self):
        self.assertEqual(MJPEG_PORT_RANGE_START, 9300)
        self.assertEqual(MJPEG_PORT_RANGE_END, 9399)
        self.assertEqual(MJPEG_REMOTE_PORT, 9100)
        self.assertEqual(ios_mjpeg_relay_manager._start_port, 9300)
        self.assertEqual(ios_mjpeg_relay_manager._end_port, 9399)
        self.assertEqual(ios_mjpeg_relay_manager._remote_port, 9100)
        # WDA HTTP relay 管理器保持原有端口段不变
        self.assertEqual(wda_relay_manager._start_port, 8200)
        self.assertEqual(wda_relay_manager._remote_port, 8100)

    def _make_manager(self):
        manager = WDARelayManager(start_port=9300, end_port=9310, remote_port=9100)
        processes = []

        def _fake_spawn(udid, local_port, remote_port):
            process = _FakeRelayProcess()
            processes.append((udid, local_port, remote_port, process))
            return process

        spawn_patch = patch.object(manager, "_spawn_relay", side_effect=_fake_spawn)
        port_patch = patch.object(WDARelayManager, "_is_port_free", staticmethod(lambda port: True))
        return manager, processes, spawn_patch, port_patch

    def test_ensure_relay_reuses_entry_with_same_remote_port(self):
        manager, processes, spawn_patch, port_patch = self._make_manager()
        with spawn_patch, port_patch:
            first = manager.ensure_relay("udid-1", remote_port=9100)
            second = manager.ensure_relay("udid-1", remote_port=9100)

        self.assertEqual(first, second)
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0][2], 9100)

    def test_ensure_relay_rebuilds_when_remote_port_changes(self):
        manager, processes, spawn_patch, port_patch = self._make_manager()
        with spawn_patch, port_patch:
            manager.ensure_relay("udid-1", remote_port=9100)
            manager.ensure_relay("udid-1", remote_port=9105)

        self.assertEqual(len(processes), 2)
        self.assertTrue(processes[0][3].terminated)
        self.assertFalse(processes[1][3].terminated)
        self.assertEqual(processes[1][2], 9105)

    def test_ensure_relay_rejects_invalid_remote_port(self):
        manager, _, spawn_patch, port_patch = self._make_manager()
        with spawn_patch, port_patch:
            with self.assertRaises(RuntimeError):
                manager.ensure_relay("udid-1", remote_port="not-a-port")
            with self.assertRaises(RuntimeError):
                manager.ensure_relay("udid-1", remote_port=0)


class MjpegStreamManagerTests(unittest.TestCase):
    def setUp(self):
        # 缩短空闲回收宽限期，保证清理路径的用例快速且确定
        patcher = patch.object(ios_mjpeg, "IDLE_SHUTDOWN_GRACE_SEC", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _upstream(self, serial="ios-1", via_relay=True):
        return MjpegUpstream(
            serial=serial,
            host="127.0.0.1",
            port=9301,
            via_relay=via_relay,
            wda_url="http://127.0.0.1:8201",
            device_port=9100,
        )

    def test_broadcasts_frames_to_multiple_clients(self):
        frames = [_jpeg(b"f1" * 20), _jpeg(b"f2" * 20), _jpeg(b"f3" * 20)]
        gate = threading.Event()
        fake_socket = _FakeUpstreamSocket(chunks=[_wda_multipart(frames)], eof=False, gate=gate)
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)
        configure = Mock()

        with patch(
            "backend.device_stream.ios_mjpeg.socket.create_connection",
            return_value=fake_socket,
        ) as connect_mock:
            client_a = manager.open_client(self._upstream(), configure=configure)
            client_b = manager.open_client(self._upstream(), configure=configure)
            gate.set()

            received_a = [client_a.next_frame(timeout=2.0) for _ in range(3)]
            received_b = [client_b.next_frame(timeout=2.0) for _ in range(3)]

            # 单上游连接被复用；configure 仅在新建流时调用一次
            connect_mock.assert_called_once()
            configure.assert_called_once()
            self.assertEqual(received_a, frames)
            self.assertEqual(received_b, frames)

            # 迟到客户端立即拿到缓存的最近一帧
            client_c = manager.open_client(self._upstream())
            self.assertEqual(client_c.next_frame(timeout=2.0), frames[-1])

            client_a.close()
            client_b.close()
            client_c.close()

        self.assertTrue(_wait_until(lambda: fake_socket.closed.is_set()))
        self.assertTrue(_wait_until(lambda: not manager.describe()))
        relay_manager.stop_relay.assert_called_with("ios-1")

    def test_upstream_eof_terminates_clients_and_releases_relay(self):
        frames = [_jpeg(b"only" * 10)]
        fake_socket = _FakeUpstreamSocket(chunks=[_wda_multipart(frames)], eof=True)
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)

        with patch(
            "backend.device_stream.ios_mjpeg.socket.create_connection",
            return_value=fake_socket,
        ):
            client = manager.open_client(self._upstream())
            self.assertEqual(client.next_frame(timeout=2.0), frames[0])
            # 上游 EOF 后，客户端收到结束哨兵
            self.assertIsNone(client.next_frame(timeout=2.0))

        self.assertTrue(_wait_until(lambda: not manager.describe()))
        relay_manager.stop_relay.assert_called_once_with("ios-1")

    def test_last_client_disconnect_closes_upstream_and_relay(self):
        fake_socket = _FakeUpstreamSocket(chunks=[], eof=False)
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)

        with patch(
            "backend.device_stream.ios_mjpeg.socket.create_connection",
            return_value=fake_socket,
        ):
            client = manager.open_client(self._upstream())
            client.close()

        self.assertTrue(_wait_until(lambda: fake_socket.closed.is_set()))
        self.assertTrue(_wait_until(lambda: not manager.describe()))
        self.assertTrue(_wait_until(lambda: relay_manager.stop_relay.called))
        relay_manager.stop_relay.assert_called_with("ios-1")

    def test_direct_upstream_does_not_touch_relay_on_cleanup(self):
        fake_socket = _FakeUpstreamSocket(chunks=[], eof=True)
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)

        with patch(
            "backend.device_stream.ios_mjpeg.socket.create_connection",
            return_value=fake_socket,
        ):
            client = manager.open_client(self._upstream(via_relay=False))
            self.assertIsNone(client.next_frame(timeout=2.0))

        self.assertTrue(_wait_until(lambda: not manager.describe()))
        relay_manager.stop_relay.assert_not_called()

    def test_reopen_after_close_creates_fresh_upstream(self):
        first_socket = _FakeUpstreamSocket(chunks=[], eof=False)
        frames = [_jpeg(b"again" * 10)]
        second_socket = _FakeUpstreamSocket(chunks=[_wda_multipart(frames)], eof=False)
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)

        with patch(
            "backend.device_stream.ios_mjpeg.socket.create_connection",
            side_effect=[first_socket, second_socket],
        ) as connect_mock:
            client = manager.open_client(self._upstream())
            client.close()
            self.assertTrue(_wait_until(lambda: not manager.describe()))

            reopened = manager.open_client(self._upstream())
            self.assertEqual(reopened.next_frame(timeout=2.0), frames[0])
            reopened.close()

        self.assertEqual(connect_mock.call_count, 2)
        self.assertTrue(_wait_until(lambda: second_socket.closed.is_set()))

    def test_quick_reconnect_within_grace_reuses_upstream(self):
        """宽限期内的断开-重连不回收上游/relay（毫秒级竞态防护）。"""
        fake_socket = _FakeUpstreamSocket(chunks=[], eof=False)
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)

        with patch.object(ios_mjpeg, "IDLE_SHUTDOWN_GRACE_SEC", 0.5), patch(
            "backend.device_stream.ios_mjpeg.socket.create_connection",
            return_value=fake_socket,
        ) as connect_mock:
            client = manager.open_client(self._upstream())
            client.close()

            # 宽限期内新客户端接入：复用同一条上游连接
            reconnected = manager.open_client(self._upstream())
            connect_mock.assert_called_once()
            self.assertFalse(fake_socket.closed.is_set())
            relay_manager.stop_relay.assert_not_called()

            # 宽限期计时器已被取消：超过宽限期后流依旧存活
            time.sleep(0.7)
            self.assertFalse(fake_socket.closed.is_set())
            self.assertEqual(len(manager.describe()), 1)

            reconnected.close()

        self.assertTrue(_wait_until(lambda: fake_socket.closed.is_set()))
        self.assertTrue(_wait_until(lambda: relay_manager.stop_relay.called))

    def test_stopped_stream_skips_relay_recycle_when_replaced(self):
        """旧流停止回调不回收已被新流接管的 relay。"""
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)
        old_stream = ios_mjpeg._DeviceMjpegStream(
            self._upstream(), on_stopped=manager._handle_stream_stopped
        )
        new_stream = ios_mjpeg._DeviceMjpegStream(
            self._upstream(), on_stopped=manager._handle_stream_stopped
        )
        with manager._lock:
            manager._streams["ios-1"] = new_stream

        old_stream._finish("upstream lost")

        relay_manager.stop_relay.assert_not_called()
        self.assertIs(manager._streams.get("ios-1"), new_stream)

        # 新流正常结束时才回收 relay
        new_stream._finish(None)
        relay_manager.stop_relay.assert_called_once_with("ios-1")

    def test_connect_failure_raises_p1005_and_cleans_up(self):
        relay_manager = Mock()
        manager = IOSMjpegStreamManager(relay_manager=relay_manager)

        with patch(
            "backend.device_stream.ios_mjpeg.socket.create_connection",
            side_effect=ConnectionRefusedError("connection refused"),
        ):
            with self.assertRaises(IOSMjpegStreamError) as ctx:
                manager.open_client(self._upstream())

        self.assertIn("P1005_WDA_UNAVAILABLE", str(ctx.exception))
        self.assertTrue(_wait_until(lambda: not manager.describe()))
        relay_manager.stop_relay.assert_called_with("ios-1")


class _FakeEndpointClient:
    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    def next_frame(self, timeout=None):
        if self._frames:
            return self._frames.pop(0)
        return None

    def frames(self):
        while True:
            frame = self.next_frame()
            if frame is None:
                return
            yield frame

    def close(self):
        self.closed = True


class MjpegEndpointTests(unittest.TestCase):
    def setUp(self):
        from backend.device_stream.router import rest_router, ws_router

        app = FastAPI()
        app.include_router(rest_router, prefix="/api/stream")
        app.include_router(ws_router)
        self.client = TestClient(app)

    def test_websocket_streams_jpeg_frames_then_closes(self):
        frames = [_jpeg(b"ws-1" * 10), _jpeg(b"ws-2" * 10)]
        fake_client = _FakeEndpointClient(frames)

        with patch(
            "backend.device_stream.router._open_ios_mjpeg_client",
            return_value=fake_client,
        ):
            with self.client.websocket_connect("/ws/ios-mjpeg/ios-1") as websocket:
                self.assertEqual(websocket.receive_bytes(), frames[0])
                self.assertEqual(websocket.receive_bytes(), frames[1])
                message = websocket.receive()

        self.assertEqual(message["type"], "websocket.close")
        self.assertEqual(message["code"], 1000)
        self.assertTrue(fake_client.closed)

    def test_websocket_closes_with_4005_when_wda_unavailable(self):
        with patch(
            "backend.device_stream.router._open_ios_mjpeg_client",
            side_effect=IOSMjpegStreamError("failed to connect MJPEG upstream 127.0.0.1:9300"),
        ):
            with self.client.websocket_connect("/ws/ios-mjpeg/ios-1") as websocket:
                message = websocket.receive()

        self.assertEqual(message["type"], "websocket.close")
        self.assertEqual(message["code"], 4005)
        self.assertIn("P1005_WDA_UNAVAILABLE", message["reason"])

    def test_http_stream_returns_multipart_frames(self):
        frames = [_jpeg(b"http-1" * 10), _jpeg(b"http-2" * 10)]
        fake_client = _FakeEndpointClient(frames)

        with patch(
            "backend.device_stream.router._open_ios_mjpeg_client",
            return_value=fake_client,
        ):
            response = self.client.get("/api/stream/ios-mjpeg/ios-1")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("multipart/x-mixed-replace")
        )
        self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate")
        for frame in frames:
            self.assertIn(frame, response.content)
        self.assertIn(b"--autodroid-mjpeg\r\nContent-Type: image/jpeg\r\n", response.content)
        self.assertTrue(fake_client.closed)

    def test_http_stream_returns_503_when_wda_unavailable(self):
        with patch(
            "backend.device_stream.router._open_ios_mjpeg_client",
            side_effect=IOSMjpegStreamError("failed to establish MJPEG relay for device ios-1"),
        ):
            response = self.client.get("/api/stream/ios-mjpeg/ios-1")

        self.assertEqual(response.status_code, 503)
        self.assertIn("P1005_WDA_UNAVAILABLE", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
