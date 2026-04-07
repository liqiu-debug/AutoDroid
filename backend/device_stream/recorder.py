import math
import os
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, List, Optional, Set


DEFAULT_REPLAY_FPS = 25.0


def _collect_h264_nal_types(data: bytes) -> Set[int]:
    nal_types: Set[int] = set()
    length = len(data)
    index = 0

    while index < length - 3:
        start_code_len = 0
        if data[index:index + 4] == b"\x00\x00\x00\x01":
            start_code_len = 4
        elif data[index:index + 3] == b"\x00\x00\x01":
            start_code_len = 3

        if not start_code_len:
            index += 1
            continue

        nal_index = index + start_code_len
        if nal_index < length:
            nal_types.add(data[nal_index] & 0x1F)
        index = nal_index

    if not nal_types and data:
        nal_types.add(data[0] & 0x1F)
    return nal_types


@dataclass
class ReplayCaptureResult:
    status: str
    path: str = ""
    filename: str = ""
    pre_roll_sec: int = 0
    post_roll_sec: int = 0
    duration_sec: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "path": self.path,
            "filename": self.filename,
            "pre_roll_sec": self.pre_roll_sec,
            "post_roll_sec": self.post_roll_sec,
            "duration_sec": self.duration_sec,
            "error": self.error,
        }


def transcode_h264_to_mp4(source_path: str, output_path: str) -> int:
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError(f"OpenCV unavailable for mp4 conversion: {exc}") from exc

    capture = cv2.VideoCapture(source_path)
    if not capture.isOpened():
        raise RuntimeError(f"unable to open h264 replay: {source_path}")

    writer = None
    frame_count = 0
    first_frame = None
    output_root, output_ext = os.path.splitext(output_path)
    temp_output_path = f"{output_root}.tmp{output_ext or '.mp4'}"
    try:
        if os.path.isfile(temp_output_path):
            os.remove(temp_output_path)
    except OSError:
        pass
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(fps) or fps <= 1 or fps > 240:
            fps = DEFAULT_REPLAY_FPS

        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        ok, first_frame = capture.read()
        if not ok or first_frame is None:
            raise RuntimeError("no decodable frames in h264 replay")

        if width <= 0 or height <= 0:
            height, width = first_frame.shape[:2]

        for codec_name in ("avc1", "H264", "mp4v"):
            fourcc = cv2.VideoWriter_fourcc(*codec_name)
            candidate = cv2.VideoWriter(temp_output_path, fourcc, fps, (width, height))
            if candidate.isOpened():
                writer = candidate
                break
            candidate.release()

        if writer is None:
            raise RuntimeError("unable to initialize mp4 writer")

        writer.write(first_frame)
        frame_count = 1

        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            writer.write(frame)
            frame_count += 1
    finally:
        capture.release()
        if writer is not None:
            writer.release()

    if frame_count <= 0:
        raise RuntimeError("empty mp4 output")
    if not os.path.isfile(temp_output_path) or os.path.getsize(temp_output_path) <= 0:
        raise RuntimeError("mp4 output file missing")
    os.replace(temp_output_path, output_path)
    return frame_count


class _BufferSegment:
    def __init__(self, path: str, sequence: int, started_mono: float):
        self.path = path
        self.sequence = sequence
        self.started_mono = started_mono
        self.ended_mono = started_mono
        self.has_data = False
        self.byte_count = 0
        self.keyframe_count = 0
        self._handle = open(path, "wb")

    def write(self, data: bytes, now_mono: float, has_keyframe: bool) -> None:
        if not data:
            return
        self._handle.write(data)
        self.has_data = True
        self.byte_count += len(data)
        self.ended_mono = now_mono
        if has_keyframe:
            self.keyframe_count += 1

    def flush(self) -> None:
        if self._handle.closed:
            return
        self._handle.flush()

    def close(self) -> None:
        if self._handle.closed:
            return
        self._handle.flush()
        self._handle.close()

    def delete(self) -> None:
        self.close()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


