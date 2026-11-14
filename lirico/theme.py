"""Default look of the overlay. ALL visual values live here.

The Settings window (tray → Settings…) edits these values live; your changes
are saved as overrides in settings.json, so this file always holds the
factory defaults. Colors are "#RRGGBB" or "#AARRGGBB" (alpha first, Qt style).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace


@dataclass(frozen=True)
class Theme:
    # ---- Typography ---------------------------------------------------------
    font_family: str = "Segoe UI Variable Display"
    # Used for characters the main font lacks. "Nirmala UI" ships with Windows
    # and covers Devanagari (Hindi).
    fallback_families: tuple = ("Segoe UI", "Nirmala UI")
    font_size: float = 14.5           # active lyric line, pt
    font_weight: int = 600            # 100..900
    next_size: float = 10.5           # upcoming line, pt
    next_weight: int = 500
    status_size: float = 11.0         # "Finding lyrics…" etc.
    max_lines: int = 2                # long lines wrap to at most this many rows

    # ---- Colors -------------------------------------------------------------
    text_color: str = "#FFFFFFFF"
    next_color: str = "#99FFFFFF"
    status_color: str = "#B3FFFFFF"
    accent_color: str = "#FF7EE0C3"   # progress underline, ♪ glyph
    shadow: bool = True
    shadow_color: str = "#99000000"

    # ---- Background panel ---------------------------------------------------
    panel_color: str = "#FF0E1014"
    panel_opacity: float = 0.45       # translucent background (0 = invisible, still draggable)
    panel_radius: float = 14.0
    panel_padding_x: int = 16
    panel_padding_y: int = 7
    line_gap: int = 2

    # ---- Layout -------------------------------------------------------------
    max_width_ratio: float = 0.42     # of screen width
    gap_above_taskbar: int = 8

    # ---- Lyrics display -----------------------------------------------------
    show_next_line: bool = False      # single line by default
    # How the words being sung are marked: "word" = a highlight that moves from
    # word to word (current word bright, the rest softer), "underline" = a line
    # that grows under the sung words, "off" = nothing.
    highlight: str = "underline"
    highlight_strength: float = 0.30  # opacity of the moving word highlight
    unsung_opacity: float = 0.62      # other words while "word" highlight is on
    underline_thickness: float = 2.0

    # ---- Motion -------------------------------------------------------------
    animation: str = "slide"          # "slide" | "fade" | "none"
    transition_ms: int = 220
    fade_ms: int = 300
    slide_px: float = 8.0
    paused_opacity: float = 0.5

    # ---- Glyphs -------------------------------------------------------------
    gap_glyph: str = "♪"

    # ---- Tray icon ----------------------------------------------------------
    tray_bg_top: str = "#FF2A2F45"
    tray_bg_bottom: str = "#FF14161F"
    tray_fg: str = "#FF7EE0C3"

    def with_overrides(self, overrides: dict | None) -> "Theme":
        """Apply saved user overrides, ignoring unknown keys and bad types."""
        if not overrides:
            return self
        overrides = dict(overrides)
        if "underline" in overrides and "highlight" not in overrides:   # settings from v1
            overrides["highlight"] = "underline" if overrides.pop("underline") else "off"
        valid = {}
        types = {f.name: type(getattr(self, f.name)) for f in fields(self)}
        for k, v in overrides.items():
            t = types.get(k)
            if t is None:
                continue
            try:
                if t is tuple:
                    v = tuple(v)
                elif t is bool:
                    v = bool(v)
                elif t in (int, float):
                    v = t(v)
                elif t is str:
                    v = str(v)
                valid[k] = v
            except (TypeError, ValueError):
                continue
        return replace(self, **valid)

    def diff(self, base: "Theme") -> dict:
        """Values that differ from `base` (what gets saved)."""
        mine, theirs = asdict(self), asdict(base)
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in mine.items() if theirs.get(k) != v}


# Factory defaults. Edit values above to change them for everyone.
THEME = Theme()
