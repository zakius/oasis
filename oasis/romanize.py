"""Romanization of names and lyrics: roma's Romanizer (roma core: Romanize.kt, and romanizeLyrics in Lyrics.kt) ported
with its default settings: Hepburn with macrons, pinyin with tones, English-style Russian, classical Greek, and Han text
read as Japanese unless there is evidence that it is Chinese. tests/romanization-cases.tsv holds roma's own outputs, so
the two stay the same: change behaviour there first.

Strings are code points here and UTF-16 units in Kotlin; the two differ only around supplementary characters (𠮷)."""
import re
import threading
import unicodedata
from functools import lru_cache
from typing import NamedTuple

import icu
from sudachipy import Dictionary, SplitMode

U = icu.UScriptCode
LATINISH = {U.LATIN, U.COMMON, U.INHERITED}
WS = " \t\n\x0b\f\r"  # Java's \s
APOSTROPHES = "'’ʼ"
VOWELS_CYR = "аеёиоуыэюяіїєaeiou"


@lru_cache(maxsize=None)
def script(c: str) -> int:
    return icu.Script.getScript(ord(c)).getScriptCode()


def is_kana(c: str) -> bool:
    return "ぁ" <= c <= "ヺ" or "ㇰ" <= c <= "ㇿ" or "ｦ" <= c <= "ﾝ"


def has_kana(s: str) -> bool:
    return any(map(is_kana, s))


def has_han(s: str) -> bool:
    return any(script(c) == U.HAN for c in s)


def opening(s: str) -> bool:
    """Opening punctuation by Unicode's classes (「 （ “)."""
    return bool(s) and all(unicodedata.category(c) in ("Ps", "Pi") for c in s)


def closing(s: str) -> bool:
    """Closing punctuation (」 ）”); ’ is the apostrophe too, so it doesn't close."""
    return bool(s) and all(c != "’" and unicodedata.category(c) in ("Pe", "Pf") for c in s)


def has_non_latin(s: str) -> bool:
    if s.isascii():  # every ASCII character is Latin or Common
        return False
    return any(script(c) not in LATINISH for c in s)


def has_cyrillic(s: str) -> bool:
    return any(script(c) == U.CYRILLIC for c in s)


def alnum(c: str) -> bool:  # Kotlin's isLetterOrDigit
    return c.isalpha() or c.isdecimal()


def is_latin(original: str, latin: str) -> bool:
    """Nothing to romanize: a difference made only by width or punctuation ((Yeah) for （Yeah）) doesn't count."""
    return original == latin or not has_non_latin(original)


_translit, _translit_lock = {}, threading.Lock()


def translit(id: str, s: str) -> str:
    with _translit_lock:
        if id not in _translit:
            _translit[id] = icu.Transliterator.createInstance(id)
        return _translit[id].transliterate(s)


def normalize(s: str) -> str:
    """NFKC on the half- and full-width forms block only (ＬＯＶＥ → LOVE, ｱｲ → アイ); a full-width parenthesis gets a
    space next to a word: テトリス（feat. X）."""
    s = re.sub(f"(?<=[^{WS}])（", " （", s)
    s = re.sub(f"）(?=[^{WS})）])", "） ", s)
    return long_vowel_marks(re.sub("[＀-￯]+", lambda m: unicodedata.normalize("NFKC", m[0]), s))


def long_vowel_marks(s: str) -> str:
    """一 or a dash typed for ー after katakana (ブル一ライト), but not before a kanji (カード一枚 is "one card")."""
    s = re.sub("(?<=[ァ-ヺ])[一—―－‐]", lambda m: "ー" if m.end() == len(s) or script(s[m.end()]) != U.HAN else m[0], s)
    return re.sub("(?<=[ァ-ヺ])-(?=[ァ-ヺ])", "ー", s)


def _around(s: str, chars: str, repl: str, before=None, after=None) -> str:
    """Each of chars replaced where the characters before and after it pass (a regex lookaround on \\p{L})."""
    return "".join(repl if c in chars and (before is None or i > 0 and before(s[i - 1]))
                   and (after is None or i + 1 < len(s) and after(s[i + 1])) else c for i, c in enumerate(s))


def _letter_or_number(c):
    return c.isalpha() or unicodedata.category(c)[0] == "N"


def punctuation(s: str) -> str:
    """、。「」『』・～ as Latin punctuation; ・ becomes a space only between words, so (´・ω・`) keeps it."""
    s = _around(s, "、", ", ", after=str.isalpha).replace("、", ",")
    s = _around(s, "。", ". ", after=str.isalpha).replace("。", ".")
    s = _around(s, "「『", ' "', before=_letter_or_number)
    s = _around(s, "」』", '" ', after=_letter_or_number)
    s = re.sub("[「」『』]", '"', s)
    s = _around(s, "・", " ", before=str.isalpha, after=str.isalpha)
    return s.replace("〜", "~").replace("～", "~")


