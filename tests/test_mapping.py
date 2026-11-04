import unittest

from lapskey import mapping


class MappingTests(unittest.TestCase):
    def test_rows_are_aligned(self):
        self.assertEqual(len(mapping.EN_UNSHIFTED), len(mapping.RU_UNSHIFTED))
        self.assertEqual(len(mapping.EN_SHIFTED), len(mapping.RU_SHIFTED))

    def test_letters(self):
        self.assertEqual(mapping.translate_text("ghbdtn", "ru"), "привет")
        self.assertEqual(mapping.translate_text("привет", "en"), "ghbdtn")
        self.assertEqual(mapping.translate_text("Руддщ", "en"), "Hello")
        self.assertEqual(mapping.translate_text("РУДДЩ", "en"), "HELLO")

    def test_punctuation_is_directional(self):
        # comma typed in EN => "б" key in RU ...
        self.assertEqual(mapping.EN_TO_RU[","], "б")
        # ... but the RU comma (Shift+/) maps back to "?" and not to ","
        self.assertEqual(mapping.RU_TO_EN[","], "?")
        self.assertEqual(mapping.RU_TO_EN["."], "/")

    def test_translate_letters_keeps_punctuation(self):
        self.assertEqual(mapping.translate_letters("ghbdtn,", "ru"), "привет,")
        self.assertEqual(mapping.translate_letters(",tp", "ru"), ",ез")

    def test_physical_keys(self):
        keys = mapping.text_to_keys("привет", "ru")
        self.assertIsNotNone(keys)
        self.assertEqual([k.code for k in keys], [34, 35, 48, 32, 20, 49])
        self.assertEqual(mapping.physical_to_text(keys, "ru"), "привет")
        self.assertEqual(mapping.physical_to_text(keys, "en"), "ghbdtn")

    def test_untypable_character(self):
        self.assertIsNone(mapping.text_to_keys("€", "ru"))

    def test_script_detection(self):
        self.assertEqual(mapping.script_of("ghbdtn"), "en")
        self.assertEqual(mapping.script_of("привет"), "ru")
        self.assertIsNone(mapping.script_of("привет hello"))
        self.assertIsNone(mapping.script_of("123"))

    def test_custom_pairs(self):
        old_en = mapping.EN_TO_RU.get("q")
        old_ru = mapping.RU_TO_EN.get("я")
        old_key = mapping.RU_CHAR_TO_KEY.get("я")
        try:
            warnings = mapping.apply_custom_pairs([["q", "я"]])
            self.assertEqual(warnings, [])
            self.assertEqual(mapping.EN_TO_RU["q"], "я")
            self.assertEqual(mapping.RU_TO_EN["я"], "q")
            self.assertEqual(
                mapping.RU_CHAR_TO_KEY["я"], mapping.EN_CHAR_TO_KEY["q"]
            )
        finally:
            if old_en is not None:
                mapping.EN_TO_RU["q"] = old_en
            if old_ru is not None:
                mapping.RU_TO_EN["я"] = old_ru
            if old_key is not None:
                mapping.RU_CHAR_TO_KEY["я"] = old_key

        warnings = mapping.apply_custom_pairs([["q"], ["q", "too long"], [1, 2]])
        self.assertEqual(len(warnings), 3)


if __name__ == "__main__":
    unittest.main()
