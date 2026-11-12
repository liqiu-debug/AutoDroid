"""崩溃/ANR 检测：logcat 流式监控与快照抓取。"""
import asyncio
import inspect
import logging
import re
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional

from backend.fastbot.adb import _terminate_subprocess

logger = logging.getLogger("FastbotRunner")

CRASH_PATTERN = re.compile(r"(FATAL EXCEPTION|ANR in)", re.IGNORECASE)
ANR_PATTERN = re.compile(r"ANR in", re.IGNORECASE)
PROC_LINE_PATTERN = re.compile(r"Process:\s*(\S+)")
ANR_PKG_PATTERN = re.compile(r"ANR in\s+(\S+)")

LOGCAT_SNAPSHOT_TIMEOUT_SECONDS = 8
DEDUP_COOLDOWN_SECONDS = 10
CRASH_SOURCE_TAG = re.compile(r"E/AndroidRuntime\s*\(")

# 快照抓取策略（对照 OPPO Reno14 Pro 线上产物：500 行只覆盖约 2.3 秒、
# 崩溃后仅剩 43ms 日志）：`logcat -d` 按 buffer 分组输出且 crash buffer 排最后，
# 按行数截断时崩溃正文最先被挤掉，所以改成「崩溃优先 + 崩溃后沉淀 + 超限只裁上下文」。
SNAPSHOT_POST_SETTLE_SEC = 2.0   # 事件后善后/上报日志落进 ring buffer 的沉淀时间
SNAPSHOT_CRASH_LINES = 300       # crash buffer 预算：崩溃正文必须抓全
SNAPSHOT_ANR_LINES = 800         # ANR 正文（ANR in / Reason / CPU 概览 / 主线程栈）预算
SNAPSHOT_CONTEXT_LINES = 1500    # 上下文预算：必须跨过事件时刻，见 _capture_logcat_snapshot
SNAPSHOT_FALLBACK_LINES = 500    # 无对应 buffer 的老机型退回旧行为的行数
SNAPSHOT_MAX_BYTES = 120_000     # 快照字节上限；超限只裁上下文段，绝不裁主段
SNAPSHOT_CRASH_HEADER = "----- logcat crash buffer -----"
SNAPSHOT_ANR_HEADER = "----- logcat ANR (system buffer) -----"
SNAPSHOT_CONTEXT_HEADER = "----- logcat context (main,system) -----"
SNAPSHOT_ANR_CONTEXT_HEADER = "----- logcat context (main) -----"


