"""Auto-sync: find where the song actually starts by listening.

Lyrics timings often don't match the video (intro scenes, dialogue, a longer
music lead-in), so lyrics start early. We listen to what the PC plays,
recognise sung words with a small offline speech model (Whisper tiny), and
work out the time shift that best lines those words up with the lyrics.

Speech recognition on music is noisy, so we never trust a single word.
Every (heard word, lyric word) pair that sounds alike votes for the shift that
would align them; the true shift collects many consistent votes while noise
spreads out. Words are compared as consonant "skeletons" so Hinglish spelling
differences don't matter (bhookh/buki -> "bk", sharam/siram -> "srm").
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

import numpy as np

from .lrc import LyricLine
from .translit import to_latin as to_hinglish   # any script -> English letters

log = logging.getLogger(__name__)

_DIGRAPHS = (("chh", "c"), ("bh", "b"), ("kh", "k"), ("gh", "g"), ("th", "t"), ("dh", "d"), ("ph", "f"),
             ("sh", "s"), ("ch", "c"), ("jh", "j"), ("ck", "k"), ("qu", "k"))
_SINGLE = str.maketrans({"w": "b", "v": "b", "z": "j", "q": "k", "x": "k", "y": "", "h": ""})
_HINDI_HINTS = {"hai", "hain", "tum", "tu", "mera", "meri", "mere", "tera", "teri", "tere", "nahi", "nahin",
                "dil", "main", "mein", "ki", "ka", "ke", "ko", "se", "hum", "ho", "kya", "jo", "toh", "yaar",
                "pyaar", "bhi", "ab", "na", "kar", "mujhe", "tujhe", "kaise", "sab", "raat", "tha", "thi"}


def skeleton(word: str) -> str:
    """Consonant skeleton of a romanized word: robust to vowel/spelling noise."""
    w = unicodedata.normalize("NFKD", word.lower())
    w = "".join(c for c in w if "a" <= c <= "z")
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    w = w.translate(_SINGLE)
    w = re.sub(r"[aeiou]", "", w)
    return re.sub(r"(.)\1+", r"\1", w)


def guess_language(lines: list[LyricLine]) -> str:
    """'hi' for Hindi/Hinglish/Punjabi-ish lyrics, else 'en' (for the speech model)."""
    text = " ".join(l.text for l in lines)
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if not words:
        return "en"
    hits = sum(1 for w in words if w in _HINDI_HINTS)
    return "hi" if hits / len(words) > 0.06 else "en"


def lyric_words(lines: list[LyricLine]) -> list[tuple[float, str, int]]:
    """(estimated time, skeleton, line index) for every lyric word. Words within a line are
    spread at a natural singing pace (same model as the on-screen highlight)."""
    out = []
    for i, line in enumerate(lines):
        if not line.text:
            continue
        nxt = next((l.start for l in lines[i + 1:]), line.start + 6.0)
        span = max(0.5, nxt - line.start)
        if line.words:
            for t, w in line.words:
                for part in w.split():
                    s = skeleton(part)
                    if len(s) >= 1:
                        out.append((t, s, i))
            continue
        words = to_hinglish(line.text).split()
        total = sum(len(w) + 1 for w in words) or 1
        natural = max(0.8, total * 0.075)
        sing = min(span * 0.95, max(natural, min(span * 0.85, natural * 2.2)))
        acc = line.start
        for w in words:
            d = sing * (len(w) + 1) / total
            s = skeleton(w)
            if s:
                out.append((acc + d * 0.3, s, i))
            acc += d
    return out


@dataclass
class SyncEstimate:
    offset: float          # add to playback time to get lyric time (lyric = media + offset)
    votes: float           # weight at the peak
    runner_up: float       # best weight away from the peak
    pairs: int             # matched word pairs supporting the peak
    words: int = 0         # distinct lyric words among them
    lines: int = 0         # distinct lyric lines among them

    @property
    def confident(self) -> bool:
        return (self.votes >= 3.0 and self.words >= 4 and self.lines >= 2
                and self.votes >= 1.6 * max(self.runner_up, 0.8))


def estimate_offset(heard: list[tuple[float, str]], lyrics: list[tuple[float, str]],
                    lo: float = -150.0, hi: float = 60.0, step: float = 0.1,
                    prior: Optional[float] = None) -> Optional[SyncEstimate]:
    """heard: (media time, word) from speech recognition; lyrics: (lyric time,
    skeleton). Returns the best shift, or None if there is nothing to go on."""
    # Whisper sometimes loops on music ("par par par par..."): a word heard
    # again within 3 s counts once.
    hs, last_seen = [], {}
    for t, w in sorted(heard):
        sk = skeleton(w)
        if len(sk) < 2 or t - last_seen.get(sk, -99.0) < 3.0:
            last_seen.setdefault(sk, t)
            continue
        last_seen[sk] = t
        hs.append((t, sk))
    if not hs or not lyrics:
        return None
    bins = np.zeros(int((hi - lo) / step) + 1)
    pairs: list[tuple[float, float, int, int]] = []   # (offset, weight, lyric word idx, line idx)
    cache: dict[tuple[str, str], float] = {}
    for ta, sa in hs:
        matches = []
        for j, (tl, sl, li) in enumerate(lyrics):
            o = tl - ta
            if o < lo or o > hi:
                continue
            key = (sa, sl)
            sim = cache.get(key)
            if sim is None:
                sim = 1.0 if sa == sl else SequenceMatcher(None, sa, sl).ratio()
                cache[key] = sim
            if sim >= 0.67:
                matches.append((o, sim, j, li))
        if not matches:
            continue
        # A common word that matches everywhere carries little information:
        # its votes are shared out (log-damped) across all its matches.
        share = 1.0 / (1.0 + np.log(len(matches)))
        for o, sim, j, li in matches:
            # Longer, more specific words count more; 1-2 consonant words barely.
            w = (sim - 0.6) * min(len(sa), len(lyrics[j][1]), 5) * share
            bins[int((o - lo) / step)] += w
            pairs.append((o, w, j, li))
    if not pairs:
        return None
    # Smooth: timings on both sides are only good to about ±0.8 s.
    k = int(0.8 / step)
    kernel = np.ones(2 * k + 1)
    score = np.convolve(bins, kernel, mode="same")
    if prior is not None:   # a mild preference, never decisive on its own
        xs = lo + np.arange(len(score)) * step
        score = score * (1.0 + 0.15 * np.exp(-((xs - prior) / 3.0) ** 2))
    peak = int(np.argmax(score))
    best = lo + peak * step
    away = np.concatenate([score[:max(0, peak - 3 * k)], score[peak + 3 * k:]])
    runner = float(away.max()) if len(away) else 0.0
    near = sorted((o, w, j, li) for o, w, j, li in pairs if abs(o - best) <= 0.8)
    if near:   # refine to the weighted median of the supporting pairs
        tot, acc = sum(x[1] for x in near), 0.0
        for o, w, _j, _li in near:
            acc += w
            if acc >= tot / 2:
                best = o
                break
    return SyncEstimate(round(float(best), 2), float(score[peak]), runner, len(near),
                        words=len({x[2] for x in near}), lines=len({x[3] for x in near}))


# --------------------------------------------------------------------------- engine
import sys as _sys
import threading as _threading
import time as _time
import types as _types
from concurrent.futures import ThreadPoolExecutor as _Pool
from pathlib import Path as _Path
from typing import Callable as _Callable


def _load_whisper(model_dir: _Path):
    # faster-whisper imports PyAV only to decode audio *files*; we pass raw
    # samples, so a stub keeps ~70 MB of FFmpeg out of the app.
    if _sys.modules.get("av") is None:
        _sys.modules.pop("av", None)
        try:
            import av  # noqa: F401
        except ImportError:
            _sys.modules["av"] = _types.ModuleType("av")
    from faster_whisper import WhisperModel
    return WhisperModel(str(model_dir), device="cpu", compute_type="int8", cpu_threads=2)


class AutoSyncEngine:
    """Listens to system audio and estimates the lyric time shift in the background.

    The controller calls `request(...)` periodically while a song plays; results
    arrive through `on_result(generation, estimate)` on a worker thread."""

    WINDOW = 14.0          # seconds of audio analysed per pass (passes overlap)
    MIN_AUDIO = 8.0        # don't analyse until this much audio is buffered
    RECENT = 45.0          # judge on the most recent this-many seconds of the song

    def __init__(self, model_dir: Optional[_Path], on_result: _Callable[[int, Optional[SyncEstimate]], None]):
        from .audio_capture import LoopbackRecorder
        self.model_dir = model_dir
        self.on_result = on_result
        self.recorder = LoopbackRecorder(seconds=self.WINDOW + 5)
        self._pool = _Pool(max_workers=1, thread_name_prefix="lirico-autosync")
        self._model = None
        self._busy = _threading.Event()
        self._cleared_at = _time.monotonic()
        # Words heard so far for the current lyrics, in *song* time. Kept across
        # passes (and seeks) so evidence accumulates: (song time, word).
        self._heard: list = []
        self._heard_lock = _threading.Lock()
        self.available = bool(model_dir and _Path(model_dir, "model.bin").exists())
        self.error: Optional[str] = None
        if not self.available:
            self.error = "speech model not found"

    def start(self) -> None:
        if self.available:
            self.recorder.start()

    def stop(self) -> None:
        self.recorder.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def reset(self) -> None:
        """Forget buffered audio (after a seek or pause): it no longer maps onto song time."""
        self.recorder.clear()
        self._cleared_at = _time.monotonic()

    def forget(self) -> None:
        """New song or new lyrics: drop all accumulated evidence."""
        self.reset()
        with self._heard_lock:
            self._heard = []

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def buffered(self) -> float:
        return _time.monotonic() - self._cleared_at

    def request(self, generation: int, media_at: _Callable[[float], float], lyrics: list,
                language: str, prior: Optional[float] = None) -> bool:
        """Analyse the latest audio. `media_at(t)` maps a monotonic time to the
        song position at that moment. Returns False if nothing was started."""
        if not self.available or self.busy or self.buffered() < self.MIN_AUDIO:
            return False
        audio, end_time = self.recorder.snapshot(min(self.WINDOW, self.buffered()))
        if len(audio) < int(self.MIN_AUDIO * 16000) or not end_time:
            return False
        media_end = media_at(end_time)
        self._busy.set()
        self._pool.submit(self._work, generation, audio, media_end, lyrics, language, prior)
        return True

    def _work(self, generation, audio, media_end, lyrics, language, prior) -> None:
        est = None
        try:
            peak = float(np.abs(audio).max()) if len(audio) else 0.0
            if peak > 1e-3:
                if self._model is None:
                    self._model = _load_whisper(self.model_dir)
                audio = (audio / peak * 0.9).astype(np.float32)
                start = media_end - len(audio) / 16000.0
                segments, _ = self._model.transcribe(
                    audio, language=language, word_timestamps=True, beam_size=1, temperature=0.0,
                    condition_on_previous_text=False, vad_filter=False, without_timestamps=False)
                fresh = []
                for seg in segments:
                    for w in seg.words or []:
                        for part in to_hinglish(w.word.strip()).split():
                            fresh.append((start + w.start, part))
                with self._heard_lock:
                    # Windows overlap: replace what we had for this stretch of the
                    # song with the newest transcription of it.
                    lo, hi = start, media_end
                    self._heard = [h for h in self._heard if not (lo - 0.2 <= h[0] <= hi + 0.2)] + fresh
                    # Videos are sometimes edited differently from the audio the
                    # lyrics were timed on (a skit, then the song): trust the most
                    # recent stretch, which is what the lyrics must follow now.
                    newest = max((h[0] for h in self._heard), default=media_end)
                    heard = [h for h in self._heard if h[0] >= newest - self.RECENT]
                est = estimate_offset(heard, lyrics, prior=prior)
                log.debug("autosync heard %d words (%d total) -> %s", len(fresh), len(heard), est)
        except Exception as exc:
            self.error = str(exc)
            log.exception("autosync failed")
        finally:
            self._busy.clear()
        try:
            self.on_result(generation, est)
        except Exception:
            log.exception("autosync callback failed")
