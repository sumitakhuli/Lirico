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


# --------------------------------------------------------------------------- title bar
# The empty strip of the active window's title bar, just left of its
# minimise / maximise / close buttons.

_dwmapi = ctypes.windll.dwmapi
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                 "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow", "TopLevelWindowForOverflowXamlIsland"}
DWMWA_CAPTION_BUTTON_BOUNDS, DWMWA_EXTENDED_FRAME_BOUNDS = 5, 9
MIN_TITLE_SLOT = 170     # logical px


def _dwm_rect(hwnd, attr) -> Optional[_RECT]:
    r = _RECT()
    try:
        if _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(r), ctypes.sizeof(r)) == 0:
            return r
    except Exception:
        pass
    return None


def _usable_window(hwnd, ignore: tuple) -> bool:
    if not hwnd or int(hwnd) in ignore or not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
        return False
    buf = ctypes.create_unicode_buffer(80)
    _user32.GetClassNameW(hwnd, buf, 80)
    return buf.value not in _SKIP_CLASSES


class TitleBarDock:
    """Finds a slot in the foreground window's title bar, left of its buttons,
    and follows the active window as you switch apps."""

    def __init__(self):
        self._hwnd = None
        self._slot: Optional[QRect] = None
        self._last_scan = 0.0
        self._last_key = None

    def target(self, width: int, ignore: tuple = (), scan: bool = True) -> Optional[QRect]:
        fg = _user32.GetForegroundWindow()
        if _usable_window(fg, ignore):
            hwnd = int(fg)
        elif self._hwnd and _user32.IsWindow(self._hwnd) and _usable_window(self._hwnd, ignore):
            hwnd = self._hwnd          # desktop/taskbar focused: stay on the last window
        else:
            return None
        frame = _dwm_rect(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS)
        win = _RECT()
        if frame is None and not _user32.GetWindowRect(hwnd, ctypes.byref(win)):
            return None
        if frame is None:
            frame = win
        _user32.GetWindowRect(hwnd, ctypes.byref(win))
        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() or 1.0
        caps = _dwm_rect(hwnd, DWMWA_CAPTION_BUTTON_BOUNDS)
        if caps is not None and caps.right > caps.left and caps.bottom > caps.top:
            # caption bounds are relative to the window rect
            # (maximised windows overhang the screen: clip to the visible frame)
            bx = win.left + caps.left
            by = max(win.top + caps.top, frame.top)
            bh = win.top + caps.bottom - by
        else:   # apps that draw their own title bar (Chrome, VS Code...): standard size
            bw = int(138 * dpr)
            bx, by, bh = frame.right - bw, frame.top, int(32 * dpr)
        bh = max(int(26 * dpr), min(bh, int(48 * dpr)))
        right = bx - int(10 * dpr)
        if right - frame.left < MIN_TITLE_SLOT * dpr:
            return None
        key = (hwnd, frame.left, frame.top, frame.right, right)
        now = time.monotonic()
        if scan and (key != self._last_key or now - self._last_scan > 2.0):
            self._last_key, self._last_scan, self._hwnd = key, now, hwnd
            self._slot = self._scan(right, by, bh, width, dpr, frame.left)
        elif not scan:
            self._hwnd = hwnd
            w = int(width * dpr)
            return QRect(round((right - w) / dpr), round(by / dpr), width, round(bh / dpr))
        return self._slot

    def _scan(self, right: int, top: int, h: int, width: int, dpr: float, left_limit: int) -> Optional[QRect]:
        """Free run of title bar ending right next to the buttons (physical px in, logical out)."""
        w = min(int(width * dpr), right - left_limit)
        screen = QGuiApplication.primaryScreen()
        x0 = right - w
        pix = screen.grabWindow(0, round(x0 / dpr), round(top / dpr), round(w / dpr), round(h / dpr))
        img = pix.toImage().convertToFormat(QImage.Format_Grayscale8)
        iw, ih, bpl = img.width(), img.height(), img.bytesPerLine()
        if iw < 20 or ih < 8:
            return None
        g = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * ih).reshape(ih, bpl)[:, :iw]
        band = g[int(ih * 0.2): int(ih * 0.8)].astype(np.float32)
        busy = (band.std(axis=0) + np.abs(np.diff(band.mean(axis=0), prepend=band[:, :1].mean())) * 0.6) > 6.0
        # walk left from the buttons until something occupies the title bar
        free = 0
        for x in range(iw - 1, -1, -1):
            if busy[x]:
                break
            free += 1
        free_logical = (free / (iw / (w / dpr))) - 8
        if free_logical < MIN_TITLE_SLOT:
            return None
        slot_w = int(min(width, free_logical))
        return QRect(round(right / dpr) - slot_w, round(top / dpr), slot_w, round(h / dpr))
