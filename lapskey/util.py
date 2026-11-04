"""Small helpers shared by the modules."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


class LapsKeyError(RuntimeError):
    """A user-visible problem (bad permissions, missing tool, ...)."""


def which(command: str) -> str | None:
    if os.path.sep in command:
        path = Path(command)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(command)


async def run_command(
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 3.0,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a command asynchronously, never raising on non-zero exit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        raise LapsKeyError(f"команда не найдена: {args[0]}") from exc
    except OSError as exc:
        raise LapsKeyError(f"не удалось запустить {args[0]}: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_bytes), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise LapsKeyError(f"таймаут команды: {' '.join(args)}")
    return proc.returncode, stdout, stderr


def decode(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()
