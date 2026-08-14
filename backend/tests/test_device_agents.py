"""远程设备接入（device_agents）测试：协议、隧道中继、keeper 状态机、迁移与同步集成。"""
import asyncio
import importlib.util
import json
import socket
import sqlite3
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.api import device_agents as device_agents_api
from backend.core.api_tokens import generate_api_token, hash_api_token
from backend.core.security import get_password_hash
from backend.database import get_session
from backend.device_agents.adb_keeper import AdbKeeper, parse_adb_devices_tunnel_states
from backend.device_agents.hub import AgentSession, tunnel_hub
from backend.device_agents.protocol import (
    AGENT_PROTOCOL_VERSION,
    TUNNEL_PORT_RANGE_END,
    TUNNEL_PORT_RANGE_START,
    decode_data_frame,
    encode_data_frame,
    parse_tunnel_serial,
    tunnel_serial,
)
from backend.models import ApiToken, RemoteAgent, RemoteAgentDevice, User

# 包 __init__ 将 adb_keeper 单例导出为同名属性，遮蔽了子模块；
# 通过 sys.modules 取模块本体用于 patch 模块级函数。
adb_keeper_module = sys.modules["backend.device_agents.adb_keeper"]

AGENT_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "device_agent.py"


def _load_agent_module():
    spec = importlib.util.spec_from_file_location("autodroid_device_agent", AGENT_SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProtocolTests(unittest.TestCase):
    def test_data_frame_roundtrip(self):
        frame = encode_data_frame(42, b"hello")
        self.assertEqual(frame[:4], struct.pack(">I", 42))
        conn_id, payload = decode_data_frame(frame)
        self.assertEqual(conn_id, 42)
        self.assertEqual(payload, b"hello")

    def test_data_frame_empty_payload(self):
        conn_id, payload = decode_data_frame(encode_data_frame(7, b""))
        self.assertEqual((conn_id, payload), (7, b""))

    def test_data_frame_too_short(self):
        with self.assertRaises(ValueError):
            decode_data_frame(b"\x00\x01")

    def test_parse_tunnel_serial(self):
        self.assertEqual(parse_tunnel_serial(tunnel_serial(TUNNEL_PORT_RANGE_START)), TUNNEL_PORT_RANGE_START)
        self.assertEqual(parse_tunnel_serial(f"127.0.0.1:{TUNNEL_PORT_RANGE_END}"), TUNNEL_PORT_RANGE_END)
        # 范围外 / 非隧道形态
        self.assertIsNone(parse_tunnel_serial(f"127.0.0.1:{TUNNEL_PORT_RANGE_END + 1}"))
        self.assertIsNone(parse_tunnel_serial("127.0.0.1:5555"))
        self.assertIsNone(parse_tunnel_serial("192.168.1.5:28100"))
        self.assertIsNone(parse_tunnel_serial("ABC123"))
        self.assertIsNone(parse_tunnel_serial(""))
        self.assertIsNone(parse_tunnel_serial("127.0.0.1:notaport"))


class AdbKeeperTests(unittest.TestCase):
    def test_parse_adb_devices_tunnel_states(self):
        output = (
            "List of devices attached\n"
            f"127.0.0.1:{TUNNEL_PORT_RANGE_START}\tdevice\n"
            f"127.0.0.1:{TUNNEL_PORT_RANGE_START + 1}\toffline\n"
            "127.0.0.1:5555\tdevice\n"
            "USBSERIAL\tdevice\n"
            "emulator-5554\tdevice\n"
        )
        states = parse_adb_devices_tunnel_states(output)
        self.assertEqual(
            states,
            {
                TUNNEL_PORT_RANGE_START: "device",
                TUNNEL_PORT_RANGE_START + 1: "offline",
            },
        )

    def _run_sync(self, desired, devices_output):
        calls = []

        async def fake_run_adb(*args, timeout=10):
            calls.append(args)
            if args == ("devices",):
                return devices_output
            return "connected" if args and args[0] == "connect" else ""

        keeper = AdbKeeper()
        keeper.configure(lambda: desired)
        with patch.object(adb_keeper_module, "_run_adb_command", fake_run_adb):
            asyncio.run(keeper._sync_once())
        return calls

    def test_sync_connects_missing_desired_port(self):
        port = TUNNEL_PORT_RANGE_START
        calls = self._run_sync({port}, "List of devices attached\n")
        self.assertIn(("connect", tunnel_serial(port)), calls)

    def test_sync_repairs_offline_desired_port(self):
        port = TUNNEL_PORT_RANGE_START
        output = f"List of devices attached\n{tunnel_serial(port)}\toffline\n"
        calls = self._run_sync({port}, output)
        self.assertIn(("disconnect", tunnel_serial(port)), calls)
        self.assertIn(("connect", tunnel_serial(port)), calls)
        # disconnect 应先于 connect
        self.assertLess(
            calls.index(("disconnect", tunnel_serial(port))),
            calls.index(("connect", tunnel_serial(port))),
        )

    def test_sync_disconnects_undesired_port(self):
        port = TUNNEL_PORT_RANGE_START + 5
        output = f"List of devices attached\n{tunnel_serial(port)}\tdevice\n"
        calls = self._run_sync(set(), output)
        self.assertIn(("disconnect", tunnel_serial(port)), calls)
        self.assertNotIn(("connect", tunnel_serial(port)), calls)

    def test_sync_leaves_unauthorized_alone(self):
        port = TUNNEL_PORT_RANGE_START
        output = f"List of devices attached\n{tunnel_serial(port)}\tunauthorized\n"
        calls = self._run_sync({port}, output)
        self.assertNotIn(("disconnect", tunnel_serial(port)), calls)
        self.assertNotIn(("connect", tunnel_serial(port)), calls)

    def test_sync_skips_healthy_desired_port(self):
        port = TUNNEL_PORT_RANGE_START
        output = f"List of devices attached\n{tunnel_serial(port)}\tdevice\n"
        calls = self._run_sync({port}, output)
        self.assertEqual([c for c in calls if c[0] in ("connect", "disconnect")], [])


class TunnelHubSessionTests(unittest.TestCase):
    """经真实 WebSocket + 真实 TCP 端口验证注册、端口固定与双向中继。"""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            user = User(username="agent-user", hashed_password=get_password_hash("pw"))
            session.add(user)
            session.commit()
            session.refresh(user)
            self.token_plain = generate_api_token()
            session.add(
                ApiToken(
                    name="agent-token",
                    token_hash=hash_api_token(self.token_plain),
                    token_prefix=self.token_plain[:12],
                    user_id=user.id,
                )
            )
            session.commit()

        self._patches = [
            patch("backend.device_agents.hub.engine", self.engine),
            patch("backend.api.device_agents.engine", self.engine),
        ]
        for p in self._patches:
            p.start()

        app = FastAPI()
        app.include_router(device_agents_api.ws_router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token_plain}"}

    def _register_message(self, name="工位B", devices=None):
        return {
            "type": "register",
            "protocol": AGENT_PROTOCOL_VERSION,
            "agent": {"name": name, "version": "1.0.0", "os": "TestOS"},
            "devices": devices if devices is not None else [
                {"usb_serial": "USB123", "model": "Pixel 8", "brand": "GOOGLE", "os_version": "15"}
            ],
        }

    def _wait_until(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_reject_without_token(self):
        with self.client.websocket_connect("/ws/device-agent") as ws:
            message = ws.receive_json()
            self.assertEqual(message["type"], "error")
            self.assertIn("鉴权失败", message["message"])

    def test_reject_wrong_protocol_version(self):
        with self.client.websocket_connect("/ws/device-agent", headers=self._auth_headers()) as ws:
            payload = self._register_message()
            payload["protocol"] = 999
            ws.send_text(json.dumps(payload))
            message = ws.receive_json()
            self.assertEqual(message["type"], "error")
            self.assertIn("协议版本", message["message"])

    def test_register_assigns_stable_port_and_marks_online(self):
        with self.client.websocket_connect("/ws/device-agent", headers=self._auth_headers()) as ws:
            ws.send_text(json.dumps(self._register_message()))
            registered = ws.receive_json()
            self.assertEqual(registered["type"], "registered")
            self.assertEqual(len(registered["devices"]), 1)
            entry = registered["devices"][0]
            self.assertEqual(entry["usb_serial"], "USB123")
            first_port = entry["tunnel_port"]
            self.assertGreaterEqual(first_port, TUNNEL_PORT_RANGE_START)
            self.assertLessEqual(first_port, TUNNEL_PORT_RANGE_END)
            self.assertEqual(entry["serial"], tunnel_serial(first_port))

            with Session(self.engine) as session:
                agent = session.exec(select(RemoteAgent)).one()
                self.assertEqual(agent.name, "工位B")
                self.assertEqual(agent.status, "ONLINE")
                mapping = session.exec(select(RemoteAgentDevice)).one()
                self.assertEqual(mapping.usb_serial, "USB123")
                self.assertEqual(mapping.tunnel_port, first_port)

            self.assertEqual(tunnel_hub.desired_ports(), {first_port})
            meta = tunnel_hub.get_remote_device_meta(tunnel_serial(first_port))
            self.assertEqual(meta["agent_name"], "工位B")
            self.assertEqual(meta["usb_serial"], "USB123")

        # 断开后：接入点 OFFLINE、监听移除
        self.assertTrue(
            self._wait_until(lambda: not tunnel_hub.is_agent_online("工位B"))
        )
        self.assertTrue(self._wait_until(lambda: tunnel_hub.desired_ports() == set()))
        with Session(self.engine) as session:
            agent = session.exec(select(RemoteAgent)).one()
            self.assertEqual(agent.status, "OFFLINE")

        # 重连同名接入点 + 同 USB 序列号 → 端口保持不变
        with self.client.websocket_connect("/ws/device-agent", headers=self._auth_headers()) as ws:
            ws.send_text(json.dumps(self._register_message()))
            registered = ws.receive_json()
            self.assertEqual(registered["devices"][0]["tunnel_port"], first_port)

    def test_tunnel_relays_tcp_data_both_directions(self):
        with self.client.websocket_connect("/ws/device-agent", headers=self._auth_headers()) as ws:
            ws.send_text(json.dumps(self._register_message()))
            registered = ws.receive_json()
            port = registered["devices"][0]["tunnel_port"]

            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            try:
                opened = ws.receive_json()
                self.assertEqual(opened["type"], "open")
                self.assertEqual(opened["usb_serial"], "USB123")
                conn_id = opened["conn_id"]
                ws.send_text(json.dumps({"type": "open_result", "conn_id": conn_id, "ok": True}))

                # 平台侧 TCP → Agent（binary 帧）
                sock.sendall(b"CNXNfake")
                frame = ws.receive_bytes()
                got_id, payload = decode_data_frame(frame)
                self.assertEqual(got_id, conn_id)
                self.assertEqual(payload, b"CNXNfake")

                # Agent → 平台侧 TCP
                ws.send_bytes(encode_data_frame(conn_id, b"AUTHreply"))
                sock.settimeout(3)
                self.assertEqual(sock.recv(64), b"AUTHreply")

                # Agent 主动关闭连接 → 平台侧 TCP 收到 EOF
                ws.send_text(json.dumps({"type": "close", "conn_id": conn_id}))
                self.assertEqual(sock.recv(64), b"")
            finally:
                sock.close()


    def test_device_removal_closes_listener(self):
        with self.client.websocket_connect("/ws/device-agent", headers=self._auth_headers()) as ws:
            ws.send_text(json.dumps(self._register_message()))
            registered = ws.receive_json()
            port = registered["devices"][0]["tunnel_port"]
            self.assertEqual(tunnel_hub.desired_ports(), {port})

            ws.send_text(json.dumps({"type": "devices", "devices": []}))
            updated = ws.receive_json()
            self.assertEqual(updated["type"], "registered")
            self.assertEqual(updated["devices"], [])
            self.assertTrue(self._wait_until(lambda: tunnel_hub.desired_ports() == set()))
            # 端口映射保留（身份稳定），仅监听关闭
            with Session(self.engine) as session:
                mapping = session.exec(select(RemoteAgentDevice)).one()
                self.assertEqual(mapping.tunnel_port, port)


class AgentSessionSendSchedulingTests(unittest.IsolatedAsyncioTestCase):
    class _FakeWebSocket:
        def __init__(self):
            self.events = []
            self.first_binary_started = asyncio.Event()
            self.release_first_binary = asyncio.Event()
            self._first_binary = True

        async def send_text(self, payload):
            self.events.append(("text", payload))

        async def send_bytes(self, payload):
            if self._first_binary:
                self._first_binary = False
                self.first_binary_started.set()
                await self.release_first_binary.wait()
            self.events.append(("binary", payload))

        async def close(self):
            return None

    async def test_control_message_preempts_remaining_video_chunks(self):
        websocket = self._FakeWebSocket()
        session = AgentSession(None, websocket, "fairness-test", 1)
        try:
            await session.send_data(1, b"A" * (16 * 1024))
            await websocket.first_binary_started.wait()
            control = asyncio.create_task(session.send_control({"type": "pong"}))
            websocket.release_first_binary.set()
            await control
            await asyncio.sleep(0)

            self.assertEqual(websocket.events[0][0], "binary")
            self.assertEqual(websocket.events[1], ("text", '{"type": "pong"}'))
            self.assertEqual(len(websocket.events[0][1]), 4 + 8 * 1024)
        finally:
            await session.shutdown_sender()


class DeviceAgentRestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            user = User(username="admin-user", hashed_password=get_password_hash("pw"), role="admin")
            session.add(user)
            session.commit()
            session.refresh(user)
            self.user_id = user.id
            agent = RemoteAgent(name="旧工位", status="OFFLINE")
            session.add(agent)
            session.commit()
            session.refresh(agent)
            self.agent_id = agent.id
            session.add(
                RemoteAgentDevice(
                    agent_id=agent.id,
                    usb_serial="USBOLD",
                    tunnel_port=TUNNEL_PORT_RANGE_START + 9,
                )
            )
            session.commit()

        self._patches = [patch("backend.device_agents.hub.engine", self.engine)]
        for p in self._patches:
            p.start()

        app = FastAPI()
        app.include_router(device_agents_api.router, prefix="/api/device-agents")

        def override_session():
            with Session(self.engine) as session:
                yield session

        # 瞬态 User，避免脱离 session 的 ORM 实例触发懒加载
        def override_user():
            return User(id=self.user_id, username="admin-user", hashed_password="x", role="admin")

        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[device_agents_api.get_current_user] = override_user
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def test_list_agents(self):
        resp = self.client.get("/api/device-agents/")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "旧工位")
        self.assertEqual(items[0]["status"], "OFFLINE")
        self.assertEqual(items[0]["device_count"], 1)
        self.assertEqual(items[0]["devices"][0]["serial"], tunnel_serial(TUNNEL_PORT_RANGE_START + 9))

    def test_delete_offline_agent_releases_mapping(self):
        resp = self.client.delete(f"/api/device-agents/{self.agent_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["released_ports"], [TUNNEL_PORT_RANGE_START + 9])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(RemoteAgent)).all(), [])
            self.assertEqual(session.exec(select(RemoteAgentDevice)).all(), [])

    def test_delete_online_agent_rejected(self):
        with patch.object(tunnel_hub, "is_agent_online", return_value=True):
            resp = self.client.delete(f"/api/device-agents/{self.agent_id}")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("在线", resp.json()["detail"])

    def test_agent_script_download(self):
        resp = self.client.get("/api/device-agents/agent-script")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("device_agent.py", resp.headers.get("content-disposition", ""))
        self.assertIn(b"AutoDroid", resp.content[:2000])


class RemoteDeviceMigrationTests(unittest.TestCase):
    def test_device_columns_added_idempotently(self):
        from backend.database import _migrate_remote_device_agents

        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE device (id INTEGER PRIMARY KEY, serial VARCHAR)")
        _migrate_remote_device_agents(cursor)
        _migrate_remote_device_agents(cursor)  # 幂等
        cursor.execute("PRAGMA table_info(device)")
        cols = {row[1] for row in cursor.fetchall()}
        self.assertIn("agent_name", cols)
        self.assertIn("source_serial", cols)
        conn.close()


class SyncIntegrationTests(unittest.TestCase):
    def test_remote_usb_fields_for_tunnel_serial(self):
        from backend.api.devices import _remote_usb_fields

        with patch.object(
            tunnel_hub,
            "get_remote_device_meta",
            return_value={"agent_name": "工位B", "usb_serial": "USB123", "online": True},
        ):
            fields = _remote_usb_fields(tunnel_serial(TUNNEL_PORT_RANGE_START))
        self.assertEqual(fields["connection_type"], "remote_usb")
        self.assertEqual(fields["agent_name"], "工位B")
        self.assertEqual(fields["source_serial"], "USB123")

    def test_remote_usb_fields_for_normal_serial(self):
        from backend.api.devices import _remote_usb_fields

        with patch.object(tunnel_hub, "get_remote_device_meta", return_value=None):
            fields = _remote_usb_fields("USBSERIAL")
        self.assertEqual(
            fields,
            {"connection_type": None, "agent_name": None, "source_serial": None},
        )


class AgentScriptLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = _load_agent_module()

    def test_parse_adb_devices_output(self):
        output = (
            "List of devices attached\n"
            "* daemon started successfully\n"
            "ABC123     device usb:1-1 product:x model:Pixel_8 device:y transport_id:1\n"
            "DEF456     unauthorized usb:1-2 transport_id:2\n"
            "192.168.1.9:5555 device product:x model:Mi_10 transport_id:3\n"
            "\n"
        )
        devices = self.agent.parse_adb_devices_output(output)
        self.assertEqual(len(devices), 3)
        self.assertEqual(devices[0], {"serial": "ABC123", "state": "device", "model": "Pixel 8"})
        self.assertEqual(devices[1]["state"], "unauthorized")

    def test_is_network_serial(self):
        self.assertTrue(self.agent.is_network_serial("192.168.1.9:5555"))
        self.assertTrue(self.agent.is_network_serial("127.0.0.1:28100"))
        self.assertFalse(self.agent.is_network_serial("ABC123"))
        self.assertFalse(self.agent.is_network_serial("emulator-5554"))

    def test_data_frame_roundtrip_matches_backend(self):
        frame = self.agent.build_data_frame(9, b"payload")
        self.assertEqual(decode_data_frame(frame), (9, b"payload"))
        conn_id, payload = self.agent.parse_data_frame(encode_data_frame(11, b"x"))
        self.assertEqual((conn_id, payload), (11, b"x"))

    def test_agent_sender_prioritizes_control_over_remaining_video_chunks(self):
        class FakeWebSocket:
            def __init__(self):
                self.events = []
                self.first_binary_started = threading.Event()
                self.release_first_binary = threading.Event()
                self.first_binary = True

            def send_text(self, payload):
                self.events.append(("text", payload))

            def send_binary(self, payload):
                if self.first_binary:
                    self.first_binary = False
                    self.first_binary_started.set()
                    self.release_first_binary.wait(timeout=1.0)
                self.events.append(("binary", payload))

        websocket = FakeWebSocket()
        sender = self.agent.TunnelSendScheduler(websocket)
        try:
            self.assertTrue(sender.send_binary(1, b"A" * (16 * 1024)))
            self.assertTrue(websocket.first_binary_started.wait(timeout=1.0))
            control_result = []
            control = threading.Thread(
                target=lambda: control_result.append(sender.send_text('{"type":"pong"}', wait=True)),
            )
            control.start()
            websocket.release_first_binary.set()
            control.join(timeout=1.0)

            self.assertFalse(control.is_alive())
            self.assertEqual(control_result, [True])
            self.assertEqual(websocket.events[0][0], "binary")
            self.assertEqual(websocket.events[1], ("text", '{"type":"pong"}'))
        finally:
            sender.stop()

    def test_mask_payload_symmetry(self):
        mask = b"\x01\x02\x03\x04"
        data = bytes(range(256)) * 3 + b"tail"
        masked = self.agent.mask_payload(mask, data)
        self.assertNotEqual(masked, data)
        self.assertEqual(self.agent.mask_payload(mask, masked), data)

    def test_build_ws_frame_lengths(self):
        # 短帧（<126）、中帧（16 位长度）、掩码位
        short = self.agent.build_ws_frame(0x2, b"a" * 10)
        self.assertEqual(short[0], 0x82)
        self.assertEqual(short[1] & 0x7F, 10)
        self.assertTrue(short[1] & 0x80)  # masked
        medium = self.agent.build_ws_frame(0x2, b"a" * 300)
        self.assertEqual(medium[1] & 0x7F, 126)
        self.assertEqual(struct.unpack(">H", medium[2:4])[0], 300)


if __name__ == "__main__":
    unittest.main()