class RollingScrcpyRecorderSession:
    def __init__(
        self,
        serial: str,
        task_id: int,
        report_dir: str,
        project_root: str,
        pre_roll_sec: int = 30,
        post_roll_sec: int = 5,
        segment_sec: int = 5,
    ):
        self.serial = str(serial or "").strip()
        self.task_id = int(task_id)
        self.report_dir = report_dir
        self.project_root = project_root
        self.pre_roll_sec = max(1, int(pre_roll_sec))
        self.post_roll_sec = max(0, int(post_roll_sec))
        self.segment_sec = max(1, int(segment_sec))

        # Keep a little extra headroom so the oldest pre-roll segment
        # is not dropped while we are capturing the post-roll tail.
        buffer_window_sec = self.pre_roll_sec + self.post_roll_sec + self.segment_sec
        self.max_segments = max(2, int(math.ceil(buffer_window_sec / self.segment_sec)))

        self.replay_dir = os.path.join(self.report_dir, "replays")
        self.buffer_dir = os.path.join(self.replay_dir, ".buffer")
        os.makedirs(self.buffer_dir, exist_ok=True)

        self._lock = Lock()
        self._capture_lock = Lock()
        self._segments: Deque[_BufferSegment] = deque()
        self._current_segment: Optional[_BufferSegment] = None
        self._segment_sequence = 0
        self._replay_sequence = 0
        self._closed = False
        self._latest_sps_pps: List[bytes] = []
        self._latest_idr_packet: bytes = b""
        self._rotate_locked(time.monotonic())

    def seed_init_packets(self, packets: List[bytes]) -> None:
        if not packets:
            return

        with self._lock:
            if self._closed:
                return

            latest_sps_pps: List[bytes] = []
            latest_idr_packet = self._latest_idr_packet

            for packet in packets:
                if not packet:
                    continue
                nal_types = _collect_h264_nal_types(packet)
                if 7 in nal_types:
                    latest_sps_pps = []
                    latest_idr_packet = b""
                if 7 in nal_types or 8 in nal_types:
                    if packet not in latest_sps_pps:
                        latest_sps_pps.append(packet)
                        latest_sps_pps = latest_sps_pps[-4:]
                if 5 in nal_types:
                    latest_idr_packet = packet

            if latest_sps_pps:
                self._latest_sps_pps = latest_sps_pps
            if latest_idr_packet:
                self._latest_idr_packet = latest_idr_packet

    def ingest(self, data: bytes) -> None:
        if not data:
            return

        now_mono = time.monotonic()
        nal_types = _collect_h264_nal_types(data)
        has_keyframe = 5 in nal_types

        with self._lock:
            if self._closed:
                return
            if (
                self._current_segment is None
                or (now_mono - self._current_segment.started_mono) >= self.segment_sec
            ):
                self._rotate_locked(now_mono)

            if 7 in nal_types:
                self._latest_sps_pps = []
                self._latest_idr_packet = b""
            if 7 in nal_types or 8 in nal_types:
                if data not in self._latest_sps_pps:
                    self._latest_sps_pps.append(data)
                    self._latest_sps_pps = self._latest_sps_pps[-4:]
            if has_keyframe:
                self._latest_idr_packet = data

            if self._current_segment is not None:
                self._current_segment.write(data, now_mono, has_keyframe=has_keyframe)

    def capture_replay(self, event_type: str, event_time: str) -> ReplayCaptureResult:
        if not self._capture_lock.acquire(blocking=False):
            return ReplayCaptureResult(
                status="SKIPPED",
                pre_roll_sec=self.pre_roll_sec,
                post_roll_sec=self.post_roll_sec,
                error="replay capture already in progress",
            )

        try:
            capture_started_mono = time.monotonic()
            window_start_mono = capture_started_mono - self.pre_roll_sec
            capture_end_mono = capture_started_mono + self.post_roll_sec

            while True:
                remaining = capture_end_mono - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))

            with self._lock:
                if self._closed and not self._segments:
                    return ReplayCaptureResult(
                        status="UNAVAILABLE",
                        pre_roll_sec=self.pre_roll_sec,
                        post_roll_sec=self.post_roll_sec,
                        error="recorder already closed",
                    )

                self._flush_locked()
                # Freeze the active segment before exporting so the replay file
                # reads from immutable buffers instead of a segment that is still
                # being appended by the scrcpy reader thread.
                if not self._closed and self._current_segment is not None and self._current_segment.has_data:
                    self._rotate_locked(time.monotonic())
                    self._flush_locked()
                selected_segments = [
                    segment
                    for segment in list(self._segments)
                    if segment.has_data
                    and segment.ended_mono >= window_start_mono
                    and segment.started_mono <= capture_end_mono
                ]
                latest_sps_pps = list(self._latest_sps_pps)
                latest_idr_packet = self._latest_idr_packet

            if not selected_segments:
                return ReplayCaptureResult(
                    status="UNAVAILABLE",
                    pre_roll_sec=self.pre_roll_sec,
                    post_roll_sec=self.post_roll_sec,
                    error="no buffered video data available",
                )

            output_path = self._build_replay_output_path(event_type=event_type, event_time=event_time)
            source_path = self._build_replay_source_path(event_type=event_type, event_time=event_time)
            init_packets = list(latest_sps_pps)
            if latest_idr_packet:
                init_packets.append(latest_idr_packet)
            try:
                byte_count = self._write_replay_file(source_path, selected_segments, init_packets)
                if byte_count <= 0:
                    return ReplayCaptureResult(
                        status="FAILED",
                        pre_roll_sec=self.pre_roll_sec,
                        post_roll_sec=self.post_roll_sec,
                        error="empty replay output",
                    )
                transcode_h264_to_mp4(source_path, output_path)
            finally:
                try:
                    if os.path.isfile(source_path):
                        os.remove(source_path)
                except OSError:
                    pass

            first_started = min(segment.started_mono for segment in selected_segments)
            last_ended = max(segment.ended_mono for segment in selected_segments)
            duration_sec = max(1, int(round(last_ended - first_started)))
            rel_path = os.path.relpath(output_path, self.project_root).replace(os.sep, "/")
            return ReplayCaptureResult(
                status="READY",
                path=rel_path,
                filename=os.path.basename(output_path),
                pre_roll_sec=self.pre_roll_sec,
                post_roll_sec=self.post_roll_sec,
                duration_sec=duration_sec,
            )
        except Exception as exc:
            return ReplayCaptureResult(
                status="FAILED",
                pre_roll_sec=self.pre_roll_sec,
                post_roll_sec=self.post_roll_sec,
                error=str(exc),
            )
        finally:
            self._capture_lock.release()

    def stop(self, cleanup_buffer: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._flush_locked()
            if self._current_segment is not None:
                self._current_segment.close()
            segments = list(self._segments)
            self._segments.clear()
            self._current_segment = None

        if cleanup_buffer:
            for segment in segments:
                segment.delete()
            try:
                if os.path.isdir(self.buffer_dir) and not os.listdir(self.buffer_dir):
                    os.rmdir(self.buffer_dir)
            except OSError:
                pass

    def _rotate_locked(self, started_mono: float) -> None:
        if self._current_segment is not None:
            self._current_segment.close()

        self._segment_sequence += 1
        segment_path = os.path.join(
            self.buffer_dir,
            f"segment_{self._segment_sequence:06d}.h264",
        )
        segment = _BufferSegment(
            path=segment_path,
            sequence=self._segment_sequence,
            started_mono=started_mono,
        )
        self._segments.append(segment)
        self._current_segment = segment

        while len(self._segments) > self.max_segments:
            expired = self._segments.popleft()
            if expired is self._current_segment:
                continue
            expired.delete()

    def _flush_locked(self) -> None:
        for segment in self._segments:
            segment.flush()

    def _build_replay_output_path(self, event_type: str, event_time: str) -> str:
        self._replay_sequence += 1
        safe_type = "".join(ch.lower() for ch in str(event_type or "event") if ch.isalnum()) or "event"
        safe_time = "".join(ch for ch in str(event_time or "") if ch.isdigit()) or str(int(time.time()))
        filename = f"{safe_type}_{safe_time}_{self._replay_sequence:02d}.mp4"
        os.makedirs(self.replay_dir, exist_ok=True)
        return os.path.join(self.replay_dir, filename)

    def _build_replay_source_path(self, event_type: str, event_time: str) -> str:
        safe_type = "".join(ch.lower() for ch in str(event_type or "event") if ch.isalnum()) or "event"
        safe_time = "".join(ch for ch in str(event_time or "") if ch.isdigit()) or str(int(time.time()))
        filename = f"{safe_type}_{safe_time}_{self._replay_sequence:02d}.source.h264"
        os.makedirs(self.replay_dir, exist_ok=True)
        return os.path.join(self.replay_dir, filename)

    @staticmethod
    def _write_replay_file(
        output_path: str,
        segments: List[_BufferSegment],
        init_packets: List[bytes],
    ) -> int:
        total_bytes = 0
        segments = sorted(segments, key=lambda item: item.sequence)

        with open(output_path, "wb") as output:
            seen_packets: Set[bytes] = set()
            for packet in init_packets:
                if not packet or packet in seen_packets:
                    continue
                output.write(packet)
                total_bytes += len(packet)
                seen_packets.add(packet)

            for segment in segments:
                with open(segment.path, "rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        total_bytes += len(chunk)
        return total_bytes
