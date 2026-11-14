"""Dev tool: builds a test corpus of real YouTube results (title, channel,
duration), the exact metadata Chrome reports to Windows for a video, so lyrics
matching can be evaluated offline with tools/eval_matching.py.

    .venv\\Scripts\\python tools\\yt_corpus.py out.json "query 1" "query 2" ... [--per-query 2]
"""
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tempfile  # noqa: E402

from yt_sync_probe import Cdp, launch_chrome, page_ws  # noqa: E402

EXTRACT_JS = r"""(() => {
  const out = [];
  const walk = (o) => {
    if (!o || typeof o !== 'object') return;
    if (o.videoRenderer) {
      const v = o.videoRenderer;
      const len = v.lengthText && v.lengthText.simpleText;
      if (len) out.push({id: v.videoId, title: v.title.runs.map(r => r.text).join(''),
                         channel: (v.ownerText && v.ownerText.runs[0].text) || '', length: len});
    }
    for (const k in o) walk(o[k]);
  };
  walk(window.ytInitialData);
  return out;
})()"""


def to_seconds(s: str) -> int:
    total = 0
    for part in s.split(":"):
        total = total * 60 + int(part)
    return total


def main() -> None:
    out = Path(sys.argv[1])
    args = sys.argv[2:]
    per = 2
    if "--per-query" in args:
        i = args.index("--per-query")
        per = int(args[i + 1])
        del args[i:i + 2]
    chrome = launch_chrome(tempfile.mkdtemp(prefix="lirico_chrome_"))
    cdp = Cdp(page_ws())
    corpus = []
    try:
        for q in args:
            cdp.call("Page.navigate", url="https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))
            items = []
            for _ in range(40):
                time.sleep(0.5)
                items = cdp.js(EXTRACT_JS) or []
                if items:
                    break
            for it in items[:per]:
                secs = to_seconds(it["length"])
                if secs > 15 * 60:
                    continue
                corpus.append({"query": q, "title": it["title"], "channel": it["channel"],
                               "duration": secs, "id": it["id"]})
                print(f"{secs:4d}s  {it['channel'][:22]:22s}  {it['title'][:90]}")
    finally:
        cdp.ws.close()
        chrome.terminate()
    out.write_text(json.dumps(corpus, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(corpus)} videos -> {out}")


if __name__ == "__main__":
    main()
