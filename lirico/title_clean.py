"""Turns messy "now playing" metadata into good lyrics search queries.

YouTube in a browser reports the *video* title as the track title and the
*channel* as the artist, e.g.

    title  = "Tum Hi Ho | Aashiqui 2 | Arijit Singh | Full Video Song"
    artist = "T-Series"

We strip decoration ("(Official Video)", "Full Video Song", hashtags, emoji),
split on separators, recognise record-label channels, and produce an ordered
list of (title, artist) candidates. The lyrics matcher tries them in order and
scores the results, so the heuristics here don't have to be perfect.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .textnorm import normalize, similarity

# Words that mark a bracketed group or segment as decoration, not song name.
_NOISE_WORDS = (
    r"official|video|audio|lyrics?|lyrical|visuali[sz]er|music\s*video|mv|"
    r"full\s*(?:video\s*)?song|video\s*song|full\s*audio|audio\s*song|full\s*video|full|"
    r"hd|hq|uhd|4k|8k|\d{3,4}p|remaster(?:ed)?(?:\s*\d{4})?|explicit|clean|"
    r"from\s+.+|ost|soundtrack|feat\.?\s.+|ft\.?\s.+|prod\.?\s.+|"
    r"slowed|reverb|sped\s*up|lofi|lo-?fi|8d|bass\s*boosted|extended|"
    r"with\s+lyrics|lyric\s+video|new\s+song|latest\s+song|hindi\s+song|"
    r"punjabi\s+song|bollywood|romantic\s+song|sad\s+song|"
    r"live|unplugged|cover|karaoke|instrumental|remix|version|edit|"
    r"\d{4}|t-?series|teaser|trailer|promo|title\s+track|reprise|"
    r"season\s*\d+|episode\s*\d+|ep\.?\s*\d+|s\d+\s*e\d+"
)
_NOISE_GROUP = re.compile(rf"^\s*(?:(?:{_NOISE_WORDS})[\s,&+/-]*)+\s*$", re.I)

# Decoration that also appears outside brackets.
_TRAILING_NOISE = re.compile(
    r"\b(?:official\s+(?:music\s+)?(?:video|audio|lyric\s+video)|full\s+video\s+song|full\s+video|"
    r"full\s+song|video\s+song|lyrical\s+video|lyric(?:al)?\s+video|full\s+audio|"
    r"audio\s+song|with\s+lyrics|lyrics|lyrical|official\s+video|music\s+video|"
    r"[48]k|hd|hq|uhd|\d{3,4}p)\b",
    re.I,
)
_HASHTAG = re.compile(r"#\S+")
_BRACKETS = re.compile(r"[\(\[\{【「『（][^\)\]\}】」』）]*[\)\]\}】」』）]")
_FEAT = re.compile(r"\s*\b(?:feat\.?|ft\.?|featuring)\s+.+$", re.I)
_FROM_MOVIE = re.compile(r"""from\s+["“'‘]?([^"”'’\)\]]+)["”'’]?""", re.I)
# "|" splits major parts; spaced dashes / colons split the head part
# (unspaced hyphens are kept: "T-Series", "Sachin-Jigar").
_MAJOR_SEP = re.compile(r"\s*(?:\|\||[|｜¦•·]|//)\s*")
_MINOR_SEP = re.compile(r"\s+[-–—~]{1,2}\s+|^[-–—]\s+|:\s+")
_SITE_SUFFIX = re.compile(r"\s*[-–|]\s*(?:youtube(?:\s+music)?|spotify|soundcloud)\s*$", re.I)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF⭐❤️]+"
)

# Channels that are record labels / studios, not singers.
_LABEL_HINTS = re.compile(
    r"\b(?:t-?series|records?|recordz|music\s+(?:company|india|world|official)|"
    r"zee\s+music|sony\s+music|saregama|tips|yrf|eros|venus|speed\s+records|"
    r"desi\s+music|white\s+hill|times\s+music|shemaroo|ultra|"
    r"films?|pictures|productions?|studios?|entertainment|media|label|"
    r"network|tv|channel|vevo|lyrics?|8d|songs|hits|beats)\b",
    re.I,
)
_DEVA = re.compile(r"[ऀ-ॿ]")
_TOPIC_SUFFIX = re.compile(r"\s*-\s*topic\s*$", re.I)
_VEVO = re.compile(r"vevo$", re.I)

_BROWSER_HINTS = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc")


@dataclass(frozen=True)
class Candidate:
    title: str
    artist: str = ""
    free: bool = False  # free-text query; `title` holds the whole query


@dataclass(frozen=True)
class CleanTrack:
    title: str                      # best-guess song title
    artist: str                     # best-guess artist ("" if unknown)
    album: str
    candidates: tuple[Candidate, ...]
    context: str                    # all useful raw text, for artist matching
    trusted: bool                   # metadata from a real music app
    raw_title: str = ""
    raw_artist: str = ""
    extra: dict = field(default_factory=dict, compare=False, hash=False)


def _split_camel(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def clean_artist_channel(channel: str) -> tuple[str, bool]:
    """Returns (artist_name, looks_like_artist) for a channel/artist string."""
    ch = (channel or "").strip()
    if not ch:
        return "", False
    if _TOPIC_SUFFIX.search(ch):  # "Arijit Singh - Topic" = auto-generated artist channel
        return _TOPIC_SUFFIX.sub("", ch).strip(), True
    if _VEVO.search(ch) and not ch.lower() == "vevo":
        return _split_camel(_VEVO.sub("", ch)).strip(), True
    ch = re.sub(r"\s*(?:official|music)\s*$", "", ch, flags=re.I).strip() or ch
    if _LABEL_HINTS.search(ch):
        return ch, False
    return ch, True


def strip_decoration(text: str) -> str:
    """Remove brackets with noise, hashtags, emoji, site suffixes."""
    t = _SITE_SUFFIX.sub("", text or "")
    t = _HASHTAG.sub(" ", t)
    t = _EMOJI.sub(" ", t)

    def repl(m: re.Match) -> str:
        inner = m.group(0)[1:-1]
        return " " if _NOISE_GROUP.match(inner) else m.group(0)

    t = _BRACKETS.sub(repl, t)
    return re.sub(r"\s+", " ", t).strip()


def strip_all_brackets(text: str) -> str:
    stripped = re.sub(r"\s+", " ", _BRACKETS.sub(" ", text or "")).strip()
    return stripped or (text or "").strip()


def _clean_segment(seg: str) -> str:
    seg = strip_all_brackets(seg)
    seg = _FEAT.sub("", seg)
    seg = _TRAILING_NOISE.sub(" ", seg)
    if len(seg.split()) >= 3:
        seg = re.sub(r"\s+(?:song|gaana|gana)\s*$", "", seg.strip(), flags=re.I)
    seg = re.sub(r"\s+", " ", seg).strip(" -–—,.:;\"'“”")
    return seg


def _is_noise_segment(seg: str) -> bool:
    return not normalize(seg) or bool(_NOISE_GROUP.match(seg))


def _dedupe(cands: list[Candidate]) -> tuple[Candidate, ...]:
    seen, out = set(), []
    for c in cands:
        if not normalize(c.title):
            continue
        key = (normalize(c.title), normalize(c.artist), c.free)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return tuple(out)


def _primary_artist(artist: str) -> str:
    """'Arijit Singh, Mithoon & Others' -> 'Arijit Singh'."""
    return re.split(r"\s*(?:,|&|\bx\b|\band\b|feat\.?|ft\.?|/|;)\s*", artist, flags=re.I)[0].strip()


def clean_track(title: str, artist: str = "", album: str = "", source_app: str = "") -> CleanTrack:
    # NFKC turns "fancy" Unicode letters (𝐏𝐡𝐢𝐫 𝐁𝐡𝐢) into plain ones.
    raw_title = unicodedata.normalize("NFKC", title or "").strip()
    raw_artist = unicodedata.normalize("NFKC", artist or "").strip()
    app = (source_app or "").lower()
    is_browser = any(b in app for b in _BROWSER_HINTS)

    movie = ""
    m = _FROM_MOVIE.search(raw_title)
    if m:
        movie = m.group(1).strip()

    # Downloaded YouTube videos in VLC etc. carry video-style titles too.
    looks_like_video = bool(re.search(r"[|｜]", raw_title) or _TRAILING_NOISE.search(raw_title))

    # --- Music apps with real metadata (Spotify, Apple Music, YT Music Topic) --
    if raw_artist and not is_browser and not looks_like_video:
        t = _clean_segment(strip_decoration(raw_title)) or raw_title
        t = re.sub(r"\s+-\s+(?:\d{4}\s+)?remaster(?:ed)?.*$", "", t, flags=re.I)
        a = _primary_artist(raw_artist)
        cands = [Candidate(t, a), Candidate(t, raw_artist), Candidate(t, "")]
        return CleanTrack(t, a, album or movie, _dedupe(cands),
                          context=f"{raw_title} {raw_artist} {album}", trusted=True,
                          raw_title=raw_title, raw_artist=raw_artist, extra={"head": t})

    # --- Browser / unknown player: parse the video title ---------------------
    channel_artist, channel_is_artist = clean_artist_channel(raw_artist)
    best_artist = channel_artist if channel_is_artist else ""
    text = strip_decoration(raw_title)

    def useful(parts: list[str]) -> list[str]:
        out = []
        for p in parts:
            seg = _clean_segment(p)
            if seg and not _is_noise_segment(seg):
                if channel_artist and normalize(seg) == normalize(channel_artist):
                    continue  # just repeats the channel/artist name
                out.append(seg)
        return out

    # Major parts are split by "|"; the head part may be "A - B" or "A: B".
    majors = [m for m in _MAJOR_SEP.split(text) if m and m.strip()]
    head = useful(_MINOR_SEP.split(majors[0])) if majors else []
    rest = useful(majors[1:])
    # Show/brand prefixes that just repeat the channel ("Coke Studio | Season 14 |
    # Pasoori" on "Coke Studio Pakistan"): the song is in a later part.
    if head and channel_artist and len(head) == 1 and rest and             normalize(head[0]) in normalize(channel_artist) and len(normalize(head[0])) >= 4:
        head, rest = rest[:1], rest[1:]
    if not head:
        head, rest = rest[:1], rest[1:]
    if not head:
        head = [_clean_segment(strip_all_brackets(text)) or raw_title]
    head = head[:2]

    def is_channel(h: str) -> bool:
        ht = set(normalize(h).split())
        return bool(ht) and (similarity(h, raw_artist) >= 0.75 or ht <= set(normalize(raw_artist).split()))

    cands: list[Candidate] = []
    if best_artist:
        cands += [Candidate(h, best_artist) for h in head if not is_channel(h)]
    if len(head) == 2 and raw_artist and sum(map(is_channel, head)) == 1:
        # One half is the channel/artist ("EMIWAY - FIRSE MACHAYENGE" on "Emiway Bantai").
        a_part = head[0] if is_channel(head[0]) else head[1]
        t_part = head[1] if a_part is head[0] else head[0]
        cands += [Candidate(t_part, a_part), Candidate(f"{t_part} {a_part}", free=True), Candidate(t_part, "")]
        head = [t_part, a_part]
    elif len(head) == 2:
        a, b = head
        # "Artist - Title", "Title - Artist", "Title - Movie": the order is
        # unknown, so free-text search first (matches title+artist in any order).
        cands += [Candidate(f"{a} {b}", free=True), Candidate(a, ""), Candidate(b, ""),
                  Candidate(b, a), Candidate(a, b)]
    else:
        cands.append(Candidate(head[0], ""))  # results get scored against the whole title
    # Later "|" parts are often movie, actors, composer, singer: try each as artist.
    for o in rest[:3]:
        cands.append(Candidate(head[0], _primary_artist(o)))
    if rest and len(head) == 1 and not is_channel(rest[0]):  # title second ("Aashiqui 2 | Tum Hi Ho")
        cands.append(Candidate(rest[0], ""))

    # Titles written in Devanagari ("अगर तुम साथ हो") rarely match LRCLIB names,
    # which are mostly romanized: add a transliterated candidate.
    if _DEVA.search(head[0]):
        from .translit import to_hinglish
        latin = re.sub(r"\s+", " ", to_hinglish(head[0])).strip(" '\"")
        # "आशिकी 2 का गाना तुम ही हो" = "song from Aashiqui 2: Tum Hi Ho"
        m2 = re.search(r"\b(?:ka|ki|ke)\s+gaa?na\s+(.+)$", latin, re.I)
        if m2:
            latin = m2.group(1)
        # "पूरा वीडियो गाना" / "पूरा ऑडियो गाना" = "full video/audio song"
        latin = re.sub(r"['\"]?\s*\b(?:poo?ra\s+)?(?:veediyo|vidiyo|video|audiyo|odiyo|audio)\b.*$",
                       "", latin, flags=re.I).strip(" '\"-") or latin
        # "मेरी गली में- डिवाइन": a dash before the artist name
        latin = re.split(r"\s*-\s+", latin)[0]
        latin = _clean_segment(latin) or latin
        cands.insert(1, Candidate(latin, best_artist))
        if rest:
            cands.insert(2, Candidate(latin, _primary_artist(rest[0])))
    cands = list(_dedupe(cands))
    guess_title = head[0]
    guess_artist = best_artist or (head[1] if len(head) == 2 else (rest[0] if rest else ""))
    context = " ".join([raw_title, raw_artist, channel_artist])
    return CleanTrack(guess_title, guess_artist, album or movie, tuple(cands), context=context,
                      trusted=False, raw_title=raw_title, raw_artist=raw_artist,
                      extra={"head": " ".join(head), "segments": list(rest[:4]),
                             "channel_artist": best_artist})
