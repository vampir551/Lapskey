"""Key injection.

Two backends are supported:

* ``uinput`` - a virtual keyboard created through /dev/uinput.  This is the
  most reliable option on Wayland: the kernel delivers the events to the
  compositor exactly like events from a real keyboard, so it works in native
  Wayland applications, XWayland and terminals alike.  Only write access to
  /dev/uinput is required.
* ``ydotool`` - used when /dev/uinput is not accessible.  Requires the
  ``ydotoold`` daemon.

Text is inserted with the key events of the *target layout*; the daemon makes
sure that layout is active first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable

from .mapping import KEY, KeyStroke
from .util import LapsKeyError, decode, run_command, which

log = logging.getLogger(__name__)

MODIFIERS = {
    "ctrl": KEY["LEFTCTRL"],
    "control": KEY["LEFTCTRL"],
    "shift": KEY["LEFTSHIFT"],
    "alt": KEY["LEFTALT"],
    "super": KEY["LEFTMETA"],
    "meta": KEY["LEFTMETA"],
    "win": KEY["LEFTMETA"],
}

NAMED_KEYS = {
    "insert": KEY["INSERT"],
    "delete": KEY["DELETE"],
    "backspace": KEY["BACKSPACE"],
    "space": KEY["SPACE"],
    "enter": KEY["ENTER"],
    "return": KEY["ENTER"],
    "tab": KEY["TAB"],
    "escape": KEY["ESC"],
    "esc": KEY["ESC"],
    "left": KEY["LEFT"],
    "right": KEY["RIGHT"],
    "up": KEY["UP"],
    "down": KEY["DOWN"],
    "home": KEY["HOME"],
    "end": KEY["END"],
    "pageup": KEY["PAGEUP"],
    "pagedown": KEY["PAGEDOWN"],
    "minus": KEY["MINUS"],
    "equal": KEY["EQUAL"],
    "slash": KEY["SLASH"],
    "comma": KEY["COMMA"],
    "dot": KEY["DOT"],
    "semicolon": KEY["SEMICOLON"],
    "apostrophe": KEY["APOSTROPHE"],
    "grave": KEY["GRAVE"],
    "leftbrace": KEY["LEFTBRACE"],
    "rightbrace": KEY["RIGHTBRACE"],
    "backslash": KEY["BACKSLASH"],
}
NAMED_KEYS.update({chr(c): KEY[chr(c).upper()] for c in range(ord("a"), ord("z") + 1)})
NAMED_KEYS.update({str(d): KEY[str(d)] for d in range(10)})

SHIFT = KEY["LEFTSHIFT"]
BACKSPACE = KEY["BACKSPACE"]


def parse_combo(combo: str) -> list[int]:
    """Parse ``ctrl+shift+c`` into a list of keycodes with the thick key last."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise LapsKeyError("пустая комбинация клавиш")
    key_name = parts[-1]
    if key_name not in NAMED_KEYS:
        raise LapsKeyError(f"неизвестная клавиша в комбинации: {key_name!r}")
    codes: list[int] = []
    for mod in parts[:-1]:
        if mod not in MODIFIERS:
            raise LapsKeyError(f"неизвестный модификатор: {mod!r}")
        codes.append(MODIFIERS[mod])
    codes.append(NAMED_KEYS[key_name])
    return codes


class UinputBackend:
    name = "uinput"

    def __init__(self, delay_ms: int = 2) -> None:
        from evdev import UInput, ecodes

        self._ecodes = ecodes
        self.delay = max(0, delay_ms) / 1000.0
        codes = sorted(set(KEY.values()) | {SHIFT, BACKSPACE})
        self.ui = UInput(
            events={ecodes.EV_KEY: codes},
            name="LapSKey virtual keyboard",
            version=1,
        )
        log.info("injector: uinput device %s", self.ui.device.path)

    def _write(self, code: int, value: int) -> None:
        self.ui.write(self._ecodes.EV_KEY, code, value)
        # uinput queues events until SYN_REPORT.  Without this call nothing is
        # delivered to libinput/KWin; the events pile up in the kernel buffer
        # and are processed much later in a burst (keyboard appears frozen and
        # libinput prints "event processing lagging behind").
        self.ui.syn()

    def _tap(self, code: int) -> None:
        self._write(code, 1)
        self._write(code, 0)
        if self.delay:
            time.sleep(self.delay)

    # -- sync implementations, called through to_thread ---------------------
    def key_combo_sync(self, combo: str) -> None:
        codes = parse_combo(combo)
        *mods, key = codes
        for code in mods:
            self._write(code, 1)
        time.sleep(0.002)
        self._tap(key)
        for code in reversed(mods):
            self._write(code, 0)
        time.sleep(0.002)

    def tap_sync(self, code: int) -> None:
        self._tap(code)

    def backspaces_sync(self, count: int) -> None:
        for _ in range(count):
            self._tap(BACKSPACE)

    def type_keys_sync(
        self,
        keys: list[KeyStroke],
        neutral: Callable[[], bool] | None = None,
    ) -> None:
        shift_down = False
        for stroke in keys:
            if neutral is not None:
                # If the user grabs Shift/Ctrl while we are retyping, pause:
                # otherwise the compositor applies that modifier to the rest of
                # the injected text ("привет, кАК ДЕЛА" out of nowhere).
                deadline = time.monotonic() + 2.0
                while not neutral():
                    if time.monotonic() > deadline:
                        raise LapsKeyError(
                            "модификатор зажат во время ввода — ввод остановлен"
                        )
                    time.sleep(0.01)
            if stroke.shift and not shift_down:
                self._write(SHIFT, 1)
                shift_down = True
                time.sleep(0.002)
            elif not stroke.shift and shift_down:
                self._write(SHIFT, 0)
                shift_down = False
                time.sleep(0.001)
            self._tap(stroke.code)
        if shift_down:
            self._write(SHIFT, 0)
            time.sleep(0.001)

    def close(self) -> None:
        try:
            self.ui.close()
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass


