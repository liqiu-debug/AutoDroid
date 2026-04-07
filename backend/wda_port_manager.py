"""
iOS WDA 端口转发管理器。

基于 `tidevice relay` 维护 UDID -> localhost 端口映射，
用于多 iOS 设备并发执行时的本地 WDA 访问。
"""
from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

WDA_REMOTE_PORT = 8100
WDA_PORT_RANGE_START = 8200
WDA_PORT_RANGE_END = 8299


@dataclass
class RelayEntry:
    udid: str
    local_port: int
    remote_port: int
    process: subprocess.Popen
    started_at: float


class WDARelayManager:
    def __init__(
        self,
        start_port: int = WDA_PORT_RANGE_START,
        end_port: int = WDA_PORT_RANGE_END,
        remote_port: int = WDA_REMOTE_PORT,
    ) -> None:
        self._start_port = start_port
        self._end_port = end_port
        self._remote_port = remote_port
        self._entries: Dict[str, RelayEntry] = {}
        self._lock = threading.Lock()

    def ensure_relay(self, udid: str, preferred_port: Optional[int] = None) -> int:
        """
        确保指定设备存在可用 relay，返回本地端口。
        """
        device_id = str(udid or "").strip()
        if not device_id:
            raise RuntimeError("invalid udid")

        with self._lock:
            self._cleanup_dead_locked()

            existing = self._entries.get(device_id)
            if existing and existing.process.poll() is None:
                return existing.local_port

            chosen_port = self._select_port_locked(device_id, preferred_port)
            process = self._spawn_relay(device_id, chosen_port, self._remote_port)

            entry = RelayEntry(
                udid=device_id,
                local_port=chosen_port,
                remote_port=self._remote_port,
                process=process,
                started_at=time.time(),
            )
            self._entries[device_id] = entry
            return chosen_port

    def stop_relay(self, udid: str) -> None:
        device_id = str(udid or "").strip()
        if not device_id:
            return
        with self._lock:
            entry = self._entries.pop(device_id, None)
            if entry:
                self._terminate_process(entry.process)

    def stop_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            self._terminate_process(entry.process)

    def list_relays(self) -> List[dict]:
        with self._lock:
            self._cleanup_dead_locked()
            result = []
            for entry in self._entries.values():
                result.append(
                    {
                        "udid": entry.udid,
                        "local_port": entry.local_port,
                        "remote_port": entry.remote_port,
                        "alive": entry.process.poll() is None,
                        "started_at": datetime.fromtimestamp(entry.started_at).isoformat(),
                    }
                )
            return result

    def _select_port_locked(self, udid: str, preferred_port: Optional[int]) -> int:
        # 优先使用指定端口（若可用）
        if preferred_port is not None:
            try:
                preferred = int(preferred_port)
                if self._is_candidate_port(preferred) and self._is_port_available_locked(preferred):
                    return preferred
            except Exception:
                pass

        # 若已有历史映射端口，优先复用
        previous = self._entries.get(udid)
        if previous and self._is_port_available_locked(previous.local_port):
            return previous.local_port

        for port in range(self._start_port, self._end_port + 1):
            if self._is_port_available_locked(port):
                return port

        raise RuntimeError(
            f"no free WDA relay port in range {self._start_port}-{self._end_port}"
        )

    def _is_candidate_port(self, port: int) -> bool:
        return self._start_port <= port <= self._end_port

    def _is_port_available_locked(self, port: int) -> bool:
        for entry in self._entries.values():
            if entry.local_port == port and entry.process.poll() is None:
                return False
        return self._is_port_free(port)

    @staticmethod
    def _is_port_free(port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _spawn_relay(self, udid: str, local_port: int, remote_port: int) -> subprocess.Popen:
        cmd = ["tidevice", "-u", udid, "relay", str(local_port), str(remote_port)]
        logger.info("start wda relay: %s", " ".join(cmd))
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("tidevice command not found") from exc
        except Exception as exc:
            raise RuntimeError(f"failed to start tidevice relay: {exc}") from exc

        # 给 relay 一点启动时间，快速探测是否立即退出。
        time.sleep(0.2)
        if process.poll() is not None:
            raise RuntimeError(
                f"tidevice relay exited early (udid={udid}, local_port={local_port})"
            )
        return process

    def _cleanup_dead_locked(self) -> None:
        dead = [udid for udid, entry in self._entries.items() if entry.process.poll() is not None]
        for udid in dead:
            self._entries.pop(udid, None)

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


wda_relay_manager = WDARelayManager()
