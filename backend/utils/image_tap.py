"""One submission only; bypass SDK recovery paths that resend side effects."""
from __future__ import annotations

import time
import http.client
import json
import socket
import threading
from urllib.parse import quote


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("tap_deadline")
    return value


def _adb_tunnel(adb_device, port, deadline):
    """Bound every ADB handshake operation; never start/restart the ADB server."""
    client = adb_device._client
    transport = socket.create_connection((client.host, client.port), timeout=_remaining(deadline))

    def read_exact(length):
        result = b""
        while len(result) < length:
            transport.settimeout(_remaining(deadline))
            chunk = transport.recv(length - len(result))
            if not chunk:
                raise RuntimeError("adb_connection_closed")
            result += chunk
        return result

    def command(value):
        encoded = value.encode("utf-8")
        transport.settimeout(_remaining(deadline))
        transport.sendall(f"{len(encoded):04x}".encode() + encoded)
        status = read_exact(4)
        if status == b"FAIL":
            length = int(read_exact(4), 16)
            raise RuntimeError("adb_transport_failed: " + read_exact(length).decode(errors="replace"))
        if status != b"OKAY":
            raise RuntimeError("adb_invalid_response")

    try:
        serial = adb_device.serial
        if not isinstance(serial, str) or not serial:
            raise RuntimeError("missing_android_serial")
        command("host:transport:" + serial)
        command("tcp:" + str(int(port)))
        transport.settimeout(_remaining(deadline))
        return transport
    except Exception:
        transport.close()
        raise


def android_tap_once(device, x, y, deadline):
    # Bypass SDK connection recovery and its automatic re-send of click RPC.
    transport = _adb_tunnel(device.adb_device, device._device_server_port, deadline)
    connection = None
    timer = None
    try:
        connection = http.client.HTTPConnection("localhost", timeout=_remaining(deadline))
        connection.sock = transport
        def expire():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(_remaining(deadline), expire)
        timer.daemon = True
        timer.start()
        transport.settimeout(_remaining(deadline))
        payload = {"jsonrpc": "2.0", "id": 1, "method": "click", "params": [int(x), int(y)]}
        connection.request("POST", "/jsonrpc/0", json.dumps(payload), {"Content-Type": "application/json", "Accept-Encoding": ""})
        transport.settimeout(_remaining(deadline))
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("tap_not_acknowledged")
        result = json.loads(response.read())
        if result.get("error") or result.get("result") is not True:
            raise RuntimeError("tap_not_acknowledged")
    finally:
        if timer is not None:
            timer.cancel()
        if connection is not None:
            connection.close()
        else:
            transport.close()


def ios_tap_once(wda_url, session_id, x, y, deadline):
    import requests

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("tap_deadline")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("missing_wda_session")
    # A new Session has max_retries=0, and has no facebook-wda callbacks or
    # Session.tap's broad exception fallback to /wda/tap/0.
    endpoint = wda_url.rstrip("/") + "/session/" + quote(session_id, safe="") + "/wda/tap"
    with requests.Session() as transport:
        response = transport.post(endpoint, json={"x": float(x), "y": float(y)}, timeout=remaining, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError("tap_not_acknowledged")
        payload = response.json()
        if payload.get("status", 0) != 0 or isinstance(payload.get("value"), dict) and payload["value"].get("error"):
            raise RuntimeError("tap_not_acknowledged")
