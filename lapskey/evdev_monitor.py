"""Read-only evdev monitor.

It does three jobs:

* keeps the last typed word as a sequence of physical key strokes (so the
  daemon can fix a word without touching the clipboard);
* detects that the user has just selected text (mouse drag, double click,
  Shift+arrows, Ctrl+A), so the selection can be read from PRIMARY;
* optionally provides the global hotkey and the automatic-conversion trigger.

Nothing is grabbed: the daemon only *observes* /dev/input.  Keys still reach
the focused application, which is why a normally unused hotkey is recommended.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import selectors
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .mapping import KEY, KeyStroke, key_to_char

log = logging.getLogger(__name__)

try:  # pragma: no cover - import depends on the host
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover
    evdev = None  # type: ignore[assignment]
    ecodes = None  # type: ignore[assignment]

SHIFT_CODES = {KEY["LEFTSHIFT"], KEY["RIGHTSHIFT"]}
CTRL_CODES = {KEY["LEFTCTRL"], KEY["RIGHTCTRL"]}
ALT_CODES = {KEY["LEFTALT"], KEY["RIGHTALT"]}
SUPER_CODES = {KEY["LEFTMETA"], KEY["RIGHTMETA"]}
MODIFIER_CODES = SHIFT_CODES | CTRL_CODES | ALT_CODES | SUPER_CODES

NAV_CODES = {
    KEY["LEFT"],
    KEY["RIGHT"],
    KEY["UP"],
    KEY["DOWN"],
    KEY["HOME"],
    KEY["END"],
    KEY["PAGEUP"],
    KEY["PAGEDOWN"],
}

BOUNDARY_CODES = {KEY["ENTER"], KEY["TAB"], KEY["ESC"]}
LETTER_CODES = {KEY[c] for c in "QWERTYUIOPASDFGHJKLZXCVBNM"}
FKEY_CODES = {KEY[f"F{i}"] for i in range(1, 13)}
MAX_WORD_LEN = 64

# BTN_LEFT .. BTN_TASK: mouse buttons are EV_KEY too, but they must be routed
# to the pointer logic even on devices that also expose a keyboard interface
# (many gaming mice/receivers do, and checking "keyboard first" used to hide
# all mouse selections from the daemon).
POINTER_BUTTONS: set[int] = (
    set(range(ecodes.BTN_LEFT, ecodes.BTN_TASK + 1)) if ecodes else set()
)


@dataclass
class BufferWord:
    keys: list[KeyStroke]
    separator: str = ""


class InputMonitor:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        devices: list[str] | None = None,
        exclude: list[str] | None = None,
        rescan_seconds: float = 10.0,
        hotkey_code: int | None = None,
        hotkey_ctrl: bool = False,
        hotkey_shift: bool = False,
        hotkey_alt: bool = False,
        auto_min_length: int = 3,
        on_hotkey: Callable[[], None] | None = None,
        on_word: Callable[[BufferWord], None] | None = None,
    ) -> None:
        self.loop = loop
        self.devices_filter = list(devices or [])
        self.exclude = [name.lower() for name in (exclude or [])]
        self.rescan_seconds = max(2.0, rescan_seconds)
        self.hotkey_code = hotkey_code
        self.hotkey_ctrl = hotkey_ctrl
        self.hotkey_shift = hotkey_shift
        self.hotkey_alt = hotkey_alt
        self.auto_min_length = auto_min_length
        self.on_hotkey = on_hotkey
        self.on_word = on_word

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._selector: selectors.BaseSelector | None = None
        self._devices: dict[str, tuple[object, str]] = {}
        self._pressed: set[int] = set()
        self._caps = False
        self._word: list[KeyStroke] = []
        self._word_overflow = False
        self._last_word: list[KeyStroke] = []
        self._last_word_time = 0.0
        self._selection_dirty = False
        self._mouse_down = False
        self._dragging = False
        self._last_click_time = 0.0
        self._event_seq = 0
        self._last_shift_fkey = 0.0
        self._last_key_event: tuple[tuple[int, int], float, int] | None = None
        self._error: str | None = None
        self._opened_keyboards = 0

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if evdev is None:
            self._error = "python-evdev не установлен"
            return False
        self._thread = threading.Thread(
            target=self._run, name="lapskey-evdev", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def available(self) -> bool:
        return self._opened_keyboards > 0

    @property
    def error(self) -> str | None:
        return self._error

    # ---------------------------------------------------------- thread main
    def _run(self) -> None:
        self._selector = selectors.DefaultSelector()
        next_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_scan:
                    self._scan()
                    next_scan = now + self.rescan_seconds
                try:
                    events = self._selector.select(timeout=0.4)
                except OSError:
                    events = []
                for key, _mask in events:
                    device, kind = key.data
                    try:
                        # read() is a generator in python-evdev, so the OSError
                        # for a removed device is raised while iterating.
                        for event in device.read():
                            try:
                                self._handle_event(device, kind, event)
                            except Exception:  # noqa: BLE001 - never kill the thread
                                log.exception("evdev event handling failed")
                    except OSError as exc:
                        log.info("input device lost: %s", exc)
                        self._drop(key.fileobj)  # type: ignore[arg-type]
                        continue
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        for path, (device, _kind) in list(self._devices.items()):
            with contextlib.suppress(Exception):
                self._selector.unregister(device.fd)  # type: ignore[union-attr]
            with contextlib.suppress(Exception):
                device.close()
            self._devices.pop(path, None)
        if self._selector is not None:
            with contextlib.suppress(Exception):
                self._selector.close()
        self._opened_keyboards = 0

    def _drop(self, fd: int) -> None:
        for path, (device, kind) in list(self._devices.items()):
            if device.fd == fd:
                with contextlib.suppress(Exception):
                    self._selector.unregister(fd)  # type: ignore[union-attr]
                with contextlib.suppress(Exception):
                    device.close()
                self._devices.pop(path, None)
                if kind == "keyboard":
                    self._opened_keyboards = max(0, self._opened_keyboards - 1)
                with self._lock:
                    # A device can vanish while a key is held; without this the
                    # daemon would wait forever for a modifier release.
                    self._pressed.clear()
                    self._word.clear()
                    self._selection_dirty = False
                return

    @staticmethod
    def _is_keyboard(device: object) -> bool:
        try:
            caps = device.capabilities()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
        keys = caps.get(ecodes.EV_KEY, [])  # type: ignore[union-attr]
        return KEY["A"] in keys and KEY["SPACE"] in keys

    @staticmethod
    def _is_pointer(device: object) -> bool:
        try:
            caps = device.capabilities()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
        keys = caps.get(ecodes.EV_KEY, [])  # type: ignore[union-attr]
        return ecodes.EV_REL in caps or 272 in keys  # 272 == BTN_LEFT  # type: ignore[union-attr]

    def _scan(self) -> None:
        try:
            paths = evdev.list_devices()
        except Exception as exc:  # noqa: BLE001
            self._error = f"не удалось получить список устройств: {exc}"
            return
        if self.devices_filter:
            wanted = set(self.devices_filter)
            paths = [
                path
                for path in paths
                if path in wanted or any(path.endswith("/" + w) for w in wanted)
            ]
        for path in paths:
            if path in self._devices:
                continue
            device = None
            try:
                device = evdev.InputDevice(path)
            except PermissionError:
                self._error = (
                    "нет доступа к /dev/input — добавьте пользователя в группу input"
                )
                continue
            except OSError as exc:
                log.debug("cannot open %s: %s", path, exc)
                continue
            name = getattr(device, "name", "") or ""
            low_name = name.lower()
            if any(part in low_name for part in self.exclude):
                device.close()
                continue
            is_keyboard = self._is_keyboard(device)
            is_pointer = self._is_pointer(device)
            if is_keyboard and is_pointer:
                kind = "both"
            elif is_keyboard:
                kind = "keyboard"
            elif is_pointer:
                kind = "pointer"
            else:
                device.close()
                continue
            try:
                self._selector.register(device.fd, selectors.EVENT_READ, (device, kind))  # type: ignore[union-attr]
            except (OSError, ValueError):
                device.close()
                continue
            self._devices[path] = (device, kind)
            if kind in ("keyboard", "both"):
                self._opened_keyboards += 1
                with contextlib.suppress(Exception):
                    if ecodes.LED_CAPSL in device.leds():  # type: ignore[union-attr]
                        self._caps = True
            log.info("monitoring %s (%s) %s", path, kind, name)

    # ----------------------------------------------------------- event logic
    def _handle_event(self, device: object, kind: str, event: object) -> None:
        etype = getattr(event, "type", None)
        code = getattr(event, "code", None)
        value = getattr(event, "value", None)
        if kind in ("pointer", "both"):
            is_pointer_event = etype == ecodes.EV_REL or (  # type: ignore[union-attr]
                etype == ecodes.EV_KEY and code in POINTER_BUTTONS  # type: ignore[union-attr]
            )
            if is_pointer_event:
                self._handle_pointer_event(etype, code, value)
                return
            if kind == "pointer":
                return
        if etype != ecodes.EV_KEY:  # type: ignore[union-attr]
            return
        # Many keyboards expose several event nodes (e.g. a gaming keyboard
        # with "input0" and "input1") that report the same key press twice.
        # Coalesce identical events that arrive almost simultaneously from
        # different devices; a human cannot press the same key twice that fast.
        now = time.monotonic()
        signature = (code, value)
        last = self._last_key_event
        if (
            last is not None
            and last[0] == signature
            and now - last[1] < 0.01
            and last[2] != id(device)
        ):
            return
        self._last_key_event = (signature, now, id(device))
        self._handle_key(code, value)

    def _handle_pointer_event(self, etype: int, code: int, value: int) -> None:
        left_button = ecodes.BTN_LEFT  # type: ignore[union-attr]
        if etype == ecodes.EV_KEY and code == left_button:  # type: ignore[union-attr]
            if value == 1:
                self._on_click()
            elif value == 0:
                with self._lock:
                    if self._dragging:
                        self._selection_dirty = True
                    self._mouse_down = False
                    self._dragging = False
            return
        if etype == ecodes.EV_KEY and code in POINTER_BUTTONS:  # type: ignore[union-attr]
            if value == 1:
                with self._lock:
                    self._word.clear()
                    self._word_overflow = False
                    self._last_word = []
                    self._selection_dirty = False
            return
        if etype == ecodes.EV_REL:
            if code in (ecodes.REL_X, ecodes.REL_Y) and value:  # type: ignore[union-attr]
                with self._lock:
                    if self._mouse_down and abs(value) > 1:
                        self._dragging = True

    def _on_click(self) -> None:
        now = time.monotonic()
        with self._lock:
            shift = any(code in self._pressed for code in SHIFT_CODES)
            double = now - self._last_click_time <= 0.4
            self._last_click_time = now
            self._word.clear()
            self._word_overflow = False
            self._last_word = []
            self._mouse_down = True
            self._dragging = False
            self._selection_dirty = bool(shift or double)

    def _handle_key(self, code: int, value: int) -> None:
        with self._lock:
            has_tracked_modifier = any(c in self._pressed for c in MODIFIER_CODES)
        if has_tracked_modifier and value == 1:
            # A missed modifier release would otherwise make every following
            # letter look like Ctrl+letter and silently clear the word buffer.
            self.modifiers_held()

        shift = any(c in self._pressed for c in SHIFT_CODES)
        ctrl = any(c in self._pressed for c in CTRL_CODES)
        alt = any(c in self._pressed for c in ALT_CODES)
        super_ = any(c in self._pressed for c in SUPER_CODES)

        if value == 2:  # autorepeat: the buffer already has the character
            return
        if value == 0:
            with self._lock:
                self._pressed.discard(code)
            if code in MODIFIER_CODES:
                log.debug("modifier released: %s", code)
            return

        # key press
        with self._lock:
            self._event_seq += 1

            # Check the hotkey before the modifier branch so that a modifier
            # (e.g. Right Ctrl) can be used as the hotkey too.
            if self.hotkey_code == code and not super_:
                if (
                    ctrl == self.hotkey_ctrl
                    and shift == self.hotkey_shift
                    and alt == self.hotkey_alt
                ):
                    log.debug("evdev hotkey pressed")
                    if code in MODIFIER_CODES:
                        self._pressed.add(code)
                    if self.on_hotkey is not None:
                        self.loop.call_soon_threadsafe(self.on_hotkey)
                    return

            if code in MODIFIER_CODES:
                self._pressed.add(code)
                log.debug("modifier pressed: %s", code)
                return
            self._pressed.add(code)

            if code == KEY["CAPSLOCK"]:
                self._caps = not self._caps
                return

            if code in NAV_CODES:
                self._word.clear()
                self._word_overflow = False
                self._last_word = []
                self._selection_dirty = bool(shift)
                return
            if code == KEY["A"] and ctrl:
                self._word.clear()
                self._selection_dirty = True
                return
            if code == KEY["SPACE"] and (ctrl or alt or super_):
                # Ctrl+Shift+Space is a very common "fix the word" hotkey,
                # Super+Space switches the KDE layout.  Neither types text, so
                # the word buffer must survive the shortcut.
                return
            if ctrl or alt or super_:
                self._word.clear()
                self._word_overflow = False
                self._selection_dirty = False
                return

            if code == KEY["SPACE"]:
                self._finish_word(separator=" ")
                self._selection_dirty = False
                return
            if code in BOUNDARY_CODES:
                self._finish_word(separator="")
                self._last_word = []
                self._selection_dirty = False
                return
            if code == KEY["BACKSPACE"]:
                if self._word:
                    self._word.pop()
                    self._word_overflow = False
                self._selection_dirty = False
                return
            if code == KEY["DELETE"] or code == KEY["INSERT"]:
                self._word.clear()
                self._word_overflow = False
                self._selection_dirty = False
                return
            if code in (
                KEY["SYSRQ"],
                KEY["PAUSE"],
                KEY["F1"], KEY["F2"], KEY["F3"], KEY["F4"], KEY["F5"],
                KEY["F6"], KEY["F7"], KEY["F8"], KEY["F9"], KEY["F10"],
                KEY["F11"], KEY["F12"],
            ):
                # Shift+F-key on the physical keyboard means "force".  The
                # KDE shortcut system may route Shift+F12 to the plain F12
                # action, so remember the gesture here instead.
                if code in FKEY_CODES and shift:
                    self._last_shift_fkey = time.monotonic()
                    log.debug("force gesture: Shift+F-key")
                # global hotkeys and function keys must not destroy the buffer
                return

            ch = key_to_char(code, shift)
            if ch is None:
                return
            effective_shift = shift
            if code in LETTER_CODES and self._caps:
                effective_shift = not effective_shift
            if len(self._word) < MAX_WORD_LEN:
                self._word.append(KeyStroke(code=code, shift=effective_shift))
            else:
                # A word this long cannot be deleted reliably any more: keep
                # the buffer unusable instead of corrupting the text.
                self._word_overflow = True
            self._selection_dirty = False
            self._last_word = []

    def _finish_word(self, separator: str) -> None:
        word = self._word
        overflow = self._word_overflow
        self._word = []
        self._word_overflow = False
        if not word:
            return
        if overflow:
            self._last_word = []
            return
        self._last_word = list(word)
        self._last_word_time = time.monotonic()
        # Automatic conversion is only safe on Space: on Enter/Tab the key has
        # already reached the application and the cursor moved.
        if separator != " " or self.on_word is None:
            return
        if len(word) < self.auto_min_length:
            return
        snapshot = BufferWord(keys=list(word), separator=separator)
        self.loop.call_soon_threadsafe(self.on_word, snapshot)

    # --------------------------------------------------------------- getters
    def snapshot_word(self) -> BufferWord | None:
        with self._lock:
            if self._word_overflow:
                return None
            if self._word:
                return BufferWord(keys=list(self._word), separator="")
            if self._last_word and time.monotonic() - self._last_word_time <= 3.0:
                return BufferWord(keys=list(self._last_word), separator=" ")
        return None

    def selection_possible(self) -> bool:
        with self._lock:
            return self._selection_dirty

    def force_gesture_recent(self, window: float = 2.0) -> bool:
        """True if Shift+F-key was pressed recently (the force gesture)."""
        with self._lock:
            return (
                self._last_shift_fkey > 0.0
                and time.monotonic() - self._last_shift_fkey <= window
            )

    def shift_held(self) -> bool:
        """Whether a physical Shift is down right now.

        This is checked as soon as the trigger request arrives, before the
        evdev thread necessarily processed the F12 press.  It makes
        Shift+F12 force conversion reliably, without a second F12 press.
        """
        with self._lock:
            return any(code in self._pressed for code in SHIFT_CODES)

    def pressed_modifier_names(self) -> list[str]:
        names = {
            KEY["LEFTCTRL"]: "Ctrl",
            KEY["RIGHTCTRL"]: "Ctrl",
            KEY["LEFTSHIFT"]: "Shift",
            KEY["RIGHTSHIFT"]: "Shift",
            KEY["LEFTALT"]: "Alt",
            KEY["RIGHTALT"]: "Alt",
            KEY["LEFTMETA"]: "Super",
            KEY["RIGHTMETA"]: "Super",
        }
        with self._lock:
            held = [code for code in self._pressed if code in MODIFIER_CODES]
        return sorted({names[code] for code in held if code in names})

    def modifiers_held(self) -> bool:
        """Whether a modifier is really held down.

        The kernel state is authoritative.  A missed release event (device
        hotplug, several event nodes per keyboard, a grabbed shortcut) would
        otherwise leave the daemon waiting forever and every following trigger
        would fail with "release Ctrl/Shift/Alt".
        """
        with self._lock:
            tracked = {code for code in self._pressed if code in MODIFIER_CODES}
            if not tracked:
                return False
            devices = list(self._devices.values())

        physical: set[int] = set()
        queried = False
        for device, kind in devices:
            if kind != "keyboard":
                continue
            try:
                physical.update(device.active_keys())  # type: ignore[attr-defined]
                queried = True
            except (OSError, AttributeError):
                continue

        if queried and not (physical & MODIFIER_CODES):
            with self._lock:
                self._pressed.difference_update(MODIFIER_CODES)
            return False
        return True

    def event_counter(self) -> int:
        with self._lock:
            return self._event_seq

    def reset(self) -> None:
        with self._lock:
            self._word.clear()
            self._word_overflow = False
            self._last_word = []
            self._selection_dirty = False
