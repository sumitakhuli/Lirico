"""Devanagari -> Hinglish (romanized Hindi, the way lyrics sites write it).

Used when the best-timed lyrics for a song are in Devanagari but the user
wants English letters. Rule-based: consonant/vowel tables plus schwa deletion
(तेरे -> tere, सकते -> sakte, नहीं -> nahin). Not perfect, but readable.
"""

from __future__ import annotations

import re
import unicodedata

_CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "ळ": "l", "ऩ": "n", "ऱ": "r", "ऴ": "zh",
}
_NUKTA = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y"}
_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
           "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e", "ऎ": "e", "ऒ": "o"}
_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
           "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e", "ॆ": "e", "ॊ": "o"}
_VIRAMA, _NUKTA_SIGN = "्", "़"
_ANUSVARA, _CHANDRA, _VISARGA = "ं", "ँ", "ः"
_DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}
_DEVA_WORD = re.compile(r"[ऀ-ॿ]+")


class _Unit:
    """One syllable-ish unit: consonant cluster + vowel (None = inherent schwa)."""
    __slots__ = ("cons", "vowel", "nasal", "dead")

    def __init__(self, cons: str = "", vowel: str | None = None):
        self.cons, self.vowel, self.nasal, self.dead = cons, vowel, "", False


def _parse(word: str) -> list[_Unit]:
    units: list[_Unit] = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        if ch in _CONS:
            c = _CONS[ch]
            if i + 1 < n and word[i + 1] == _NUKTA_SIGN:
                c = _NUKTA.get(ch, c)
                i += 1
            if units and units[-1].dead:   # previous consonant had a virama: cluster
                units[-1].cons += c
                units[-1].dead = False
            else:
                units.append(_Unit(c))
        elif ch in _VOWELS:
            units.append(_Unit("", _VOWELS[ch]))
        elif ch in _MATRAS and units:
            units[-1].vowel = _MATRAS[ch]
        elif ch == _VIRAMA and units:
            units[-1].dead = True
        elif ch in (_ANUSVARA, _CHANDRA) and units:
            units[-1].nasal = "n"
        elif ch == _VISARGA and units:
            units[-1].nasal = "h"
        elif ch in _DIGITS:
            units.append(_Unit(_DIGITS[ch], ""))
        i += 1
    return units


_EXCEPTIONS = {"में": "mein", "हैं": "hain", "है": "hai", "मैं": "main", "कहीं": "kahin",
               "यहीं": "yahin", "वहीं": "wahin", "ज़िंदगी": "zindagi", "ज़िन्दगी": "zindagi",
               "जिंदगी": "zindagi", "जिन्दगी": "zindagi", "केसरिया": "kesariya", "हूँ": "hoon", "हूं": "hoon"}


def _word(word: str) -> str:
    special = _EXCEPTIONS.get(unicodedata.normalize("NFC", word))
    if special:
        return special
    units = _parse(unicodedata.normalize("NFC", word).replace("क़", "क़"))
    if not units:
        return ""
    # Schwa deletion: word-final schwa, then medial V C(a) C V from the right.
    has_schwa = [u.cons != "" and u.vowel is None and not u.dead for u in units]
    if len(units) > 1 and has_schwa[-1] and not units[-1].nasal:
        has_schwa[-1] = False
        units[-1].vowel = ""
    for i in range(len(units) - 2, 0, -1):
        if has_schwa[i] and not units[i].nasal and units[i - 1].vowel != "" and units[i + 1].vowel not in ("",):
            if (units[i + 1].vowel is not None or has_schwa[i + 1]) and units[i - 1].vowel != "" \
                    and not (i + 2 <= len(units) - 1 and units[i + 1].vowel == ""):
                if has_schwa[i - 1] is False or units[i - 1].vowel is not None or i - 1 == 0:
                    units[i].vowel = ""
                    has_schwa[i] = False
    out = []
    last = len(units) - 1
    for idx, u in enumerate(units):
        if u.dead:
            out.append(u.cons)
            continue
        v = "a" if u.vowel is None else u.vowel
        if idx == last:          # word-final long vowels read short in Hinglish
            v = {"aa": "a", "ee": "i", "oo": "u"}.get(v, v)
        cons = u.cons
        if cons == "v" and v in ("a", "aa", "o", "e", "ai", "au", ""):
            cons = "w"
        out.append(cons + v + u.nasal)
    s = "".join(out)
    return s.replace("nn", "n") if s.endswith("nn") else s


