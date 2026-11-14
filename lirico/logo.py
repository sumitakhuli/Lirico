"""The Lirico logo, defined once and used everywhere (tray, exe icon, site, video).

The mark: a dark tile holding two lyric lines. The lower (current) line carries
the mint progress underline and a playhead dot — exactly what the overlay does.
"""

MINT = "#7EE0C3"
INK = "#0D0F15"

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0.35" y2="1">
      <stop offset="0" stop-color="#232838"/>
      <stop offset="1" stop-color="#0D0F15"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="248" height="248" rx="60" fill="url(#tile)"/>
  <rect x="56" y="74" width="108" height="22" rx="11" fill="#FFFFFF" fill-opacity="0.30"/>
  <rect x="56" y="118" width="144" height="26" rx="13" fill="#FFFFFF"/>
  <rect x="56" y="162" width="96" height="12" rx="6" fill="#7EE0C3"/>
  <circle cx="160" cy="168" r="13" fill="#7EE0C3"/>
</svg>
"""


def svg_bytes() -> bytes:
    return LOGO_SVG.encode("utf-8")
