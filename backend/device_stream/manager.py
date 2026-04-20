"""
ScrcpyDeviceManager - USB 设备监听 & Scrcpy 视频流管理

核心功能：
- 守护线程监听 USB 设备插拔 (adbutils.track_devices)
- 设备接入时自动部署 scrcpy-server 并启动视频流服务
- 设备断开时自动清理进程和端口资源
"""
import os
import socket
import struct
import subprocess
import threading
import time
import logging
import queue
from typing import Dict, Optional, Generator, List, Set

import adbutils
from .recorder import ReplayCaptureResult, RollingScrcpyRecorderSession

# 配置日志
logger = logging.getLogger("ScrcpyManager")
logger.setLevel(logging.INFO)
# scrcpy-server jar 路径（相对于项目根目录）
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
SCRCPY_SERVER_PATH = os.path.join(PROJECT_ROOT, "assets", "scrcpy-server.jar")
DEVICE_JAR_PATH = "/data/local/tmp/scrcpy-server.jar"

# 端口分配范围
PORT_RANGE_START = 27183
PORT_RANGE_END = 27283
SCRCPY_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT = 2
SCRCPY_POINTER_ID_GENERIC_FINGER = -2
ANDROID_MOTION_EVENT_ACTION_DOWN = 0
ANDROID_MOTION_EVENT_ACTION_UP = 1
ANDROID_MOTION_EVENT_ACTION_MOVE = 2


class DeviceInfo:
    """已连接设备的运行时状态"""

    def __init__(self, serial: str, local_port: int):
        self.serial = serial
        self.local_port = local_port
        self.scrcpy_process: Optional[subprocess.Popen] = None
        self.video_socket: Optional[socket.socket] = None
        self.control_socket: Optional[socket.socket] = None
        self.device_name: str = ""
        self.screen_width: int = 0
        self.screen_height: int = 0
        self.ready: bool = False
        self.error: Optional[str] = None
        
        # 广播机制
        self.input_queues: List[queue.Queue] = []
        self.reader_thread: Optional[threading.Thread] = None
        self.running: bool = True
        self.sps_pps_packets: List[bytes] = [] # 缓存 SPS/PPS 用于新连接初始化
        self.last_keyframe_packet: Optional[bytes] = None # 缓存最近 IDR，用于新连接首帧初始化
        self.recorder: Optional[RollingScrcpyRecorderSession] = None
        self.control_lock = threading.Lock()


def _collect_h264_nal_types(data: bytes) -> Set[int]:
    """
    从 Annex-B 格式的 H.264 数据中提取 NAL 类型集合。

    scrcpy 推送的数据通常带 start code；这里兼容 3/4 字节 start code。
    如果未找到 start code，则退化为按单 NAL 处理。
    """
    nal_types: Set[int] = set()
    length = len(data)
    i = 0

    while i < length - 3:
        start_code_len = 0
        if data[i:i + 4] == b"\x00\x00\x00\x01":
            start_code_len = 4
        elif data[i:i + 3] == b"\x00\x00\x01":
            start_code_len = 3

        if not start_code_len:
            i += 1
            continue

        nal_index = i + start_code_len
        if nal_index < length:
            nal_types.add(data[nal_index] & 0x1F)
        i = nal_index

    if not nal_types and data:
        # 兜底处理没有 start code 的单 NAL 数据。
        nal_types.add(data[0] & 0x1F)

    return nal_types


def _update_h264_init_cache(dev_info: DeviceInfo, data: bytes) -> Set[int]:
    """
    更新新客户端初始化所需的缓存包。

    - SPS/PPS：用于初始化解码器
    - 最近 IDR：确保新客户端在画面静止时也能立即解出首帧
    """
    nal_types = _collect_h264_nal_types(data)

    if 7 in nal_types:
        # 新 SPS 到来通常意味着编码参数切换，旧关键帧已不再可靠。
        dev_info.sps_pps_packets = []
        dev_info.last_keyframe_packet = None

    if 7 in nal_types or 8 in nal_types:
        if data not in dev_info.sps_pps_packets:
            dev_info.sps_pps_packets.append(data)

    if 5 in nal_types:
        dev_info.last_keyframe_packet = data

    return nal_types


