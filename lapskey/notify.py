"""Desktop notifications (best effort, never fatal)."""

from __future__ import annotations

import asyncio
import logging

from .util import LapsKeyError, which

log = logging.getLogger(__name__)


async def notify(
    title: str,
    body: str = "",
    *,
    urgency: str = "normal",
    timeout_ms: int = 2500,
) -> None:
    if not which("notify-send"):
        log.debug("notify-send not available: %s / %s", title, body)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send",
            "-a",
            "LapSKey",
            "-u",
            urgency,
            "-t",
            str(timeout_ms),
            # Replace the previous LapSKey notification instead of stacking them.
            "-h",
            "string:x-canonical-private-synchronous:lapskey",
            title,
            body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (LapsKeyError, asyncio.TimeoutError, OSError) as exc:
        log.debug("notification failed: %s", exc)
