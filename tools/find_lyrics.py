"""Look up lyrics for a title/channel the way the app would, without the UI.

    .venv\\Scripts\\python tools\\find_lyrics.py "Tum Hi Ho | Aashiqui 2 | Arijit Singh | Full Video Song" "T-Series" [--app chrome.exe] [--duration 262] [--no-cache]
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lirico.lyrics_source import LrclibClient, LyricsCache  # noqa: E402
from lirico.title_clean import clean_track  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("title")
    ap.add_argument("artist", nargs="?", default="")
    ap.add_argument("--app", default="chrome.exe")
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--script", default="auto", choices=["auto", "devanagari", "latin"])
    ap.add_argument("--no-cache", action="store_true", help="use a throwaway cache")
    a = ap.parse_args()

    track = clean_track(a.title, a.artist, "", a.app)
    print("cleaned:", track.title, "|", track.artist)
    for c in track.candidates:
        print("  candidate:", c)
    root = Path(tempfile.mkdtemp()) if a.no_cache else None
    from lirico.paths import cache_dir
    client = LrclibClient(LyricsCache(root or cache_dir()))
    r = client.find(track, a.duration, a.script)
    print(f"status={r.status} match={r.track!r} by {r.artist!r} id={r.lrclib_id} cached={r.from_cache} lines={len(r.lines)}")
    for line in r.lines[:6]:
        print(f"  [{line.start:6.2f}] {line.text}")


if __name__ == "__main__":
    main()
