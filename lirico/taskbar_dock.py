"""Docks the lyrics inside the taskbar's empty space.

Windows 11 has no API for apps to reserve room in the taskbar, so Lirico sits
on top of it, exactly over a free stretch, at taskbar height. The free stretch
is found by *looking*: a tiny capture of the taskbar strip, where columns with
icons, the search box or the clock have strong vertical detail and empty
columns are flat background. That works for centred and left-aligned icons,
Windows 10 and 11, light and dark taskbars.

Auto-hide: while the taskbar is hidden, the lyrics take the taskbar's spot at
the bottom edge; when it slides up, they move into its free gap.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication, QImage

from .winutil import _RECT

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32
_user32.FindWindowW.restype = wintypes.HWND
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]

MIN_GAP = 220        # logical px: narrower free space is not worth docking into
EDGE_MARGIN = 14     # keep this far from icons / screen edges
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11


class _APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


def exclude_from_capture(hwnd: int, enabled: bool) -> None:
    """Keep our own window out of screen captures, so scanning the taskbar never
    'sees' the lyrics we placed on it (Windows 10 2004+; silently ignored before)."""
    try:
        _user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE)
    except Exception:
        pass


@dataclass
class TaskbarState:
    rect: QRect          # taskbar rectangle, logical px
    autohide: bool
    visible: bool        # False while an auto-hide taskbar is tucked away
    bottom: bool         # docked at the bottom edge (the only layout we support)
    screen_rect: QRect


def taskbar_state() -> Optional[TaskbarState]:
    hwnd = _user32.FindWindowW("Shell_TrayWnd", None)
    screen = QGuiApplication.primaryScreen()
    if not hwnd or screen is None:
        return None
    r = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    dpr = screen.devicePixelRatio() or 1.0
    rect = QRect(round(r.left / dpr), round(r.top / dpr), round((r.right - r.left) / dpr), round((r.bottom - r.top) / dpr))
    abd = _APPBARDATA()
    abd.cbSize = ctypes.sizeof(_APPBARDATA)
    state = _shell32.SHAppBarMessage(0x4, ctypes.byref(abd))  # ABM_GETSTATE
    autohide = bool(state & 0x1)
    geo = screen.geometry()
    bottom = rect.width() >= geo.width() * 0.6 and rect.center().y() > geo.center().y()
    visible = rect.top() < geo.bottom() - 6
    return TaskbarState(rect, autohide, visible, bottom, geo)


def find_gaps(gray: np.ndarray, scale: float) -> list[tuple[int, int]]:
    """Free horizontal runs in a captured taskbar strip (grayscale, device px).
    Returns [(x0, x1)] in logical px relative to the strip."""
    h, w = gray.shape
    if h < 8 or w < 50:
        return []
    band = gray[int(h * 0.18): int(h * 0.82)].astype(np.float32)
    # vertical detail inside each column (icons, text, the clock)
    detail = band.std(axis=0)
    # sharp left/right edges between columns (icon borders)
    edges = np.abs(np.diff(band.mean(axis=0), prepend=band[:, :1].mean()))
    score = detail + edges * 0.6
    busy = score > max(6.0, float(np.median(score)) * 1.8)
    # grow occupied areas so the lyrics keep a margin from icons
    pad = max(1, int(EDGE_MARGIN * scale))
    kernel = np.ones(2 * pad + 1)
    busy = np.convolve(busy.astype(np.float32), kernel, mode="same") > 0
    gaps, start = [], None
    for x, b in enumerate(list(busy) + [True]):
        if not b and start is None:
            start = x
        elif b and start is not None:
            gaps.append((start, x))
            start = None
    return [(round(a / scale), round(b / scale)) for a, b in gaps if (b - a) / scale >= MIN_GAP]


class TaskbarDock:
    """Works out where the lyrics should sit for the current taskbar state."""

    def __init__(self):
        self._gap: Optional[QRect] = None
        self._misses = 0
        self._last_scan = 0.0
        self._last_rect: Optional[QRect] = None
        self._shown_since = 0.0

    def target(self, max_width: int) -> Optional[QRect]:
        """Rect for the docked lyrics, or None to float (no taskbar / no room)."""
        st = taskbar_state()
        if st is None or not st.bottom:
            return None
        if st.autohide and not st.visible:
            # Taskbar tucked away: take its spot at the bottom edge.
            self._shown_since = 0.0
            h = max(40, st.rect.height())
            w = min(max_width, st.screen_rect.width() - 2 * EDGE_MARGIN)
            x = st.screen_rect.x() + (st.screen_rect.width() - w) // 2
            return QRect(x, st.screen_rect.bottom() + 1 - h - 2, w, h)
        now = time.monotonic()
        if st.autohide and self._shown_since == 0.0:
            self._shown_since = now          # it just slid up: let the animation finish
        if st.autohide and now - self._shown_since < 0.35:
            return self._gap
        if st.rect != self._last_rect or now - self._last_scan > 2.0 or self._gap is None:
            self._last_rect = QRect(st.rect)
            self._last_scan = now
            self._scan(st, max_width)
        return self._gap

    def _scan(self, st: TaskbarState, max_width: int) -> None:
        screen = QGuiApplication.primaryScreen()
        r = st.rect.intersected(st.screen_rect)
        if r.isEmpty():
            self._gap = None
            return
        pix = screen.grabWindow(0, r.x(), r.y(), r.width(), r.height())
        img = pix.toImage().convertToFormat(QImage.Format_Grayscale8)
        w, h, bpl = img.width(), img.height(), img.bytesPerLine()
        if w == 0 or h == 0:
            return
        buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)[:, :w]
        scale = w / max(1, r.width())
        gaps = find_gaps(buf, scale)
        if not gaps:
            self._miss()
            return
        # Keep the current spot while it is still free (no jumping around);
        # otherwise take the widest free stretch.
        if self._gap is not None:
            for a, b in gaps:
                if r.x() + a <= self._gap.left() and self._gap.right() <= r.x() + b:
                    self._misses = 0
                    return
        a, b = max(gaps, key=lambda g: g[1] - g[0])
        width = min(b - a, max_width)
        x = r.x() + a + (b - a - width) // 2
        new = QRect(x, r.y(), width, r.height())
        if self._gap is None or self._misses >= 1:
            self._gap, self._misses = new, 0
        else:
            self._misses += 1              # confirm on the next scan (hover glow etc.)

    def _miss(self) -> None:
        self._misses += 1
        if self._misses >= 2:
            self._gap = None               # really no room: float above the taskbar