def to_hinglish(text: str) -> str:
    if not text or not _DEVA_WORD.search(text):
        return text
    s = _DEVA_WORD.sub(lambda m: _word(m.group(0)), text)
    s = s.replace("।", ".").replace("॥", ".")
    return s[:1].upper() + s[1:]


# =========================================================================== all scripts
# Lyrics in any script -> English letters. Devanagari uses the rules above;
# the other Indian scripts share Devanagari's Unicode layout, so they are
# shifted onto it and reuse those rules. Urdu/Arabic, Korean, Russian/Ukrainian,
# Greek and Japanese kana have their own small tables. Scripts that can't be
# read without a dictionary (Chinese characters, kanji, Thai) stay as they are.

_INDIC_BLOCKS = (0x0980, 0x0A00, 0x0A80, 0x0B00, 0x0B80, 0x0C00, 0x0C80, 0x0D00)  # Bengali .. Malayalam
_GURMUKHI_FIX = {"ੰ": "ं", "ੱ": ""}     # tippi -> anusvara, addak (doubling) dropped


def _indic_to_devanagari(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if ch in _GURMUKHI_FIX:
            out.append(_GURMUKHI_FIX[ch])
        elif any(b <= cp < b + 0x80 for b in _INDIC_BLOCKS):
            out.append(chr(0x0900 + (cp & 0x7F)))
        else:
            out.append(ch)
    return "".join(out)


# ---- Urdu / Arabic script ---------------------------------------------------
_AR_CONS = {"ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h", "خ": "kh",
            "د": "d", "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh",
            "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "ك": "k",
            "گ": "g", "ل": "l", "م": "m", "ن": "n", "ہ": "h", "ه": "h", "ۃ": "t", "ة": "t", "ݨ": "n"}
_AR_WORDS = {
    "میں": "mein", "ہیں": "hain", "ہے": "hai", "نہیں": "nahin", "کے": "ke", "کی": "ki", "کا": "ka", "کو": "ko",
    "سے": "se", "تو": "tu", "وہ": "woh", "یہ": "yeh", "اور": "aur", "دل": "dil", "ہم": "hum", "تم": "tum",
    "جو": "jo", "کیا": "kya", "کیوں": "kyun", "پیار": "pyaar", "عشق": "ishq", "محبت": "mohabbat",
    "زندگی": "zindagi", "آپ": "aap", "میرا": "mera", "میری": "meri", "میرے": "mere", "تیرا": "tera",
    "تیری": "teri", "تیرے": "tere", "بھی": "bhi", "ہو": "ho", "تھا": "tha", "تھی": "thi", "تھے": "the",
    "کوئی": "koi", "جیسے": "jaise", "ایسا": "aisa", "ایسی": "aisi", "دیکھا": "dekha", "یار": "yaar",
    "رب": "rab", "اللہ": "Allah", "خدا": "khuda", "جب": "jab", "سب": "sab", "اب": "ab", "کب": "kab",
    "پر": "par", "ساتھ": "saath", "بات": "baat", "رات": "raat", "ہوں": "hoon", "نہ": "na", "کہ": "ke",
    "لیے": "liye", "گا": "ga", "گی": "gi", "گے": "ge", "تجھے": "tujhe", "مجھے": "mujhe", "تجھ": "tujh",
    "مجھ": "mujh", "دا": "da", "دی": "di", "دے": "de", "نال": "naal", "وچ": "vich", "توں": "tu",
    "اے": "ae", "سجنا": "sajna", "ماہی": "maahi", "آنکھیں": "aankhein", "خوبصورت": "khoobsurat",
    "جسم": "jism", "حسن": "husn", "صنم": "sanam", "جان": "jaan", "ہی": "hi", "ان": "in", "اس": "is",
}
_AR_PUNCT = {"،": ",", "؟": "?", "۔": ".", "؛": ";", "٪": "%", "、": ", ", "。": ". ", "「": '"', "」": '"',
             "！": "!", "？": "?", "΄": "'", "ʼ": "'"}
_AR_DIGITS = {chr(0x0660 + i): str(i) for i in range(10)} | {chr(0x06F0 + i): str(i) for i in range(10)}
_AR_WORD = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+")
_AR_MARKS = "ًٌٍَُِّْٰ"


def _urdu_word(word: str) -> str:
    word = unicodedata.normalize("NFC", word).replace("ـ", "")   # tatweel
    base = "".join(c for c in word if c not in _AR_MARKS)
    if base in _AR_WORDS:
        return _AR_WORDS[base]
    if base and all(c in _AR_DIGITS for c in base):
        return "".join(_AR_DIGITS[c] for c in base)
    if base in _AR_PUNCT:
        return _AR_PUNCT[base]
    # tokens: [text, kind] with kind C (consonant), V (vowel), S (unwritten short "a")
    tok: list[list[str]] = []

    def cons(t: str) -> None:
        if tok and tok[-1][1] == "C":
            tok.append(["a", "S"])
        tok.append([t, "C"])

    n = len(word)
    for i, ch in enumerate(word):
        nxt = next((c for c in word[i + 1:] if c not in _AR_MARKS), "")
        first = not tok
        last = nxt == ""
        if ch == "َ":
            tok.append(["a", "V"])
        elif ch == "ِ":
            tok.append(["i", "V"])
        elif ch == "ُ":
            tok.append(["u", "V"])
        elif ch == "ّ" and tok and tok[-1][1] == "C":
            tok[-1][0] += tok[-1][0][-1:]
        elif ch in _AR_MARKS:
            continue
        elif ch == "ھ":
            if tok and tok[-1][1] == "C":
                tok[-1][0] += "h"
        elif ch == "آ":
            tok.append(["aa", "V"])
        elif ch in "اأإٱ":
            tok.append(["a", "V"])
        elif ch in "عء":
            continue
        elif ch == "ئ":
            if not (nxt in "یےيى" or last):
                cons("y")
        elif ch in "وؤ":
            if first or nxt in "اآ":
                cons("w")
            else:
                tok.append(["o", "V"])
        elif ch in "یيى":
            if first:
                cons("y")
            elif last:
                tok.append(["i", "V"])
            elif len(tok) == 1 and tok[0] == ["a", "V"]:
                tok[0] = ["ai", "V"]                    # ای- at the start: "ai"
            elif nxt in "اآو":
                if tok[-1][1] == "C":
                    tok.append(["i", "V"])
                cons("y")
            else:
                tok.append(["i" if nxt == "ں" else "e", "V"])
        elif ch in "ےۓ":
            tok.append(["e", "V"])
        elif ch == "ں":
            tok.append(["n", "N"])
        elif ch in "ہه" and last and not first:
            tok.append(["a", "V"] if tok[-1][1] == "C" else ["h", "C"])
        elif ch in _AR_CONS:
            cons(_AR_CONS[ch])
        elif ch in _AR_DIGITS:
            tok.append([_AR_DIGITS[ch], "V"])
    # Drop the unwritten "a" where Hindustani doesn't say it: V C a C V -> V C C V
    # (qalanadar -> qalandar, ajanata -> ajanta), checked from the right.
    vowel = lambda k: k in ("V", "S")
    for i in range(len(tok) - 3, 1, -1):
        if tok[i][1] == "S" and tok[i - 1][1] == "C" and tok[i + 1][1] == "C"                 and vowel(tok[i - 2][1]) and i + 2 < len(tok) and vowel(tok[i + 2][1]):
            tok[i][1] = "X"
    return "".join(t for t, k in tok if k != "X")


# ---- Korean (Revised Romanization, no sound changes) -------------------------
_KO_L = "g kk n d tt r m b pp s ss _ j jj ch k t p h".split()
_KO_V = "a ae ya yae eo e yeo ye o wa wae oe yo u wo we wi yu eu ui i".split()
_KO_T = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p",
         "t", "t", "ng", "t", "t", "k", "t", "p", "t"]


