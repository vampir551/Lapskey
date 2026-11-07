"""The daemon: socket server, trigger pipeline and auto mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import mapping
from .clipboard import TEXT_TYPES, Clipboard, ClipboardSnapshot
from .config import Config
from .detect import Detector
from .evdev_monitor import BufferWord, InputMonitor
from .injector import Injector, create_injector
from .layout import KdeLayouts
from .mapping import KEY
from .notify import notify
from .util import LapsKeyError

log = logging.getLogger(__name__)


@dataclass
class Acquisition:
    text: str
    origin: str  # "selection" | "buffer"
    delete_count: int = 0
    suffix: str = ""
    created_selection: bool = False


@dataclass
class InsertionPlan:
    kind: str  # "keys" | "clipboard"
    keys: list | None = None
    snapshot: ClipboardSnapshot | None = None
    layout_ok: bool = False


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.detector = Detector(
            extra_files=cfg.dictionaries.files,
            auto_detect=cfg.dictionaries.auto_detect,
        )
        self.clipboard = Clipboard(cfg.paths.wl_paste, cfg.paths.wl_copy)
        self.layouts = KdeLayouts(cfg.fix.layout_switch_timeout_ms)
        self.injector: Injector | None = None
        self.injector_error: str | None = None
        self.monitor: InputMonitor | None = None
        self._busy = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._last_noop = 0.0
        self._last_noop_notify = 0.0
        self._last_error_notify = 0.0
        self._last_trigger_time = 0.0
        self._last_force_time = 0.0
        self._server: asyncio.AbstractServer | None = None
        self._stop_event = asyncio.Event()
        self._started_at = time.time()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        for warning in self.cfg.warnings:
            log.warning("%s", warning)
        for warning in mapping.apply_custom_pairs(self.cfg.mapping.pairs):
            log.warning("%s", warning)

        await asyncio.to_thread(self.detector.load_dictionaries)

        try:
            self.injector = await asyncio.to_thread(
                create_injector,
                self.cfg.fix.inter_key_delay_ms,
                self.cfg.paths.ydotool,
            )
            log.info("injector ready: %s", self.injector.name)
        except LapsKeyError as exc:
            self.injector_error = str(exc)
            log.error("key injection unavailable: %s", exc)

        hotkey_code = None
        if self.cfg.hotkey.mode in ("evdev", "both"):
            hotkey_code = KEY.get(self.cfg.hotkey.evdev_key)
            if hotkey_code is None:
                log.error("unknown evdev_key=%r", self.cfg.hotkey.evdev_key)

        monitor_exclude = list(self.cfg.keyboard.exclude)
        if self.injector is not None:
            monitor_exclude.extend(self.injector.exclude_names)

        self.monitor = InputMonitor(
            asyncio.get_running_loop(),
            devices=self.cfg.keyboard.devices,
            exclude=monitor_exclude,
            rescan_seconds=self.cfg.keyboard.rescan_seconds,
            hotkey_code=hotkey_code,
            hotkey_ctrl=self.cfg.hotkey.evdev_ctrl,
            hotkey_shift=self.cfg.hotkey.evdev_shift,
            hotkey_alt=self.cfg.hotkey.evdev_alt,
            auto_min_length=self.cfg.auto.min_word_length,
            on_hotkey=self._on_hotkey,
            on_word=self._on_word,
        )
        if not self.monitor.start():
            log.error("input monitor unavailable: %s", self.monitor.error)
        elif not self.monitor.available():
            await asyncio.sleep(0.2)
            if not self.monitor.available():
                log.warning(
                    "no readable keyboards (/dev/input). Selection and buffer "
                    "acquisition will be limited: %s",
                    self.monitor.error or "permission denied",
                )

        self._install_signals()
        await self._start_server()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            loop.add_signal_handler(signal.SIGHUP, self._schedule_reload)
        except (NotImplementedError, RuntimeError):
            pass

    def _schedule_reload(self) -> None:
        self._spawn(self._reload())

    async def _reload(self) -> None:
        log.info("reloading configuration")
        new_cfg = await asyncio.to_thread(config_mod.load, None)
        for warning in new_cfg.warnings:
            log.warning("%s", warning)
        # Hotkey and device settings need a restart; the rest can be updated.
        new_cfg.hotkey = self.cfg.hotkey
        new_cfg.keyboard = self.cfg.keyboard
        self.cfg = new_cfg
        self.detector = await asyncio.to_thread(
            Detector,
            new_cfg.dictionaries.files,
            new_cfg.dictionaries.auto_detect,
        )
        await asyncio.to_thread(self.detector.load_dictionaries)
        self.layouts.timeout = max(100, new_cfg.fix.layout_switch_timeout_ms) / 1000.0
        log.info("configuration reloaded (hotkey/device changes need a restart)")

    async def _start_server(self) -> None:
        path = config_mod.socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if await self._probe_socket(path):
                raise LapsKeyError(f"демон уже запущен (сокет {path})")
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(path)
        )
        os.chmod(path, 0o600)
        log.info("listening on %s", path)

    @staticmethod
    async def _probe_socket(path: Path) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=0.5
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        log.info("shutting down")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        if self.monitor is not None:
            await asyncio.to_thread(self.monitor.stop)
        if self.injector is not None:
            self.injector.close()
        with contextlib.suppress(OSError):
            config_mod.socket_path().unlink()

    # ------------------------------------------------------------- callbacks
    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_hotkey(self) -> None:
        self._spawn(self.trigger(source="hotkey"))

    def _on_word(self, word: BufferWord) -> None:
        if not self.cfg.auto.enabled:
            return
        self._spawn(self._auto_fix(word))

    async def _auto_fix(self, word: BufferWord) -> None:
        seq = self.monitor.event_counter() if self.monitor else 0
        await asyncio.sleep(self.cfg.auto.delay_ms / 1000.0)
        if self.monitor is None or self.monitor.event_counter() != seq:
            log.debug("auto: user kept typing, skipped")
            return
        if self.monitor.modifiers_held() or self._busy.locked():
            return
        layout = await self.layouts.current()
        lang = layout.lang if layout else None
        if lang not in ("ru", "en"):
            log.debug("auto: layout is not ru/en, skipped")
            return
        text = mapping.physical_to_text(word.keys, lang)
        if not text or not text.strip():
            return
        decision = self.detector.convert_text(
            text,
            threshold=self.cfg.auto.score_threshold,
            source_lang=lang,
        )
        if not decision.changed or decision.target is None:
            return
        if len(decision.converted) != len(text):
            return
        delete_count = len(word.keys) + (1 if word.separator else 0)

        async with self._busy:
            if self.monitor.event_counter() != seq:
                log.debug("auto: input appeared during the delay, skipped")
                return
            try:
                result = await self._apply(
                    converted=decision.converted,
                    target=decision.target,
                    delete_count=delete_count,
                    suffix=word.separator,
                    origin="buffer",
                    allow_clipboard=False,
                )
                log.info("auto-fixed %r -> %r (%s)", text, decision.converted, result["insert"])
            except LapsKeyError as exc:
                log.warning("auto fix failed: %s", exc)
            finally:
                self.monitor.reset()

    # ---------------------------------------------------------------- socket
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=3.0)
            if not raw.strip():
                # A liveness probe connects and closes without a request.
                return
            try:
                request = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                response = {"ok": False, "error": "invalid JSON"}
            else:
                if not isinstance(request, dict):
                    response = {"ok": False, "error": "request must be an object"}
                else:
                    response = await self._dispatch(request)
        except asyncio.TimeoutError:
            response = {"ok": False, "error": "timeout"}
        except Exception as exc:  # noqa: BLE001 - the server must stay alive
            log.exception("request failed")
            response = {"ok": False, "error": str(exc)}
        try:
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _dispatch(self, request: dict) -> dict:
        command = request.get("cmd", "trigger")
        if command == "trigger":
            log.info(
                "trigger request: force=%s selection=%s source=%s",
                bool(request.get("force")),
                bool(request.get("selection")),
                request.get("source"),
            )
            return await self.trigger(
                force=bool(request.get("force")),
                selection=bool(request.get("selection")),
                source=str(request.get("source", "cli")),
            )
        if command == "status":
            return await self.status()
        if command == "reload":
            await self._reload()
            return {"ok": True, "message": "configuration reloaded"}
        if command == "stop":
            self._stop_event.set()
            return {"ok": True, "message": "stopping"}
        return {"ok": False, "error": f"unknown command: {command}"}

    async def status(self) -> dict:
        layout = await self.layouts.current()
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_s": int(time.time() - self._started_at),
            "socket": str(config_mod.socket_path()),
            "config": str(self.cfg.source_path),
            "injector": self.injector.name if self.injector else None,
            "injector_error": self.injector_error,
            "monitor": {
                "available": bool(self.monitor and self.monitor.available()),
                "error": self.monitor.error if self.monitor else None,
                "modifiers_held": bool(self.monitor and self.monitor.modifiers_held()),
                "pressed_modifiers": (
                    self.monitor.pressed_modifier_names() if self.monitor else []
                ),
                "selection_possible": bool(
                    self.monitor and self.monitor.selection_possible()
                ),
                "force_gesture": bool(
                    self.monitor and self.monitor.force_gesture_recent()
                ),
                "buffer_keys": (
                    len(word.keys)
                    if self.monitor and (word := self.monitor.snapshot_word())
                    else 0
                ),
            },
            "clipboard": {
                "paste": self.clipboard.paste_available(),
                "copy": self.clipboard.copy_available(),
            },
            "layout": (
                {
                    "index": layout.index,
                    "code": layout.code,
                    "name": layout.name,
                    "lang": layout.lang,
                }
                if layout
                else None
            ),
            "dictionaries": self.detector.dictionary_info(),
            "auto": self.cfg.auto.enabled,
        }

    # --------------------------------------------------------------- trigger
    async def trigger(
        self,
        force: bool = False,
        selection: bool = False,
        source: str = "cli",
    ) -> dict:
        now = time.monotonic()

        # The Shift+F12 shortcut is delivered by KDE as a plain F12 action,
        # so the request may arrive before the evdev thread has seen the F12.
        # Shift itself, however, is already down: use that as the immediate
        # force signal.  This is what makes Shift+F12 act on the first press.
        if (
            not force
            and self.monitor is not None
            and self.monitor.shift_held()
        ):
            log.info("Shift held at trigger: forcing conversion")
            force = True

        # KDE repeats a held global shortcut; without a debounce the daemon
        # would flip the same text back and forth many times per second.
        # A deliberate Shift+F12 following a smart F12 must not be rejected.
        if force:
            if now - self._last_force_time < 0.4:
                return {"ok": False, "error": "слишком часто"}
            self._last_force_time = now
        elif now - self._last_trigger_time < 0.3:
            return {"ok": False, "error": "слишком часто"}
        self._last_trigger_time = now

        if self._busy.locked():
            return {"ok": False, "error": "предыдущее исправление ещё выполняется"}
        async with self._busy:
            try:
                return await self._trigger_locked(force, selection, source)
            except LapsKeyError as exc:
                log.warning("trigger failed: %s", exc)
                await self._notify_error(str(exc))
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - report, never crash
                log.exception("unexpected trigger failure")
                await self._notify_error(f"внутренняя ошибка: {exc}")
                return {"ok": False, "error": f"внутренняя ошибка: {exc}"}

    async def _trigger_locked(
        self, force: bool, selection: bool, source: str
    ) -> dict:
        if self.injector is None:
            raise LapsKeyError(
                self.injector_error
                or "эмуляция клавиатуры недоступна (см. `lapskey doctor`)"
            )

        now = time.monotonic()
        if (
            not force
            and self._last_noop
            and (now - self._last_noop) * 1000.0 <= self.cfg.fix.double_press_ms
        ):
            log.info("second press: forcing conversion")
            force = True
        if self.cfg.fix.mode == "always":
            force = True

        await self._wait_modifiers_released()
        # Give the evdev thread a moment to drain the F12/Shift events that
        # triggered this very command; the CLI can reach us first.
        if self.monitor is not None:
            await asyncio.sleep(0.04)

        def _gesture() -> bool:
            return bool(
                self.monitor is not None and self.monitor.force_gesture_recent(5.0)
            )

        if not force and _gesture():
            log.info("Shift+F-key gesture detected: forcing conversion")
            force = True

        acquisition = await self._acquire(prefer_selection=selection)
        if not force and _gesture():
            log.info("Shift+F-key gesture detected: forcing conversion")
            force = True
        if not acquisition.text.strip():
            raise LapsKeyError("не удалось прочитать текст")

        active_layout = await self.layouts.current()
        layout_lang = active_layout.lang if active_layout else None
        max_chars = (
            self.cfg.input.selection_max_chars
            if acquisition.origin == "selection"
            else None
        )
        decision = self.detector.convert_text(
            acquisition.text,
            threshold=self.cfg.fix.score_threshold,
            force=force,
            max_chars=max_chars,
            source_lang=layout_lang,
        )

        if acquisition.origin == "selection" and max_chars is not None:
            if len(acquisition.text) > max_chars and not force:
                raise LapsKeyError(
                    f"выделено слишком много текста ({len(acquisition.text)} символов); "
                    "выделите слово/фразу покороче"
                )

        if not decision.changed or decision.target is None:
            if acquisition.created_selection and self.injector is not None:
                # _copy_last_word() selected the previous word; collapse that
                # selection so the next keystroke does not overwrite it.
                await self.injector.tap(KEY["RIGHT"])
            self._last_noop = time.monotonic()
            if self.cfg.fix.double_press_ms > 0:
                message = (
                    "текст не изменён. Принудительно: Shift+F12 "
                    "(или нажмите F12 ещё раз)"
                )
            else:
                message = "текст не изменён. Принудительно: Shift+F12"
            now_notify = time.monotonic()
            if (
                self.cfg.general.notify
                and now_notify - self._last_noop_notify > 2.0
            ):
                self._last_noop_notify = now_notify
                await notify("LapSKey", message)
            return {
                "ok": True,
                "changed": False,
                "message": message,
                "origin": acquisition.origin,
                "text": acquisition.text,
            }

        self._last_noop = 0.0
        result = await self._apply(
            converted=decision.converted,
            target=decision.target,
            delete_count=acquisition.delete_count,
            suffix=acquisition.suffix,
            origin=acquisition.origin,
            allow_clipboard=True,
        )
        if self.monitor is not None:
            self.monitor.reset()
        result.update(
            {
                "ok": True,
                "changed": True,
                "original": acquisition.text,
                "converted": decision.converted,
                "target": decision.target,
                "origin": acquisition.origin,
                "source": source,
            }
        )
        log.info(
            "fixed %r -> %r (%s, %s%s)",
            acquisition.text,
            decision.converted,
            result["insert"],
            acquisition.origin,
            ", force" if force else "",
        )
        return result

    async def _apply(
        self,
        *,
        converted: str,
        target: str,
        delete_count: int,
        suffix: str,
        origin: str,
        allow_clipboard: bool,
    ) -> dict:
        if self.injector is None:
            raise LapsKeyError("эмуляция клавиатуры недоступна")

        full_text = converted + suffix
        plan = await self._prepare_insertion(
            full_text, target, allow_clipboard=allow_clipboard
        )

        # Put the text into the clipboard *before* deleting anything: if this
        # fails we can abort without destroying the user's text.
        if plan.kind == "clipboard":
            if not await self.clipboard.set_text(full_text):
                await self.clipboard.restore(plan.snapshot)
                raise LapsKeyError("не удалось поместить текст в буфер обмена")

        if origin == "selection":
            await self.injector.tap(KEY["BACKSPACE"])
        else:
            await self.injector.backspaces(delete_count)
        await asyncio.sleep(0.04)

        neutral = None
        if self.monitor is not None and self.monitor.available():
            neutral = lambda: not self.monitor.modifiers_held()  # noqa: E731

        if plan.kind == "keys":
            await self.injector.type_keys(plan.keys or [], neutral)
        else:
            await self.injector.key_combo(self.cfg.fix.paste_combo)
            await asyncio.sleep(0.3)
            if self.cfg.input.restore_clipboard:
                await self.clipboard.restore(plan.snapshot)

        return {"insert": plan.kind, "layout_switched": plan.layout_ok}

    async def _prepare_insertion(
        self, text: str, target: str, *, allow_clipboard: bool
    ) -> InsertionPlan:
        insert_mode = self.cfg.fix.insert
        keys = mapping.text_to_keys(text, target)

        layout_ok = False
        if self.cfg.fix.switch_layout:
            layout_ok = await self.layouts.ensure_lang(target)
        else:
            current = await self.layouts.current()
            layout_ok = bool(current and current.lang == target)

        if insert_mode == "keys":
            if not layout_ok or keys is None:
                raise LapsKeyError(
                    "не удалось переключить раскладку/напечатать текст; "
                    "проверьте настройки раскладок KDE"
                )
            return InsertionPlan(kind="keys", keys=keys, layout_ok=layout_ok)

        if insert_mode != "clipboard" and layout_ok and keys is not None:
            return InsertionPlan(kind="keys", keys=keys, layout_ok=layout_ok)

        if not allow_clipboard:
            raise LapsKeyError("автозамена требует доступной раскладки для ввода")

        if not self.clipboard.copy_available():
            raise LapsKeyError(
                "нужен wl-copy (пакет wl-clipboard) для вставки через буфер"
            )
        types = await self.clipboard.list_types()
        if types and not any(t in TEXT_TYPES for t in types):
            raise LapsKeyError(
                "в буфере обмена нет текста (картинка/файл); вставка через буфер "
                "затёрла бы его. Настройте раскладки KDE, чтобы ввод шёл клавишами"
            )
        snapshot = (
            await self.clipboard.snapshot()
            if self.cfg.input.restore_clipboard
            else None
        )
        return InsertionPlan(kind="clipboard", snapshot=snapshot, layout_ok=layout_ok)

    # ----------------------------------------------------------- acquisition
    async def _acquire(self, prefer_selection: bool = False) -> Acquisition:
        mode = self.cfg.input.acquisition
        monitor = self.monitor
        monitor_ok = bool(monitor and monitor.available())
        selection_flag = bool(monitor and monitor.selection_possible())

        if prefer_selection or (selection_flag and mode != "buffer"):
            text = await self._read_selection()
            if text:
                return Acquisition(text=text, origin="selection")
            # The user has just selected something; converting an older word
            # from the buffer would be surprising, so fail loudly instead.
            raise LapsKeyError(
                "не удалось прочитать выделенный текст. "
                "Проверьте copy_combo (ctrl+insert) и пакет wl-clipboard"
            )

        if mode in ("auto", "buffer") and monitor_ok:
            word = monitor.snapshot_word()  # type: ignore[union-attr]
            if word is not None:
                layout = await self.layouts.current()
                lang = layout.lang if layout else None
                if lang in ("ru", "en"):
                    text = mapping.physical_to_text(word.keys, lang)
                    if text and text.strip():
                        return Acquisition(
                            text=text,
                            origin="buffer",
                            delete_count=len(word.keys) + (1 if word.separator else 0),
                            suffix=word.separator,
                        )
                else:
                    log.debug("active layout is %r; buffer path skipped", lang)

        if mode in ("auto", "clipboard"):
            # No fresh selection was detected: probe the clipboard (harmless)
            # and then select the previous word.  PRIMARY is deliberately not
            # used here because it can be stale.
            text = await self._copy_selection()
            if text:
                return Acquisition(text=text, origin="selection")

            # Selecting the previous word with Ctrl+Shift+Left is a last
            # resort: it moves the user's selection/caret and feels like the
            # keyboard is not responding.  Only do it when the evdev buffer is
            # unavailable (no /dev/input permission); otherwise ask the user to
            # type the word again instead of injecting anything.
            if mode == "clipboard" or not monitor_ok:
                text = await self._copy_last_word()
                if text:
                    return Acquisition(
                        text=text, origin="selection", created_selection=True
                    )

        raise LapsKeyError(
            "буфер слова пуст — наберите слово заново и сразу нажмите F12 "
            "(клик мышью, стрелки и Enter сбрасывают буфер)"
        )

    async def _read_selection(self) -> str | None:
        """Read the current selection without touching the clipboard if possible."""
        if self.cfg.input.primary_selection and self.clipboard.paste_available():
            text = await self.clipboard.read_primary()
            if text:
                return text
        if self.cfg.input.copy_probe:
            return await self._copy_selection()
        return None

    async def _copy_selection(self) -> str | None:
        if not self.cfg.input.copy_probe or self.injector is None:
            return None
        if not self.clipboard.paste_available():
            return None
        types = await self.clipboard.list_types()
        if types and not any(t in TEXT_TYPES for t in types):
            # Copying would overwrite (and lose) an image or a file.
            log.debug("clipboard holds non-text data; copy probe skipped")
            return None
        previous = await self.clipboard.read_text()
        snapshot = await self.clipboard.snapshot()
        try:
            await self.injector.key_combo(self.cfg.input.copy_combo)
            return await self.clipboard.wait_for_change(
                previous, self.cfg.input.clipboard_timeout_ms
            )
        finally:
            if self.cfg.input.restore_clipboard and snapshot is not None:
                await self.clipboard.restore(snapshot)

    async def _copy_last_word(self) -> str | None:
        if self.injector is None or not self.cfg.input.copy_probe:
            return None
        log.debug("selecting the previous word through the keyboard")
        await self.injector.key_combo(self.cfg.input.select_word_combo)
        await asyncio.sleep(0.03)
        text = await self._copy_selection()
        if text is None:
            # Collapse a possible selection to the right (where the caret was
            # before selecting backwards); otherwise the next key would
            # replace the selected text.
            with contextlib.suppress(LapsKeyError):
                await self.injector.tap(KEY["RIGHT"])
        return text

    async def _wait_modifiers_released(self) -> None:
        if self.monitor is None or not self.monitor.available():
            await asyncio.sleep(min(0.25, self.cfg.fix.modifier_release_timeout_ms / 1000))
            return
        deadline = (
            asyncio.get_running_loop().time()
            + self.cfg.fix.modifier_release_timeout_ms / 1000.0
        )
        while self.monitor.modifiers_held():
            if asyncio.get_running_loop().time() > deadline:
                held = ", ".join(self.monitor.pressed_modifier_names()) or "модификаторы"
                raise LapsKeyError(
                    f"не дождался отпускания ({held}). Отпустите клавиши и нажмите "
                    "горячую клавишу ещё раз"
                )
            await asyncio.sleep(0.01)

    async def _notify_error(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_error_notify < 1.5:
            return
        self._last_error_notify = now
        if self.cfg.general.notify and self.cfg.general.notify_errors:
            # Normal urgency (not critical): it auto-hides instead of staying
            # on screen until the user clicks it.  Clicking used to steal focus
            # and clear the word buffer.
            await notify("LapSKey", message, urgency="normal", timeout_ms=4000)