def _get_h264_init_packets(dev_info: DeviceInfo) -> List[bytes]:
    """返回新客户端接入时应补发的初始化包，去重并保持顺序。"""
    packets: List[bytes] = []
    seen: Set[bytes] = set()

    for packet in dev_info.sps_pps_packets:
        if packet and packet not in seen:
            packets.append(packet)
            seen.add(packet)

    if dev_info.last_keyframe_packet and dev_info.last_keyframe_packet not in seen:
        packets.append(dev_info.last_keyframe_packet)

    return packets


def _offer_video_packet(
    client_queue: queue.Queue,
    data: bytes,
    nal_types: Set[int],
    init_packets: Optional[List[bytes]] = None,
) -> bool:
    """
    将视频包放入客户端队列。

    队列拥塞时：
    - 非同步包（普通 P/B 帧）直接丢弃，避免把半截 GOP 塞给解码器导致花屏
    - SPS/PPS/IDR 到来时，清空旧队列，仅保留最新的解码初始化序列
    """
    if not data:
        return False

    try:
        client_queue.put_nowait(data)
        return True
    except queue.Full:
        pass

    is_sync_packet = any(nal_type in nal_types for nal_type in (5, 7, 8))
    if not is_sync_packet:
        return False

    try:
        while True:
            client_queue.get_nowait()
    except queue.Empty:
        pass

    packets = [packet for packet in (init_packets or []) if packet]
    if data not in packets:
        packets.append(data)

    maxsize = int(getattr(client_queue, "maxsize", 0) or 0)
    if maxsize > 0 and len(packets) > maxsize:
        packets = packets[-maxsize:]

    for packet in packets:
        try:
            client_queue.put_nowait(packet)
        except queue.Full:
            return False

    return True


def _build_touch_control_packet(
    action: int,
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    *,
    pointer_id: int = SCRCPY_POINTER_ID_GENERIC_FINGER,
    pressure: float = 1.0,
    action_button: int = 0,
    buttons: int = 0,
) -> bytes:
    safe_pressure = max(0.0, min(1.0, float(pressure)))
    pressure_u16 = int(round(safe_pressure * 0xFFFF))
    return b"".join(
        [
            struct.pack(">B", SCRCPY_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT),
            struct.pack(">B", int(action)),
            struct.pack(">q", int(pointer_id)),
            struct.pack(">I", max(0, int(x))),
            struct.pack(">I", max(0, int(y))),
            struct.pack(">H", max(0, int(screen_width))),
            struct.pack(">H", max(0, int(screen_height))),
            struct.pack(">H", pressure_u16),
            struct.pack(">I", int(action_button)),
            struct.pack(">I", int(buttons)),
        ]
    )