def _hangul(ch: str) -> str:
    idx = ord(ch) - 0xAC00
    l, v, t = idx // 588, (idx % 588) // 28, idx % 28
    return (_KO_L[l] if _KO_L[l] != "_" else "") + _KO_V[v] + _KO_T[t]


# ---- Cyrillic and Greek ------------------------------------------------------
_CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
                "a b v g d e yo zh z i y k l m n o p r s t u f kh ts ch sh shch _ y _ e yu ya i yi ye g".split()))
_GRK = dict(zip("αβγδεζηθικλμνξοπρσςτυφχψω",
                "a v g d e z i th i k l m n x o p r s s t y f ch ps o".split()))

# ---- Japanese kana ------------------------------------------------------------
_KANA = dict(zip(
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
    "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽゔ",
    ("a i u e o ka ki ku ke ko sa shi su se so ta chi tsu te to na ni nu ne no ha hi fu he ho "
     "ma mi mu me mo ya yu yo ra ri ru re ro wa o n "
     "ga gi gu ge go za ji zu ze zo da ji zu de do ba bi bu be bo pa pi pu pe po vu").split()))
_SMALL = {"ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o"}
_KANA_RUN = re.compile(r"[ぁ-ゖァ-ヺー]+")


def _kana_run(run: str) -> str:
    out: list[str] = []
    double = False
    for ch in run:
        if "ァ" <= ch <= "ヶ":            # katakana -> hiragana
            ch = chr(ord(ch) - 0x60)
        if ch == "っ":
            double = True
            continue
        if ch == "ー" and out:                # ー: long vowel
            out[-1] += out[-1][-1:]
            continue
        if ch in _SMALL and out:
            prev = out[-1]
            if _SMALL[ch].startswith("y") and prev.endswith("i"):
                stem = prev[:-1]
                out[-1] = stem + (_SMALL[ch][1:] if stem.endswith(("sh", "ch", "j")) else _SMALL[ch])
            else:
                out[-1] = prev[:-1] + _SMALL[ch]
            continue
        r = _KANA.get(ch, ch)
        if double and r and r[0].isalpha():
            r = ("t" if r.startswith("ch") else r[0]) + r
        double = False
        out.append(r)
    return "".join(out)


