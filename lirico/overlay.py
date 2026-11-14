"""The lyrics overlay: frameless, translucent, always-on-top, docked just above
the taskbar. Drag it anywhere with the mouse (transparent areas let clicks
through). It's a pure view: the controller tells it what to show, and every
visual value comes from the Theme.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen,
                           QTextLayout, QTextOption)
from PySide6.QtWidgets import QWidget

from . import winutil
from .theme import Theme

LYRIC, STATUS, GAP, INTRO = "lyric", "status", "gap", "intro"
_WORD_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class _Row:
    text: str = ""
    kind: str = LYRIC


def _font(theme: Theme, size: float, weight: int) -> QFont:
    f = QFont()
    f.setFamilies([theme.font_family, *theme.fallback_families])
    f.setPointSizeF(max(6.0, size))
    f.setWeight(QFont.Weight(max(100, min(900, int(weight)))))
    f.setHintingPreference(QFont.PreferNoHinting)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class _Layout:
    """A wrapped, centered text block (QTextLayout) with helpers for the underline."""

    def __init__(self, text: str, font: QFont, width: float, max_lines: int):
        self.text, self.font = text, QFont(font)
        self.layout = self._build(text, width)
        # Never cut words off with "…": wrap, and if it still needs more rows
        # than allowed, shrink the font a little at a time (down to 60%).
        base = font.pixelSize() if font.pixelSize() > 0 else font.pointSizeF()
        step = 1.0
        while self.layout.lineCount() > max_lines and step > 0.6:
            step -= 0.05
            self.font = QFont(font)
            if font.pixelSize() > 0:
                self.font.setPixelSize(max(9, int(base * step)))
            else:
                self.font.setPointSizeF(max(7.0, base * step))
            self.layout = self._build(text, width)
        # Balance wrapped rows ("meri struggle dekh, meri / shakal dekh" ->
        # two rows of similar length): narrow the wrap width while the row
        # count stays the same.
        n = self.layout.lineCount()
        if n > 1:
            natural = QFontMetricsF(self.font).horizontalAdvance(text)
            w = max(natural / n, width * 0.4)
            while w < width:
                trial = self._build(text, w, width)
                if trial.lineCount() <= n:
                    self.layout = trial
                    break
                w += width * 0.03
        rects = [self.layout.lineAt(i).naturalTextRect() for i in range(self.layout.lineCount())]
        self.height = sum(self.layout.lineAt(i).height() for i in range(self.layout.lineCount()))
        self.text_width = max((r.width() for r in rects), default=0.0)

    def _build(self, text: str, width: float, full: float | None = None) -> QTextLayout:
        lay = QTextLayout(text, self.font)
        opt = QTextOption(Qt.AlignHCenter)
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        lay.setTextOption(opt)
        lay.beginLayout()
        y = 0.0
        while True:
            line = lay.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            # balanced (narrower) rows stay centred within the full width
            line.setPosition(QPointF(((full or width) - width) / 2, y))
            y += line.height()
        lay.endLayout()
        return lay

    def word_rect(self, chars: float):
        """Rect (layout coords) of the word being sung after `chars` characters,
        or None before the first word."""
        if chars <= 0 or not self.text:
            return None
        words = [(m.start(), m.end()) for m in _WORD_RE.finditer(self.text)]
        if not words:
            return None
        idx = 0
        for k, (s, _e) in enumerate(words):
            if s <= chars:
                idx = k
        s, e = words[idx]
        for i in range(self.layout.lineCount()):
            line = self.layout.lineAt(i)
            ls, ll = line.textStart(), line.textLength()
            if ls <= s < ls + ll:
                e = min(e, ls + ll)
                x1, x2 = _x(line, s), _x(line, e)
                return QRectF(min(x1, x2), line.y(), abs(x2 - x1), line.height())
        return None

    def underline_segments(self, chars: float):
        """[(x1, x2, y)] covering the first `chars` characters, per wrapped row."""
        segs = []
        for i in range(self.layout.lineCount()):
            line = self.layout.lineAt(i)
            start, length = line.textStart(), line.textLength()
            if chars <= start:
                break
            # Trailing spaces belong to the row but have no visible width.
            visible = len(self.text[start:start + length].rstrip())
            end = min(chars, start + visible)
            x1 = line.naturalTextRect().left()
            whole = int(end)
            xa = _x(line, whole)
            xb = _x(line, min(whole + 1, start + visible))
            x2 = xa + (xb - xa) * (end - whole)
            y = line.y() + line.ascent() + max(2.5, line.descent() * 0.8)
            if x2 > x1:
                segs.append((x1, x2, y))
        return segs


def _x(line, pos: int) -> float:
    r = line.cursorToX(pos)
    return float(r[0] if isinstance(r, tuple) else r)


class Overlay(QWidget):
    moved = Signal(QPoint)            # user finished dragging: new top-left
    context_menu = Signal(QPoint)     # right-click at global pos

    def __init__(self, theme: Theme, click_through: bool = False):
        super().__init__(None)
        self.setWindowTitle("Lirico")
        self._click_through = click_through
        self._apply_flags()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMouseTracking(True)

        self._row = _Row()
        self._prev_row = _Row()
        self._next = ""
        self._sung = 0.0               # characters of the current line already sung
        self._marker: QRectF | None = None   # smoothed word highlight (layout coords)
        self._panel_from = (0.0, 0.0)
        self._paused = False
        self._active = False
        self._preview = False
        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None
        self._saved_pos: QPoint | None = None
        self._hover = False
        self._layout_cache: dict = {}
        self._dock: QRect | None = None      # docked inside the taskbar (logical rect)

        self._trans = 1.0
        self._trans_anim = QVariantAnimation(self, startValue=0.0, endValue=1.0)
        self._trans_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._trans_anim.valueChanged.connect(self._on_trans)
        self._opacity = 0.0
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._fade_anim.valueChanged.connect(self._on_fade)
        self._fade_anim.finished.connect(self._on_fade_done)

        self._topmost_timer = QTimer(self, interval=2000)
        self._topmost_timer.timeout.connect(lambda: winutil.keep_topmost(int(self.winId())))

        self.apply_theme(theme)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            screen.availableGeometryChanged.connect(lambda *_: self._reposition())

    # ------------------------------------------------------------------ theme
    def apply_theme(self, theme: Theme) -> None:
        self.theme = t = theme
        if self._dock is not None:      # re-derive the docked fonts from the new theme
            dock, self._dock = self._dock, None
            self._apply_theme_fonts(t)
            self.set_dock(dock)
            return
        self._apply_theme_fonts(t)

    def _apply_theme_fonts(self, theme: Theme) -> None:
        self.theme = t = theme
        self.f_current = _font(t, t.font_size, t.font_weight)
        self.f_next = _font(t, t.next_size, t.next_weight)
        self.f_status = _font(t, t.status_size, max(400, t.next_weight))
        self.c_text, self.c_next = QColor(t.text_color), QColor(t.next_color)
        self.c_status, self.c_accent = QColor(t.status_color), QColor(t.accent_color)
        self.c_shadow = QColor(t.shadow_color)
        self._trans_anim.setDuration(max(1, t.transition_ms))
        self._fade_anim.setDuration(max(1, t.fade_ms))
        self._layout_cache.clear()
        self._reposition()
        self.update()

    # ------------------------------------------------------------------ window
    def _apply_flags(self) -> None:
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        if self._click_through:
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)

    def set_click_through(self, enabled: bool) -> None:
        if enabled == self._click_through:
            return
        self._click_through = enabled
        visible, pos = self.isVisible(), self.pos()
        self._apply_flags()           # re-applying flags hides the window
        self.move(pos)
        if visible:
            self.show()
            self._harden()

    def _harden(self) -> None:
        hwnd = int(self.winId())
        winutil.harden_overlay(hwnd)      # never takes focus, not in Alt+Tab
        winutil.keep_topmost(hwnd)
        from .taskbar_dock import exclude_from_capture
        # Docked: hide ourselves from screen captures so the taskbar scan
        # only sees the taskbar, never our own lyrics.
        exclude_from_capture(hwnd, self._dock is not None)

    def raise_above_taskbar(self) -> None:
        if self.isVisible():
            winutil.keep_topmost(int(self.winId()))

    def _screen(self):
        if self._saved_pos is not None:
            s = QGuiApplication.screenAt(self._saved_pos + QPoint(self.width() // 2, self.height() // 2))
            if s is not None:
                return s
        return QGuiApplication.primaryScreen()

    def _resize_for_screen(self) -> None:
        t = self.theme
        geo = self._screen().availableGeometry()
        w = int(max(240, geo.width() * t.max_width_ratio))
        fc, fn = QFontMetricsF(self.f_current), QFontMetricsF(self.f_next)
        h = int(t.panel_padding_y * 2 + fc.lineSpacing() * t.max_lines + t.line_gap
                + fn.lineSpacing() + t.slide_px + 8)
        self.setFixedSize(w, h)

    def docked_position(self) -> QPoint:
        geo = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(geo.x() + (geo.width() - self.width()) // 2,
                      geo.y() + geo.height() - self.height() - self.theme.gap_above_taskbar)

    def set_saved_position(self, pos: QPoint | None) -> None:
        """None = docked above the taskbar (default)."""
        if pos is not None and QGuiApplication.screenAt(pos + QPoint(40, 20)) is None:
            pos = None   # that monitor is gone
        self._saved_pos = pos
        self._reposition()

    def _reposition(self) -> None:
        if self._drag_offset is not None:
            return
        if self._dock is not None:
            self.setFixedSize(self._dock.size())
            self.move(self._dock.topLeft())
            return
        old_bottom_center = None
        if self._saved_pos is not None:
            old_bottom_center = QPoint(self._saved_pos.x() + self.width() // 2, self._saved_pos.y() + self.height())
        self._resize_for_screen()
        if old_bottom_center is not None:
            # keep the bottom-center anchored when the size changes (font size etc.)
            self._saved_pos = QPoint(old_bottom_center.x() - self.width() // 2, old_bottom_center.y() - self.height())
        self.move(self._saved_pos if self._saved_pos is not None else self.docked_position())

    # ------------------------------------------------------------------ taskbar dock
    @property
    def docked(self) -> bool:
        return self._dock is not None

    def set_dock(self, rect: QRect | None) -> None:
        """Sit exactly on `rect` (a free stretch of the taskbar), or float (None)."""
        if rect == self._dock:
            return
        was = self._dock is not None
        self._dock = QRect(rect) if rect is not None else None
        if self._dock is not None:
            h = self._dock.height()
            # one line, sized to the taskbar: ~38% of its height
            self.f_current = _font(self.theme, self.theme.font_size, self.theme.font_weight)
            self.f_current.setPixelSize(max(11, min(int(h * 0.38), int(self.theme.font_size * 1.34))))
            self.f_status = QFont(self.f_current)
            self.f_status.setWeight(QFont.Weight(500))
            self._layout_cache.clear()
            self.setFixedSize(self._dock.size())
            self.move(self._dock.topLeft())
        elif was:
            self.apply_theme(self.theme)          # restores fonts, size and position
        if self.isVisible():
            self._harden()
        self.update()

    # ------------------------------------------------------------------ content API
    def set_content(self, text: str, kind: str = LYRIC, next_text: str = "", sung: float = 0.0) -> None:
        row = _Row(text, kind)
        if row != self._row:
            self._panel_from = self._panel_size(self._row, self._next) if self._trans >= 1 else self._current_panel()
            self._prev_row, self._row = self._row, row
            self._marker = None
            if self.theme.animation == "none":
                self._trans = 1.0
            else:
                self._trans_anim.stop()
                self._trans_anim.start()
        next_text = next_text if self.theme.show_next_line else ""
        moved = self._update_marker(row, sung)
        if next_text != self._next or abs(sung - self._sung) > 0.01 or moved:
            self._next, self._sung = next_text, sung
            self.update()

    def _update_marker(self, row: _Row, sung: float) -> bool:
        """Glide the word highlight toward the word being sung (called ~60x/s)."""
        if self.theme.highlight != "word" or row.kind != LYRIC or not row.text:
            changed = self._marker is not None
            self._marker = None
            return changed
        target = self._layout(row).word_rect(sung)
        if target is None:
            changed = self._marker is not None
            self._marker = None
            return changed
        m = self._marker
        if m is None or abs(m.y() - target.y()) > 1:     # new line / new row: appear in place
            self._marker = QRectF(target)
            return True
        k = 0.32                                           # smoothing per frame
        nx = m.x() + (target.x() - m.x()) * k
        nw = m.width() + (target.width() - m.width()) * k
        self._marker = QRectF(nx, target.y(), nw, target.height())
        return abs(nx - m.x()) > 0.05 or abs(nw - m.width()) > 0.05

    def set_preview(self, enabled: bool) -> None:
        """Show sample text (used while the Settings window is open)."""
        self._preview = enabled
        self._fade_to(self._target_opacity())
        self.update()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._fade_to(self._target_opacity())

    def set_paused(self, paused: bool) -> None:
        if paused != self._paused:
            self._paused = paused
            self._fade_to(self._target_opacity())

    def _target_opacity(self) -> float:
        if self._preview:
            return 1.0
        if not self._active:
            return 0.0
        return self.theme.paused_opacity if self._paused else 1.0

    def _fade_to(self, target: float) -> None:
        if target > 0 and not self.isVisible():
            self._reposition()
            self.show()
            self._harden()
            self._topmost_timer.start()
        if self.theme.fade_ms <= 1:
            self._opacity = target
            self._on_fade_done()
            self.update()
            return
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity)
        self._fade_anim.setEndValue(target)
        self._fade_anim.start()

    def _on_fade(self, v) -> None:
        self._opacity = float(v)
        self.update()

    def _on_fade_done(self) -> None:
        if self._opacity <= 0.001 and not self._preview:
            self.hide()
            self._topmost_timer.stop()

    def _on_trans(self, v) -> None:
        self._trans = float(v)
        self.update()

    # ------------------------------------------------------------------ mouse (drag anywhere)
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.pos()
            self.setCursor(Qt.ClosedHandCursor)
        elif e.button() == Qt.RightButton:
            self.context_menu.emit(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._drag_offset is not None:
            moved = self._press_pos is not None and (e.globalPosition().toPoint() - self._press_pos).manhattanLength() > 3
            self._drag_offset = None
            self.setCursor(Qt.OpenHandCursor)
            if moved:
                self._saved_pos = self.pos()
                self.moved.emit(self.pos())

    def enterEvent(self, e):
        self._hover = True
        self.setCursor(Qt.OpenHandCursor)
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    # ------------------------------------------------------------------ painting
    def _area_width(self) -> float:
        if self._dock is not None:
            return self.width() - 28
        return self.width() - 2 * self.theme.panel_padding_x - 6

    def _style(self, kind: str):
        if kind == LYRIC:
            return self.f_current, self.c_text, self.theme.max_lines
        if kind == GAP:
            return self.f_current, self.c_accent, 1
        return self.f_status, self.c_status, 1

    def _layout(self, row: _Row) -> _Layout:
        key = (row, self.width())
        lay = self._layout_cache.get(key)
        if lay is None:
            font, _, max_lines = self._style(row.kind)
            if self._dock is not None:
                max_lines = 1
                fm = QFontMetricsF(font)
                natural = fm.horizontalAdvance(row.text)
                if natural > self._area_width():
                    font = QFont(font)
                    if natural * 0.86 <= self._area_width():
                        # a slight shrink keeps it on one row
                        font.setPixelSize(max(10, int(font.pixelSize() * self._area_width() / natural)))
                    else:
                        # two rows that together fit the taskbar's height
                        font.setPixelSize(max(10, int(self._dock.height() * 0.30)))
                        max_lines = 2
            lay = _Layout(row.text, font, self._area_width(), max_lines)
            if len(self._layout_cache) > 16:
                self._layout_cache.clear()
            self._layout_cache[key] = lay
        return lay

    def _panel_size(self, row: _Row, next_text: str) -> tuple[float, float]:
        t = self.theme
        if not row.text and not next_text:
            return (0.0, 0.0)
        w, h = 0.0, 0.0
        if row.text:
            lay = self._layout(row)
            w, h = lay.text_width, lay.height
        if next_text:
            fm = QFontMetricsF(self.f_next)
            w = max(w, min(fm.horizontalAdvance(next_text), self._area_width()))
            h += (t.line_gap if row.text else 0) + fm.lineSpacing()
        return (w + 2 * t.panel_padding_x, h + 2 * t.panel_padding_y)

    def _current_panel(self) -> tuple[float, float]:
        target = self._panel_size(self._row, self._next)
        return (_lerp(self._panel_from[0], target[0], self._trans),
                _lerp(self._panel_from[1], target[1], self._trans))

    def _draw_layout(self, p: QPainter, lay: _Layout, x: float, y: float, color: QColor, opacity: float):
        if opacity <= 0.01 or not lay.text:
            return
        base = p.opacity()
        p.setOpacity(base * opacity)
        if self.theme.shadow:
            p.setPen(self.c_shadow)
            lay.layout.draw(p, QPointF(x + 1.0, y + 1.2))
        p.setPen(color)
        lay.layout.draw(p, QPointF(x, y))
        p.setOpacity(base)

    def _paint_docked(self, p: QPainter, row: _Row, sung: float) -> None:
        t = self.theme
        W, H = self.width(), self.height()
        pill = QRectF(3, 5, W - 6, H - 10)
        path = QPainterPath()
        path.addRoundedRect(pill, min(t.panel_radius, pill.height() / 2), min(t.panel_radius, pill.height() / 2))
        fill = QColor(t.panel_color)
        fill.setAlphaF(max(1 / 255, min(1.0, t.panel_opacity)))
        p.fillPath(path, fill)
        if self._hover:
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.drawPath(path)
        x0 = (W - self._area_width()) / 2

        def draw(r: _Row, opacity: float, dy: float, chars: float, current: bool):
            if not r.text:
                return
            lay = self._layout(r)
            _, color, _ = self._style(r.kind)
            y = (H - lay.height) / 2 + dy
            self._draw_layout(p, lay, x0, y, color, opacity)
            if current and r.kind == LYRIC and t.highlight == "underline" and chars > 0:
                th = max(1.0, t.underline_thickness)
                base = p.opacity()
                p.setOpacity(base * opacity)
                for x1, x2, yy in lay.underline_segments(chars):
                    bar = QPainterPath()
                    bar.addRoundedRect(QRectF(x0 + x1, y + min(yy, lay.height + 1) - th / 2, max(th, x2 - x1), th), th / 2, th / 2)
                    p.fillPath(bar, self.c_accent)
                p.setOpacity(base)

        trans = 1.0 if self._preview else self._trans
        p.save()
        p.setClipRect(pill)
        if trans < 1.0 and t.animation != "none":
            slide = min(t.slide_px, H * 0.2) if t.animation == "slide" else 0.0
            draw(self._prev_row, 1.0 - trans, -slide * trans, 999.0, False)
            draw(row, trans, slide * (1.0 - trans), sung, True)
        else:
            draw(row, 1.0, 0.0, sung, True)
        p.restore()

    def paintEvent(self, _event) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        W, H = self.width(), self.height()

        row, next_text, sung = self._row, self._next, self._sung
        if self._dock is not None:
            next_text = ""
        if self._preview and not (self._active and row.text):
            row, next_text, sung = _Row("Lyrics look like this while a song plays", LYRIC), "and the next line waits below", 18.0
        p.setOpacity(self._opacity)

        if self._dock is not None:
            self._paint_docked(p, row, sung)
            p.end()
            return

        # --- panel (bottom-anchored so it hugs the taskbar while resizing)
        target = self._panel_size(row, next_text)
        trans = 1.0 if self._preview else self._trans
        pw = min(_lerp(self._panel_from[0], target[0], trans), W - 2)
        ph = _lerp(self._panel_from[1], target[1], trans)
        panel = QRectF((W - pw) / 2, H - ph - 1, pw, ph)
        if pw > 1 and ph > 1:
            path = QPainterPath()
            path.addRoundedRect(panel, t.panel_radius, t.panel_radius)
            fill = QColor(t.panel_color)
            # never fully transparent: an alpha of ~1/255 keeps it draggable
            fill.setAlphaF(max(1 / 255, min(1.0, t.panel_opacity)))
            p.fillPath(path, fill)
            if self._hover and not self._click_through:
                p.setPen(QPen(QColor(255, 255, 255, 40), 1))
                p.drawPath(path)

        # --- rows, laid out bottom-up: next line at the bottom, current above
        x0 = (W - self._area_width()) / 2
        bottom = H - 1 - t.panel_padding_y
        fm_n = QFontMetricsF(self.f_next)
        next_h = fm_n.lineSpacing() if next_text else 0.0
        if next_text:
            p.setFont(self.f_next)
            nt = fm_n.elidedText(next_text, Qt.ElideRight, self._area_width())
            p.setOpacity(self._opacity * (1.0 if self._preview else self._trans))
            rect = QRectF(x0, bottom - next_h, self._area_width(), next_h)
            if t.shadow:
                p.setPen(self.c_shadow)
                p.drawText(rect.translated(1.0, 1.2), Qt.AlignHCenter | Qt.AlignVCenter, nt)
            p.setPen(self.c_next)
            p.drawText(rect, Qt.AlignHCenter | Qt.AlignVCenter, nt)
            p.setOpacity(self._opacity)
        cur_bottom = bottom - (next_h + t.line_gap if next_text else 0.0)

        def draw_row(r: _Row, opacity: float, dy: float, sung_chars: float, current: bool = False):
            if not r.text:
                return
            lay = self._layout(r)
            _, color, _ = self._style(r.kind)
            y = cur_bottom - lay.height + dy
            marker = self._marker if (current and t.highlight == "word" and r.kind == LYRIC) else None
            if marker is not None and opacity > 0.05:
                # Moving word highlight: soft pill + the word at full brightness,
                # the rest of the line slightly softer.
                pad_x, pad_y = 4.0, 1.0
                rect = marker.translated(x0, y).adjusted(-pad_x, pad_y, pad_x, -pad_y)
                pill = QPainterPath()
                pill.addRoundedRect(rect, rect.height() / 2.6, rect.height() / 2.6)
                fill = QColor(self.c_accent)
                fill.setAlphaF(max(0.0, min(1.0, t.highlight_strength)))
                base = p.opacity()
                p.setOpacity(base * opacity)
                p.fillPath(pill, fill)
                p.setOpacity(base)
                soft = QColor(color)
                soft.setAlphaF(color.alphaF() * max(0.0, min(1.0, t.unsung_opacity)))
                self._draw_layout(p, lay, x0, y, soft, opacity)
                p.save()
                p.setClipRect(rect)
                p.setOpacity(base * opacity)
                p.setPen(color)
                lay.layout.draw(p, QPointF(x0, y))
                p.restore()
                return
            self._draw_layout(p, lay, x0, y, color, opacity)
            if r.kind == LYRIC and t.highlight == "underline" and sung_chars > 0 and opacity > 0.05:
                th = max(1.0, t.underline_thickness)
                base = p.opacity()
                p.setOpacity(base * opacity)
                for x1, x2, yy in lay.underline_segments(sung_chars):
                    bar = QPainterPath()
                    bar.addRoundedRect(QRectF(x0 + x1, y + yy - th / 2, max(th, x2 - x1), th), th / 2, th / 2)
                    p.fillPath(bar, self.c_accent)
                p.setOpacity(base)

        if trans < 1.0 and t.animation != "none":
            slide = t.slide_px if t.animation == "slide" else 0.0
            draw_row(self._prev_row, 1.0 - trans, -slide * trans, 999.0)
            draw_row(row, trans, slide * (1.0 - trans), sung, current=True)
        else:
            draw_row(row, 1.0, 0.0, sung, current=True)
        p.end()
