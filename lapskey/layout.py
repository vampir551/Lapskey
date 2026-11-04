"""KDE Plasma keyboard layout control.

Plasma (both 5 and 6) exposes the layouts on the session bus:

    service   org.kde.keyboard
    path      /Layouts
    interface org.kde.KeyboardLayouts

We use ``gdbus`` from glib2 instead of a DBus python binding so the daemon has
one dependency less.  Everything fails softly: if the service is missing, the
tool falls back to clipboard insertion.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from dataclasses import dataclass

from .util import LapsKeyError, decode, run_command, which

log = logging.getLogger(__name__)

DBUS_ARGS = [
    "--session",
    "--dest",
    "org.kde.keyboard",
    "--object-path",
    "/Layouts",
    "--method",
]


@dataclass(frozen=True)
class LayoutInfo:
    index: int
    code: str
    name: str
    lang: str | None


def layout_lang(code: str, name: str) -> str | None:
    low_code = (code or "").lower()
    low_name = (name or "").lower()
    if low_code.startswith("ru") or "рус" in low_name or "russ" in low_name:
        return "ru"
    if low_code.startswith(("us", "gb", "en")) or "англ" in low_name or "english" in low_name:
        return "en"
    return None


class KdeLayouts:
    def __init__(self, switch_timeout_ms: int = 500) -> None:
        self.timeout = max(100, switch_timeout_ms) / 1000.0
        self.gdbus = which("gdbus")
        if self.gdbus is None:
            log.warning("gdbus not found: layout switching disabled")

    def available(self) -> bool:
        return self.gdbus is not None

    async def _call(self, method: str, *args: str, timeout: float = 2.0) -> str | None:
        if self.gdbus is None:
            return None
        try:
            rc, out, err = await run_command(
                [self.gdbus, "call", *DBUS_ARGS, method, *args], timeout=timeout
            )
        except LapsKeyError as exc:
            log.debug("layout dbus call failed: %s", exc)
            return None
        if rc != 0:
            log.debug("layout dbus call %s failed: %s", method, decode(err))
            return None
        return decode(out)

    async def layouts(self) -> list[LayoutInfo]:
        raw = await self._call("org.kde.KeyboardLayouts.getLayoutsList")
        if not raw:
            return []
        items: list[tuple[str, str, str]] = []
        try:
            value = ast.literal_eval(raw)
            if isinstance(value, tuple) and value and isinstance(value[0], (list, tuple)):
                items = [tuple(str(x) for x in row) for row in value[0]]
        except (ValueError, SyntaxError):
            pass
        if not items:
            for match in re.finditer(
                r"\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)", raw
            ):
                items.append(match.groups())
        result = []
        for index, (code, _variant, name) in enumerate(items):
            result.append(
                LayoutInfo(
                    index=index,
                    code=code,
                    name=name,
                    lang=layout_lang(code, name),
                )
            )
        return result

    async def current_index(self) -> int | None:
        raw = await self._call("org.kde.KeyboardLayouts.getLayout")
        if not raw:
            return None
        match = re.search(r"uint32\s+(\d+)", raw)
        if match:
            return int(match.group(1))
        # qdbus style output is just the number
        try:
            return int(raw.strip().strip("()").strip())
        except ValueError:
            return None

    async def current(self) -> LayoutInfo | None:
        index = await self.current_index()
        if index is None:
            return None
        for layout in await self.layouts():
            if layout.index == index:
                return layout
        return None

    async def ensure_lang(self, lang: str) -> bool:
        """Make sure the active layout is ``lang``; return success."""
        current = await self.current()
        if current is not None and current.lang == lang:
            return True
        layouts = await self.layouts()
        target = next((layout for layout in layouts if layout.lang == lang), None)
        if target is None:
            log.info("layout for %s is not configured", lang)
            return False
        raw = await self._call("org.kde.KeyboardLayouts.setLayout", str(target.index))
        if raw is not None and "false" in raw.lower():
            log.warning("setLayout(%d) was rejected", target.index)
            return False

        deadline = asyncio.get_running_loop().time() + self.timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
            if await self.current_index() == target.index:
                log.debug("layout switched to %s (%d)", lang, target.index)
                return True
        log.warning("layout did not switch to %s in %.0f ms", lang, self.timeout * 1000)
        return False
