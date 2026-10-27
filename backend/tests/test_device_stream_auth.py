"""设备流端点鉴权收口（R6'）测试。

覆盖：POST /stream/devices/{serial}/touch|reconnect、stream-profile（GET/POST）、
WS /ws/scrcpy/{serial}、WS /ws/ios-mjpeg/{serial}、GET /ios-mjpeg/{serial}。

口径与 GET /stream/devices 一致：get_current_user + 归属过滤（admin 全量，
普通用户仅本人登记设备）；WS 在 accept 前校验（拒绝时握手不建立）。
GET /ios-mjpeg 供 <img src> 直挂，兼容 ?token= 查询参数（浏览器无法带
Authorization 头，同下载类接口口径）。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from backend.core.security import ALGORITHM, SECRET_KEY
from backend.database import get_session
from backend.device_stream import router as stream_router
from backend.device_stream.ios_mjpeg import IOSMjpegStreamError
from backend.models import Device, User


def _png_jpeg(payload: bytes) -> bytes:
    return b"\xff\xd8\xff\xe0" + payload + b"\xff\xd9"


class _FakeMjpegClient:
    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    def next_frame(self, timeout=None):
        return self._frames.pop(0) if self._frames else None

    def frames(self):
        while True:
            frame = self.next_frame()
            if frame is None:
                return
            yield frame

    def close(self):
        self.closed = True


class StreamEndpointAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.admin = self._add_user("admin", role="admin")
        self.alice = self._add_user("alice")
        self.bob = self._add_user("bob")
        self._add_device("mine-1", owner_id=self.alice.id)
        self._add_device("other-1", owner_id=self.bob.id)

        app = FastAPI()
        app.include_router(stream_router.rest_router, prefix="/api/stream")
        app.include_router(stream_router.ws_router)
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _add_user(self, username: str, role: str = "user") -> User:
        user = User(username=username, hashed_password="x", role=role, full_name=username)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _add_device(self, serial: str, **fields) -> Device:
        device = Device(serial=serial, platform="android", **fields)
        self.session.add(device)
        self.session.commit()
        self.session.refresh(device)
        return device

    @staticmethod
    def _token(username: str) -> dict:
        return {"Authorization": f"Bearer {jwt.encode({'sub': username}, SECRET_KEY, algorithm=ALGORITHM)}"}

    @staticmethod
    def _token_query(username: str) -> str:
        return jwt.encode({"sub": username}, SECRET_KEY, algorithm=ALGORITHM)

    # ---------- REST：touch / reconnect ----------

    def test_touch_requires_auth(self):
        with patch.object(stream_router.device_manager, "send_touch_event") as send:
            resp = self.client.post(
                "/api/stream/devices/mine-1/touch", json={"action": 0, "x": 1, "y": 2}
            )
        self.assertEqual(resp.status_code, 401)
        send.assert_not_called()

    def test_touch_enforces_ownership(self):
        with patch.object(stream_router.device_manager, "send_touch_event") as send:
            resp = self.client.post(
                "/api/stream/devices/other-1/touch",
                json={"action": 0, "x": 1, "y": 2},
                headers=self._token("alice"),
            )
        self.assertEqual(resp.status_code, 404)
        send.assert_not_called()

        with patch.object(stream_router.device_manager, "send_touch_event") as send:
            resp = self.client.post(
                "/api/stream/devices/mine-1/touch",
                json={"action": 0, "x": 1, "y": 2},
                headers=self._token("alice"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        send.assert_called_once()

        with patch.object(stream_router.device_manager, "send_touch_event") as send:
            resp = self.client.post(
                "/api/stream/devices/other-1/touch",
                json={"action": 0, "x": 1, "y": 2},
                headers=self._token("admin"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        send.assert_called_once()

    def test_reconnect_requires_auth_and_ownership(self):
        with patch.object(stream_router.device_manager, "reconnect_device") as reconnect:
            resp = self.client.post("/api/stream/devices/mine-1/reconnect")
        self.assertEqual(resp.status_code, 401)
        reconnect.assert_not_called()

        with patch.object(stream_router.device_manager, "reconnect_device") as reconnect:
            resp = self.client.post(
                "/api/stream/devices/other-1/reconnect", headers=self._token("alice")
            )
        self.assertEqual(resp.status_code, 404)
        reconnect.assert_not_called()

        with patch.object(stream_router.device_manager, "reconnect_device") as reconnect:
            resp = self.client.post(
                "/api/stream/devices/mine-1/reconnect", headers=self._token("alice")
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        reconnect.assert_called_once_with("mine-1")

    # ---------- REST：stream-profile ----------

    def test_stream_profile_requires_auth_and_ownership(self):
        with patch.object(stream_router.device_manager, "reconnect_device") as reconnect:
            resp = self.client.get("/api/stream/devices/mine-1/stream-profile")
            self.assertEqual(resp.status_code, 401)
            resp = self.client.post(
                "/api/stream/devices/mine-1/stream-profile", json={"profile": "hd"}
            )
        self.assertEqual(resp.status_code, 401)
        reconnect.assert_not_called()

        resp = self.client.get(
            "/api/stream/devices/other-1/stream-profile", headers=self._token("alice")
        )
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(
            "/api/stream/devices/other-1/stream-profile",
            json={"profile": "hd"},
            headers=self._token("alice"),
        )
        self.assertEqual(resp.status_code, 404)

        resp = self.client.get(
            "/api/stream/devices/mine-1/stream-profile", headers=self._token("alice")
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["serial"], "mine-1")

        with patch.object(stream_router.device_manager, "get_device", return_value=None):
            resp = self.client.post(
                "/api/stream/devices/mine-1/stream-profile",
                json={"profile": "hd"},
                headers=self._token("alice"),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["profile"], "hd")
        self.addCleanup(stream_router.set_stream_profile_override, "mine-1", None)

    # ---------- REST：iOS MJPEG（img src，兼容 ?token=） ----------

    def test_ios_mjpeg_http_requires_auth(self):
        with patch.object(stream_router, "_open_ios_mjpeg_client") as open_client:
            resp = self.client.get("/api/stream/ios-mjpeg/mine-1")
        self.assertEqual(resp.status_code, 401)
        open_client.assert_not_called()

    def test_ios_mjpeg_http_accepts_query_token(self):
        frames = [_png_jpeg(b"f1")]
        fake = _FakeMjpegClient(frames)
        with patch.object(stream_router, "_open_ios_mjpeg_client", return_value=fake):
            resp = self.client.get(
                f"/api/stream/ios-mjpeg/mine-1?token={self._token_query('alice')}"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.headers["content-type"].startswith("multipart/x-mixed-replace"))
        self.assertIn(frames[0], resp.content)

    def test_ios_mjpeg_http_enforces_ownership(self):
        with patch.object(stream_router, "_open_ios_mjpeg_client") as open_client:
            resp = self.client.get(
                f"/api/stream/ios-mjpeg/other-1?token={self._token_query('alice')}"
            )
        self.assertEqual(resp.status_code, 404)
        open_client.assert_not_called()

    def test_ios_mjpeg_http_error_paths_keep_auth(self):
        with patch.object(
            stream_router,
            "_open_ios_mjpeg_client",
            side_effect=IOSMjpegStreamError("failed to connect MJPEG upstream"),
        ):
            resp = self.client.get(
                f"/api/stream/ios-mjpeg/mine-1?token={self._token_query('alice')}"
            )
        self.assertEqual(resp.status_code, 503)
        self.assertIn("P1005_WDA_UNAVAILABLE", resp.json()["detail"])

    # ---------- WS：accept 前校验 ----------

    def test_scrcpy_ws_rejects_missing_token_before_accept(self):
        # 桩掉推流：鉴权未拦截时立刻失败（不连真机）
        with patch.object(
            stream_router.device_manager, "get_video_generator", return_value=iter(())
        ):
            with self.assertRaises(Exception) as ctx:
                with self.client.websocket_connect("/ws/scrcpy/mine-1"):
                    pass
        self.assertEqual(getattr(ctx.exception, "code", None), 4401)

    def test_scrcpy_ws_rejects_foreign_device(self):
        with patch.object(
            stream_router.device_manager, "get_video_generator", return_value=iter(())
        ):
            with self.assertRaises(Exception) as ctx:
                with self.client.websocket_connect(
                    f"/ws/scrcpy/other-1?token={self._token_query('alice')}"
                ):
                    pass
        self.assertEqual(getattr(ctx.exception, "code", None), 4403)

    def test_scrcpy_ws_accepts_owner_and_streams(self):
        chunks = [b"\x00\x00\x00\x01\x67", None]

        def _generator(serial):
            for chunk in chunks:
                if chunk is None:
                    return
                yield chunk

        with patch.object(
            stream_router.device_manager, "get_video_generator", side_effect=_generator
        ):
            # 服务端推完流后直接返回（不主动 close），由客户端断开——与浏览器行为一致
            with self.client.websocket_connect(
                f"/ws/scrcpy/mine-1?token={self._token_query('alice')}"
            ) as websocket:
                self.assertEqual(websocket.receive_bytes(), b"\x00\x00\x00\x01\x67")

    def test_ios_mjpeg_ws_rejects_missing_token_before_accept(self):
        with patch.object(
            stream_router, "_open_ios_mjpeg_client", return_value=_FakeMjpegClient([])
        ):
            with self.assertRaises(Exception) as ctx:
                with self.client.websocket_connect("/ws/ios-mjpeg/mine-1"):
                    pass
        self.assertEqual(getattr(ctx.exception, "code", None), 4401)

    def test_ios_mjpeg_ws_accepts_owner_with_header_token(self):
        frames = [_png_jpeg(b"ws-1")]
        fake = _FakeMjpegClient(frames)
        with patch.object(stream_router, "_open_ios_mjpeg_client", return_value=fake):
            with self.client.websocket_connect(
                "/ws/ios-mjpeg/mine-1", headers=self._token("alice")
            ) as websocket:
                self.assertEqual(websocket.receive_bytes(), frames[0])
                message = websocket.receive()
        self.assertEqual(message["type"], "websocket.close")
        self.assertTrue(fake.closed)

    def test_ios_mjpeg_ws_accepts_owner_with_query_token(self):
        # R6' 鉴权收口后浏览器 WS 只能走 ?token=（与 /ws/scrcpy、GET /ios-mjpeg 同通道）
        frames = [_png_jpeg(b"ws-query")]
        fake = _FakeMjpegClient(frames)
        with patch.object(stream_router, "_open_ios_mjpeg_client", return_value=fake):
            with self.client.websocket_connect(
                f"/ws/ios-mjpeg/mine-1?token={self._token_query('alice')}"
            ) as websocket:
                self.assertEqual(websocket.receive_bytes(), frames[0])
                message = websocket.receive()
        self.assertEqual(message["type"], "websocket.close")
        self.assertTrue(fake.closed)

    def test_admin_can_stream_any_registered_device(self):
        with patch.object(stream_router.device_manager, "reconnect_device") as reconnect:
            resp = self.client.post(
                "/api/stream/devices/other-1/reconnect", headers=self._token("admin")
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        reconnect.assert_called_once()

        def _generator(serial):
            return iter(())

        with patch.object(
            stream_router.device_manager, "get_video_generator", side_effect=_generator
        ):
            # 握手成功即证明 admin 越过归属过滤（被拒会在 connect 处抛 WebSocketDisconnect）
            with self.client.websocket_connect(
                f"/ws/scrcpy/other-1?token={self._token_query('admin')}"
            ):
                pass


class StreamWsAuthHelperTests(unittest.TestCase):
    """WS 鉴权辅助：token 提取（Authorization 头 / ?token=）与失败返回 None。"""

    def test_extract_ws_token_prefers_bearer_header(self):
        ws = SimpleNamespace(
            headers={"authorization": "Bearer abc.def"},
            query_params={"token": "from-query"},
        )
        self.assertEqual(stream_router._extract_ws_token(ws), "abc.def")

    def test_extract_ws_token_falls_back_to_query(self):
        ws = SimpleNamespace(headers={}, query_params={"token": "from-query"})
        self.assertEqual(stream_router._extract_ws_token(ws), "from-query")

    def test_authenticate_stream_user_returns_none_without_token(self):
        ws = SimpleNamespace(headers={}, query_params={})
        self.assertIsNone(stream_router._authenticate_stream_user(ws, session=None))
