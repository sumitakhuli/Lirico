"""Dev tool: measures how accurately Lirico tracks YouTube playback in Chrome.

Launches Chrome with a throwaway profile under DevTools control, plays real
YouTube songs, and compares Lirico's playback clock (fed only by Windows media
controls, exactly like the app) against the video element's true currentTime.
Runs scripted seeks/pauses and prints per-phase error statistics.

    .venv\\Scripts\\python tools\\yt_sync_probe.py "song query 1" "song query 2" ... [--volume 0.15]

Needs: pip install websocket-client  (in requirements-dev.txt)
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websocket  # noqa: E402

from lirico.clock import PlaybackClock, TimelineGuard  # noqa: E402
from lirico.media_watch import MediaWatcher  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9333


class Cdp:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


def launch_chrome(profile: str) -> subprocess.Popen:
    args = [CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
            "--autoplay-policy=no-user-gesture-required", "--no-first-run", "--no-default-browser-check",
            "--window-size=900,600", "--window-position=40,40", "about:blank"]
    proc = subprocess.Popen(args)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Chrome did not start")


def page_ws() -> str:
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
    return next(t["webSocketDebuggerUrl"] for t in tabs if t.get("type") == "page")


TRUTH_JS = """(() => { const v = document.querySelector('video.html5-main-video') || document.querySelector('video');
  const p = document.querySelector('#movie_player');
  if (!v) return null;
  return {t: v.currentTime, paused: v.paused, dur: v.duration, title: document.title,
          ad: !!(p && p.classList.contains('ad-showing'))}; })()"""


def wait_until(pred, timeout=60, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--volume", type=float, default=0.15)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    profile = tempfile.mkdtemp(prefix="lirico_chrome_")
    chrome = launch_chrome(profile)
    cdp = Cdp(page_ws())
    cdp.call("Runtime.enable")

    latest = {"snap": None}
    clock = PlaybackClock()

    guard = TimelineGuard()
    last_key = {"k": None}

    def on_snap(s):
        latest["snap"] = s
        if s is None:
            return
        if s.track_key != last_key["k"]:
            last_key["k"] = s.track_key
            guard.reset()
        if guard.accept(s.duration, s.playing, s.captured_at):
            clock.update(s.track_key, s.position, s.playing, s.rate, s.captured_at, s.duration)
        else:  # foreign/stale timeline: keep our own position, only follow play/pause
            clock.update(s.track_key, clock.position(s.captured_at), s.playing, s.rate, s.captured_at,
                         guard.duration)

    watcher = MediaWatcher(on_snap, interval=0.25)
    watcher.start()
    rows = []
    phase = {"name": "init"}
    stop = threading.Event()
    lock = threading.Lock()

    def sampler():
        while not stop.is_set():
            with lock:
                t0 = time.monotonic()
                truth = cdp.js(TRUTH_JS)
                t1 = time.monotonic()
            s = latest["snap"]
            if truth and s is not None and "chrome" in s.source_app.lower() and not truth["ad"]:
                mid = (t0 + t1) / 2
                rows.append({"phase": phase["name"], "truth": truth["t"], "paused": truth["paused"],
                             "clock": clock.position(mid), "smtc_raw": s.position, "smtc_dur": s.duration,
                             "dur": truth["dur"], "title": s.title, "playing": s.playing})
            time.sleep(0.2)

    def js(expr):
        with lock:
            return cdp.js(expr)

    def skip_ads():
        for _ in range(240):
            st = js(TRUTH_JS)
            if st and not st["ad"] and st["t"] > 0.3:
                return True
            js("(() => { const b = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern'); if (b) b.click(); })()")
            time.sleep(0.5)
        return False

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    results = []
    try:
        for q in a.queries:
            phase["name"] = "nav"
            with lock:
                cdp.call("Page.navigate", url="https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))
            href = wait_until(lambda: js("(() => { const a = document.querySelector('ytd-video-renderer a#video-title'); return a && a.href; })()"), 30)
            if not href:
                print("no result for", q)
                continue
            with lock:
                cdp.call("Page.navigate", url=href)
            wait_until(lambda: js(TRUTH_JS), 30)
            skip_ads()
            js(f"(() => {{ const v = document.querySelector('video.html5-main-video') || document.querySelector('video'); v.muted = false; v.volume = {a.volume}; v.play(); }})()")
            st = wait_until(lambda: (lambda x: x if x and x.get("dur") and not x["ad"] else None)(js(TRUTH_JS)), 30) or {}
            title, dur = st.get("title", "?"), st.get("dur") or 0.0
            print(f"\n=== {q}\n    {title}  ({dur:.0f}s)  {href}")
            script = [("start", 10), ("seek60", 8, "v.currentTime = 60"), ("pause", 4, "v.pause()"),
                      ("resume", 8, "v.play()"), ("seek_back20", 8, "v.currentTime = 20"),
                      ("pause2", 6, "v.pause()"), ("seek_paused", 3, "v.currentTime = 90"),
                      ("resume2", 8, "v.play()"), ("rate1.25", 6, "v.playbackRate = 1.25"),
                      ("rate1", 4, "v.playbackRate = 1")]
            for step in script:
                name, secs = step[0], step[1]
                if len(step) > 2:
                    js(f"(() => {{ const v = document.querySelector('video.html5-main-video') || document.querySelector('video'); {step[2]}; }})()")
                phase["name"] = f"{q[:18]}:{name}"
                time.sleep(secs)
            results.append((q, title, dur, href))
    finally:
        stop.set()
        th.join(timeout=3)
        watcher.stop()
        cdp.ws.close()
        chrome.terminate()

    # ---- report
    by_phase: dict[str, list] = {}
    for r in rows:
        by_phase.setdefault(r["phase"], []).append(r)
    print("\nphase                              n   median_err  p90|err|  max|err|   (err = lirico clock - true video time, s)")
    for name, rs in by_phase.items():
        if ":" not in name:
            continue
        # ignore the first 1.2 s after each action (time to observe the change)
        errs = [r["clock"] - r["truth"] for r in rs][6:]
        if not errs:
            continue
        ab = sorted(abs(e) for e in errs)
        print(f"{name:32s} {len(errs):3d}   {statistics.median(errs):+8.3f}  {ab[int(len(ab) * 0.9) - 1]:8.3f}  {ab[-1]:8.3f}")
    if a.out:
        Path(a.out).write_text(json.dumps({"rows": rows, "videos": results}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
