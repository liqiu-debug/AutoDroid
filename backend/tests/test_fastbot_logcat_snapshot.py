"""事件日志快照（_capture_logcat_snapshot）行为测试。

两仓各一份且逐行一致（仅 import 不同）。覆盖：正文主段在前、上下文段在后；
超字节上限只裁上下文段且**留尾**（保住事件后刚沉淀的日志）；ANR 走 system buffer
而不是 crash buffer；无对应 buffer 退回 `logcat -d -t 500`；抓取前先等沉淀时间。
adb 与沉淀等待全程 stub，不连真设备、不联网。
"""
import asyncio
from unittest.mock import patch

from backend.fastbot import logcat as logcat_mod

CRASH_TEXT = (
    "04-02 17:26:34.454 E/AndroidRuntime(1234): FATAL EXCEPTION: main\n"
    "04-02 17:26:34.454 E/AndroidRuntime(1234): Process: com.example.app, PID: 1234\n"
    "04-02 17:26:34.455 E/AndroidRuntime(1234): java.lang.NullPointerException: boom\n"
)
ANR_TEXT = (
    "04-02 17:44:36.100 E/ActivityManager(500): ANR in com.example.app\n"
    "04-02 17:44:36.101 E/ActivityManager(500): Reason: Input dispatching timed out\n"
)
CONTEXT_TEXT = (
    "04-02 17:26:32.173 E/OplusExceptionHelper(999): vendor noise\n"
    "04-02 17:26:34.480 I/BizReport(1234): cleanup after crash\n"
)


class _FakeProc:
    """logcat 转储替身：communicate 直接吐出预置文本。"""

    def __init__(self, stdout: str = ""):
        self._stdout = stdout.encode("utf-8")
        self.returncode = None

    async def communicate(self):
        return self._stdout, b""

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode or 0


class _FakeAdb:
    """按调用顺序分发转储结果，并记下每次 logcat 的参数。"""

    def __init__(self, results, timeline):
        self._results = list(results)
        self._timeline = timeline
        self.calls = []

    async def __call__(self, device_serial, args):
        self.calls.append(list(args))
        self._timeline.append(("dump", list(args)))
        item = self._results.pop(0) if self._results else ""
        if isinstance(item, BaseException):
            raise item
        return _FakeProc(item)


def _capture(results, kind="crash"):
    """跑一次快照抓取：stub 掉 adb 与沉淀等待。

    返回 (快照文本, 每次 logcat 的参数, 事件时间线)。
    """
    timeline: list = []
    adb = _FakeAdb(results, timeline)

    async def _fake_sleep(delay, *args, **kwargs):
        timeline.append(("sleep", delay))

    with patch.object(logcat_mod, "_spawn_logcat", new=adb), patch(
        "asyncio.sleep", new=_fake_sleep
    ):
        text = asyncio.run(logcat_mod._capture_logcat_snapshot("device-1", kind=kind))
    return text, adb.calls, timeline


def test_crash_section_first_and_context_after():
    """崩溃正文在前、上下文在后，中间有明显分隔，且 crash buffer 单独给足预算。"""
    text, calls, _ = _capture([CRASH_TEXT, CONTEXT_TEXT])

    assert calls == [
        ["-d", "-b", "crash", "-t", str(logcat_mod.SNAPSHOT_CRASH_LINES)],
        ["-d", "-b", "main,system", "-t", str(logcat_mod.SNAPSHOT_CONTEXT_LINES)],
    ]
    # crash 正文原样留在最前面
    assert text.startswith(
        f"{logcat_mod.SNAPSHOT_CRASH_HEADER}\n{CRASH_TEXT.rstrip()}\n"
    )
    assert f"\n{logcat_mod.SNAPSHOT_CONTEXT_HEADER}\n" in text
    assert text.index("FATAL EXCEPTION") < text.index("cleanup after crash")
    assert CONTEXT_TEXT.strip().splitlines()[-1] in text


def test_over_budget_truncates_context_only_and_keeps_the_tail():
    """超字节上限时只裁上下文段且**留尾**：crash 段一字不动，最近的日志必须留住。

    留头是错的：上下文转储取的就是「最近 N 条」，事件时刻在尾部附近，
    留头会把刚沉淀下来的善后日志裁掉，正好抵消 SNAPSHOT_POST_SETTLE_SEC。
    """
    context = "".join(f"{i:06d} noisy system line\n" for i in range(20_000))
    assert len(context.encode("utf-8")) > logcat_mod.SNAPSHOT_MAX_BYTES

    text, _, _ = _capture([CRASH_TEXT, context])

    assert text.startswith(
        f"{logcat_mod.SNAPSHOT_CRASH_HEADER}\n{CRASH_TEXT.rstrip()}\n"
    )
    assert len(text.encode("utf-8")) <= logcat_mod.SNAPSHOT_MAX_BYTES
    assert "noisy system line" in text
    # 尾部（最近）那行在，头部（最旧）那行被裁掉
    assert context.strip().splitlines()[-1] in text
    assert context.strip().splitlines()[0] not in text


def test_over_budget_keeps_crash_section_intact():
    """崩溃正文自身就吃满预算时：只丢上下文段，绝不裁 crash 段。"""
    with patch.object(logcat_mod, "SNAPSHOT_MAX_BYTES", 16):
        text, _, _ = _capture([CRASH_TEXT, CONTEXT_TEXT])

    assert text == f"{logcat_mod.SNAPSHOT_CRASH_HEADER}\n{CRASH_TEXT.rstrip()}\n"