async def _monitor_logcat(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    crash_events: List[Dict],
    capture_log: bool,
    abort_on_crash: bool = False,
    abort_event: Optional[asyncio.Event] = None,
    replay_callback: Optional[Callable[[str, str], Awaitable[Optional[Dict]]]] = None,
    event_sink: Optional[Callable[[Dict], object]] = None,
):
    """子协程：持续读取 logcat 流，只抓取目标包名相关的崩溃/ANR。

    策略：
    - 启动前调用方已清空 logcat 缓冲区，避免旧日志干扰
    - 只认 E/AndroidRuntime 标签的 FATAL EXCEPTION，忽略厂商重复条目
    - 同类事件在冷却期(10s)内不重复计数
    - abort_on_crash=True 时，检测到崩溃后触发 abort_event 通知主协程终止 Monkey
    """
    async def dispatch_event(event: Dict) -> None:
        if not event_sink:
            return
        try:
            result = event_sink(dict(event))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # An optional caller sink must never disable Fastbot's own monitor.
            logger.warning("Crash/ANR event sink failed and was ignored: %s", exc)

    proc = await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} logcat -v time *:E",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    pending_crash = False
    pending_lines: List[str] = []
    crash_timestamp = ""
    MAX_LOOK_AHEAD = 15
    last_crash_time: Optional[datetime] = None
    last_anr_time: Optional[datetime] = None

    try:
        while not stop_event.is_set():
            try:
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if pending_crash:
                    pending_crash = False
                    pending_lines.clear()
                continue
            if not line_bytes:
                break

            line = line_bytes.decode(errors="ignore")

            if ANR_PATTERN.search(line):
                anr_pkg = ANR_PKG_PATTERN.search(line)
                if anr_pkg and package_name in anr_pkg.group(1):
                    now = datetime.now()
                    if last_anr_time and (now - last_anr_time).total_seconds() < DEDUP_COOLDOWN_SECONDS:
                        logger.debug(f"ANR 去重冷却中，忽略: {line.strip()[:120]}")
                        continue
                    last_anr_time = now
                    full_log = ""
                    if capture_log:
                        full_log = await _capture_logcat_snapshot(device_serial, kind="anr")
                    event_time = now.strftime("%H:%M:%S")
                    event = {
                        "time": event_time,
                        "type": "ANR",
                        "full_log": full_log,
                    }
                    if replay_callback:
                        try:
                            replay = await replay_callback("ANR", event_time)
                            if replay:
                                event["replay"] = replay
                        except Exception as exc:
                            event["replay"] = {
                                "status": "FAILED",
                                "error": str(exc),
                            }
                    crash_events.append(event)
                    await dispatch_event(event)
                    logger.warning(f"检测到 ANR ({package_name}): {line.strip()[:200]}")
                    if abort_on_crash and abort_event:
                        logger.warning(f"容错策略=立即停止，触发终止")
                        abort_event.set()
                        return
                continue

            if re.search(r"FATAL EXCEPTION", line, re.IGNORECASE):
                if not CRASH_SOURCE_TAG.search(line):
                    continue
                pending_crash = True
                pending_lines = [line]
                crash_timestamp = datetime.now().strftime("%H:%M:%S")
                continue

            if pending_crash:
                pending_lines.append(line)
                proc_match = PROC_LINE_PATTERN.search(line)
                if proc_match:
                    crash_pkg = proc_match.group(1).rstrip(",")
                    if package_name in crash_pkg:
                        now = datetime.now()
                        if last_crash_time and (now - last_crash_time).total_seconds() < DEDUP_COOLDOWN_SECONDS:
                            logger.debug(f"CRASH 去重冷却中，忽略: {pending_lines[0].strip()[:120]}")
                        else:
                            last_crash_time = now
                            full_log = ""
                            if capture_log:
                                full_log = await _capture_logcat_snapshot(device_serial, kind="crash")
                            event = {
                                "time": crash_timestamp,
                                "type": "CRASH",
                                "full_log": full_log,
                            }
                            if replay_callback:
                                try:
                                    replay = await replay_callback("CRASH", crash_timestamp)
                                    if replay:
                                        event["replay"] = replay
                                except Exception as exc:
                                    event["replay"] = {
                                        "status": "FAILED",
                                        "error": str(exc),
                                    }
                            crash_events.append(event)
                            await dispatch_event(event)
                            logger.warning(f"检测到 CRASH ({package_name}): {pending_lines[0].strip()[:200]}")
                            if abort_on_crash and abort_event:
                                logger.warning(f"容错策略=立即停止，触发终止")
                                abort_event.set()
                                pending_crash = False
                                pending_lines.clear()
                                return
                    else:
                        logger.debug(f"忽略非目标包 CRASH: {crash_pkg}")
                    pending_crash = False
                    pending_lines.clear()
                elif len(pending_lines) >= MAX_LOOK_AHEAD:
                    logger.debug(f"FATAL EXCEPTION 后 {MAX_LOOK_AHEAD} 行内未找到 Process 行，忽略")
                    pending_crash = False
                    pending_lines.clear()
    finally:
        proc.terminate()
        try:
            await proc.wait()
        except Exception:
            pass


