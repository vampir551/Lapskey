import unittest
from types import SimpleNamespace

from lapskey.injector import Injector, UinputBackend, parse_combo
from lapskey.mapping import KEY, KeyStroke
from lapskey.util import LapsKeyError


class FakeUInput:
    def __init__(self):
        self.calls = []

    def write(self, etype, code, value):
        self.calls.append(("write", etype, code, value))

    def syn(self):
        self.calls.append(("syn",))


class InjectorTests(unittest.TestCase):
    def test_every_event_is_followed_by_syn(self):
        # uinput queues events until SYN_REPORT; without it KWin receives the
        # keys minutes later and the keyboard looks frozen.
        backend = UinputBackend.__new__(UinputBackend)
        backend.ui = FakeUInput()
        backend._ecodes = SimpleNamespace(EV_KEY=1)
        backend.delay = 0
        backend._write(KEY["A"], 1)
        backend._write(KEY["A"], 0)
        self.assertEqual(
            backend.ui.calls,
            [
                ("write", 1, KEY["A"], 1),
                ("syn",),
                ("write", 1, KEY["A"], 0),
                ("syn",),
            ],
        )

    def test_parse_combo(self):
        self.assertEqual(
            parse_combo("ctrl+shift+left"),
            [KEY["LEFTCTRL"], KEY["LEFTSHIFT"], KEY["LEFT"]],
        )
        self.assertEqual(parse_combo("ctrl+insert"), [KEY["LEFTCTRL"], KEY["INSERT"]])

    def test_parse_combo_errors(self):
        with self.assertRaises(LapsKeyError):
            parse_combo("ctrl+")
        with self.assertRaises(LapsKeyError):
            parse_combo("hyper+a")

    def test_shift_is_released_when_typing_aborts(self):
        backend = UinputBackend.__new__(UinputBackend)
        backend.ui = FakeUInput()
        backend._ecodes = SimpleNamespace(EV_KEY=1)
        backend.delay = 0
        calls = {"n": 0}

        def neutral():
            calls["n"] += 1
            if calls["n"] == 1:
                return True
            raise LapsKeyError("modifier pressed")

        with self.assertRaises(LapsKeyError):
            backend.type_keys_sync(
                [KeyStroke(KEY["A"], True), KeyStroke(KEY["B"], False)],
                neutral,
            )

        self.assertEqual(
            backend.ui.calls[-2], ("write", 1, KEY["LEFTSHIFT"], 0)
        )

    def test_injector_excludes_its_own_device(self):
        backend = UinputBackend.__new__(UinputBackend)
        injector = Injector.__new__(Injector)
        injector.backend = backend
        names = injector.exclude_names
        self.assertIn("lapskey", names)
        self.assertIn("lapskey virtual keyboard", names)


if __name__ == "__main__":
    unittest.main()