def _ydotool_socket() -> str | None:
    env = os.environ.get("YDOTOOL_SOCKET")
    if env:
        return env
    candidates = [
        Path(f"/run/user/{os.getuid()}/.ydotool_socket"),
        Path("/tmp/.ydotool_socket"),
        Path.home() / ".ydotool_socket",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


class YdotoolBackend:
    name = "ydotool"

    def __init__(self, binary: str = "ydotool", delay_ms: int = 2) -> None:
        exe = which(binary)
        if exe is None:
            raise LapsKeyError("ydotool не установлен")
        self.exe = exe
        self.delay = max(0, delay_ms) / 1000.0
        self.env: dict[str, str] = {}
        sock = _ydotool_socket()
        if sock:
            self.env["YDOTOOL_SOCKET"] = sock
        else:
            raise LapsKeyError(
                "ydotoold не найден: запустите `systemctl enable --now ydotoold`"
            )

    async def _key(self, events: list[str]) -> None:
        # Keep argv reasonably small for long texts.
        chunk = 400
        for i in range(0, len(events), chunk):
            rc, _out, err = await run_command(
                [self.exe, "key", *events[i : i + chunk]],
                timeout=5.0,
                env=self.env,
            )
            if rc != 0:
                raise LapsKeyError(f"ydotool: {decode(err) or rc}")
            if self.delay:
                await asyncio.sleep(self.delay)

    async def _press_release(self, codes: list[int]) -> None:
        events = [f"{code}:1" for code in codes] + [f"{code}:0" for code in reversed(codes)]
        await self._key(events)

    async def tap(self, code: int) -> None:
        await self._key([f"{code}:1", f"{code}:0"])

    async def key_combo(self, combo: str) -> None:
        await self._press_release(parse_combo(combo))

    async def backspaces(self, count: int) -> None:
        events: list[str] = []
        for _ in range(count):
            events.extend([f"{BACKSPACE}:1", f"{BACKSPACE}:0"])
        await self._key(events)

    async def type_keys(self, keys: list[KeyStroke]) -> None:
        events: list[str] = []
        shift_down = False
        for stroke in keys:
            if stroke.shift and not shift_down:
                events.append(f"{SHIFT}:1")
                shift_down = True
            elif not stroke.shift and shift_down:
                events.append(f"{SHIFT}:0")
                shift_down = False
            events.extend([f"{stroke.code}:1", f"{stroke.code}:0"])
        if shift_down:
            events.append(f"{SHIFT}:0")
        await self._key(events)

    def close(self) -> None:
        return None


class Injector:
    """Facade that exposes one async API for the chosen backend."""

    def __init__(self, backend: UinputBackend | YdotoolBackend) -> None:
        self.backend = backend
        self.name = backend.name

    async def tap(self, code: int) -> None:
        if isinstance(self.backend, UinputBackend):
            await asyncio.to_thread(self.backend.tap_sync, code)
        else:
            await self.backend.tap(code)

    async def key_combo(self, combo: str) -> None:
        if isinstance(self.backend, UinputBackend):
            await asyncio.to_thread(self.backend.key_combo_sync, combo)
        else:
            await self.backend.key_combo(combo)

    async def backspaces(self, count: int, inter_delay_ms: int = 2) -> None:
        if count <= 0:
            return
        if isinstance(self.backend, UinputBackend):
            old = self.backend.delay
            self.backend.delay = max(0, inter_delay_ms) / 1000.0
            try:
                await asyncio.to_thread(self.backend.backspaces_sync, count)
            finally:
                self.backend.delay = old
        else:
            await self.backend.backspaces(count)

    async def type_keys(
        self,
        keys: list[KeyStroke],
        neutral: Callable[[], bool] | None = None,
    ) -> None:
        if not keys:
            return
        if isinstance(self.backend, UinputBackend):
            await asyncio.to_thread(self.backend.type_keys_sync, keys, neutral)
        else:
            await self.backend.type_keys(keys)

    def close(self) -> None:
        self.backend.close()


def create_injector(delay_ms: int = 2, ydotool_binary: str = "ydotool") -> Injector:
    """Prefer uinput, fall back to ydotool, and explain the failure clearly."""
    errors: list[str] = []
    try:
        return Injector(UinputBackend(delay_ms))
    except PermissionError as exc:
        errors.append(f"/dev/uinput: {exc}")
    except ImportError as exc:
        errors.append(f"python-evdev: {exc}")
    except OSError as exc:
        errors.append(f"/dev/uinput: {exc}")

    try:
        return Injector(YdotoolBackend(ydotool_binary, delay_ms))
    except LapsKeyError as exc:
        errors.append(str(exc))

    raise LapsKeyError(
        "нет доступа к эмуляции клавиатуры. "
        "Проверьте права: группа input и /dev/uinput. Подробности: "
        + "; ".join(errors)
    )
