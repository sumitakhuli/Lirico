"""LRC (timed lyrics) parsing and line lookup."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_TAG = re.compile(r"^\[([a-zA-Z#]+):(.*)\]\s*$")
_WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")  # enhanced LRC word timing
_WORDS = re.compile(r"\S+")


@dataclass(frozen=True)
class LyricLine:
    start: float  # seconds
    text: str     # "" marks an instrumental gap
    words: tuple | None = None  # ((start, word), ...) when the LRC has word timing


def _to_seconds(mm: str, ss: str, frac: str | None) -> float:
    value = int(mm) * 60 + int(ss)
    if frac:
        value += int(frac) / (10 ** len(frac))
    return float(value)


def _parse_words(rest: str) -> tuple | None:
    """'<00:01.00>Hello <00:01.50>world' -> ((1.0, 'Hello'), (1.5, 'world'))."""
    parts = re.split(r"<(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?>", rest)
    if len(parts) < 4:
        return None
    words = []
    for i in range(1, len(parts) - 3, 4):
        t = _to_seconds(parts[i], parts[i + 1], parts[i + 2])
        w = parts[i + 3].strip()
        if w:
            words.append((t, w))
    return tuple(words) or None


def parse_lrc(text: str) -> list[LyricLine]:
    """Parse LRC text into time-sorted lines. Unknown tags are ignored."""
    offset = 0.0
    lines: list[LyricLine] = []
    for raw in (text or "").splitlines():
        raw = raw.strip().lstrip("﻿")
        if not raw:
            continue
        meta = _META_TAG.match(raw)
        if meta and not _TIME_TAG.match(raw):
            if meta.group(1).lower() == "offset":
                try:
                    # Positive offset = lyrics should appear sooner.
                    offset = int(meta.group(2).strip()) / 1000.0
                except ValueError:
                    pass
            continue
        stamps = []
        pos = 0
        while True:
            m = _TIME_TAG.match(raw, pos)
            if not m:
                break
            stamps.append(_to_seconds(*m.groups()))
            pos = m.end()
        if not stamps:
            continue
        rest = raw[pos:]
        words = _parse_words(rest)
        body = re.sub(r"\s+", " ", _WORD_TAG.sub("", rest)).strip()
        for s in stamps:
            lines.append(LyricLine(s, body, words))
    lines.sort(key=lambda l: l.start)
    if offset:
        lines = [LyricLine(max(0.0, l.start - offset), l.text,
                           tuple((max(0.0, t - offset), w) for t, w in l.words) if l.words else None)
                 for l in lines]
    return lines


class LyricTimeline:
    """Fast "which line is active at time t" lookup."""

    def __init__(self, lines: list[LyricLine]):
        self.lines = lines
        self._starts = [l.start for l in lines]

    def __len__(self) -> int:
        return len(self.lines)

    def index_at(self, t: float) -> int:
        """Index of the active line, or -1 before the first line."""
        return bisect.bisect_right(self._starts, t) - 1

    def line(self, i: int) -> LyricLine | None:
        return self.lines[i] if 0 <= i < len(self.lines) else None

    def next_text(self, i: int) -> str:
        """Next non-empty line after index i (for the preview row)."""
        for j in range(i + 1, len(self.lines)):
            if self.lines[j].text:
                return self.lines[j].text
        return ""

    def sung_chars(self, i: int, t: float) -> float:
        """How many characters of line i have been sung at time t (fractional),
        for the progress underline. Uses real word timing when the LRC has it,
        otherwise estimates: words get time in proportion to their length and
        the line is assumed to be sung at a natural pace (not stretched over a
        long instrumental pause before the next line)."""
        line = self.line(i)
        if line is None or not line.text:
            return 0.0
        text = line.text
        nxt = self.line(i + 1)
        span = (nxt.start - line.start) if nxt is not None else 6.0
        # (char_end_index, start_time, end_time) per word
        spans = []
        if line.words:
            pos = 0
            for k, (ws, w) in enumerate(line.words):
                idx = text.find(w, pos)
                if idx < 0:
                    spans = []
                    break
                pos = idx + len(w)
                we = line.words[k + 1][0] if k + 1 < len(line.words) else min(line.start + span, ws + 1.2)
                spans.append((idx, pos, ws, max(ws + 0.05, we)))
        if not spans:
            words = [(m.start(), m.end()) for m in _WORDS.finditer(text)]
            if not words:
                return 0.0
            total_chars = sum(e - s for s, e in words) + len(words)
            # Natural pace ~13 chars/s; slow songs stretch up to 2.2x that, but a
            # line never "lasts" through a long instrumental pause.
            natural = max(0.8, total_chars * 0.075)
            sing = min(span * 0.95, max(natural, min(span * 0.85, natural * 2.2)))
            acc = line.start
            for s, e in words:
                d = sing * (e - s + 1) / total_chars
                spans.append((s, e, acc, acc + d))
                acc += d
        if t <= spans[0][2]:
            return 0.0
        for s, e, ws, we in spans:
            if t < we:
                frac = 0.0 if t <= ws else (t - ws) / (we - ws)
                return s + (e - s) * frac
        return float(len(text))

    def progress(self, i: int, t: float) -> float:
        """0..1 progress through line i (until the next line starts)."""
        cur, nxt = self.line(i), self.line(i + 1)
        if cur is None or nxt is None:
            return 1.0
        span = max(0.01, nxt.start - cur.start)
        return min(1.0, max(0.0, (t - cur.start) / span))
