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
            except Exception:
                pass

            # 4. 设置端口转发
            device.forward(f"tcp:{local_port}", "localabstract:scrcpy")
            logger.info(f"端口转发: tcp:{local_port} → localabstract:scrcpy")

            # 5. 使用 subprocess 启动 scrcpy server v3.3.4
            scrcpy_cmd = (
                f"adb -s {serial} shell "
                f"CLASSPATH=/data/local/tmp/scrcpy-server.jar "
                f"app_process / com.genymobile.scrcpy.Server 3.3.4 "
                f"log_level=info tunnel_forward=true video=true audio=false "
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

            # 7. 读取 dummy byte（server 在第一个连接上发送）
            try:
                self._recv_exactly(video_sock, 1)  # dummy byte
                logger.info("收到 dummy byte")
            except Exception as e:
                logger.error(f"读取 dummy byte 失败: {e}")
                dev_info.error = f"Dummy byte 失败: {e}"
                video_sock.close()
                proc.terminate()
                self._release_port(local_port)
                with self._lock:
                    self._devices[serial] = dev_info
                return

            # 8. 建立 control socket（第二个连接，scrcpy 需要双连接）
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

        except Exception as e:
            logger.error(f"设备 {serial} 初始化失败: {e}")
            import traceback
            traceback.print_exc()


    def _on_device_disconnected(self, serial: str):
        """设备断开时清理资源"""
        with self._lock:
            self._cleanup_device(serial)

    def _cleanup_device(self, serial: str):
        """清理指定设备的所有资源"""
        dev_info = self._devices.pop(serial, None)
        if not dev_info:
            return

        # 停止广播线程
        dev_info.running = False
        if dev_info.reader_thread:
            dev_info.reader_thread.join(timeout=1)

        # 关闭 sockets
        if dev_info.video_socket:
            try:
                dev_info.video_socket.close()
            except Exception:
                pass
        if dev_info.control_socket:
            try:
                dev_info.control_socket.close()
            except Exception:
                pass

        # 终止 scrcpy 进程
        if dev_info.scrcpy_process:
            try:
                dev_info.scrcpy_process.terminate()
            except Exception:
                pass

        # 释放端口
        self._release_port(dev_info.local_port)

        # 移除 adb forward
        try:
            adb = adbutils.AdbClient()
            device = adb.device(serial)
            device.forward_remove(f"tcp:{dev_info.local_port}")
        except Exception:
            pass

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
            # 缓存 SPS/PPS
            if len(data) > 4:
                nal_type = data[4] & 0x1F
                if nal_type == 7 or nal_type == 8:
                    logger.info(f"收到 SPS/PPS (type={nal_type}, len={len(data)})")
                    if nal_type == 7:
                        dev_info.sps_pps_packets = []
                    dev_info.sps_pps_packets.append(data)

            queues = list(dev_info.input_queues)
            for q in queues:
                if q.full():
                    try:
                        while not q.empty():
                            q.get_nowait()
                    except Exception:
                        pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

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

        # 如果有缓存的 SPS/PPS，先发送给新客户端（数据已包含 Annex-B Start Code）
        if dev_info.sps_pps_packets:
            logger.info(f"向新客户端发送缓存的 {len(dev_info.sps_pps_packets)} 个 SPS/PPS 包")
            for packet in dev_info.sps_pps_packets:
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

        # 通过 adb shell 发送 input 事件（简化版，不走 scrcpy control socket）
        try:
            adb = adbutils.AdbClient()
            device = adb.device(serial)
            if action == 0:
                # 按下+抬起 = tap
                device.shell(f"input tap {x} {y}")
            elif action == 2:
                # 移动事件（swipe 需要起止坐标，这里只做 tap）
                pass
        except Exception as e:
            logger.error(f"触控事件发送失败: {e}")
            raise

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
