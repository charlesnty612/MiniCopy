"""Async task poller with progress callback."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from .client import MiniMaxClient
from .errors import TaskError

log = logging.getLogger(__name__)

# Status values from the MiniMax public API
STATUS_PENDING = "Pending"
STATUS_QUEUED = "Queuing"
STATUS_RUNNING = "Processing"
STATUS_SUCCEEDED = "Success"
STATUS_FAILED = "Failed"
STATUS_CANCELLED = "Cancelled"
STATUS_EXPIRED = "Expired"
TERMINAL_STATES = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}

ProgressCb = Callable[[str], Awaitable[None] | None]


async def poll_until_done(
    client: MiniMaxClient,
    task_id: str,
    *,
    interval_seconds: float = 30.0,
    max_wait_seconds: float = 7200.0,  # 2h
    on_progress: Optional[ProgressCb] = None,
) -> dict:
    """Poll a task until terminal. Returns the final task object.

    Raises TaskError on terminal failure.
    """
    elapsed = 0.0
    last_status = ""
    while elapsed < max_wait_seconds:
        task = await client.query_video_task(task_id)
        status = task.get("status", "")
        if status != last_status:
            log.info("task %s: %s", task_id, status)
            if on_progress is not None:
                ret = on_progress(status)
                if asyncio.iscoroutine(ret):
                    await ret
            last_status = status

        if status == STATUS_SUCCEEDED:
            return task
        if status in (STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED):
            err = task.get("error") or task.get("base_resp") or task
            raise TaskError(f"task {task_id} ended in {status}: {err}")

        await asyncio.sleep(interval_seconds)
        elapsed += interval_seconds

    raise TaskError(f"task {task_id} timed out after {max_wait_seconds:.0f}s")