class ScrcpyDeviceManager:
    """
    Scrcpy 设备管理器（单例模式）。

    生命周期：
    1. FastAPI startup → start_tracking() 启动守护线程
    2. 设备接入 → _on_device_connected() 自动部署 + 启动
    3. 前端请求 → get_video_generator() 返回 H.264 流
    4. 设备断开 → _on_device_disconnected() 清理资源
    5. FastAPI shutdown → stop_tracking() 停止所有
    """

    def __init__(self):
        self._devices: Dict[str, DeviceInfo] = {}
        self._used_ports: set = set()
        self._tracking_thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._lock = threading.Lock()

    # ==================== 设备追踪 ====================

    def start_tracking(self):
        """启动 USB 设备监听守护线程"""
        if self._running:
            return
        self._running = True
        self._tracking_thread = threading.Thread(
            target=self._track_devices_loop,
            daemon=True,
            name="device-tracker"
        )
        self._tracking_thread.start()
        logger.info("USB 设备监听已启动")

        # 初始扫描已连接的设备
        self._scan_existing_devices()

    def stop_tracking(self):
        """停止监听并清理所有设备资源"""
        self._running = False
        with self._lock:
            for serial in list(self._devices.keys()):
                self._cleanup_device(serial)
        logger.info("设备监听已停止，资源已清理")

    def _scan_existing_devices(self):
        """扫描当前已连接的设备"""
        try:
            adb = adbutils.AdbClient()
            for device in adb.device_list():
                serial = device.serial
                if serial not in self._devices:
                    logger.info(f"发现已连接设备: {serial}")
                    threading.Thread(
                        target=self._on_device_connected,
                        args=(serial,),
                        daemon=True
                    ).start()
        except Exception as e:
            logger.error(f"扫描设备失败: {e}")

    def _track_devices_loop(self):
        """守护线程：持续监听设备插拔事件"""
        while self._running:
            try:
                adb = adbutils.AdbClient()
                # track_devices 会阻塞并 yield 设备事件
                for event in adb.track_devices():
                    if not self._running:
                        break
                    serial = event.serial
                    status = event.status

                    if status == "device":
                        if serial not in self._devices:
                            logger.info(f"设备接入: {serial}")
                            threading.Thread(
                                target=self._on_device_connected,
                                args=(serial,),
                                daemon=True
                            ).start()
                    elif status in ("offline", "absent", "disconnect"):
                        if serial in self._devices:
                            logger.info(f"设备断开: {serial}")
                            self._on_device_disconnected(serial)
            except Exception as e:
                logger.error(f"设备监听异常，5秒后重试: {e}")
                time.sleep(5)

    # ==================== 设备连接/断开处理 ====================

    def _on_device_connected(self, serial: str):
        """设备接入时：部署 scrcpy-server → 分配端口 → 启动流"""
        try:
            adb = adbutils.AdbClient()
            device = adb.device(serial)

            # 1. 分配本地端口
            local_port = self._allocate_port()
            if local_port is None:
                logger.error(f"端口分配失败，无可用端口 (范围 {PORT_RANGE_START}-{PORT_RANGE_END})")
                return

            dev_info = DeviceInfo(serial, local_port)

            # 2. 部署 scrcpy-server.jar
            if not self._deploy_scrcpy_server(device):
                self._release_port(local_port)
                dev_info.error = "scrcpy-server 部署失败"
                with self._lock:
                    self._devices[serial] = dev_info
                return

            # 3. 先清理旧的 forward 和 scrcpy 进程
            try:
                device.shell("pkill -f scrcpy 2>/dev/null || true")
                time.sleep(0.5)
            except Exception as e:
                logger.debug("预清理 scrcpy 进程失败（忽略）: serial=%s error=%s", serial, e)

            # 4. 设置端口转发
            device.forward(f"tcp:{local_port}", "localabstract:scrcpy")
            logger.info(f"端口转发: tcp:{local_port} → localabstract:scrcpy")

            # 5. 使用 subprocess 启动 scrcpy server v3.3.4
            scrcpy_cmd = (
                f"adb -s {serial} shell "
                f"CLASSPATH=/data/local/tmp/scrcpy-server.jar "
                f"app_process / com.genymobile.scrcpy.Server 3.3.4 "
                f"log_level=info tunnel_forward=true video=true control=true audio=false "
                f"send_frame_meta=true "
                f"max_size=1280 "
                f"video_bit_rate=2000000"
            )
            logger.info(f"启动 scrcpy: {scrcpy_cmd}")
            proc = subprocess.Popen(
                scrcpy_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            dev_info.scrcpy_process = proc

            # 6. 等待并建立 video socket（第一个连接）
            video_sock = None
            for attempt in range(5):
                time.sleep(1)
                if proc.poll() is not None:
                    stderr_out = proc.stderr.read().decode(errors='ignore')
                    logger.error(f"scrcpy 进程已退出 (code={proc.returncode}): {stderr_out}")
                    dev_info.error = f"scrcpy 进程退出: {stderr_out[:200]}"
                    self._release_port(local_port)
                    with self._lock:
                        self._devices[serial] = dev_info
                    return
                try:
                    video_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    video_sock.settimeout(3)
                    video_sock.connect(("127.0.0.1", local_port))
                    logger.info(f"Video socket 连接成功 (第 {attempt + 1} 次尝试)")
                    break
                except Exception as e:
                    video_sock.close()
                    video_sock = None
                    logger.info(f"Video socket 重试 ({attempt + 1}/5): {e}")

            if video_sock is None:
                logger.error("Video socket 连接超时")
                dev_info.error = "Video socket 连接超时"
                proc.terminate()
                self._release_port(local_port)
                with self._lock:
                    self._devices[serial] = dev_info
                return

            # 7. 建立 control socket（第二个连接，scrcpy 需要双连接）
            try:
                ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ctrl_sock.settimeout(3)
                ctrl_sock.connect(("127.0.0.1", local_port))
                dev_info.control_socket = ctrl_sock
                logger.info("Control socket 连接成功")
            except Exception as e:
                logger.error(f"Control socket 连接失败: {e}")
                dev_info.error = f"Control socket 失败: {e}"
                video_sock.close()
                proc.terminate()
                self._release_port(local_port)
                with self._lock:
                    self._devices[serial] = dev_info
                return

            # 8. 读取 dummy byte（在 video/control 双连接建立后再读，避免握手时序问题）
            try:
                self._recv_exactly(video_sock, 1)  # dummy byte
                logger.info("收到 dummy byte")
            except Exception as e:
                logger.error(f"读取 dummy byte 失败: {e}")
                dev_info.error = f"Dummy byte 失败: {e}"
                video_sock.close()
                ctrl_sock.close()
                proc.terminate()
                self._release_port(local_port)
                with self._lock:
                    self._devices[serial] = dev_info
                return

            dev_info.video_socket = video_sock

            # 9. 读取设备名 header（64字节）
            try:
                device_name_bytes = self._recv_exactly(video_sock, 64)
                dev_info.device_name = device_name_bytes.rstrip(b'\x00').decode('utf-8', errors='ignore')
                logger.info(f"设备名: {dev_info.device_name}")
            except Exception as e:
                logger.error(f"读取设备名失败: {e}")
                dev_info.error = f"Header 读取失败: {e}"
                video_sock.close()
                ctrl_sock.close()
                proc.terminate()
                self._release_port(local_port)
                with self._lock:
                    self._devices[serial] = dev_info
                return

            # 10. 获取屏幕分辨率（通过 adb）
            try:
                size_output = device.shell("wm size").strip()
                # 格式: "Physical size: 1080x2400"
                if 'x' in size_output:
                    parts = size_output.split(':')[-1].strip().split('x')
                    dev_info.screen_width = int(parts[0])
                    dev_info.screen_height = int(parts[1])
            except Exception:
                dev_info.screen_width = 800
                dev_info.screen_height = 1600

            dev_info.ready = True
            dev_info.running = True
            logger.info(
                f"设备就绪: {dev_info.device_name} "
                f"({dev_info.screen_width}x{dev_info.screen_height})"
            )

            # 11. 启动视频流读取广播线程
            self._start_video_reader(dev_info)

            with self._lock:
                self._devices[serial] = dev_info

        except Exception:
            logger.exception("设备流初始化异常: serial=%s", serial)


    def _on_device_disconnected(self, serial: str):
        """设备断开时清理资源"""
        with self._lock:
            self._cleanup_device(serial)

    def _cleanup_device(self, serial: str):
        """清理指定设备的所有资源"""
        dev_info = self._devices.pop(serial, None)
        if not dev_info:
            return

        if dev_info.recorder:
            try:
                dev_info.recorder.stop(cleanup_buffer=True)
            except Exception as e:
                logger.debug("停止录制器失败（忽略）: serial=%s error=%s", serial, e)
            finally:
                dev_info.recorder = None

        # 停止广播线程
        dev_info.running = False
        if dev_info.reader_thread:
            dev_info.reader_thread.join(timeout=1)

        # 关闭 sockets
        if dev_info.video_socket:
            try:
                dev_info.video_socket.close()
            except Exception as e:
                logger.debug("关闭视频 socket 失败（忽略）: serial=%s error=%s", serial, e)
        if dev_info.control_socket:
            try:
                dev_info.control_socket.close()
            except Exception as e:
                logger.debug("关闭控制 socket 失败（忽略）: serial=%s error=%s", serial, e)

        # 终止 scrcpy 进程
        if dev_info.scrcpy_process:
            try:
                dev_info.scrcpy_process.terminate()
            except Exception as e:
                logger.debug("终止 scrcpy 进程失败（忽略）: serial=%s error=%s", serial, e)

        # 释放端口
        self._release_port(dev_info.local_port)

        # 移除 adb forward
        try:
            adb = adbutils.AdbClient()
            device = adb.device(serial)
            device.forward_remove(f"tcp:{dev_info.local_port}")
        except Exception as e:
            logger.debug("移除端口转发失败（忽略）: serial=%s port=%s error=%s", serial, dev_info.local_port, e)

        logger.info(f"设备 {serial} 资源已清理")

    # ==================== Scrcpy Server 部署 ====================

    def _deploy_scrcpy_server(self, device) -> bool:
        """检测并部署 scrcpy-server.jar 到设备"""
        if not os.path.exists(SCRCPY_SERVER_PATH):
            logger.error(
                f"scrcpy-server.jar 不存在: {SCRCPY_SERVER_PATH}\n"
                f"请下载 scrcpy-server 并放到 assets/ 目录"
            )
            return False

        try:
            # 检查设备上是否已有 jar
            remote_size = device.shell(f"wc -c < {DEVICE_JAR_PATH} 2>/dev/null || echo 0").strip()
            local_size = str(os.path.getsize(SCRCPY_SERVER_PATH))

            if remote_size != local_size:
                logger.info(f"部署 scrcpy-server.jar (本地: {local_size}, 远端: {remote_size})")
                device.sync.push(SCRCPY_SERVER_PATH, DEVICE_JAR_PATH)
                logger.info("scrcpy-server.jar 部署完成")
            else:
                logger.info("scrcpy-server.jar 已是最新版本")

            return True
        except Exception as e:
            logger.error(f"部署 scrcpy-server 失败: {e}")
            return False

    # ==================== 端口管理 ====================

    def _allocate_port(self) -> Optional[int]:
        """分配一个空闲端口"""
        with self._lock:
            for port in range(PORT_RANGE_START, PORT_RANGE_END):
                if port not in self._used_ports:
                    self._used_ports.add(port)
                    return port
        return None

    def _release_port(self, port: int):
        """释放端口"""
        self._used_ports.discard(port)

    # ==================== 视频流 ====================

    # ==================== 视频流 ====================

    def _start_video_reader(self, dev_info: DeviceInfo):
        """启动后台线程，从 socket 读取视频流并广播给所有客户端"""
        def _broadcast_frame(data: bytes):
            """广播一帧给所有客户端队列"""
            if not data:
                return
            nal_types = _update_h264_init_cache(dev_info, data)
            recorder = dev_info.recorder
            if recorder:
                try:
                    recorder.ingest(data)
                except Exception as e:
                    logger.warning("写入本地复现录制失败（忽略）: serial=%s error=%s", dev_info.serial, e)
            if 7 in nal_types or 8 in nal_types:
                logger.info("收到视频初始化包: serial=%s nal_types=%s len=%s", dev_info.serial, sorted(nal_types), len(data))
            if 5 in nal_types:
                logger.info("缓存最近关键帧: serial=%s len=%s", dev_info.serial, len(data))

            queues = list(dev_info.input_queues)
            init_packets = _get_h264_init_packets(dev_info) if any(nal_type in nal_types for nal_type in (5, 7, 8)) else None
            for q in queues:
                offered = _offer_video_packet(q, data, nal_types, init_packets=init_packets)
                if not offered and q.full():
                    logger.debug(
                        "丢弃非关键视频包以追赶实时画面: serial=%s nal_types=%s len=%s",
                        dev_info.serial,
                        sorted(nal_types),
                        len(data),
                    )

        def _reader_loop():
            serial = dev_info.serial
            sock = dev_info.video_socket
            if not sock:
                return

            logger.info(f"视频流广播线程启动: {serial}")
            sock.settimeout(5)

            # 1. 消费 scrcpy Codec Header (12 字节: 4 bytes codec + 4 bytes W + 4 bytes H)
            try:
                codec_header = self._recv_exactly(sock, 12)
                if codec_header and codec_header[0:4] == b'h264':
                    w = struct.unpack(">I", codec_header[4:8])[0]
                    h = struct.unpack(">I", codec_header[8:12])[0]
                    logger.info(f"Codec Header: h264, {w}x{h}")
                else:
                    logger.warning(f"未识别到 Codec Header: {codec_header.hex() if codec_header else 'None'}")
            except Exception as e:
                logger.error(f"读取 Codec Header 失败: {e}")
                return

            # 2. 试探第一帧，自动判断是 Meta 模式还是 Raw 模式
            use_raw_mode = False
            try:
                first_12 = self._recv_exactly(sock, 12)
                if not first_12:
                    return
                if first_12.startswith(b'\x00\x00\x00\x01'):
                    use_raw_mode = True
                    logger.info("检测到 Raw H.264 流 (无 Meta Header)，切换到 Raw 模式")
                else:
                    logger.info("检测到 Meta Header 模式 (PTS + Size)")
            except Exception as e:
                logger.error(f"流模式探测失败: {e}")
                return

            # =============== Raw 模式 ===============
            if use_raw_mode:
                # 把已读的 12 字节作为 buffer 起始
                buf = bytearray(first_12)
                START_CODE = b'\x00\x00\x00\x01'

                while dev_info.running and self._running:
                    try:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        buf.extend(chunk)

                        # 按 start code 切分 NAL 单元
                        while True:
                            # 查找第二个 start code 的位置
                            pos = buf.find(START_CODE, 4)
                            if pos == -1:
                                # 缓冲区里只有一个不完整的 NAL，等更多数据
                                break
                            # 完整的 NAL: buf[0:pos]
                            nal_unit = bytes(buf[:pos])
                            _broadcast_frame(nal_unit)
                            buf = buf[pos:]

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"Raw 流读取异常: {e}")
                        dev_info.error = f"视频流中断: {e}"
                        break
            else:
                # =============== Meta Header 模式 ===============
                # first_12 已经是第一帧的 header
                pending_header = first_12

                while dev_info.running and self._running:
                    try:
                        if pending_header:
                            header_data = pending_header
                            pending_header = None
                        else:
                            header_data = self._recv_exactly(sock, 12)
                            if not header_data:
                                break

                        pkt_len = struct.unpack(">I", header_data[8:12])[0]

                        if pkt_len == 0 or pkt_len > 5 * 1024 * 1024:
                            logger.warning(f"异常帧大小: {pkt_len}, 跳过")
                            continue

                        h264_data = self._recv_exactly(sock, pkt_len)
                        if not h264_data:
                            continue

                        _broadcast_frame(h264_data)

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"视频流广播线程异常: {e}")
                        dev_info.error = f"视频流中断: {e}"
                        break

            logger.info(f"视频流广播线程结束: {serial}")
            dev_info.ready = False

        t = threading.Thread(target=_reader_loop, daemon=True, name=f"ScrcpyReader-{dev_info.serial}")
        dev_info.reader_thread = t
        t.start()

    def get_video_generator(self, serial: str) -> Generator[bytes, None, None]:
        """
        获取指定设备的 H.264 视频流生成器 (支持多客户端)。
        
        Yields:
            bytes: 纯 H.264 NAL 单元数据
        """
        dev_info = self._devices.get(serial)
        if not dev_info or not dev_info.ready:
            raise ValueError(f"设备 {serial} 未就绪或不存在")

        # 创建客户端队列
        client_queue = queue.Queue(maxsize=5)
        dev_info.input_queues.append(client_queue)
        logger.info(f"客户端加入视频流: {serial} (当前客户端数: {len(dev_info.input_queues)})")

        # 先补发解码初始化包，避免画面静止时新客户端拿不到首帧。
        init_packets = _get_h264_init_packets(dev_info)
        if init_packets:
            logger.info(f"向新客户端发送缓存初始化包: serial={serial} packets={len(init_packets)}")
            for packet in init_packets:
                try:
                    client_queue.put(packet)
                except queue.Full:
                    pass

        try:
            while self._running and serial in self._devices and dev_info.ready:
                try:
                    # 从队列获取帧，设置超时以便检测连接状态
                    data = client_queue.get(timeout=0.1)
                    yield data
                except queue.Empty:
                    if not dev_info.ready:
                        break
                    continue
        finally:
            if client_queue in dev_info.input_queues:
                dev_info.input_queues.remove(client_queue)
            logger.info(f"客户端退出视频流: {serial}")

    def send_touch_event(self, serial: str, action: int, x: int, y: int):
        """
        向设备发送触控事件。

        Args:
            serial: 设备序列号
            action: 0=按下, 1=抬起, 2=移动
            x: 屏幕 X 坐标
            y: 屏幕 Y 坐标
        """
        dev_info = self._devices.get(serial)
        if not dev_info or not dev_info.ready:
            raise ValueError(f"设备 {serial} 未就绪")

        screen_width = max(1, int(dev_info.screen_width or 0))
        screen_height = max(1, int(dev_info.screen_height or 0))
        clamped_x = min(screen_width - 1, max(0, int(x)))
        clamped_y = min(screen_height - 1, max(0, int(y)))

        control_sock = dev_info.control_socket
        if control_sock:
            try:
                with dev_info.control_lock:
                    if action == ANDROID_MOTION_EVENT_ACTION_MOVE:
                        control_sock.sendall(
                            _build_touch_control_packet(
                                ANDROID_MOTION_EVENT_ACTION_MOVE,
                                clamped_x,
                                clamped_y,
                                screen_width,
                                screen_height,
                                pressure=1.0,
                            )
                        )
                    elif action == ANDROID_MOTION_EVENT_ACTION_UP:
                        control_sock.sendall(
                            _build_touch_control_packet(
                                ANDROID_MOTION_EVENT_ACTION_UP,
                                clamped_x,
                                clamped_y,
                                screen_width,
                                screen_height,
                                pressure=0.0,
                            )
                        )
                    else:
                        control_sock.sendall(
                            _build_touch_control_packet(
                                ANDROID_MOTION_EVENT_ACTION_DOWN,
                                clamped_x,
                                clamped_y,
                                screen_width,
                                screen_height,
                                pressure=1.0,
                            )
                        )
                        control_sock.sendall(
                            _build_touch_control_packet(
                                ANDROID_MOTION_EVENT_ACTION_UP,
                                clamped_x,
                                clamped_y,
                                screen_width,
                                screen_height,
                                pressure=0.0,
                            )
                        )
                return
            except Exception as exc:
                logger.warning("scrcpy control socket 注入失败，降级 adb input: serial=%s error=%s", serial, exc)

        # 兜底通过 adb shell 发送 input 事件
        try:
            adb = adbutils.AdbClient()
            device = adb.device(serial)
            if action == ANDROID_MOTION_EVENT_ACTION_MOVE:
                # move 事件需要成对 down/move/up，这里仅保留 no-op 兜底，优先依赖 control socket。
                return
            if action == ANDROID_MOTION_EVENT_ACTION_UP:
                return
            if action == ANDROID_MOTION_EVENT_ACTION_DOWN:
                # 按下+抬起 = tap
                device.shell(f"input tap {clamped_x} {clamped_y}")
        except Exception as e:
            logger.error(f"触控事件发送失败: {e}")
            raise

    def start_recording(
        self,
        serial: str,
        task_id: int,
        report_dir: str,
        pre_roll_sec: int = 30,
        post_roll_sec: int = 5,
        segment_sec: int = 5,
    ) -> RollingScrcpyRecorderSession:
        dev_info = self._devices.get(serial)
        if not dev_info or not dev_info.ready:
            raise ValueError(f"设备 {serial} 未就绪")

        if dev_info.recorder:
            try:
                dev_info.recorder.stop(cleanup_buffer=True)
            except Exception:
                logger.exception("替换已有录制器失败: serial=%s", serial)

        recorder = RollingScrcpyRecorderSession(
            serial=serial,
            task_id=task_id,
            report_dir=report_dir,
            project_root=PROJECT_ROOT,
            pre_roll_sec=pre_roll_sec,
            post_roll_sec=post_roll_sec,
            segment_sec=segment_sec,
        )
        recorder.seed_init_packets(_get_h264_init_packets(dev_info))
        dev_info.recorder = recorder
        logger.info("已启动本地复现录制: serial=%s task_id=%s", serial, task_id)
        return recorder

    def capture_replay(
        self,
        serial: str,
        event_type: str,
        event_time: str,
    ) -> ReplayCaptureResult:
        dev_info = self._devices.get(serial)
        if not dev_info or not dev_info.recorder:
            return ReplayCaptureResult(
                status="UNAVAILABLE",
                error=f"device recorder unavailable: {serial}",
            )
        return dev_info.recorder.capture_replay(event_type=event_type, event_time=event_time)

    def stop_recording(self, serial: str) -> None:
        dev_info = self._devices.get(serial)
        if not dev_info or not dev_info.recorder:
            return
        try:
            dev_info.recorder.stop(cleanup_buffer=True)
        finally:
            dev_info.recorder = None
        logger.info("已停止本地复现录制: serial=%s", serial)

    # ==================== 重连 ====================

    def reconnect_device(self, serial: str):
        """清理旧连接，重新初始化设备"""
        with self._lock:
            self._cleanup_device(serial)
        logger.info(f"设备 {serial} 准备重连...")
        threading.Thread(
            target=self._on_device_connected,
            args=(serial,),
            daemon=True
        ).start()

    # ==================== 查询接口 ====================

    def get_devices_list(self) -> list:
        """返回所有已管理设备的状态信息"""
        result = []
        with self._lock:
            for serial, info in self._devices.items():
                result.append({
                    "serial": serial,
                    "device_name": info.device_name,
                    "screen_width": info.screen_width,
                    "screen_height": info.screen_height,
                    "ready": info.ready,
                    "error": info.error,
                    "port": info.local_port
                })
        return result

    def get_device(self, serial: str) -> Optional[dict]:
        """获取单个设备信息"""
        dev_info = self._devices.get(serial)
        if not dev_info:
            return None
        return {
            "serial": serial,
            "device_name": dev_info.device_name,
            "screen_width": dev_info.screen_width,
            "screen_height": dev_info.screen_height,
            "ready": dev_info.ready,
            "error": dev_info.error
        }

    # ==================== 工具方法 ====================

    @staticmethod
    def _recv_exactly(sock: socket.socket, n: int) -> bytes:
        """从 socket 精确读取 n 字节"""
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError(f"Socket 连接断开 (已读取 {len(data)}/{n} 字节)")
            data += chunk
        return data


# ==================== 全局单例 ====================

device_manager = ScrcpyDeviceManager()