async def _spawn_logcat(device_serial: str, args: List[str]):
    """启动一次 logcat 子进程：只管创建，收集与超时交给调用方。

    云端这份沿用 shell 字符串形态（同 backend/fastbot/adb.py）；桌面那份是
    exec 参数数组 + serial 白名单校验。除本函数外两份文件逐行一致。
    """
    return await asyncio.create_subprocess_shell(
        f"adb -s {device_serial} logcat {' '.join(args)}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _dump_logcat(device_serial: str, args: List[str]) -> str:
    """转储一段 logcat；启动失败/超时返回空串，由上层决定降级。

    单段失败不能拖垮整个快照，所以这里吞掉异常只记日志。
    """
    try:
        proc = await _spawn_logcat(device_serial, ["-d", *args])
    except Exception as exc:
        logger.warning("logcat 转储启动失败: serial=%s args=%s err=%s", device_serial, args, exc)
        return ""
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=LOGCAT_SNAPSHOT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _terminate_subprocess(proc)
        logger.warning("抓取 logcat 快照超时: serial=%s", device_serial)
        return ""
    except Exception as exc:
        await _terminate_subprocess(proc)
        logger.warning("logcat 转储失败: serial=%s args=%s err=%s", device_serial, args, exc)
        return ""
    return stdout.decode(errors="ignore")


def _truncate_utf8_tail(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节预算截断，保留**尾部**（最近的日志），不把多字节字符切成半个。

    必须留尾：上下文转储取的就是「最近 N 条」，事件时刻落在尾部附近，
    留头等于把刚沉淀下来的善后/上报日志裁掉，正好抵消 SNAPSHOT_POST_SETTLE_SEC。
    """
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    cut = len(encoded) - max_bytes
    if encoded[cut - 1:cut] != b"\n":
        # 切在了行中间：退到下一行行首，宁可少一行也不留残行污染锚点识别
        next_newline = encoded.find(b"\n", cut)
        if next_newline < 0:
            return ""
        cut = next_newline + 1
    return encoded[cut:].decode("utf-8", errors="ignore")


def _compose_snapshot(
    primary_text: str,
    context_text: str,
    primary_header: str,
    context_header: str,
) -> str:
    """主段（崩溃/ANR 正文）在前、上下文段在后拼接；超字节上限只裁上下文段。"""
    primary_part = f"{primary_header}\n{primary_text.rstrip()}\n"
    context_body = context_text.rstrip()
    if not context_body:
        return primary_part

    header_line = f"{context_header}\n"
    budget = SNAPSHOT_MAX_BYTES - len(primary_part.encode("utf-8"))
    if budget <= len(header_line.encode("utf-8")):
        # 正文自身就吃满预算：主段绝不裁，上下文段整段舍弃
        return primary_part
    budget -= len(header_line.encode("utf-8"))
    return primary_part + header_line + _truncate_utf8_tail(context_body + "\n", budget)


async def _capture_logcat_snapshot(device_serial: str, kind: str = "crash") -> str:
    """事件优先的 logcat 快照：正文主段在前、上下文段在后。

    三个坑（线上产物实测，OPPO Reno14 Pro 任务 43）：
    - 按行数截断遇上嘈杂的 system buffer，500 条只覆盖约 2.3 秒；
    - `logcat -d` 按 buffer 分组输出且 crash buffer 排最后，预算被 main/system 吃掉；
    - 事件瞬间就抓，进程善后/上报日志还没落进 ring buffer，事件后只剩 43ms。
    所以先等沉淀时间，再按正文 buffer 优先抓全，上下文补尾，超限只裁上下文。

    上下文预算要跨过事件时刻：沉淀 2 秒后再取「最近 N 条」，N 太小会只拿到事件之后那几秒，
    事件**之前**的上下文全丢（那正是定位触发页面的关键）。所以预算按秒级窗口取够，
    真正的落盘大小由 SNAPSHOT_MAX_BYTES 裁，裁的时候留尾 —— 留下的永远是紧邻事件的一段。

    kind="anr" 走另一条：ANR 正文在 system buffer（ActivityManager 的 ANR in / Reason /
    CPU 概览），crash buffer 里的旧崩溃跟本次无关，摆到前面会被 clean_log_for_ai 当锚点。
    """
    await asyncio.sleep(SNAPSHOT_POST_SETTLE_SEC)

    if kind == "anr":
        primary_args = ["-b", "system", "-t", str(SNAPSHOT_ANR_LINES)]
        context_args = ["-b", "main", "-t", str(SNAPSHOT_CONTEXT_LINES)]
        primary_header, context_header = SNAPSHOT_ANR_HEADER, SNAPSHOT_ANR_CONTEXT_HEADER
    else:
        primary_args = ["-b", "crash", "-t", str(SNAPSHOT_CRASH_LINES)]
        context_args = ["-b", "main,system", "-t", str(SNAPSHOT_CONTEXT_LINES)]
        primary_header, context_header = SNAPSHOT_CRASH_HEADER, SNAPSHOT_CONTEXT_HEADER

    primary_text = await _dump_logcat(device_serial, primary_args)
    if not primary_text.strip():
        # 老机型/无对应 buffer：退回旧行为，快照不能整体失败
        return await _dump_logcat(device_serial, ["-t", str(SNAPSHOT_FALLBACK_LINES)])

    context_text = await _dump_logcat(device_serial, context_args)
    return _compose_snapshot(primary_text, context_text, primary_header, context_header)
