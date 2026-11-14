"""Renders the overlay with sample text to PNGs (over dark and light backdrops)
so you can check styling after changing the theme:

    .venv\\Scripts\\python tools\\render_preview.py [out_dir]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lirico.overlay import GAP, INTRO, LYRIC, STATUS, Overlay  # noqa: E402
from lirico.settings import Settings  # noqa: E402
from lirico.theme import THEME  # noqa: E402

SAMPLES = [  # placeholder text written for this preview, not song lyrics
    ("english", "This is how the current line looks", LYRIC, "and the next line waits below it", 14.0),
    ("hinglish", "Yeh romanized Hindi line hai, dekho", LYRIC, "Agli line yahan aayegi", 9.0),
    ("hindi", "यह हिन्दी पंक्ति सही दिखनी चाहिए", LYRIC, "अगली पंक्ति यहाँ दिखेगी", 12.0),
    ("long", "A deliberately long lyric line that keeps on going so we can see how wrapping behaves across two rows",
     LYRIC, "short next", 60.0),
    ("intro", "♪  Song Title — Artist Name", INTRO, "first line of lyrics appears here", 0.0),
    ("gap", "♪", GAP, "lyrics resume with this line", 0.0),
    ("status", "No synced lyrics found", STATUS, "Song Title — Artist Name", 0.0),
]


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "preview")
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)  # noqa: F841
    theme = THEME.with_overrides(Settings.load().theme)
    ov = Overlay(theme)
    for name, text, kind, nxt, sung in SAMPLES:
        ov.set_content(text, kind, nxt, sung)
        ov._trans_anim.stop()
        ov._trans, ov._opacity = 1.0, 1.0
        ov._panel_from = ov._panel_size(ov._row, ov._next)
        shot = ov.grab()
        for bg_name, bg in (("dark", "#2b2f3a"), ("light", "#e9e4da")):
            canvas = QPixmap(shot.size())
            canvas.setDevicePixelRatio(shot.devicePixelRatio())  # handle display scaling
            canvas.fill(QColor(bg))
            p = QPainter(canvas)
            p.drawPixmap(0, 0, shot)
            p.end()
            canvas.save(str(out / f"{name}_{bg_name}.png"))
    print("wrote", out.resolve())


if __name__ == "__main__":
    main()
