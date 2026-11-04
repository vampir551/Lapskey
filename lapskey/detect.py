"""Language detection and the actual "wrong layout" decision.

The detector deliberately does not depend on any machine-learning library.
Two signals are combined:

1.  A spelling dictionary (hunspell ``.dic`` files, or simple word lists).
2.  Cheap language statistics: vowel ratio, consonant clusters and common
    bigrams.  The second signal is what makes short words and unusual forms
    work when no dictionary is installed.

The important property of layout mistakes is that the two candidates use
different alphabets, so "does this look like a word of language X" is a much
easier question than general language detection.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import mapping

log = logging.getLogger(__name__)

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")

VOWELS = {
    "en": frozenset("aeiouy"),
    "ru": frozenset("аеёиоуыэюя"),
}
ALPHABETS = {
    "en": mapping.EN_LETTERS | mapping.EN_UPPER,
    "ru": mapping.RU_LETTERS | mapping.RU_UPPER,
}

# A carefully chosen subset of the most frequent words.  It is intentionally
# small: the dictionary files provide the real coverage, this list only helps
# when the user has not installed hunspell dictionaries yet.
COMMON_WORDS = {
    "ru": frozenset(
        """
        и в во не что он на я с со как а то все она так его но да ты к у же вы
        за бы по только ее мне было вот от меня еще нет о из ему теперь когда
        даже ну вдруг ли если уже или ни быть был него до вас вам ведь там
        потом себя ничего ей может они тут где есть надо ней для мы тебя их чем
        была сам чтоб без будто чего раз тоже себе под будет тогда кто этот
        того потому этого какой совсем ним здесь этом один почти мой тем чтобы
        нее сейчас были куда зачем всех никогда можно перед наконец два об
        другой хоть после над больше тот через эти нас про всего них какая
        много разве три эту моя впрочем хорошо свою этой иногда лучше чуть том
        нельзя такой им более всегда конечно всю между привет спасибо
        пожалуйста здравствуйте пока добрый утро вечер день ночь время год
        человек дело жизнь работа дом рука слово место лицо дверь стол город
        вода земля страна мир вопрос ответ книга язык друг семья ребенок
        женщина мужчина мама папа брат сестра сын дочь голова глаз сердце душа
        мысль имя число история система программа компьютер клавиатура
        раскладка текст строка файл ошибка ввод вывод экран окно кнопка нажать
        печатать писать читать говорить думать знать видеть слышать хотеть мочь
        делать сделать идти прийти дать взять стать жить пить спать сидеть
        стоять лежать смотреть слушать ждать искать найти открыть закрыть
        начать кончить работать учить учиться играть любить помогать
        сегодня вчера завтра сейчас очень просто извините также поэтому
        который которая которое которые весь вся всё плохо мало быстро
        медленно утром вечером ночью извини здравствуй доброе
        """.split()
    ),
    "en": frozenset(
        """
        the be to of and a in that have i it for not on with he as you do at
        this but his by from they we say her she or an will my one all would
        there their what so up out if about who get which go me when make can
        like time no just him know take people into year your good some could
        them see other than then now look only come its over think also back
        after use two how our work first well way even new want because these
        give day most us is are was were been has had did does done should may
        might must shall hello world test ping thanks thank please keyboard
        layout switch text word line file error input output screen window
        button press type write read speak listen wait search find open close
        start finish study learn play love help house home city water earth
        country question answer book language friend family child woman man
        mother father brother sister son daughter head eye heart soul thought
        name number history system program computer
        """.split()
    ),
}

COMMON_BIGRAMS = {
    "en": frozenset(
        """
        th he in er an re on at en nd ti es or te of ed is it al ar st to nt
        ng se ha as ou io le ve co me de hi ri ro ic ne ea ra ce li ch ll be
        ma si om ur ca el ta la ns di fo ho pe ec pr no ct us ac ot il tr ly
        nc et ut ss so rs un lo wa ge ie wh ee wi em ad ol rt po we na ul ni
        ts mo ow pa im mi ai sh ir su id os iv ia am fi ci vi pl ig tu ev ld
        ry mp fe bl ab gh ty op wo sa ay ex ke fr oo av ag if ap gr od bo sp
        rd do uc bu qu
        """.split()
    ),
    "ru": frozenset(
        """
        ст но ен то на ов ни ра во ко пр по ро ос го не ал ли ре ло ан ет ер та
        ть ом ит ие ол ес ка от ат ор те же ла ил ва де ед ри ин ск ль за да ме
        об зн др кл бл пл чт зд вс вз сн тв рн рт рс нт ар ов ер ол ор
        """.split()
    ),
}

RARE_BIGRAMS = {
    "en": frozenset("qj jq qx xq qz zq jj vv kk qq wx xw".split()),
    "ru": frozenset(
        "ъь ыь ьь ъъ жы шы чы щы йй щщ ъэ ыъ ьъ ъы жю шю чю щю".split()
    ),
}

# Very small suffix stripper.  Hunspell dictionaries store stems, so inflected
# forms would otherwise be invisible to the exact lookup.
SUFFIXES = {
    "ru": (
        "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ая", "яя",
        "ое", "ее", "ые", "ие", "ой", "ей", "ий", "ый", "ов", "ев", "ам",
        "ям", "ах", "ях", "ом", "ем", "ём", "ью", "ия", "ии", "ию", "ел",
        "ла", "ло", "ли", "ть", "тся", "ешь", "ишь", "ите", "ете", "ат",
        "ят", "ут", "ют", "ал", "ил", "ыл", "а", "я", "ы", "и", "у", "ю",
        "е", "о", "ь",
    ),
    "en": (
        "ingly", "edly", "ing", "ers", "est", "ies", "ied", "ed", "es",
        "er", "ly", "s", "'s",
    ),
}

MAX_DICT_FILE_BYTES = 32 * 1024 * 1024

# A converted candidate must look like a real word, not merely "better" than
# the source.  Without this, typing gibberish such as "dfrty" was converted to
# the equally meaningless "вакен" just because the numbers were slightly higher.
MIN_CANDIDATE_SCORE = 5.0


SENTENCE_HEAD = frozenset("\"'«(\"")
SENTENCE_TAIL = frozenset(".,!?;:…»)\"'")


def _punctuation_only(text: str, target: str) -> str:
    """Translate punctuation that stays punctuation.

    ``?`` (US, Shift+/) is ``,`` in the Russian layout, so a user who selected
    only ``?`` expects ``,``.  But ``.`` maps to the letter ``ю``; converting a
    lone period into a letter would be wrong, so that one is left untouched.
    """
    table = mapping.EN_TO_RU if target == "ru" else mapping.RU_TO_EN
    out: list[str] = []
    changed = False
    for ch in text:
        mapped = table.get(ch, ch)
        if mapped != ch and not mapped.isalpha():
            out.append(mapped)
            changed = True
        else:
            out.append(ch)
    return "".join(out) if changed else text


def _internal_punct(text: str) -> int:
    """Count punctuation that is unlikely to belong to a real word.

    Internal punctuation is always suspicious; a leading ``;`` or ``[`` is
    almost certainly a Russian letter typed in the US layout, while a trailing
    comma or full stop is usually real sentence punctuation.
    """
    positions = [i for i, ch in enumerate(text) if ch.isalpha()]
    if not positions:
        return 0
    first, last = positions[0], positions[-1]
    core = text[first : last + 1]
    penalty = sum(1 for ch in core if not ch.isalpha())
    penalty += sum(1 for ch in text[:first] if ch not in SENTENCE_HEAD)
    penalty += sum(1 for ch in text[last + 1 :] if ch not in SENTENCE_TAIL)
    return penalty


@dataclass
class WordDecision:
    original: str
    converted: str
    source: str
    target: str
    convert: bool
    original_score: float
    converted_score: float
    force: bool = False

    @property
    def margin(self) -> float:
        return self.converted_score - self.original_score


@dataclass
class TextDecision:
    original: str
    converted: str
    target: str | None
    words: list[WordDecision] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.converted != self.original


class Detector:
    def __init__(
        self,
        extra_files: list[str] | None = None,
        auto_detect: bool = True,
    ) -> None:
        self._words: dict[str, set[str]] = {"ru": set(), "en": set()}
        self._dicts_loaded = False
        self._extra_files = list(extra_files or [])
        self._auto_detect = auto_detect
        self._dict_sources: list[str] = []

    # ------------------------------------------------------------------ dicts
    def _candidate_dict_files(self) -> list[tuple[Path, str]]:
        result: list[tuple[Path, str]] = []
        for name in self._extra_files:
            path = Path(os.path.expanduser(name))
            result.append((path, self._guess_lang(path.name)))
        if not self._auto_detect:
            return [(p, lang) for p, lang in result if lang]

        home = Path.home()
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        patterns = [
            ("ru", "/usr/share/hunspell/ru_RU.dic"),
            ("ru", "/usr/share/hunspell/ru.dic"),
            ("ru", "/usr/share/myspell/ru_RU.dic"),
            ("ru", "/usr/share/myspell/dicts/ru_RU.dic"),
            ("en", "/usr/share/hunspell/en_US.dic"),
            ("en", "/usr/share/hunspell/en_GB.dic"),
            ("en", "/usr/share/myspell/en_US.dic"),
            ("en", "/usr/share/myspell/dicts/en_US.dic"),
            ("en", "/usr/share/dict/words"),
        ]
        for lang, raw in patterns:
            result.append((Path(raw), lang))
        user_dir = xdg / "lapskey"
        for path in (user_dir / "words_ru.txt", user_dir / "words_en.txt"):
            result.append((path, self._guess_lang(path.name)))
        return [(p, lang) for p, lang in result if lang]

    @staticmethod
    def _guess_lang(name: str) -> str | None:
        low = name.lower()
        if "ru" in low or "rus" in low:
            return "ru"
        if "en" in low or "eng" in low or low == "words":
            return "en"
        return None

    def load_dictionaries(self) -> None:
        if self._dicts_loaded:
            return
        self._dicts_loaded = True
        for path, lang in self._candidate_dict_files():
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
                if size > MAX_DICT_FILE_BYTES:
                    log.warning("skipping huge dictionary %s (%d bytes)", path, size)
                    continue
                if path.suffix == ".dic":
                    words = self._read_hunspell(path)
                else:
                    words = self._read_wordlist(path)
                before = len(self._words[lang])
                self._words[lang].update(words)
                added = len(self._words[lang]) - before
                if added:
                    self._dict_sources.append(f"{lang}:{path} (+{added})")
                    log.info("loaded %d words for %s from %s", added, lang, path)
            except (OSError, UnicodeError) as exc:
                log.warning("cannot read dictionary %s: %s", path, exc)

        for lang, words in COMMON_WORDS.items():
            self._words[lang].update(words)
        log.info("dictionaries: %s", ", ".join(self._dict_sources) or "built-in only")

    @staticmethod
    def _read_hunspell(path: Path) -> set[str]:
        words: set[str] = set()
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.readline()  # first line is the entry count
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # "word/FLAGS" - flags are affix rules we do not expand
                word = line.split("/", 1)[0].strip()
                if word:
                    words.add(word.lower())
        return words

    @staticmethod
    def _read_wordlist(path: Path) -> set[str]:
        words: set[str] = set()
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                word = line.strip()
                if word and not word.startswith("#"):
                    words.add(word.lower())
        return words

    def dictionary_info(self) -> list[str]:
        self.load_dictionaries()
        return list(self._dict_sources)

    def _is_word(self, word: str, lang: str) -> bool:
        self.load_dictionaries()
        return word in self._words[lang]

    def _is_inflected(self, word: str, lang: str) -> bool:
        if len(word) < 4:
            return False
        for suffix in SUFFIXES[lang]:
            if len(word) - len(suffix) < 3:
                continue
            if word.endswith(suffix) and word[: -len(suffix)] in self._words[lang]:
                return True
        return False

    # ----------------------------------------------------------------- score
    def score(self, word: str, lang: str) -> float:
        """How plausible is ``word`` as a word of ``lang`` (higher is better)."""
        if not word:
            return -100.0
        lower = word.lower()
        letters = [ch for ch in lower if ch.isalpha()]
        if not letters:
            return 0.0
        alphabet = ALPHABETS[lang]
        if any(ch not in alphabet for ch in letters):
            return -100.0
        self.load_dictionaries()

        n = len(letters)
        if n == 1:
            if lower in COMMON_WORDS[lang]:
                return 9.0
            if lower in self._words[lang]:
                return 6.5
            return -4.0

        score = 0.0
        vowels = VOWELS[lang]
        vowel_count = sum(1 for ch in letters if ch in vowels)
        ratio = vowel_count / n

        if vowel_count == 0:
            score -= 6.0
        else:
            # natural ratio is around 0.40 - 0.45 for both languages
            score += 2.5 * max(0.0, 1.0 - abs(ratio - 0.43) / 0.57)

        # consonant clusters longer than three are rare in both languages
        run = 0
        for ch in letters:
            if ch in vowels:
                run = 0
            else:
                run += 1
                if run > 3:
                    score -= 1.2

        for a, b in zip(lower, lower[1:]):
            bigram = a + b
            if not (a.isalpha() and b.isalpha()):
                continue
            if bigram in COMMON_BIGRAMS[lang]:
                score += 0.55
            elif bigram in RARE_BIGRAMS[lang]:
                score -= 0.8

        # a few structural rules
        if lang == "ru":
            if lower.startswith(("ъ", "ь")):
                score -= 2.0
            if any(bad in lower for bad in ("ъь", "ыь", "ьь")):
                score -= 3.0
        else:
            if lower.startswith("q") and not lower.startswith("qu"):
                score -= 1.5
            if "q" in lower and "qu" not in lower:
                score -= 1.0

        if lower in self._words[lang]:
            score += 6.0 + min(n, 8) * 0.5
        elif self._is_inflected(lower, lang):
            score += 3.0
        # Frequency matters: "tot" is a valid English noun, but "еще" is a
        # thousand times more likely in real text.
        if lower in COMMON_WORDS[lang]:
            score += 4.0
        return score

    # -------------------------------------------------------------- decision
    def decide_word(
        self,
        word: str,
        threshold: float = 2.0,
        force: bool = False,
    ) -> WordDecision | None:
        _converted, decision = self._convert_chunk(word, threshold, force)
        return decision

    def score_text(self, text: str, lang: str) -> float:
        """Sum of word scores; punctuation is ignored."""
        return sum(self.score(match.group(0), lang) for match in WORD_RE.finditer(text))

    def _candidate_plausible(
        self, text: str, lang: str, source_score: float = 0.0
    ) -> bool:
        letters = "".join(WORD_RE.findall(text))
        if not letters:
            return False
        lower = letters.lower()
        if self._is_word(lower, lang) or self._is_inflected(lower, lang):
            return True
        score = self.score(lower, lang)
        if score >= MIN_CANDIDATE_SCORE:
            return True
        # Short inflected/rare words may not be in the dictionary.  Allow them
        # only when the source text is clearly impossible in its own language.
        return source_score < 0 and score >= 2.5

    def _convert_chunk(
        self,
        chunk: str,
        threshold: float,
        force: bool,
        source_lang: str | None = None,
    ) -> tuple[str, WordDecision | None]:
        """Convert one whitespace-free chunk.

        Two candidates are considered: translating letters only, and
        translating every key (punctuation included).  The second form is
        needed because Russian letters such as ``б``, ``ю``, ``ж``, ``х``,
        ``ъ`` and ``э`` are produced by punctuation keys in the US layout
        (``б`` -> ``,``, ``ю`` -> ``.``, ``ж`` -> ``;`` ...).  The better
        looking candidate wins, so a real trailing comma is preserved while
        ``,tp`` still becomes ``без``.

        When the chunk contains no letters at all (the user selected just a
        punctuation mark), the direction comes from the active keyboard layout
        and only punctuation-to-punctuation symbols are converted: ``?`` typed
        in the English layout becomes ``,`` in Russian, while ``.`` is left
        alone because its Russian counterpart is the letter ``ю``.
        """
        if not WORD_RE.search(chunk):
            if source_lang in ("ru", "en"):
                target = mapping.other_script(source_lang)
                converted = _punctuation_only(chunk, target)
                if converted != chunk:
                    return converted, WordDecision(
                        original=chunk,
                        converted=converted,
                        source=source_lang,
                        target=target,
                        convert=True,
                        original_score=0.0,
                        converted_score=0.0,
                        force=force,
                    )
            return chunk, None

        script = mapping.script_of(chunk)
        if script is None:
            return chunk, None
        target = mapping.other_script(script)

        letters_only = mapping.translate_letters(chunk, target)
        full = mapping.translate_text(chunk, target)
        if full == chunk:
            full = letters_only

        # Score the source without the "fake word" effect of punctuation that
        # really represents letters in the other layout ("e;t" -> "уже").
        if _internal_punct(chunk):
            original_score = self.score("".join(WORD_RE.findall(chunk)), script)
        else:
            original_score = self.score_text(chunk, script)

        # A real word has no punctuation in its middle.  Without this penalty
        # "vj;yj" would prefer the letters-only form "мо;но" (two short words)
        # over the correct "можно".
        score_letters = self.score_text(letters_only, target) - 20 * _internal_punct(letters_only)
        score_full = self.score_text(full, target) - 20 * _internal_punct(full)

        letters_ok = self._candidate_plausible(letters_only, target, original_score)
        full_ok = self._candidate_plausible(full, target, original_score)

        # Prefer the full layout translation on a tie: this is what turns the
        # trailing "?" into ",", while a "." or "," still wins the letters-only
        # form because the resulting word ("приветю", "приветб") is not real.
        if score_full >= score_letters and (full_ok or not letters_ok):
            candidate, best_score, plausible = full, score_full, full_ok
        else:
            candidate, best_score, plausible = letters_only, score_letters, letters_ok

        single = len(chunk) == 1 and chunk.isalpha()
        if single:
            convert = force or (
                chunk.lower() not in COMMON_WORDS[script]
                and candidate.lower() in COMMON_WORDS[target]
            )
        else:
            convert = force or (
                plausible and (best_score - original_score) >= threshold
            )

        decision = WordDecision(
            original=chunk,
            converted=candidate,
            source=script,
            target=target,
            convert=convert,
            original_score=original_score,
            converted_score=best_score,
            force=force,
        )
        return (candidate if convert else chunk), decision

    def convert_text(
        self,
        text: str,
        threshold: float = 2.0,
        force: bool = False,
        max_chars: int | None = None,
        source_lang: str | None = None,
    ) -> TextDecision:
        if max_chars is not None and len(text) > max_chars and not force:
            # Never mass-convert an unexpectedly huge selection in smart mode.
            return TextDecision(original=text, converted=text, target=None)

        decisions: list[WordDecision] = []
        pieces: list[str] = []
        # Split into non-space chunks, keeping the whitespace untouched.
        for match in re.finditer(r"\s+|\S+", text):
            chunk = match.group(0)
            if not chunk or chunk.isspace():
                pieces.append(chunk)
                continue

            if mapping.script_of(chunk) is None and WORD_RE.search(chunk):
                # Mixed scripts inside one chunk - handle every word on its own
                # and never translate punctuation in this case.
                def replace_word(m: re.Match[str]) -> str:
                    word = m.group(0)
                    decision = self.decide_word(word, threshold, force)
                    if decision is None:
                        return word
                    decisions.append(decision)
                    return decision.converted if decision.convert else word

                pieces.append(WORD_RE.sub(replace_word, chunk))
            else:
                converted, decision = self._convert_chunk(
                    chunk, threshold, force, source_lang
                )
                if decision is not None:
                    decisions.append(decision)
                pieces.append(converted)

        target = None
        for decision in decisions:
            if decision.convert:
                target = decision.target

        return TextDecision(
            original=text,
            converted="".join(pieces),
            target=target,
            words=decisions,
        )
