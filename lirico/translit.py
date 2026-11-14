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
    "ळ": "l",
}
_NUKTA = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y"}
_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
           "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
           "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
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
