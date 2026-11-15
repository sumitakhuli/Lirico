"""Finds synced lyrics on LRCLIB (https://lrclib.net), with a local disk cache.

Sync-first matching: LRCLIB often holds several timings of one song (album
audio, official video with intro, lyric video...). Lyrics are only "in sync"
when they were timed against the same recording that is playing, and the best
evidence for that is the duration. So we gather a broad pool of candidates and
choose the best title/artist match with a steep penalty for duration mismatch.

Nothing is bundled: every lyric comes from LRCLIB at runtime and is cached
under %USERPROFILE%\\.lirico\\cache so a song is fetched only once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import requests

from . import __version__
from .lrc import LyricLine, parse_lrc
from .textnorm import contains_phrase, normalize, similarity, tokens
from .title_clean import CleanTrack, strip_all_brackets, strip_decoration
from .translit import latin_ratio, to_latin

log = logging.getLogger(__name__)

API = "https://lrclib.net/api"
USER_AGENT = f"Lirico/{__version__} (Windows desktop lyrics overlay)"
NOT_FOUND_TTL = 2 * 24 * 3600     # retry "not found" songs after 2 days
CACHE_VERSION = 2
MAX_SEARCHES = 7                  # network requests per song, at most
EXACT_DURATION = 1.6              # seconds: "this was timed on the same recording"

SYNCED, PLAIN, INSTRUMENTAL, NOT_FOUND, ERROR = "synced", "plain", "instrumental", "not_found", "error"

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_VARIANT_WORDS = re.compile(
    r"\b(remix|lo-?fi|slowed|reverb|sped|live|cover|acoustic|unplugged|reprise|mashup|"
    r"flip|karaoke|instrumental|female|male|8d|edit|mix|version|extended)\b", re.I)


@dataclass
class LyricsResult:
    status: str
    lines: list[LyricLine] = field(default_factory=list)
    plain: str = ""
    track: str = ""
    artist: str = ""
    lrclib_id: Optional[int] = None
    duration: float = 0.0              # duration LRCLIB lists for these lyrics
    duration_delta: Optional[float] = None   # lyrics duration - media duration
    from_cache: bool = False
    message: str = ""
    romanized: bool = False            # text converted/transferred to Latin script
    alternatives: list = field(default_factory=list)  # other candidates [{id, track, artist, duration, synced}]


def devanagari_ratio(text: str) -> float:
    # Count code points, not isalpha(): Devanagari vowel signs aren't "alpha".
    text = text or ""
    deva = len(_DEVANAGARI.findall(text))
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    return deva / (deva + latin) if deva + latin else 0.0


def _is_deva(item: dict) -> bool:
    return devanagari_ratio(item.get("syncedLyrics") or item.get("plainLyrics") or "") > 0.5


def is_non_latin(text: str) -> bool:
    """Lyrics mostly in another script (Devanagari, Urdu, Korean, Cyrillic...)."""
    return latin_ratio(text or "") < 0.5


def _non_latin_item(item: dict) -> bool:
    return is_non_latin(item.get("syncedLyrics") or item.get("plainLyrics") or "")


def wants_original(script_pref: str) -> bool:
    return script_pref in ("original", "devanagari")


def _split_artists(artist: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|\bx\b|\band\b|feat\.?|ft\.?|/|;)\s*", artist or "", flags=re.I)
    return [p for p in (s.strip() for s in parts) if p]


def duration_penalty(delta: Optional[float]) -> float:
    if delta is None:
        return 0.0
    d = abs(delta)
    for limit, pen in ((EXACT_DURATION, 0.0), (3, 0.08), (6, 0.15), (12, 0.28), (30, 0.38)):
        if d <= limit:
            return pen
    return 0.48


def _is_name_of(text: str, artist: str) -> bool:
    """'EMIWAY' is a name of 'Emiway Bantai'; 'BALI' is a name of 'Mr. BALI'."""
    if not text or not artist:
        return False
    tt = set(normalize(text).split())
    return bool(tt) and (similarity(text, artist) >= 0.75 or tt <= set(normalize(artist).split()))


def _artist_sim(track: CleanTrack, item: dict) -> float:
    guesses = {c.artist for c in track.candidates if c.artist and not c.free}
    if track.artist:
        guesses.add(track.artist)
    guesses |= set(_split_artists(" , ".join(track.extra.get("segments", []))))
    a_sim = 0.0
    for name in _split_artists(item.get("artistName") or ""):
        if contains_phrase(track.context, name):
            return 1.0
        for g in guesses:   # "Varun J" in the title vs "Varun Jain" on LRCLIB
            a_sim = max(a_sim, similarity(name, g))
    return a_sim


def match_score(track: CleanTrack, item: dict) -> tuple[float, float]:
    """(title/artist match 0..1, title similarity). Ignores duration."""
    raw_name = item.get("trackName") or item.get("name") or ""
    found_title = strip_all_brackets(raw_name)
    found_artist = item.get("artistName") or ""
    # A title candidate that is really the artist ("EMIWAY - FIRSE MACHAYENGE")
    # must not match songs whose title merely contains the artist's name.
    titles = [c.title for c in track.candidates if not c.free
              and not _is_name_of(c.title, found_artist) and not _is_name_of(c.title, track.raw_artist)]
    titles = titles or [track.title]
    t_sim = max(similarity(t, found_title) for t in titles)
    ft = tokens(found_title)
    # The found title appearing verbatim in the raw video title is a strong hint,
    # if it's specific enough (not a one-word title like "Ho").
    # ...but only inside the song-title part, not among actors/movie names later on
    # ("Heeriye ... | Salman Khan | ..." must not match a song called "Salman Khan").
    head = track.extra.get("head") or track.title
    if (len(ft) >= 2 or sum(map(len, ft)) >= 6) and contains_phrase(head, found_title):
        t_sim = max(t_sim, 0.92)
    # Entries named after the whole video ("Coldplay - Yellow (Official Video)").
    if " - " in raw_name:
        t_sim = max(t_sim, similarity(strip_decoration(raw_name), strip_decoration(track.raw_title)))

    a_sim = _artist_sim(track, item)
    score = 0.6 * t_sim + 0.4 * a_sim
    channel_artist = track.extra.get("channel_artist") or ""
    if channel_artist and a_sim < 0.5 and similarity(channel_artist, item.get("artistName") or "") < 0.5:
        score -= 0.5   # the channel is clearly an artist, and it's not this one: another song
    for word in _VARIANT_WORDS.findall(raw_name):
        if not re.search(rf"\b{re.escape(word)}", track.context, re.I):
            score -= 0.25  # a remix/live/lofi/mashup version the user isn't playing
            break
    return score, t_sim


def rank(track: CleanTrack, items: Iterable[dict], duration: float, script_pref: str = "latin"):
    """All acceptable candidates, best first, as (final_score, delta, item)."""
    ranked = []
    for item in items:
        m, t_sim = match_score(track, item)
        if t_sim < 0.6 or m < 0.45:
            continue
        has_synced = bool(item.get("syncedLyrics"))
        delta = None
        if duration and item.get("duration"):
            delta = float(item["duration"]) - duration
        final = m - duration_penalty(delta)
        if delta is not None:
            final -= 0.01 * min(abs(delta), 10)   # tie-break: closer is better
        if duration and not item.get("duration"):
            final -= 0.3      # unknown length: can't tell if it's timed for this recording
        if delta is not None and abs(delta) > 20 and _artist_sim(track, item) < 0.9:
            continue          # far-off length AND unconfirmed artist: almost surely another song
        if track.trusted and delta is not None and abs(delta) > 60:
            final -= 0.3   # exact metadata but very different length: another version
        lyr = item.get("syncedLyrics") or item.get("plainLyrics") or ""
        if lyr:
            other_script = is_non_latin(lyr)
            if wants_original(script_pref) == other_script:
                final += 0.04  # small: timing matters more, text can be transferred
        tier = 2 if has_synced else (1 if lyr.strip() else 0)
        ranked.append((tier, final, delta, item))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [(f, d, it) for _, f, d, it in ranked]


def pick_best(track: CleanTrack, items: Iterable[dict], duration: float,
              script_pref: str = "latin") -> Optional[tuple[dict, float]]:
    r = rank(track, list(items), duration, script_pref)
    return (r[0][2], r[0][0]) if r else None


def _nonempty(lines: list[LyricLine]) -> list[LyricLine]:
    return [l for l in lines if l.text]


def transfer_text(timed: list[LyricLine], text_source: str) -> Optional[list[LyricLine]]:
    """Put another version's text (e.g. romanized) onto `timed` lines when both
    have the same line structure. Returns None if they don't line up."""
    src = parse_lrc(text_source) if "[" in (text_source or "") else [
        LyricLine(0, t.strip()) for t in (text_source or "").splitlines()]
    src_text = [l.text for l in src if l.text]
    targets = _nonempty(timed)
    if not targets or len(src_text) != len(targets):
        return None
    it = iter(src_text)
    return [LyricLine(l.start, next(it), None) if l.text else l for l in timed]