def test_falls_back_to_plain_dump_when_crash_buffer_empty():
    """老机型/无 crash buffer：退回 `logcat -d -t 500`，快照不整体失败。"""
    text, calls, _ = _capture(["", "old style dump\n"])

    assert text == "old style dump\n"
    assert calls == [
        ["-d", "-b", "crash", "-t", str(logcat_mod.SNAPSHOT_CRASH_LINES)],
        ["-d", "-t", str(logcat_mod.SNAPSHOT_FALLBACK_LINES)],
    ]


def test_falls_back_to_plain_dump_when_crash_dump_fails():
    """crash buffer 转储抛错（adb 不认 -b crash）时同样降级。"""
    text, calls, _ = _capture([RuntimeError("unsupported buffer"), "old style dump\n"])

    assert text == "old style dump\n"
    assert calls[1] == ["-d", "-t", str(logcat_mod.SNAPSHOT_FALLBACK_LINES)]


def test_anr_uses_system_buffer_not_stale_crash_buffer():
    """ANR 正文在 system buffer；crash buffer 里的旧崩溃跟本次无关，不能当主段。

    摆到前面会被 clean_log_for_ai 当锚点，把真正的 ANR 内容挤掉。
    """
    text, calls, _ = _capture([ANR_TEXT, "main noise\n"], kind="anr")

    assert calls == [
        ["-d", "-b", "system", "-t", str(logcat_mod.SNAPSHOT_ANR_LINES)],
        ["-d", "-b", "main", "-t", str(logcat_mod.SNAPSHOT_CONTEXT_LINES)],
    ]
    assert text.startswith(f"{logcat_mod.SNAPSHOT_ANR_HEADER}\n{ANR_TEXT.rstrip()}\n")
    assert f"\n{logcat_mod.SNAPSHOT_ANR_CONTEXT_HEADER}\n" in text
    assert logcat_mod.SNAPSHOT_CRASH_HEADER not in text


def test_anr_falls_back_when_system_buffer_empty():
    """ANR 主段为空时同样退回整段转储。"""
    text, calls, _ = _capture(["", "old style dump\n"], kind="anr")

    assert text == "old style dump\n"
    assert calls[1] == ["-d", "-t", str(logcat_mod.SNAPSHOT_FALLBACK_LINES)]


def test_waits_for_post_settle_before_dumping():
    """抓快照前先等事件后沉淀时间，否则善后/上报日志落不进 ring buffer。"""
    _, _, timeline = _capture([CRASH_TEXT, CONTEXT_TEXT])

    assert timeline[0] == ("sleep", logcat_mod.SNAPSHOT_POST_SETTLE_SEC)
    assert logcat_mod.SNAPSHOT_POST_SETTLE_SEC == 2.0
    dumps = [entry[1] for entry in timeline if entry[0] == "dump"]
    assert dumps[0] == ["-d", "-b", "crash", "-t", str(logcat_mod.SNAPSHOT_CRASH_LINES)]


def test_empty_snapshot_stays_empty():
    """两段都为空时保持旧行为：full_log 为空串。"""
    text, _, _ = _capture(["", ""])

    assert text == ""


def test_truncation_never_splits_multibyte_chars():
    """按字节裁上下文时不能切出半个字符，也不能留半行。"""
    # 每行 5 个中文字符 = 15 字节 + 换行 = 16 字节；三行共 48 字节
    context = "中文第一行\n中文第二行\n中文第三行\n"
    crash_part = f"{logcat_mod.SNAPSHOT_CRASH_HEADER}\n{CRASH_TEXT.rstrip()}\n"
    context_header = f"{logcat_mod.SNAPSHOT_CONTEXT_HEADER}\n"
    budget = len(crash_part.encode("utf-8")) + len(context_header.encode("utf-8")) + 33

    with patch.object(logcat_mod, "SNAPSHOT_MAX_BYTES", budget):
        text, _, _ = _capture([CRASH_TEXT, context])

    assert "�" not in text
    # 33 字节预算只能留最后两行；切在行中间要退到下一行行首，不留残行
    assert text.endswith("中文第二行\n中文第三行\n")
    assert "中文第一行" not in text


def test_spawn_logcat_command_carries_buffer_and_line_budget():
    """adb 命令行本身要带全 buffer 选择与行数预算（exec 参数数组 / shell 字符串都锁）。"""
    recorded: dict = {}

    async def _fake_exec(*argv, **kwargs):
        recorded["argv"] = list(argv)

    async def _fake_shell(cmd, **kwargs):
        recorded["cmd"] = cmd

    with patch("asyncio.create_subprocess_exec", new=_fake_exec), patch(
        "asyncio.create_subprocess_shell", new=_fake_shell
    ), patch.object(
        logcat_mod, "resolve_adb_binary", new=lambda: "/fake/adb", create=True
    ):
        asyncio.run(logcat_mod._spawn_logcat("device-1", ["-d", "-b", "crash", "-t", "300"]))

    tokens = recorded.get("argv") or recorded["cmd"].split()
    assert tokens[tokens.index("-s") + 1] == "device-1"
    assert "logcat" in tokens
    assert tokens[tokens.index("logcat") + 1:] == ["-d", "-b", "crash", "-t", "300"]
