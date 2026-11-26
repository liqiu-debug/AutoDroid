"""Cancellation helpers that keep resources held until in-flight I/O exits."""
from __future__ import annotations

import asyncio
import contextlib
import time


class StartupCancelled(Exception):
    pass


class StartupInvalid(Exception):
    """The requested lifecycle state could not be verified before measurement."""


def check_cancel(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise StartupCancelled("启动测试已取消")


async def interruptible_sleep(seconds: float, stop_event=None) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        check_cancel(stop_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.1))


async def call_sync(function, *args, **kwargs):
    # Cancelling an asyncio wrapper does not stop a WDA/u2 HTTP request. Await
    # its bounded transport completion before the caller releases the device.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.shield(task)
        raise


async def wait_with_cancel(task: asyncio.Task, stop_event=None):
    try:
        while not task.done():
            check_cancel(stop_event)
            await asyncio.wait({task}, timeout=0.1)
        check_cancel(stop_event)
        return await task
    except (StartupCancelled, asyncio.CancelledError):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