def _is_latinish(c: str) -> bool:
    return c.isascii() or unicodedata.name(c, "").startswith("LATIN")


def latin_ratio(text: str) -> float:
    """Share of the letters in `text` that are Latin (English letters)."""
    latin = other = 0
    for c in text or "":
        if c.isalpha() or unicodedata.category(c) in ("Mn", "Mc"):
            if _is_latinish(c):
                latin += 1
            else:
                other += 1
    return latin / (latin + other) if latin + other else 1.0


def to_latin(text: str) -> str:
    """Any script -> English letters, as far as it can be done without a
    dictionary. Text that would stay mostly unreadable is returned unchanged."""
    if not text or latin_ratio(text) >= 0.999:
        return text
    s = to_hinglish(_indic_to_devanagari(text))
    s = _AR_WORD.sub(lambda m: _urdu_word(m.group(0)), s)
    s = "".join(_AR_PUNCT.get(c, c) for c in s)
    s = _KANA_RUN.sub(lambda m: _kana_run(m.group(0)), s)
    out = []
    for c in s:
        lo = c.lower()
        base = unicodedata.normalize("NFD", lo)[:1]
        if "가" <= c <= "힣":
            out.append(_hangul(c))
        elif lo in _CYR or base in _GRK:
            r = (_CYR.get(lo) or _GRK.get(base) or "").replace("_", "")
            out.append(r.capitalize() if c != lo else r)
        else:
            out.append(c)
    s = "".join(out)
    if latin_ratio(s) < 0.7:          # kanji / Chinese / Thai...: keep the original
        return text
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s[:1].upper() + s[1:]