def _result_from_item(item: dict, duration: float = 0.0, script_pref: str = "latin",
                      pool: Iterable[dict] = (), from_cache: bool = False) -> LyricsResult:
    synced = item.get("syncedLyrics") or ""
    plain = item.get("plainLyrics") or ""
    d = float(item.get("duration") or 0)
    common = dict(track=item.get("trackName") or "", artist=item.get("artistName") or "",
                  lrclib_id=item.get("id"), from_cache=from_cache, duration=d,
                  duration_delta=(d - duration) if (d and duration) else None)
    if synced:
        lines = parse_lrc(synced)
        if lines:
            romanized = False
            if not wants_original(script_pref) and is_non_latin(synced):
                # Prefer a human romanization with the same line structure...
                for other in pool:
                    if other.get("id") == item.get("id") or _non_latin_item(other):
                        continue
                    if is_non_latin(other.get("trackName", "")):
                        continue
                    moved = transfer_text(lines, other.get("syncedLyrics") or other.get("plainLyrics") or "")
                    if moved:
                        lines, romanized = moved, True
                        break
                # ...else transliterate.
                if not romanized:
                    lines = [LyricLine(l.start, to_latin(l.text), None) for l in lines]
                    romanized = any(l.text != o.text for l, o in zip(lines, parse_lrc(synced)))
            return LyricsResult(SYNCED, lines=lines, plain=plain, romanized=romanized, **common)
    if plain.strip():
        return LyricsResult(PLAIN, plain=plain, message="Only unsynced lyrics available", **common)
    if item.get("instrumental"):
        return LyricsResult(INSTRUMENTAL, message="Instrumental", **common)
    return LyricsResult(NOT_FOUND, message="No lyrics found", **common)


class LyricsCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(raw_title: str, raw_artist: str) -> str:
        basis = f"{normalize(raw_title)}\x1f{normalize(raw_artist)}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> Optional[dict]:
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("v") != CACHE_VERSION:
            return None
        if data.get("status") == NOT_FOUND and time.time() - data.get("at", 0) > NOT_FOUND_TTL:
            return None
        return data

    def put(self, key: str, data: dict) -> None:
        data = {"v": CACHE_VERSION, "at": time.time(), **data}
        tmp = self._path(key).with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._path(key))
        except OSError as exc:
            log.warning("cache write failed: %s", exc)


_KEEP = ("id", "trackName", "artistName", "albumName", "duration", "instrumental", "plainLyrics", "syncedLyrics")


def _slim(item: dict) -> dict:
    return {k: item.get(k) for k in _KEEP}


class LrclibClient:
    def __init__(self, cache: LyricsCache, session: Optional[requests.Session] = None, timeout: float = 10):
        self.cache = cache
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        self.timeout = timeout

    def _get(self, path: str, params: dict):
        for attempt in range(3):
            resp = self.http.get(f"{API}/{path}", params=params, timeout=self.timeout)
            if resp.status_code == 429 and attempt < 2:   # rate limited: back off politely
                try:
                    wait = float(resp.headers.get("Retry-After", "")) if resp.headers else 0
                except ValueError:
                    wait = 0
                time.sleep(min(10.0, wait or 2.0 * (attempt + 1)))
                continue
            break
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # -- public -------------------------------------------------------------
    def find(self, track: CleanTrack, duration: float, script_pref: str = "latin",
             use_cache: bool = True, pinned_id: Optional[int] = None) -> LyricsResult:
        """Best lyrics for `track`. `pinned_id` forces a specific LRCLIB entry
        (the user picked it from the alternatives list)."""
        key = LyricsCache.key_for(track.raw_title, track.raw_artist)
        cached = self.cache.get(key) if use_cache else None
        if cached is not None:
            pool = cached.get("pool") or []
            if cached.get("status") == NOT_FOUND and pinned_id is None:
                return LyricsResult(NOT_FOUND, from_cache=True, message="No synced lyrics found")
        else:
            try:
                pool = self._search(track, duration, script_pref)
            except requests.RequestException as exc:
                log.warning("LRCLIB request failed: %s", exc)
                return LyricsResult(ERROR, message="Lyrics service unreachable")

        ranked = rank(track, pool, duration, script_pref)
        chosen = None
        if pinned_id is not None:
            chosen = next((it for it in pool if it.get("id") == pinned_id), None)
            if chosen is None:
                try:
                    chosen = self._get(f"get/{pinned_id}", {})
                except requests.RequestException:
                    chosen = None
        if chosen is None and ranked:
            chosen = ranked[0][2]
            if not wants_original(script_pref) and _non_latin_item(chosen):
                # Urdu / Hindi / Punjabi... text: one more search for a version in
                # English letters timed to the same recording (Afreen Afreen has one).
                more = self._latin_versions(chosen, pool)
                if more:
                    pool = pool + more
                    ranked = rank(track, pool, duration, script_pref)
                    chosen = ranked[0][2]
                    if cached is not None:
                        cached["pool"] = [_slim(i) for i in pool[:60]]
                        self.cache.put(key, cached)
        if cached is None:
            self.cache.put(key, {"status": NOT_FOUND if chosen is None else "found",
                                 "query": track.title, "pool": [_slim(i) for i in pool[:40]]})
        if chosen is None:
            return LyricsResult(NOT_FOUND, from_cache=cached is not None, message="No synced lyrics found")
        result = _result_from_item(chosen, duration, script_pref, pool, from_cache=cached is not None)
        result.alternatives = [
            {"id": it.get("id"), "track": it.get("trackName"), "artist": it.get("artistName"),
             "duration": it.get("duration"), "synced": bool(it.get("syncedLyrics")),
             "devanagari": _is_deva(it), "other_script": _non_latin_item(it)}
            for _, _, it in ranked[:12]]
        return result

    def _latin_versions(self, item: dict, pool: list) -> list:
        """Other LRCLIB entries of `item`'s song written in English letters."""
        d = float(item.get("duration") or 0)
        for it in pool:
            if (not _non_latin_item(it) and it.get("syncedLyrics")
                    and (not d or abs(float(it.get("duration") or 0) - d) <= EXACT_DURATION)):
                return []           # already have one
        name = strip_all_brackets(item.get("trackName") or "")
        artist = (_split_artists(item.get("artistName") or "") or [""])[0]
        if not name:
            return []
        try:
            found = self._get("search", {"q": f"{name} {artist}".strip()}) or []
        except requests.RequestException:
            return []
        ids = {it.get("id") for it in pool}
        return [it for it in found if isinstance(it, dict) and it.get("id") not in ids
                and it.get("syncedLyrics") and not _non_latin_item(it)]

    # -- search -------------------------------------------------------------
    def _search(self, track: CleanTrack, duration: float, script_pref: str) -> list[dict]:
        pool: dict[int, dict] = {}
        made = 0

        def add(items):
            for it in items or []:
                if isinstance(it, dict) and it.get("id") is not None:
                    pool.setdefault(it["id"], it)

        def done() -> bool:
            r = rank(track, pool.values(), duration, script_pref)
            if not r:
                return False
            final, delta, item = r[0]
            exact = delta is not None and abs(delta) <= EXACT_DURATION
            return bool(item.get("syncedLyrics")) and final >= 0.8 and (exact or not duration)

        if track.trusted and track.artist and duration:
            params = {"track_name": track.title, "artist_name": track.artist, "duration": int(round(duration))}
            if track.album:
                params["album_name"] = track.album
            item = self._get("get", params)
            made += 1
            add([item] if item else [])
            if done():
                return list(pool.values())

        queries = []
        for cand in track.candidates:
            if cand.free or not cand.artist:
                queries.append({"q": cand.title} if cand.free else {"track_name": cand.title})
            else:
                queries.append({"track_name": cand.title, "artist_name": cand.artist})
        # People often time lyrics to the YouTube upload itself and name the entry
        # after it ("Coldplay - Yellow (Official Video)"): search the raw title too.
        if not track.trusted:
            head = re.split(r"\s*[|｜]\s*", track.raw_title)[0]
            queries.insert(min(2, len(queries)), {"q": strip_decoration(head)})
        seen = set()

        def run(params) -> bool:
            nonlocal made
            key = tuple(sorted(params.items()))
            if key in seen or not any(v.strip() for v in params.values()) or made >= MAX_SEARCHES:
                return False
            seen.add(key)
            add(self._get("search", params))
            made += 1
            return done()

        for i, params in enumerate(queries):
            if made >= MAX_SEARCHES - 1:
                break
            if run(params):
                return list(pool.values())
            if i == 2 and duration:
                # Adaptive step: the first results reveal the real song + artist
                # ("Tum Hi Ho" by "Arijit Singh"); ask for all of that artist's
                # versions of it, which usually includes the one timed to this video.
                r = rank(track, pool.values(), duration, script_pref)
                if r and r[0][0] >= 0.5:
                    best = r[0][2]
                    artist = (_split_artists(best.get("artistName") or "") or [""])[0]
                    name = strip_all_brackets(best.get("trackName") or "")
                    if artist and name:
                        # LRCLIB returns max 20 rows per query, and different query
                        # styles surface different versions: try both.
                        if run({"track_name": name, "artist_name": artist}):
                            return list(pool.values())
                        if run({"q": f"{name} {artist}"}):
                            return list(pool.values())
        return list(pool.values())