def strip_marks(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def capitalize(r: str, all_caps: bool = False) -> str:
    """The first letter, wherever it is: "~anata" → "~Anata"."""
    if all_caps:
        return r.upper()
    i = next((i for i, c in enumerate(r) if c.isalpha()), -1)
    return r if i < 0 else r[:i] + r[i].upper() + r[i + 1:]


def words(s: str, title: bool) -> str:
    return " ".join(capitalize(w) for w in s.split(" ")) if title else s


def cap_first(original: str, latin: str) -> str:
    """The first word is capitalized (βίος → "Bios"), unless it was Latin already or stayed as it was."""
    cp = next((c for c in original if c.isalpha()), None)
    out = next((c for c in latin if c.isalpha()), None)
    if out is None or cp is None or script(cp) == U.LATIN or script(out) != U.LATIN:
        return latin
    return capitalize(latin)


def all_caps(s: str, i: int) -> bool:
    """The capital at i is part of an all-caps word: the next letter is a capital too, or it ends one ("ПЛАЩ")."""
    nxt = s[i + 1] if i + 1 < len(s) else None
    return nxt is not None and nxt.isupper() or (nxt is None or not nxt.isalpha()) and i > 0 and s[i - 1].isupper()


def append_word(out: str, word: str) -> str:
    return out + (" " if out and word and alnum(out[-1]) and alnum(word[0]) else "") + word


def stylized(word: str) -> bool:
    """A word mixing Latin with Greek or Cyrillic letters (ΛRCTIC, ЯOCK, μ's) stays as it is."""
    scripts = {script(c) for c in word if c.isalpha()}
    return U.LATIN in scripts and (U.GREEK in scripts or U.CYRILLIC in scripts)


VERSION_TAG = re.compile(r"\b(ver|version|size|mix|remix|live|edit|acoustic|instrumental|inst|cover|feat|ft|prod|from|op|ed|"
                         r"opening|ending|theme|remaster|remastered|mv|short|full|tv|demo|karaoke|off vocal)\b", re.I | re.A)


def own_latin(s: str) -> str | None:
    """"スピカ - Spica" → "Spica": the Latin form a title carries after " - ", " / " or in parentheses."""
    n = normalize(s)
    m = re.search(r"^(.+?) (?:-|/) (.+)$", n) or re.search(f"^(.+?)[{WS}]*\\(([^()]+)\\)$", n)
    if not m:
        return None
    left, right = m[1], m[2]
    if not has_non_latin(left) or has_non_latin(right) or not any(c.isalpha() for c in right) \
            or VERSION_TAG.search(re.sub(r"\(.*?\)", "", right)):
        return None
    return right.strip()


def is_japanese(s: str, siblings=()) -> bool:
    """Kana in the string or a sibling (the item's other fields) means Japanese; Han text without kana is Japanese
    unless it or a sibling looks Chinese."""
    if has_kana(s) or any(map(has_kana, siblings)):
        return True
    return has_han(s) and not looks_chinese(s) and not any(map(looks_chinese, siblings))


def _variant(c: str) -> bool:  # compatibility ideographs and supplementary planes: variant forms Japanese names use (﨑, 𠮷)
    return ord(c) >= 0x10000 or 0xF900 <= ord(c) <= 0xFAFF


def _jis(c: str) -> bool:
    try:
        c.encode("shift_jis")
        return True
    except UnicodeEncodeError:
        return False


@lru_cache(maxsize=4096)
def looks_chinese(s: str) -> bool:
    """Evidence that Han text is Chinese: a character outside JIS X 0208, a Han word the Japanese dictionary doesn't
    know, or three one-character Han words in a row (周|杰|倫), as Japanese compounds are dictionary words."""
    if not has_han(s):
        return False
    if any(script(c) == U.HAN and not _variant(c) and not _jis(c) for c in s):
        return True
    found, run = tokens(s), 0
    if any(has_han(t.surface) and not has_kana(t.reading) and not any(map(_variant, t.surface)) for t in found):
        return True
    for t in found:
        run = run + 1 if len(t.surface) == 1 and ord(t.surface) < 0x10000 and has_han(t.surface) else 0
        if run >= 3:
            return True
    return False


class Token(NamedTuple):
    surface: str
    reading: str
    pos: tuple
    begin: int
    end: int


_tokenizer, _tokenizer_lock = None, threading.Lock()


def tokens(s: str) -> list[Token]:
    """SudachiDict core, split mode C (roma's). A token's surface is the original text: 君(きみ) keeps its yomigana."""
    global _tokenizer
    with _tokenizer_lock:
        if _tokenizer is None:
            _tokenizer = Dictionary(dict="core").tokenizer(SplitMode.C)
        return [Token(s[m.begin():m.end()], m.reading_form(), tuple(m.part_of_speech()), m.begin(), m.end())
                for m in _tokenizer.tokenize(s, SplitMode.C)]


LOOSE_AUX = {"助動詞-デス", "助動詞-ラシイ", "助動詞-ダ"}  # auxiliaries that stay words of their own: "iku deshō"
O_ROW = "オコソトノホモヨロゴゾドボポョォ"
U_ROW = "ウクスツヌフムユルグズヅブプュゥ"


# Names Sudachi reads wrong, by their tokens: the reading their MusicBrainz sort name gives (Yonezu, Kenshi).
NAME_READINGS = {("米津", "玄師"): ("ヨネヅ", "ケンシ")}
NAME_FRAGMENTS = {"助詞", "助動詞", "感動詞", "接尾辞"}


def names(found: list[Token], title: bool) -> list[Token]:
    """NAME_READINGS applied; in title case also kana after a surname that Sudachi split into function words and that
    runs to the end of the name (幾田|り|ら, 椎名|も|た) made one given name, as a name has no grammar to split."""
    out, i = [], 0
    while i < len(found):
        key = next((k for k in NAME_READINGS if tuple(t.surface for t in found[i:i + len(k)]) == k), None)
        if key:
            out += [t._replace(reading=r) for t, r in zip(found[i:i + len(key)], NAME_READINGS[key])]
            i += len(key)
            continue
        out.append(found[i])
        j = i + 1
        while title and found[i].pos[3] == "姓" and j < len(found) and all(map(is_kana, found[j].surface)) and found[j].pos[0] in NAME_FRAGMENTS:
            j += 1
        if j - i > 2 and found[i + 1].pos[0] != "接尾辞" and (j == len(found) or not has_kana(found[j].surface) and not has_han(found[j].surface)):
            run = found[i + 1:j]
            out.append(Token("".join(t.surface for t in run), "".join(t.reading for t in run), ("名詞", "固有名詞", "人名", "名", "*", "*"), run[0].begin, run[-1].end))
            i = j
        else:
            i += 1
    return out


def japanese(s: str, title: bool) -> list[tuple[str, str, str | None]]:
    """(surface, romaji with its leading space, reading) per token. In title case every word but a particle is
    capitalized."""
    result, found = [], names(tokens(s), title)
    prev_word, prev, sokuon, stray = False, None, False, False
    prev_close = False  # the previous token was closing punctuation (」, ）, ”): a word after it starts a new one
    for i, t in enumerate(found):
        surface, pos, nxt = t.surface, t.pos[0], found[i + 1] if i + 1 < len(found) else None
        prev_pos = prev.pos[0] if prev else None
        japanese_text = has_kana(surface) or has_han(surface)
        # A kanji the dictionary doesn't know (a simplified Chinese 词) has no kana reading: it goes the Chinese way.
        unknown_han = has_han(surface) and not has_kana(t.reading)
        reading = spoken(t.reading if has_kana(t.reading) else to_katakana(surface), t, nxt) if japanese_text and not unknown_han else None
        romaji = kana(reading) if reading is not None else other(surface, title, cyrillic_language(surface)) if has_non_latin(surface) else surface
        if sokuon and japanese_text:  # the previous token ended in ッ: dat|ta
            if romaji.startswith("ch"):
                romaji = "t" + romaji
            elif romaji and romaji[0].isalpha() and romaji[0] not in "aiueonāīūēō":
                romaji = romaji[0] + romaji
        sokuon = japanese_text and to_katakana(surface).endswith("ッ")
        # Auxiliaries, suffixes, a prefix, the te-form and ば attach to their word ("aruita", "okāsan", "ikeba"); a
        # subsidiary verb glues to a verb stem ("hashiridasu") but not after the te-form ("tabete iru").
        after_inflected = prev_pos in ("動詞", "形容詞", "助動詞")
        glue = (pos == "助動詞" and after_inflected and t.pos[4] not in LOOSE_AUX
                or pos == "接尾辞" or prev_pos == "接頭辞"
                or pos == "動詞" and t.pos[1] == "非自立可能" and prev_pos == "動詞"
                or pos == "形容詞" and t.pos[1] == "非自立可能" and after_inflected
                or t.pos[1] == "接続助詞" and (surface in ("て", "で", "ちゃ", "じゃ") or surface == "ば" and after_inflected)
                or surface == "って" and prev is not None and prev.surface == "だ"
                or prev is not None and prev.pos[1] == "数詞" and pos == "名詞" or stray
                or t.pos[1] == "準体助詞" and surface == "ん"
                or prev is not None and all(c in "ぁぃぅぇぉァィゥェォ" for c in surface))  # うっせぇ: a small vowel stretches
        # A sentence-final particle can't start a phrase: it is a misread first syllable (よ|ふかし).
        stray = t.pos[1] == "終助詞" and not prev_word and nxt is not None and has_kana(nxt.surface)
        particle = pos == "助詞" and not stray or pos == "助動詞" and t.pos[4] == "助動詞-ダ" and surface in ("に", "な", "で")
        is_word = bool(romaji) and alnum(romaji[0])
        if title and japanese_text and is_word and not (prev_word and glue) and not particle:
            romaji = capitalize(romaji)
        # Paired punctuation sits against what it encloses, with a space on its outer side: "Itai" to "itai".
        space = prev_word and is_word and not glue or prev_close and (is_word or opening(surface)) or prev_word and opening(surface)
        result.append((surface, (" " if space else "") + romaji, reading))
        prev_word, prev, prev_close = bool(romaji) and alnum(romaji[-1]), t, closing(surface)
    return result


def spoken(reading: str, t: Token, nxt: Token | None) -> str:
    """Hepburn writes what is said: particles は へ as wa e, a long o or u as ō ū where the spelling has オウ, オオ or
    ウウ (tōkyō, ōkii, kūki), but a verb's final ウ is its ending (omou) except in the volitional (ikō). 何 is nan
    before t, d and n sounds."""
    if t.pos[0] == "助詞" and t.surface in ("は", "へ"):
        return {"は": "ワ", "へ": "エ"}[t.surface]
    if t.surface == "何" and t.pos[0] == "代名詞":
        return "ナン" if nxt and nxt.reading[:1] and nxt.reading[0] in "タチツテトダヂヅデドナニヌネノ" else "ナニ"
    verb_ending = t.pos[0] == "動詞" and t.pos[5] != "意志推量形"
    return "".join("ー" if i > 0 and (c == "ウ" and (reading[i - 1] in O_ROW or reading[i - 1] in U_ROW) or c == "オ" and reading[i - 1] in O_ROW)
                   and not (verb_ending and i == len(reading) - 1) else c for i, c in enumerate(reading))


def to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


KANA = {}
for chars, romaji in [("アイウエオ", "a i u e o"), ("カキクケコ", "ka ki ku ke ko"), ("サシスセソ", "sa shi su se so"),
                      ("タチツテト", "ta chi tsu te to"), ("ナニヌネノ", "na ni nu ne no"), ("ハヒフヘホ", "ha hi fu he ho"),
                      ("マミムメモ", "ma mi mu me mo"), ("ヤユヨ", "ya yu yo"), ("ラリルレロ", "ra ri ru re ro"),
                      ("ワヰヱヲ", "wa i e o"), ("ガギグゲゴ", "ga gi gu ge go"), ("ザジズゼゾ", "za ji zu ze zo"),
                      ("ダヂヅデド", "da ji zu de do"), ("バビブベボ", "ba bi bu be bo"), ("パピプペポ", "pa pi pu pe po"),
                      ("ァィゥェォ", "a i u e o"), ("ャュョヮヵヶヴ", "ya yu yo wa ka ke vu")]:
    KANA.update(zip(chars, romaji.split()))
MACRONS = dict(zip("aiueo", "āīūēō"))


def kana(text: str) -> str:
    """Katakana or hiragana to Modified Hepburn."""
    s, out, i = to_katakana(text), "", 0
    while i < len(s):
        c = s[i]
        if c == "ッ":
            following = syllable(s, i + 1)[0]
            out += "t" if following.startswith("ch") else following[0] if following and following[0] not in "aiueon" else ""
        elif c == "ン":
            following = syllable(s, i + 1)[0]
            out += "n'" if following and following[0] in "aiueoy" else "n"
        elif c == "ー":
            if out and out[-1] in MACRONS:
                out = out[:-1] + MACRONS[out[-1]]
            elif not out or out[-1] not in "āīūēō":
                out += "-"
        else:
            romaji, used = syllable(s, i)
            out += romaji
            i += used
            continue
        i += 1
    return out


def syllable(s: str, i: int) -> tuple[str, int]:
    if i >= len(s):
        return "", 1
    c, n = s[i], s[i + 1] if i + 1 < len(s) else ""
    if (base := KANA.get(c)) is None:
        return c, 1
    if n and n in "ャュョ":
        stem = base[:-1]
        return stem + ("" if stem.endswith(("sh", "ch", "j")) else "y") + KANA[n][1:], 2
    if n and n in "ァィゥェォ" and c not in "ァィゥェォャュョッン":
        return ("w" if c == "ウ" else base[:-1]) + KANA[n], 2
    return base, 1


def other(s: str, title: bool, cyr_lang: str) -> str:
    """Non-Japanese text: each run of one script converted, a space kept between words; stylized words stay."""
    out = ""
    for chunk in re.findall(f"[{WS}]+|[^{WS}]+", s):
        if chunk.isspace() or stylized(chunk):
            out += chunk
            continue
        i = 0
        while i < len(chunk):
            sc, j = script(chunk[i]), i + 1
            # An apostrophe inside a Cyrillic word belongs to it: Ukrainian м'ясо.
            while j < len(chunk) and (script(chunk[j]) == sc or sc == U.CYRILLIC and chunk[j] in APOSTROPHES
                                      and j + 1 < len(chunk) and script(chunk[j + 1]) == sc):
                j += 1
            run = chunk[i:j]
            # A lone Greek or Cyrillic letter between symbols is part of a kaomoji: (´・ω・`)
            kaomoji = j - i == 1 and i > 0 and j < len(chunk) and not chunk[i - 1].isalpha() and not chunk[j].isalpha()
            if sc == U.HAN:
                converted = words(translit("Han-Latin", run), title)
            elif sc == U.HANGUL:
                converted = words(hangul(run), title)
            elif sc == U.CYRILLIC:
                converted = run if kaomoji else cyrillic(run, cyr_lang)
            elif sc == U.GREEK:
                converted = run if kaomoji else strip_marks(translit("Greek-Latin", run))
            elif sc in LATINISH:
                converted = run
            else:
                converted = words(translit("Any-Latin", run), title)
            out = append_word(out, converted)
            i = j
    return out


# Korean: Revised Romanization
INITIAL = "g kk n d tt r m b pp s ss _ j jj ch k t p h".replace("_", "").split(" ")
VOWEL = "a ae ya yae eo e yeo ye o wa wae oe yo u wo we wi yu eu ui i".split()
CODA = ",k,k,k,n,n,n,t,l,k,m,l,l,l,p,l,m,p,p,t,t,ng,t,t,k,t,p,t".split(",")
LIAISON = [tuple(p.split(":")) for p in ":,:g,:kk,k:s,:n,n:j,n:,:d,:r,l:g,l:m,l:b,l:s,l:t,l:p,l:,:m,:b,p:s,:s,:ss,ng:,:j,:ch,:k,:t,:p,:".split(",")]
NASAL = {"k": "ng", "t": "n", "p": "m"}


def hangul(s: str) -> str:
    out, pending = "", None
    for i, c in enumerate(s):
        if not "가" <= c <= "힣":
            out, pending = out + c, None
            continue
        code = ord(c) - 0xAC00
        initial, vowel, t = pending if pending is not None else INITIAL[code // 588], VOWEL[code % 588 // 28], code % 28
        pending, coda = None, CODA[t]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if nxt and "가" <= nxt <= "힣" and t != 0:
            next_l = (ord(nxt) - 0xAC00) // 588
            if next_l == 11 and t != 21:  # ㅇ: the final consonant moves to the next syllable
                coda, pending = LIAISON[t]
            elif next_l in (2, 6):  # ㄴ ㅁ: nasalization
                coda = NASAL.get(coda, coda)
                if coda == "l" and next_l == 2:
                    pending = "l"
            elif next_l == 5:  # ㄹ
                coda, pending = ("l", "l") if coda in ("n", "l") else (NASAL.get(coda, coda), "n")
            elif t in (27, 6, 15) and next_l in (0, 3, 12):  # ㅎ before ㄱ ㄷ ㅈ: aspiration
                coda, pending = "" if t == 27 else CODA[t], {0: "k", 3: "t"}.get(next_l, "ch")
        out += initial + vowel + coda
    return out


def cyrillic_language(s: str) -> str:
    """"kk", "ky", "uk", "be", "sr", "mk", "bg", "mn" or "ru" (the default), from the letters only some languages have."""
    t = s.lower()

    def has(letters):
        return any(c in letters for c in t)
    if has("әғқұһ") or has("і") and has("ңөү"):
        return "kk"
    if has("ң"):
        return "ky"
    if has("өү"):
        return "mn"
    if has("ў") or has("і") and has("ыэё") and not has("иєїґ"):
        return "be"
    if has("єїґі"):
        return "uk"
    if has("ѓќѕ"):
        return "mk"
    if has("ђјљњћџ"):
        return "sr"
    if re.search("ъ[бвгджзйклмнпрстфхцчшщ]", t) and not has("ыэё"):
        return "bg"
    return "ru"


RU = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
ALPHABETS = {"ru": RU, "kk": RU + "әғқңөұүһі", "ky": RU + "ңөү", "mn": RU + "өү", "uk": "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя",
             "be": "абвгдеёжзійклмнопрстуўфхцчшыьэюя", "bg": "абвгдежзийклмнопрстуфхцчшщъьюя",
             "sr": "абвгдђежзијклљмнњопрстћуфхцчџш", "mk": "абвгдѓежзѕијклљмнњопрстќуфхцчџш"}


def cyrillic_language_of_line(line: str, song: str) -> str:
    """The song's language, unless the line has a Cyrillic letter that language lacks; then the line's own."""
    alphabet = ALPHABETS.get(song)
    fits = alphabet is None or all(script(c) != U.CYRILLIC or c in alphabet for c in line.lower())
    return song if fits else cyrillic_language(line)


def _table(letters: str, latin: str) -> dict:
    return dict(zip(letters, latin.split(","), strict=True))


CYR = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґў"
ENGLISH = _table(CYR, "a,b,v,g,d,e,yo,zh,z,i,y,k,l,m,n,o,p,r,s,t,u,f,kh,ts,ch,sh,shch,,y,,e,yu,ya,i,yi,ye,g,w")
NATIONAL = {
    # Kyrgyz has no official Latin alphabet: BGN/PCGN 1979. Kazakh: the 2021 alphabet. Ukrainian: the 2010 national
    # transliteration. Belarusian: the 2007 national system (е ё ю я are j or i plus this vowel). Serbian: Gaj's
    # alphabet. Macedonian: the official romanization. Bulgarian: the 2009 Streamlined System.
    "ky": _table("абвгдеёжзийклмнңоөпрстуүфхцчшщъыьэюя", "a,b,v,g,d,e,yo,j,z,i,y,k,l,m,n,ng,o,ö,p,r,s,t,u,ü,f,kh,ts,ch,sh,shch,,y,,e,yu,ya"),
    "kk": _table("абвгдеёжзийклмнопрстуфхцчшщъыьэюяәғқңөұүһі", "a,b,v,g,d,e,io,j,z,i,i,k,l,m,n,o,p,r,s,t,u,f,h,ts,tş,ş,ştş,,y,,e,iu,ia,ä,ğ,q,ñ,ö,ū,ü,h,ı"),
    "uk": _table("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя", "a,b,v,h,g,d,e,ie,zh,z,y,i,i,i,k,l,m,n,o,p,r,s,t,u,f,kh,ts,ch,sh,shch,,iu,ia"),
    "be": _table("абвгґдеёжзійклмнопрстуўфхцчшыэюя", "a,b,v,h,g,d,e,o,ž,z,i,j,k,l,m,n,o,p,r,s,t,u,ŭ,f,ch,c,č,š,y,e,u,a"),
    "sr": _table("абвгдђежзијклљмнњопрстћуфхцчџш", "a,b,v,g,d,đ,e,ž,z,i,j,k,l,lj,m,n,nj,o,p,r,s,t,ć,u,f,h,c,č,dž,š"),
    "mk": _table("абвгдѓежзѕијклљмнњопрстќуфхцчџш", "a,b,v,g,d,gj,e,ž,z,dz,i,j,k,l,lj,m,n,nj,o,p,r,s,t,kj,u,f,h,c,č,dž,š"),
    "bg": _table("абвгдежзийклмнопрстуфхцчшщъьюя", "a,b,v,g,d,e,zh,z,i,y,k,l,m,n,o,p,r,s,t,u,f,h,ts,ch,sh,sht,a,y,yu,ya"),
}
UK_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def cyrillic(s: str, lang: str) -> str:
    """Russian English-style, Mongolian by ICU's MNS, the others by their national Latin forms. What a table leaves
    goes through ICU's Cyrillic-Latin, so no Cyrillic remains."""
    if lang == "ru":
        out = _english(s)
    elif lang == "mn":
        try:
            out = translit("mn-mn_Latn/MNS", s)
        except icu.ICUError:
            out = s
    else:
        out = _national(s, lang)
    if not has_cyrillic(out):
        return out
    out = translit("Cyrillic-Latin", out)
    if not has_cyrillic(out):
        return out
    # ICU skips letters such as Tatar җ and Tajik ҷ: take the letter they are built on (ж, ч), or drop them.
    based = "".join((_base_letter(c) or "") if has_cyrillic(c) else c for c in out)
    return "".join(c for c in translit("Cyrillic-Latin", based) if not has_cyrillic(c))


def _english(s: str) -> str:
    out = ""
    for i, c in enumerate(s):
        lower = c.lower()
        r = ENGLISH.get(lower, c)
        if lower == "е" and (i == 0 or s[i - 1].lower() in VOWELS_CYR or not s[i - 1].isalpha()):
            r = "ye"  # at the start of a word and after a vowel
        out += capitalize(r, all_caps(s, i)) if c.isupper() else r
    return out


def _national(s: str, lang: str) -> str:
    table, out = NATIONAL[lang], ""
    for i, c in enumerate(s):
        lower = c.lower()
        prev, nxt = s[i - 1].lower() if i > 0 else None, s[i + 1].lower() if i + 1 < len(s) else None
        initial = prev is None or not prev.isalpha() and prev not in APOSTROPHES
        if lower in APOSTROPHES and lang in ("uk", "be"):
            continue
        if lang == "uk" and lower in "єїйюя":
            r = UK_INITIAL[lower] if initial else table[lower]
        elif lang == "uk" and lower == "г" and prev == "з":
            r = "gh"
        elif lang == "be" and lower in "еёюя":
            soft = initial or prev in VOWELS_CYR or prev in APOSTROPHES or prev in "ьў"
            r = ("j" if soft else "i") + table[lower]
        elif lang == "be" and lower == "ь":
            if prev is not None and prev in "злнсц":  # зь ль нь сь ць take an acute: ź ĺ ń ś ć
                out = out[:-1] + unicodedata.normalize("NFC", out[-1] + "́")
            continue
        elif lang == "bg" and lower == "я" and prev == "и" and (nxt is None or not nxt.isalpha()):
            r = "a"  # final ия: ia
        else:
            r = table.get(lower, c)
        if c.isupper():
            r = capitalize(r, all_caps(s, i))
            if lang == "kk" and r.startswith("I") and lower != "і":
                r = "İ" + r[1:]
        out += r
    return out


CYRILLIC_NAMES = {unicodedata.name(chr(c)): chr(c) for c in range(0x400, 0x460)}


def _base_letter(c: str) -> str | None:
    """"CYRILLIC SMALL LETTER ZHE WITH DESCENDER" → ж; "… BASHKIR KA" → к."""
    if not (name := unicodedata.name(c, "")):
        return None
    case = "CAPITAL" if c.isupper() else "SMALL"
    letter = name.split("LETTER ", 1)[-1].split(" WITH ")[0]
    return CYRILLIC_NAMES.get(f"CYRILLIC {case} LETTER {letter}") or CYRILLIC_NAMES.get(f"CYRILLIC {case} LETTER {letter.split(' ')[-1]}")


def raw(n: str, japanese_text: bool, title: bool, cyr_lang: str | None = None) -> str:
    return punctuation("".join(r for _, r, _ in japanese(n, title)) if japanese_text else other(n, title, cyr_lang or cyrillic_language(n)))


def machine(s: str, siblings=(), sentence: bool = False) -> str:
    """Title case for names and titles; sentence case (only the first letter) for lyric lines."""
    n = normalize(s)
    if not has_non_latin(n):
        return n
    return _machine(n, is_japanese(n, siblings), sentence)


@lru_cache(maxsize=20000)
def _machine(n: str, japanese: bool, sentence: bool) -> str:
    return cap_first(n, raw(n, japanese, not sentence))  # siblings only decide whether it's Japanese


def text(s: str, siblings=(), title_of: bool = False) -> str:
    """s's Latin form. Siblings are the item's other fields (artist, album, title). A song or album title passes
    title_of, so "スピカ - Spica" gives its own Latin form."""
    return (title_of and own_latin(s)) or machine(s, siblings)


def parts(items: list[str], siblings=(), cyr_lang: str | None = None) -> list[str]:
    """A line split into parts (karaoke words), one result per part. Japanese is read as a whole line, so each reading
    stays right in context; a token spanning several parts goes to the part where it starts. Sentence case."""
    normed = [normalize(p) for p in items]
    joined = long_vowel_marks("".join(normed))  # again across parts: ブル|一|ライト
    starts = [0]
    for p in normed:
        starts.append(starts[-1] + len(p))
    norm = [joined[a:b] for a, b in zip(starts, starts[1:])]
    if not is_japanese(joined, siblings):
        lang = cyr_lang or cyrillic_language(joined)
        out = [raw(p, is_japanese(p, siblings), False, lang) if has_non_latin(p) else p for p in norm]
        # Chinese words are one or two syllables apart ("gù shì de"); Hangul syllables join into their word.
        for k in range(1, len(out)):
            if out[k] and alnum(out[k][0]) and script(norm[k][0]) == U.HAN and (done := "".join(out[:k])) and alnum(done[-1]):
                out[k] = " " + out[k]
    else:
        found, pos, last_part = [""] * len(norm), 0, len(norm) - 1
        for surface, romaji, reading in japanese(joined, False):
            i = min(max(k for k, at in enumerate(starts) if at <= pos), last_part)
            last = min(max(max(k for k, at in enumerate(starts) if at < pos + len(surface)), i), last_part)
            split = _split_reading(surface, pos, reading, romaji, starts, i, last) if last > i and reading is not None else None
            if split is None:
                found[i] += romaji
            else:
                for k, r in enumerate(split):
                    found[i + k] += r
            pos += len(surface)
        out = [punctuation(w) for w in found]
    if (first := next((i for i, w in enumerate(out) if w.strip()), None)) is not None:
        out[first] = cap_first(norm[first], out[first])
    return out


def _split_reading(surface, at, reading, romaji, starts, first, last) -> list[str] | None:
    """A word timed over several parts (忙|し|い): kana parts at either end take their own reading, the kanji between
    the rest ("isoga" | "shi" | "i"). None when they wouldn't add up to the word's romaji (行|こ|う)."""
    pieces = [surface[max(starts[p] - at, 0):min(starts[p + 1] - at, len(surface))] for p in range(first, last + 1)]
    out, rest, lo, hi = [""] * len(pieces), reading, 0, len(pieces) - 1
    while lo <= hi and all(map(is_kana, pieces[lo])) and rest.startswith(to_katakana(pieces[lo])):
        out[lo], rest, lo = kana(to_katakana(pieces[lo])), rest[len(pieces[lo]):], lo + 1
    while hi >= lo and all(map(is_kana, pieces[hi])) and rest.endswith(to_katakana(pieces[hi])):
        out[hi], rest, hi = kana(to_katakana(pieces[hi])), rest[:len(rest) - len(pieces[hi])], hi - 1
    if lo <= hi:
        out[lo] = kana(rest)
    elif rest:
        return None
    if "".join(out) != romaji.lstrip():
        return None
    k = next((k for k, o in enumerate(out) if o), 0)
    out[k] = romaji[:len(romaji) - len(romaji.lstrip(" "))] + out[k]
    return out


def pieces(line: str, siblings=()) -> list[str]:
    """A line split into the words its romanization shows: in Japanese a word with its glued endings and the particles
    apart (食べます|君|に、), opening punctuation with the word it opens (と|「居たい」), elsewhere at spaces. The pieces join back into the line."""
    if not is_japanese(normalize(line), siblings):
        return re.findall(f"^[{WS}]*[^{WS}]+[{WS}]*|[^{WS}]+[{WS}]*", line) or [line]
    out, cur, has_word, ends_loose = [], "", False, False
    for surface, romaji, _ in japanese(line, False):
        trimmed = romaji.lstrip()
        is_word = bool(trimmed) and alnum(trimmed[0]) or has_non_latin(surface)
        opens = opening(surface)
        if cur and has_word and (is_word and (romaji.startswith(" ") or ends_loose) or opens):
            out.append(cur)
            cur, has_word = "", False
        cur += surface
        has_word, ends_loose = has_word or is_word, not is_word and not opens
    return out + [cur] if cur else out


def lyrics(lines: list[str], siblings=(), lang: str | None = None) -> list[str]:
    """Lyric lines in sentence case. The language is decided once per song: kana in any line makes every line
    Japanese, else Chinese evidence in any line makes every Han line Chinese; the Cyrillic language comes from the whole
    lyrics, unless a line has a letter that language lacks. lang is the song's language tag, if it has one."""
    sib, song = song_context(lines, siblings, lang)
    return ["".join(parts(pieces(line, sib), sib, cyrillic_language_of_line(line, song))).strip() for line in lines]


CHINESE_TAGS = ("zh", "zho", "chi", "cmn", "yue")
CHINESE_HINT = "们"  # a simplified character outside JIS X 0208: Chinese evidence as a sibling


def song_context(lines: list[str], siblings=(), lang: str | None = None) -> tuple[list[str], str]:
    """Siblings plus a line that decides the song's Han reading: kana in any line, else Chinese evidence in any line,
    else the song's language tag (lang: ISO 639, as lyrics carry it) when it is Chinese, for Han text no clue settles."""
    chinese_tag = (lang or "").lower().split("-")[0] in CHINESE_TAGS
    hint = (next((t for t in lines if has_kana(t)), None) or next((t for t in lines if looks_chinese(t)), None)
            or (CHINESE_HINT if chinese_tag else None))
    return [*siblings, *([hint] if hint else [])], cyrillic_language("\n".join(lines))


def lyric_words(lines: list[str], words: list[list[str]], siblings=(), lang: str | None = None) -> list[list[str]]:
    """Each line's karaoke words romanized, one result per word (roma's wordLatin); [] for a line without words."""
    sib, song = song_context(lines, siblings, lang)
    return [parts(w, sib, cyrillic_language_of_line(line, song)) if w else [] for line, w in zip(lines, words)]


def language(lines: list[str], siblings=(), lang: str | None = None) -> str:
    """The language lyrics are romanized from, as romanize.lyrics decides it: ja, zh, ko, el, a Cyrillic one or und."""
    text = "\n".join(lines)
    scripts = {script(c) for c in text}
    if has_kana(text) or U.HAN in scripts:
        return "ja" if is_japanese(text, song_context(lines, siblings, lang)[0]) else "zh"
    if U.CYRILLIC in scripts:
        return cyrillic_language(text)
    return "ko" if U.HANGUL in scripts else "el" if U.GREEK in scripts else "und"
