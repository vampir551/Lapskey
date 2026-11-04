"""Keyboard layout tables (US / Russian) and helpers.

The whole tool is built around one idea: a physical key produces a different
character depending on the active XKB layout.  The two rows below describe the
same physical keys on a standard 104-key keyboard, once for the US layout and
once for the Russian (ЙЦУКЕН) layout.

The tables are used for three things:

* translating text between layouts (``ghbdtn`` -> ``привет``);
* translating a physical key sequence into text of a known layout;
* finding the keycode + shift state needed to type a character in a layout.
"""

from __future__ import annotations

from dataclasses import dataclass

# Linux input event keycodes (linux/input-event-codes.h).  We intentionally do
# not depend on python-evdev here so that the pure-python parts stay testable.
KEY = {
    "ESC": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
    "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "MINUS": 12, "EQUAL": 13,
    "BACKSPACE": 14, "TAB": 15,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22,
    "I": 23, "O": 24, "P": 25,
    "LEFTBRACE": 26, "RIGHTBRACE": 27, "ENTER": 28,
    "LEFTCTRL": 29,
    "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35, "J": 36,
    "K": 37, "L": 38,
    "SEMICOLON": 39, "APOSTROPHE": 40, "GRAVE": 41,
    "LEFTSHIFT": 42, "BACKSLASH": 43,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
    "COMMA": 51, "DOT": 52, "SLASH": 53,
    "RIGHTSHIFT": 54,
    "LEFTALT": 56, "SPACE": 57, "CAPSLOCK": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
    "F7": 65, "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    "RIGHTCTRL": 97, "RIGHTALT": 100, "RIGHTMETA": 126,
    "LEFTMETA": 125,
    "INSERT": 110, "DELETE": 111,
    "HOME": 102, "END": 107, "PAGEUP": 104, "PAGEDOWN": 109,
    "LEFT": 105, "RIGHT": 106, "UP": 103, "DOWN": 108,
    "PAUSE": 119, "SYSRQ": 99,
    "KP0": 82, "KP1": 79, "KP2": 80, "KP3": 81, "KP4": 75,
    "KP5": 76, "KP6": 77, "KP7": 71, "KP8": 72, "KP9": 73,
    "KPASTERISK": 55, "KPMINUS": 74, "KPPLUS": 78, "KPDOT": 83,
}

# Physical row 1-4 of a US keyboard, unshifted and shifted.  Same length, same
# order.  ``\`` is escaped in the literals.
EN_UNSHIFTED = "`1234567890-=qwertyuiop[]\\asdfghjkl;'zxcvbnm,./"
EN_SHIFTED = '~!@#$%^&*()_+QWERTYUIOP{}|ASDFGHJKL:"ZXCVBNM<>?'

# The same physical keys with the Russian layout active.
RU_UNSHIFTED = "ё1234567890-=йцукенгшщзхъ\\фывапролджэячсмитьбю."
RU_SHIFTED = 'Ё!"№;%:?*()_+ЙЦУКЕНГШЩЗХЪ/ФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,'

assert len(EN_UNSHIFTED) == len(EN_SHIFTED) == len(RU_UNSHIFTED) == len(RU_SHIFTED), (
    len(EN_UNSHIFTED), len(EN_SHIFTED), len(RU_UNSHIFTED), len(RU_SHIFTED)
)

# keycode for every physical key of the four rows, in table order
_ROW_KEYCODES = [
    KEY["GRAVE"],
    *[KEY[str(i)] for i in range(1, 10)], KEY["0"],
    KEY["MINUS"], KEY["EQUAL"],
    *[KEY[c] for c in "QWERTYUIOP"],
    KEY["LEFTBRACE"], KEY["RIGHTBRACE"], KEY["BACKSLASH"],
    *[KEY[c] for c in "ASDFGHJKL"],
    KEY["SEMICOLON"], KEY["APOSTROPHE"],
    *[KEY[c] for c in "ZXCVBNM"],
    KEY["COMMA"], KEY["DOT"], KEY["SLASH"],
]

assert len(_ROW_KEYCODES) == len(EN_UNSHIFTED)

# Two *directional* tables.  A single bidirectional dict is impossible because
# some characters are produced by different physical keys in the two layouts:
#   EN "," (comma key)   -> RU "б",   while RU "," (shift + "/" key) -> EN "?"
#   EN "." (dot key)     -> RU "ю",   while RU "." ("/" key)        -> EN "/"
# Keeping the directions separate avoids silent corruption of punctuation.
EN_TO_RU: dict[str, str] = {}
RU_TO_EN: dict[str, str] = {}
for en, ru in zip(EN_UNSHIFTED, RU_UNSHIFTED):
    if en != ru:
        EN_TO_RU[en] = ru
        RU_TO_EN[ru] = en
for en, ru in zip(EN_SHIFTED, RU_SHIFTED):
    if en != ru:
        EN_TO_RU[en] = ru
        RU_TO_EN[ru] = en

