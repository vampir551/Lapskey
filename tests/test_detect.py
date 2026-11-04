import unittest

from lapskey.detect import Detector
from lapskey import mapping


class DetectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # auto_detect=False keeps the test independent from installed hunspell
        # dictionaries; the built-in mini dictionary must be enough here.
        cls.detector = Detector(auto_detect=False)

    def convert(self, text, force=False):
        return self.detector.convert_text(text, threshold=1.5, force=force)

    def test_typical_layout_errors(self):
        cases = {
            "ghbdtn": "привет",
            "зштп": "ping",
            "руддщ": "hello",
            "nfr": "так",
            "ytn": "нет",
            "lf": "да",
            "ghbdtn vbh": "привет мир",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(self.convert(original).converted, expected)

    def test_correct_text_is_not_touched(self):
        for text in ("привет", "hello", "привет мир", "hello world", "the", "я", "и"):
            with self.subTest(text=text):
                self.assertFalse(self.convert(text).changed)

    def test_punctuation_survives(self):
        # A trailing comma/dot would become a letter (б/ю), so it is kept.
        self.assertEqual(self.convert("ghbdtn,").converted, "привет,")
        self.assertEqual(self.convert("ghbdtn.").converted, "привет.")
        self.assertEqual(self.convert("ghbdtn!").converted, "привет!")
        # ? -> , is punctuation to punctuation: a user typing ? with the US
        # layout means the Russian comma (Shift+/).
        self.assertEqual(self.convert("ghbdtn vbh, rfr ltkf?").converted,
                         "привет мир, как дела,")

    def test_punctuation_only_with_active_layout(self):
        self.assertEqual(
            self.detector.convert_text("?", source_lang="en").converted, ","
        )
        self.assertEqual(
            self.detector.convert_text(",", source_lang="ru").converted, "?"
        )
        # A period would become the letter "ю"; leave it alone.
        self.assertFalse(
            self.detector.convert_text(".", source_lang="en").changed
        )

    def test_punctuation_as_russian_letter(self):
        # "без" typed with the US layout is ",tp" (the comma key is "б")
        self.assertEqual(self.convert(",tp").converted, "без")
        self.assertEqual(self.convert("'rhfy").converted, "экран")

    def test_case_is_preserved(self):
        self.assertEqual(self.convert("Ghbdtn").converted, "Привет")
        self.assertEqual(self.convert("GHBDTN").converted, "ПРИВЕТ")
        self.assertEqual(self.convert("Руддщ").converted, "Hello")

    def test_force_converts_even_ambiguous_text(self):
        decision = self.convert("привет", force=True)
        self.assertTrue(decision.changed)
        self.assertEqual(decision.converted, "ghbdtn")

    def test_single_letter_words(self):
        self.assertEqual(self.convert("z").converted, "я")
        self.assertFalse(self.convert("я").changed)
        self.assertFalse(self.convert("a").changed)

    def test_round_trip_of_frequent_words(self):
        # Sanity check: a correctly typed word must not be converted, while its
        # wrong-layout image must be converted back.
        words = ["привет", "спасибо", "пожалуйста", "сегодня", "человек",
                 "hello", "world", "keyboard", "please", "computer"]
        for word in words:
            with self.subTest(word=word):
                source = mapping.script_of(word)
                wrong = mapping.translate_text(word, mapping.other_script(source))
                decision = self.detector.decide_word(wrong, threshold=1.5)
                self.assertIsNotNone(decision)
                self.assertTrue(decision.convert, f"{wrong} -> {decision.converted}")
                self.assertEqual(decision.converted.lower(), word.lower())


if __name__ == "__main__":
    unittest.main()
