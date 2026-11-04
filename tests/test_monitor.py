import asyncio
import unittest

from evdev import ecodes

from lapskey import mapping
from lapskey.evdev_monitor import InputMonitor, BufferWord
from lapskey.mapping import KEY


class FakeEvent:
    def __init__(self, etype, code, value):
        self.type = etype
        self.code = code
        self.value = value


def press(monitor, code, value=1):
    monitor._handle_key(code, value)


def type_word(monitor, text, caps=False):
    """Feed physical key presses for text typed in the US layout."""
    keys = mapping.text_to_keys(text, "en")
    assert keys is not None
    for stroke in keys:
        if stroke.shift:
            press(monitor, KEY["LEFTSHIFT"])
        press(monitor, stroke.code)
        press(monitor, stroke.code, 0)
        if stroke.shift:
            press(monitor, KEY["LEFTSHIFT"], 0)


class MonitorBufferTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        return InputMonitor(asyncio.get_running_loop(), **kwargs)

    async def test_word_buffer_and_boundary(self):
        words = []
        monitor = self.make(on_word=words.append, auto_min_length=3)
        type_word(monitor, "ghbdtn")

        snapshot = monitor.snapshot_word()
        self.assertIsInstance(snapshot, BufferWord)
        self.assertEqual(snapshot.separator, "")
        self.assertEqual(
            mapping.physical_to_text(snapshot.keys, "en"), "ghbdtn"
        )
        self.assertEqual(
            mapping.physical_to_text(snapshot.keys, "ru"), "привет"
        )

        press(monitor, KEY["SPACE"])
        self.assertEqual(monitor.snapshot_word().separator, " ")
        press(monitor, KEY["SPACE"], 0)
        await asyncio.sleep(0)
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0].separator, " ")

    async def test_backspace_edits_buffer(self):
        monitor = self.make()
        type_word(monitor, "ghbdtn")
        press(monitor, KEY["BACKSPACE"])
        press(monitor, KEY["BACKSPACE"], 0)
        self.assertEqual(
            mapping.physical_to_text(monitor.snapshot_word().keys, "en"),
            "ghbdt",
        )

    async def test_navigation_clears_buffer(self):
        monitor = self.make()
        type_word(monitor, "ghbdtn")
        press(monitor, KEY["LEFT"])
        self.assertIsNone(monitor.snapshot_word())

    async def test_capslock_is_applied(self):
        monitor = self.make()
        press(monitor, KEY["CAPSLOCK"])
        press(monitor, KEY["CAPSLOCK"], 0)
        press(monitor, KEY["G"])
        press(monitor, KEY["G"], 0)
        snapshot = monitor.snapshot_word()
        self.assertEqual(mapping.physical_to_text(snapshot.keys, "en"), "G")

    async def test_space_shortcut_keeps_the_word_buffer(self):
        monitor = self.make()
        type_word(monitor, "ghbdtn")
        press(monitor, KEY["LEFTCTRL"])
        press(monitor, KEY["LEFTSHIFT"])
        press(monitor, KEY["SPACE"])
        self.assertIsNotNone(monitor.snapshot_word())
        self.assertEqual(
            mapping.physical_to_text(monitor.snapshot_word().keys, "en"), "ghbdtn"
        )

    async def test_shift_navigation_marks_selection(self):
        monitor = self.make()
        press(monitor, KEY["LEFTSHIFT"])
        press(monitor, KEY["RIGHT"])
        self.assertTrue(monitor.selection_possible())

    async def test_mouse_drag_marks_selection(self):
        monitor = self.make()
        monitor._handle_pointer_event(2, 0, 0)  # EV_REL, REL_X
        monitor._handle_pointer_event(1, 272, 1)  # EV_KEY, BTN_LEFT press
        monitor._handle_pointer_event(2, 0, 8)  # EV_REL, REL_X movement
        monitor._handle_pointer_event(1, 272, 0)  # release
        self.assertTrue(monitor.selection_possible())

    async def test_both_device_routes_mouse_and_keyboard(self):
        monitor = self.make()
        device = object()  # only used as an identity for the dedup logic
        monitor._handle_event(
            device, "both", FakeEvent(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        )
        monitor._handle_event(
            device, "both", FakeEvent(ecodes.EV_REL, ecodes.REL_X, 8)
        )
        monitor._handle_event(
            device, "both", FakeEvent(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        )
        self.assertTrue(monitor.selection_possible())

        monitor._handle_event(
            device, "both", FakeEvent(ecodes.EV_KEY, KEY["G"], 1)
        )
        snapshot = monitor.snapshot_word()
        self.assertIsNotNone(snapshot)
        self.assertEqual(mapping.physical_to_text(snapshot.keys, "en"), "g")
        self.assertFalse(monitor.selection_possible())

    async def test_plain_click_does_not_mark_selection(self):
        monitor = self.make()
        monitor._handle_pointer_event(1, 272, 1)
        monitor._handle_pointer_event(1, 272, 0)
        self.assertFalse(monitor.selection_possible())

    async def test_stale_modifier_is_cleared_from_kernel_state(self):
        class FakeDevice:
            def __init__(self, keys):
                self._keys = keys

            def active_keys(self):
                return self._keys

        monitor = self.make()
        monitor._devices["fake"] = (FakeDevice([]), "keyboard")
        with monitor._lock:
            monitor._pressed.add(KEY["LEFTCTRL"])
        self.assertFalse(monitor.modifiers_held())
        self.assertNotIn(KEY["LEFTCTRL"], monitor._pressed)

    async def test_physically_held_modifier_is_detected(self):
        class FakeDevice:
            def active_keys(self):
                return [KEY["LEFTSHIFT"]]

        monitor = self.make()
        monitor._devices["fake"] = (FakeDevice(), "keyboard")
        with monitor._lock:
            monitor._pressed.add(KEY["LEFTSHIFT"])
        self.assertTrue(monitor.modifiers_held())
        self.assertEqual(monitor.pressed_modifier_names(), ["Shift"])

    async def test_stale_modifier_does_not_swallow_typing(self):
        class FakeDevice:
            def active_keys(self):
                return []

        monitor = self.make()
        monitor._devices["fake"] = (FakeDevice(), "keyboard")
        with monitor._lock:
            monitor._pressed.add(KEY["LEFTCTRL"])
        press(monitor, KEY["G"])
        self.assertNotIn(KEY["LEFTCTRL"], monitor._pressed)
        snapshot = monitor.snapshot_word()
        self.assertIsNotNone(snapshot)
        self.assertEqual(mapping.physical_to_text(snapshot.keys, "en"), "g")

    async def test_shift_fkey_force_gesture(self):
        monitor = self.make()
        press(monitor, KEY["F12"])
        self.assertFalse(monitor.force_gesture_recent())
        press(monitor, KEY["LEFTSHIFT"])
        press(monitor, KEY["F12"])
        self.assertTrue(monitor.force_gesture_recent())
        # A plain F12 must not enable force mode, even right after a capital.
        monitor2 = self.make()
        press(monitor2, KEY["LEFTSHIFT"])
        press(monitor2, KEY["A"])
        press(monitor2, KEY["LEFTSHIFT"], 0)
        press(monitor2, KEY["F12"])
        self.assertFalse(monitor2.force_gesture_recent())

    async def test_hotkey_callback(self):
        hits = []
        monitor = self.make(
            hotkey_code=KEY["F12"], on_hotkey=lambda: hits.append(1)
        )
        press(monitor, KEY["F12"])
        press(monitor, KEY["F12"], 0)
        await asyncio.sleep(0)
        self.assertEqual(hits, [1])

    async def test_hotkey_requires_configured_modifiers(self):
        hits = []
        monitor = self.make(
            hotkey_code=KEY["F12"], hotkey_ctrl=True, on_hotkey=lambda: hits.append(1)
        )
        press(monitor, KEY["LEFTCTRL"])
        press(monitor, KEY["F12"])
        press(monitor, KEY["F12"], 0)
        press(monitor, KEY["LEFTCTRL"], 0)
        await asyncio.sleep(0)
        self.assertEqual(hits, [1])


if __name__ == "__main__":
    unittest.main()
