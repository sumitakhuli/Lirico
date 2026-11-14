"""Unicode-aware text normalization and fuzzy similarity helpers.

Works for both Latin (incl. romanized Hindi) and Devanagari text: we never strip
combining marks from Indic scripts, since vowel signs (matras) are combining
characters there.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_LATIN_FOLDS = (
    # Common romanized-Hindi spelling variants, applied to Latin tokens only.
    ("ph", "f"),
    ("ee", "i"),
    ("oo", "u"),
    ("w", "v"),
    ("z", "j"),
)


def _strip_latin_accents(ch: str) -> str:
    decomposed = unicodedata.normalize("NFD", ch)
    if decomposed and ord(decomposed[0]) < 0x250:  # Latin range: drop accents
        return "".join(c for c in decomposed if not unicodedata.combining(c))
    return ch


def normalize(text: str) -> str:
    """Casefold, drop punctuation/symbols, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in "PSZC":  # punctuation, symbols, separators, control
            out.append(" ")
        else:
            out.append(_strip_latin_accents(ch))
    return " ".join("".join(out).split())


def _fold_token(tok: str) -> str:
    if not tok.isascii():
        return tok
    for a, b in _LATIN_FOLDS:
        tok = tok.replace(a, b)
    tok = re.sub(r"(.)\1+", r"\1", tok)  # "naa" -> "na", "dill" -> "dil"
    if len(tok) > 3 and tok.endswith("h"):
        tok = tok[:-1]  # "hain"/"hai" stay, "dekh" vs "dek" align
    return tok


def tokens(text: str) -> list[str]:
    return [_fold_token(t) for t in normalize(text).split()]


def similarity(a: str, b: str) -> float:
    """0..1 fuzzy similarity tolerant of extra words and spelling variants."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = " ".join(ta), " ".join(tb)
    if sa == sb:
        return 1.0
    ratio = SequenceMatcher(None, sa, sb).ratio()
    set_a, set_b = set(ta), set(tb)
    overlap = len(set_a & set_b)
    containment = overlap / min(len(set_a), len(set_b))
    # Containment only counts fully when the shorter side is a decent size,
    # so "Ho" doesn't match every title that contains "ho".
    weight = 0.95 if min(len(set_a), len(set_b)) >= 2 else 0.7
    jaccard = overlap / len(set_a | set_b)
    return max(ratio, containment * weight * (0.6 + 0.4 * jaccard) / 1.0, jaccard)


def contains_phrase(haystack: str, needle: str) -> bool:
    """True if all tokens of `needle` occur, in order, inside `haystack`."""
    hs, nd = tokens(haystack), tokens(needle)
    if not nd:
        return False
    h = " " + " ".join(hs) + " "
    return (" " + " ".join(nd) + " ") in h
