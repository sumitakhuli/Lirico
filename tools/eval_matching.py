"""Dev tool: evaluates lyrics matching on a corpus of real YouTube metadata.

For each video it runs the same cleaning + LRCLIB matching as the app and
reports the chosen lyrics and |duration delta| (lyrics vs video). A delta
within ~1.6 s means the lyrics were timed on this very recording, which is
what keeps the overlay in sync.

    .venv\\Scripts\\python tools\\eval_matching.py tests\\data\\yt_corpus.json [--script latin]
"""
import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from lirico.lyrics_source import EXACT_DURATION, LrclibClient, LyricsCache  # noqa: E402
from lirico.title_clean import clean_track  # noqa: E402

HTTP_CACHE = Path(tempfile.gettempdir()) / "lirico_http_cache"


class CachedSession(requests.Session):
    """Caches GET responses on disk so repeated evaluations don't hit LRCLIB."""

    def get(self, url, params=None, **kw):
        HTTP_CACHE.mkdir(exist_ok=True)
        key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
        f = HTTP_CACHE / key
        if f.exists():
            status, body = json.loads(f.read_text(encoding="utf-8"))
        else:
            time.sleep(0.6)  # be gentle with LRCLIB
            r = super().get(url, params=params, **kw)
            status, body = r.status_code, (r.json() if r.status_code == 200 else None)
            if status in (200, 404):  # never cache rate-limit/server errors
                f.write_text(json.dumps([status, body]), encoding="utf-8")
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode()
        return resp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--script", default="latin")
    a = ap.parse_args()
    corpus = json.loads(Path(a.corpus).read_text(encoding="utf-8"))
    client = LrclibClient(LyricsCache(Path(tempfile.mkdtemp())), session=CachedSession())
    stats = {"total": 0, "synced": 0, "exact": 0, "close5": 0, "missed_exact": 0}
    for v in corpus:
        track = clean_track(v["title"], v["channel"], "", "chrome.exe")
        res = client.find(track, v["duration"], a.script)
        stats["total"] += 1
        delta = res.duration_delta
        mark = "  "
        if res.status == "synced":
            stats["synced"] += 1
            if delta is not None and abs(delta) <= EXACT_DURATION:
                stats["exact"] += 1
                mark = "OK"
            elif delta is not None and abs(delta) <= 5:
                stats["close5"] += 1
                mark = "~ "
            else:
                mark = "!!"
        # Was an exact-duration synced entry with a decent match available?
        best_exact = [alt for alt in res.alternatives if alt["synced"] and alt["duration"]
                      and abs(alt["duration"] - v["duration"]) <= EXACT_DURATION]
        if mark != "OK" and best_exact:
            stats["missed_exact"] += 1
            mark = "MX"
        first = res.lines[1].text if len(res.lines) > 1 else ""
        print(f"{mark} {v['duration']:4d}s Δ{'' if delta is None else f'{delta:+.0f}':>5} {res.status:9s} "
              f"{(res.track or '-')[:28]:28s} | {(res.artist or '')[:18]:18s} | {v['title'][:52]}"
              + (f"\n      » {first[:70]}" if first else ""))
    t = stats["total"]
    print(f"\n{t} videos: synced {stats['synced']}  |  timed to this exact recording (±{EXACT_DURATION}s): "
          f"{stats['exact']} ({100 * stats['exact'] / t:.0f}%)  |  within 5s: {stats['close5']}  |  "
          f"exact version available but not chosen: {stats['missed_exact']}")


if __name__ == "__main__":
    main()