RU_LETTERS = frozenset("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
EN_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz")
RU_UPPER = frozenset("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
EN_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def translate_char(ch: str, target: str) -> str:
    """Translate one character into ``target`` layout; keep unknown chars."""
    table = EN_TO_RU if target == "ru" else RU_TO_EN
    return table.get(ch, ch)


def translate_text(text: str, target: str) -> str:
    """Translate every mappable character, leave the rest untouched."""
    table = EN_TO_RU if target == "ru" else RU_TO_EN
    return "".join(table.get(ch, ch) for ch in text)


def translate_letters(text: str, target: str) -> str:
    """Translate letters only; punctuation and digits stay as typed.

    This is what a word-level fix must use.  A comma typed in the wrong layout
    is still a comma for the user, but the physical comma key would mean "б"
    in the Russian layout, so blindly translating punctuation corrupts text.
    """
    table = EN_TO_RU if target == "ru" else RU_TO_EN
    out: list[str] = []
    for ch in text:
        if ch in EN_LETTERS or ch in EN_UPPER or ch in RU_LETTERS or ch in RU_UPPER:
            out.append(table.get(ch, ch))
        else:
            out.append(ch)
    return "".join(out)


def script_of(text: str) -> str | None:
    """Return ``"ru"``/``"en"`` when the text contains letters of one script.

    Mixed text returns ``None``.  Text without any letters also returns
    ``None``.  Punctuation and digits are ignored.
    """
    has_ru = has_en = False
    for ch in text:
        if ch in RU_LETTERS or ch in RU_UPPER:
            has_ru = True
        elif ch in EN_LETTERS or ch in EN_UPPER:
            has_en = True
    if has_ru and not has_en:
        return "ru"
    if has_en and not has_ru:
        return "en"
    return None


def other_script(script: str) -> str:
    return "en" if script == "ru" else "ru"


@dataclass(frozen=True)
class KeyStroke:
    code: int
    shift: bool = False


def _build_char_key_map(unshifted: str, shifted: str) -> dict[str, KeyStroke]:
    table: dict[str, KeyStroke] = {}
    for code, ch in zip(_ROW_KEYCODES, unshifted):
        if ch not in table:
            table[ch] = KeyStroke(code, False)
    for code, ch in zip(_ROW_KEYCODES, shifted):
        if ch not in table:
            table[ch] = KeyStroke(code, True)
    return table


EN_CHAR_TO_KEY = _build_char_key_map(EN_UNSHIFTED, EN_SHIFTED)
RU_CHAR_TO_KEY = _build_char_key_map(RU_UNSHIFTED, RU_SHIFTED)
EN_CHAR_TO_KEY[" "] = KeyStroke(KEY["SPACE"], False)
RU_CHAR_TO_KEY[" "] = KeyStroke(KEY["SPACE"], False)

# keycode -> character(s) on that physical key (shifted/unshifted)
KEYCODE_TO_EN: dict[int, tuple[str, str]] = {}
for code, plain, shifted in zip(_ROW_KEYCODES, EN_UNSHIFTED, EN_SHIFTED):
    KEYCODE_TO_EN[code] = (plain, shifted)


def key_to_char(code: int, shift: bool = False, caps: bool = False) -> str | None:
    """Return the character produced by a physical key in the US layout."""
    pair = KEYCODE_TO_EN.get(code)
    if pair is None:
        return None
    ch = pair[1] if shift else pair[0]
    if caps and ch.isalpha() and len(ch) == 1:
        ch = ch.upper() if ch.islower() else ch
    return ch


def physical_to_text(keys: list[KeyStroke], layout: str) -> str | None:
    """Translate a physical key sequence into text of ``layout``.

    Returns ``None`` if a key is not part of a printable layout key.
    """
    chars: list[str] = []
    for stroke in keys:
        en = key_to_char(stroke.code, stroke.shift)
        if en is None:
            return None
        chars.append(en if layout == "en" else translate_char(en, "ru"))
    return "".join(chars)


def text_to_keys(text: str, layout: str) -> list[KeyStroke] | None:
    """Return the key sequence needed to type ``text`` in ``layout``.

    ``None`` means at least one character cannot be typed with this layout.
    """
    table = EN_CHAR_TO_KEY if layout == "en" else RU_CHAR_TO_KEY
    keys: list[KeyStroke] = []
    for ch in text:
        stroke = table.get(ch)
        if stroke is None:
            return None
        keys.append(stroke)
    return keys


def apply_custom_pairs(pairs: list[list[str]] | list[tuple[str, str]]) -> list[str]:
    """Add/override character pairs from the config.

    ``pairs = [["q", "й"], ["w", "ц"]]`` - English character first, Russian
    character second.  The built-in standard ЙЦУКЕН table stays the base; the
    entries only override individual keys (useful for custom layout tweaks).
    Returns human-readable warnings for invalid entries.
    """
    warnings: list[str] = []
    for index, pair in enumerate(pairs or []):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            warnings.append(f"mapping.pairs[{index}]: expected two characters")
            continue
        en, ru = pair
        if not isinstance(en, str) or not isinstance(ru, str):
            warnings.append(f"mapping.pairs[{index}]: values must be strings")
            continue
        if len(en) != 1 or len(ru) != 1:
            warnings.append(f"mapping.pairs[{index}]: values must be single characters")
            continue
        EN_TO_RU[en] = ru
        RU_TO_EN[ru] = en
        stroke = EN_CHAR_TO_KEY.get(en)
        if stroke is not None:
            RU_CHAR_TO_KEY[ru] = stroke
    return warnings
