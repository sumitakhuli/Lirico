"""Dev tool: records the first N seconds of real YouTube songs (system audio via
loopback) together with the video's true position, for offline auto-sync tests.

    .venv\\Scripts\\python tools\\capture_clips.py out_dir "query 1" "query 2" ... [--seconds 75] [--volume 0.25]

Writes <out_dir>/<n>.npy (16 kHz mono float32) and clips.json with
title/channel/duration and `media_at_end` (video time of the last sample).
"""
import argparse
import json
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from lirico.audio_capture import LoopbackRecorder  # noqa: E402
from yt_sync_probe import TRUTH_JS, Cdp, launch_chrome, page_ws, wait_until  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--seconds", type=float, default=75)
    ap.add_argument("--volume", type=float, default=0.25)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / "clips.json"
    clips = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else []

    rec = LoopbackRecorder(seconds=a.seconds + 5)
    rec.start()
    chrome = launch_chrome(tempfile.mkdtemp(prefix="lirico_chrome_"))
    cdp = Cdp(page_ws())
    cdp.call("Runtime.enable")
    try:
        for q in a.queries:
            cdp.call("Page.navigate", url="https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))
            href = wait_until(lambda: cdp.js("(() => { const a = document.querySelector('ytd-video-renderer a#video-title'); return a && a.href; })()"), 30)
            if not href:
                print("no result:", q)
                continue
            cdp.call("Page.navigate", url=href)
            for _ in range(240):  # wait for ads to finish / skip them
                st = cdp.js(TRUTH_JS)
                if st and not st["ad"] and st["dur"]:
                    break
                cdp.js("(() => { const b = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern'); if (b) b.click(); })()")
                time.sleep(0.5)
            cdp.js(f"(() => {{ const v = document.querySelector('video.html5-main-video'); v.muted=false; v.volume={a.volume}; v.currentTime=0; v.play(); }})()")
            time.sleep(0.3)
            rec.clear()
            time.sleep(a.seconds)
            audio, _end = rec.snapshot()
            st = cdp.js(TRUTH_JS)
            cdp.js("(() => { const v = document.querySelector('video.html5-main-video'); v.pause(); })()")
            title = st["title"].removesuffix(" - YouTube")
            channel = cdp.js("(() => { const a = document.querySelector('#owner #channel-name a, ytd-channel-name a'); return a ? a.textContent.trim() : ''; })()") or ""
            n = len(clips)
            np.save(out / f"{n}.npy", audio.astype(np.float32))
            clips.append({"file": f"{n}.npy", "query": q, "title": title, "channel": channel,
                          "duration": st["dur"], "media_at_end": st["t"], "seconds": len(audio) / 16000})
            print(f"[{n}] {title}  |  {channel}  ({st['dur']:.0f}s)  audio {len(audio) / 16000:.1f}s, rms {float(np.sqrt(np.mean(audio ** 2))):.4f}", flush=True)
            meta_path.write_text(json.dumps(clips, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        cdp.ws.close()
        chrome.terminate()
        rec.stop()


if __name__ == "__main__":
    main()
