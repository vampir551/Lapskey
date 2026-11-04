import tempfile
import unittest
from pathlib import Path

from lapskey.config import Config, load, save
from lapskey.tray import format_pairs, parse_list, parse_pairs


class ConfigSaveTests(unittest.TestCase):
    def test_roundtrip(self):
        cfg = Config()
        cfg.general.log_level = "debug"
        cfg.fix.score_threshold = 2.75
        cfg.auto.enabled = True
        cfg.mapping.pairs = [["q", "й"], ["[", "х"]]
        cfg.keyboard.exclude = ["lapskey", "ydotool"]
        cfg.dictionaries.files = ["/tmp/words ru.txt"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            written = save(cfg, path)
            self.assertEqual(written, path)
            loaded = load(path)
        self.assertEqual(loaded.general.log_level, "debug")
        self.assertEqual(loaded.fix.score_threshold, 2.75)
        self.assertTrue(loaded.auto.enabled)
        self.assertEqual(loaded.mapping.pairs, [["q", "й"], ["[", "х"]])
        self.assertEqual(loaded.keyboard.exclude, ["lapskey", "ydotool"])
        self.assertEqual(loaded.dictionaries.files, ["/tmp/words ru.txt"])

    def test_quotes_are_escaped(self):
        cfg = Config()
        cfg.keyboard.exclude = ['weird"name', "back\\slash"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            save(cfg, path)
            loaded = load(path)
        self.assertEqual(loaded.keyboard.exclude, ['weird"name', "back\\slash"])


class TrayHelpersTests(unittest.TestCase):
    def test_pairs(self):
        pairs = parse_pairs("q:й, w:ц, [:х")
        self.assertEqual(pairs, [["q", "й"], ["w", "ц"], ["[", "х"]])
        self.assertEqual(format_pairs(pairs), "q:й, w:ц, [:х")

    def test_bad_pairs(self):
        with self.assertRaises(ValueError):
            parse_pairs("q")
        with self.assertRaises(ValueError):
            parse_pairs("qq:й")

    def test_lists(self):
        self.assertEqual(parse_list("a, b\nc;d"), ["a", "b", "c", "d"])
        self.assertEqual(parse_list("  "), [])


if __name__ == "__main__":
    unittest.main()
