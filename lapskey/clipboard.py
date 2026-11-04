"""Wayland clipboard access through wl-clipboard.

We only *read* the PRIMARY selection in the normal path, so the user's
clipboard is not touched.  The copy-probe fallback (Ctrl+Insert) and the
clipboard insertion fallback do touch it; in that case the text clipboard is
saved and restored on a best-effort basis.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .util import LapsKeyError, decode, run_command, which

log = logging.getLogger(__name__)

TEXT_TYPES = (
    "text/plain;charset=utf-8",
    "UTF8_STRING",
    "text/plain",
    "STRING",
    "TEXT",
)


@dataclass
class ClipboardSnapshot:
    mime: str
    data: bytes


class Clipboard:
    def __init__(self, wl_paste: str = "wl-paste", wl_copy: str = "wl-copy") -> None:
        self.wl_paste = wl_paste
        self.wl_copy = wl_copy
        self.last_error: str | None = None

    def paste_available(self) -> bool:
        return which(self.wl_paste) is not None

    def copy_available(self) -> bool:
        return which(self.wl_copy) is not None

    def available(self) -> bool:
        return self.paste_available() and self.copy_available()

    async def _paste(self, *, primary: bool = False) -> str | None:
        if not self.paste_available():
            return None
        args = [self.wl_paste]
        if primary:
            args.append("--primary")
        args.append("--no-newline")
        try:
            rc, out, err = await run_command(args, timeout=2.0)
        except LapsKeyError as exc:
            self.last_error = str(exc)
            return None
        if rc != 0:
            self.last_error = decode(err) or f"wl-paste exited with {rc}"
            return None
        return out.decode("utf-8", "replace")

    async def read_primary(self) -> str | None:
        text = await self._paste(primary=True)
        if text is None:
            return None
        text = text.strip("\x00")
        return text if text.strip() else None

    async def read_text(self) -> str | None:
        text = await self._paste(primary=False)
        return text if text and text.strip() else None

    async def list_types(self, *, primary: bool = False) -> list[str]:
        if not self.paste_available():
            return []
        args = [self.wl_paste, "--list-types"]
        if primary:
            args.append("--primary")
        try:
            rc, out, _err = await run_command(args, timeout=2.0)
        except LapsKeyError:
            return []
        if rc != 0:
            return []
        return [line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip()]

    async def snapshot(self) -> ClipboardSnapshot | None:
        """Save the best text representation of the current clipboard."""
        if not self.paste_available():
            return None
        types = await self.list_types()
        if not types:
            return None
        mime = next((t for t in TEXT_TYPES if t in types), None)
        if mime is None:
            return None
        try:
            rc, out, _err = await run_command(
                [self.wl_paste, "--type", mime, "--no-newline"], timeout=2.0
            )
        except LapsKeyError:
            return None
        if rc != 0:
            return None
        return ClipboardSnapshot(mime=mime, data=out)

    async def _copy_raw(self, data: bytes, mime: str | None = None) -> bool:
        """Run wl-copy without capturing its pipes.

        wl-copy forks a child that keeps serving the clipboard.  That child
        inherits stdout/stderr, so ``communicate()`` would wait for EOF until
        the timeout.  Redirecting both to /dev/null makes the parent exit
        cleanly while the daemonised child continues to serve the selection.
        """
        if not self.copy_available():
            return False
        args = [self.wl_copy]
        if mime:
            args += ["--type", mime]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as exc:
            log.warning("cannot start wl-copy: %s", exc)
            return False
        try:
            await asyncio.wait_for(proc.communicate(data), timeout=2.5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("wl-copy did not fork in time")
            return False
        if proc.returncode != 0:
            log.warning("wl-copy exited with %s", proc.returncode)
            return False
        return True

    async def restore(self, snapshot: ClipboardSnapshot | None) -> None:
        if snapshot is None:
            return
        if not await self._copy_raw(snapshot.data, snapshot.mime):
            log.warning("cannot restore clipboard")

    async def set_text(self, text: str) -> bool:
        return await self._copy_raw(text.encode("utf-8"))

    async def wait_for_change(
        self,
        previous: str | None,
        timeout_ms: int = 350,
    ) -> str | None:
        """Poll the clipboard after a copy keypress."""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.03)
            text = await self.read_text()
            if text is not None and text != previous:
                return text
        return None
