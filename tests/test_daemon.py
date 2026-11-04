import asyncio
import unittest

from lapskey.config import Config
from lapskey.daemon import Daemon
from lapskey.detect import Detector
from lapskey.evdev_monitor import BufferWord
from lapskey.layout import LayoutInfo
from lapskey.mapping import KEY, KeyStroke, text_to_keys


def word_keys(text, layout="en"):
    keys = text_to_keys(text, layout)
    assert keys is not None
    return keys


class FakeInjector:
    name = "fake"

    def __init__(self):
        self.calls = []

    async def tap(self, code):
        self.calls.append(("tap", code))

    async def key_combo(self, combo):
        self.calls.append(("combo", combo))

    async def backspaces(self, count, inter_delay_ms=2):
        self.calls.append(("backspaces", count))

    async def type_keys(self, keys, neutral=None):
        self.calls.append(("type", [(k.code, k.shift) for k in keys]))

    def close(self):
        pass


class FakeMonitor:
    def __init__(
        self,
        word=None,
        selection=False,
        available=True,
        force_gesture=False,
        shift_held=False,
    ):
        self.word = word
        self.selection = selection
        self.is_available = available
        self.force_gesture = force_gesture
        self.shift_down = shift_held
        self.reset_count = 0
        self.error = None

    def force_gesture_recent(self, window=2.0):
        return self.force_gesture

    def shift_held(self):
        return self.shift_down

    def available(self):
        return self.is_available

    def selection_possible(self):
        return self.selection

    def snapshot_word(self):
        return self.word

    def modifiers_held(self):
        return False

    def event_counter(self):
        return 0

    def reset(self):
        self.reset_count += 1
        self.word = None
        self.selection = False


class FakeLayouts:
    def __init__(self, lang="en", can_switch=True):
        self.lang = lang
        self.can_switch = can_switch
        self.ensured = []

    async def current(self):
        return LayoutInfo(0, self.lang, self.lang, self.lang)

    async def ensure_lang(self, lang):
        self.ensured.append(lang)
        return self.can_switch


class FakeClipboard:
    def __init__(self, primary=None, text=None):
        self.primary = primary
        self.text = text
        self.snapshot_value = None
        self.set_values = []
        self.restored = 0

    def paste_available(self):
        return True

    def copy_available(self):
        return True

    async def read_primary(self):
        value, self.primary = self.primary, None
        return value

    async def read_text(self):
        return self.text

    async def list_types(self, primary=False):
        return ["text/plain;charset=utf-8"]

    async def snapshot(self):
        return self.snapshot_value

    async def restore(self, snapshot):
        self.restored += 1

    async def set_text(self, text):
        self.set_values.append(text)
        return True

    async def wait_for_change(self, previous, timeout_ms=350):
        return None


def make_daemon(monitor, layouts, clipboard, injector):
    cfg = Config()
    cfg.general.notify = False
    cfg.general.notify_errors = False
    cfg.input.copy_probe = False
    cfg.fix.insert = "keys"
    daemon = Daemon(cfg)
    daemon.detector = Detector(auto_detect=False)
    daemon.monitor = monitor
    daemon.layouts = layouts
    daemon.clipboard = clipboard
    daemon.injector = injector
    return daemon


class DaemonFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_word_is_converted_and_retyped(self):
        monitor = FakeMonitor(
            word=BufferWord(keys=word_keys("ghbdtn", "en"), separator="")
        )
        layouts = FakeLayouts("en")
        clipboard = FakeClipboard()
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)

        result = await daemon.trigger()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["changed"])
        self.assertEqual(result["converted"], "привет")
        self.assertEqual(result["target"], "ru")
        self.assertEqual(layouts.ensured, ["ru"])
        self.assertEqual(
            injector.calls[0], ("backspaces", len(word_keys("ghbdtn", "en")))
        )
        kind, keys = injector.calls[1]
        self.assertEqual(kind, "type")
        self.assertEqual([code for code, _shift in keys], [k.code for k in word_keys("привет", "ru")])
        self.assertEqual(monitor.reset_count, 1)

    async def test_selection_is_replaced_with_one_backspace(self):
        monitor = FakeMonitor(selection=True)
        layouts = FakeLayouts("en")
        clipboard = FakeClipboard(primary="руддщ")
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)

        result = await daemon.trigger()

        self.assertTrue(result["changed"])
        self.assertEqual(result["origin"], "selection")
        self.assertEqual(result["converted"], "hello")
        self.assertEqual(injector.calls[0], ("tap", KEY["BACKSPACE"]))
        self.assertEqual(injector.calls[1][0], "type")

    async def test_noop_does_not_touch_anything(self):
        monitor = FakeMonitor(word=BufferWord(keys=word_keys("привет", "ru")))
        layouts = FakeLayouts("ru")
        clipboard = FakeClipboard()
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)

        result = await daemon.trigger()

        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(injector.calls, [])
        self.assertEqual(layouts.ensured, [])
        self.assertEqual(monitor.reset_count, 0)

    async def test_clipboard_insertion_fallback(self):
        monitor = FakeMonitor(
            word=BufferWord(keys=word_keys("ghbdtn", "en"), separator="")
        )
        layouts = FakeLayouts("en", can_switch=False)
        clipboard = FakeClipboard()
        clipboard.snapshot_value = object()
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)
        daemon.cfg.fix.insert = "auto"

        result = await daemon.trigger()

        self.assertTrue(result["changed"])
        self.assertEqual(result["insert"], "clipboard")
        self.assertIn("привет", clipboard.set_values)
        self.assertIn(("combo", "ctrl+v"), injector.calls)
        self.assertGreaterEqual(clipboard.restored, 1)

    async def test_second_press_forces_conversion(self):
        # A correctly typed word is a no-op the first time...
        keys = word_keys("привет", "ru")
        monitor = FakeMonitor(word=BufferWord(keys=keys))
        layouts = FakeLayouts("ru")
        clipboard = FakeClipboard()
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)
        daemon.cfg.fix.double_press_ms = 1200

        first = await daemon.trigger()
        self.assertFalse(first["changed"])

        # ...but a second press within the window forces the conversion.
        monitor.word = BufferWord(keys=keys)
        monitor.selection = False
        daemon._last_trigger_time = 0.0  # bypass the rapid-repeat debounce
        second = await daemon.trigger()
        self.assertTrue(second["changed"])
        self.assertEqual(second["converted"], "ghbdtn")

    async def test_shift_held_forces_on_the_first_press(self):
        keys = word_keys("привет", "ru")
        monitor = FakeMonitor(
            word=BufferWord(keys=keys), shift_held=True
        )
        layouts = FakeLayouts("ru")
        clipboard = FakeClipboard()
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)

        result = await daemon.trigger()

        self.assertTrue(result["changed"])
        self.assertEqual(result["converted"], "ghbdtn")

    async def test_shift_fkey_gesture_forces_conversion(self):
        keys = word_keys("привет", "ru")
        monitor = FakeMonitor(
            word=BufferWord(keys=keys), force_gesture=True
        )
        layouts = FakeLayouts("ru")
        clipboard = FakeClipboard()
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)

        result = await daemon.trigger()

        self.assertTrue(result["changed"])
        self.assertEqual(result["converted"], "ghbdtn")
        self.assertEqual(layouts.ensured, ["en"])

    async def test_auto_mode_converts_last_word(self):
        cfg_word = BufferWord(keys=word_keys("ghbdtn", "en"), separator=" ")
        monitor = FakeMonitor()
        layouts = FakeLayouts("en")
        clipboard = FakeClipboard()
        injector = FakeInjector()
        daemon = make_daemon(monitor, layouts, clipboard, injector)
        daemon.cfg.auto.enabled = True
        daemon.cfg.auto.delay_ms = 0
        daemon.monitor = monitor

        await daemon._auto_fix(cfg_word)

        self.assertEqual(injector.calls[0], ("backspaces", len(cfg_word.keys) + 1))
        kind, keys = injector.calls[1]
        self.assertEqual(kind, "type")
        self.assertEqual([code for code, _ in keys][-1], KEY["SPACE"])
        self.assertEqual(layouts.ensured, ["ru"])


if __name__ == "__main__":
    unittest.main()
