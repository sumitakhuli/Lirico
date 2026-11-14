"""Prints what Lirico sees from Windows media controls. Run while music plays:

    .venv\\Scripts\\python tools\\probe_media.py [seconds]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lirico.media_watch import MediaWatcher  # noqa: E402
from lirico.title_clean import clean_track  # noqa: E402


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5
    last = {"key": None}

    def show(snap):
        if snap is None:
            print("(no media session)")
            return
        if snap.track_key != last["key"]:
            last["key"] = snap.track_key
            print(f"\nsource={snap.source_app!r}\ntitle={snap.title!r}\nartist={snap.artist!r} album={snap.album!r}")
            print("cleaned:", clean_track(snap.title, snap.artist, snap.album, snap.source_app))
        print(f"  playing={snap.playing} pos={snap.position:7.2f}s / {snap.duration:.1f}s rate={snap.rate}")

    w = MediaWatcher(show, interval=1.0)
    w.start()
    time.sleep(seconds)
    w.stop()


if __name__ == "__main__":
    main()
