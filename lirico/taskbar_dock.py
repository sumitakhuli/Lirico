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
import os
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
        self.light = False         # the taskbar is light (Windows light mode)

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
        self.light = float(buf.mean()) > 150
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
# The lyrics attach to ONE window's title bar (say VS Code), in the empty
# strip left of its minimise / maximise / close buttons. While you work in
# another app they visit the taskbar; when that window is active again they
# go back to its title bar. They never just disappear.

_dwmapi = ctypes.windll.dwmapi
_kernel32 = ctypes.windll.kernel32
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetTopWindow.restype = wintypes.HWND
_user32.GetTopWindow.argtypes = [wintypes.HWND]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                 "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow", "TopLevelWindowForOverflowXamlIsland",
                 "TaskListThumbnailWnd", "ForegroundStaging", "MultitaskingViewFrame"}
DWMWA_CAPTION_BUTTON_BOUNDS, DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 5, 9, 14
GA_ROOTOWNER, GW_HWNDNEXT = 3, 2
MIN_TITLE_SLOT = 150     # logical px
_OWN_PID = os.getpid()


def _dwm_rect(hwnd, attr) -> Optional[_RECT]:
    r = _RECT()
    try:
        if _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(r), ctypes.sizeof(r)) == 0:
            return r
    except Exception:
        pass
    return None


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def exe_name(hwnd) -> str:
    """'Code.exe' for a VS Code window ('' if unknown)."""
    h = _kernel32.OpenProcess(0x1000, False, _pid(hwnd))     # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wintypes.DWORD(520)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(h)


def _cloaked(hwnd) -> bool:
    v = wintypes.DWORD()
    try:
        _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        return False
    return bool(v.value)


def _usable_window(hwnd) -> bool:
    """A normal app window (not ours, not the shell, not hidden or minimised)."""
    if not hwnd or not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
        return False
    if _pid(hwnd) == _OWN_PID or _cloaked(hwnd):
        return False
    ex = _user32.GetWindowLongW(hwnd, -20)                   # GWL_EXSTYLE
    if ex & 0x80 or ex & 0x08000000:                         # tool windows, no-activate popups
        return False
    if _user32.GetWindowTextLengthW(hwnd) == 0:              # untitled helper windows
        return False
    r = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)) or r.right - r.left < 300 or r.bottom - r.top < 120:
        return False
    buf = ctypes.create_unicode_buffer(80)
    _user32.GetClassNameW(hwnd, buf, 80)
    return buf.value not in _SKIP_CLASSES


def _root(hwnd) -> int:
    r = _user32.GetAncestor(hwnd, GA_ROOTOWNER) if hwnd else None
    return int(r or hwnd or 0)


class TitleBarDock:
    """Remembers the window the lyrics are attached to and finds the free slot
    in its title bar."""

    def __init__(self, app: str = ""):
        self.anchor: Optional[int] = None    # attached window (this session)
        self.app = app                       # its exe, remembered across restarts
        self.active = 0                      # last app window you used (not ours)
        self._raw_fg = None
        self._slots: dict = {}               # hwnd -> (key, time, slot)

    # -- which window ---------------------------------------------------------
    def poll(self) -> None:
        """Track the window you're using. Only real changes count, so a window we
        just attached to stays 'active' until you switch to another one."""
        fg = _user32.GetForegroundWindow()
        fg = int(fg) if fg else 0
        if fg == self._raw_fg:
            return
        self._raw_fg = fg
        root = _root(fg)
        if _usable_window(root):
            self.active = root
            if self.anchor is None and self.app and exe_name(root).lower() == self.app.lower():
                self.anchor = root          # after a restart: re-attach to the same app

    def attach(self, hwnd: Optional[int] = None) -> bool:
        """Attach to `hwnd` (default: the window you used last)."""
        hwnd = _root(hwnd or self.active)
        if not hwnd or not _usable_window(hwnd):
            return False
        self.anchor, self.active = hwnd, hwnd
        self.app = exe_name(hwnd)
        return True

    def window_at(self, x: int, y: int) -> Optional[int]:
        """Top-most app window whose frame (or the band just above it) holds the
        point, in logical px."""
        dpr = QGuiApplication.primaryScreen().devicePixelRatio() or 1.0
        px, py = x * dpr, y * dpr
        h = _user32.GetTopWindow(None)
        for _ in range(600):
            if not h:
                break
            if _usable_window(h):
                f = _dwm_rect(h, DWMWA_EXTENDED_FRAME_BOUNDS)
                if f is not None and f.left <= px < f.right and f.top - 40 * dpr <= py < f.bottom:
                    return int(h)
            h = _user32.GetWindow(h, GW_HWNDNEXT)
        return None

    # -- where ----------------------------------------------------------------
    def target(self, width: int) -> Optional[QRect]:
        """The title-bar slot while the attached window is the one in use;
        None otherwise (the caller shows the lyrics in the taskbar meanwhile)."""
        self.poll()
        if self.anchor is not None and not _user32.IsWindow(self.anchor):
            self.anchor = None                # closed: wait for the app to come back
            self.poll()
        if self.anchor is None or self.active != self.anchor or not _usable_window(self.anchor):
            return None
        return self.slot(self.anchor, width)

    def slot(self, hwnd: int, width: int) -> Optional[QRect]:
        frame = _dwm_rect(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS)
        win = _RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(win)):
            return None
        if frame is None:
            frame = win
        dpr = QGuiApplication.primaryScreen().devicePixelRatio() or 1.0
        caps = _dwm_rect(hwnd, DWMWA_CAPTION_BUTTON_BOUNDS)
        if caps is not None and caps.right > caps.left and caps.bottom > caps.top:
            # caption bounds are relative to the window rect
            # (maximised windows overhang the screen: clip to the visible frame)
            bx = min(win.left + caps.left, frame.right - int(100 * dpr))
            by = max(win.top + caps.top, frame.top)
            bh = win.top + caps.bottom - by
        else:   # apps that draw their own title bar (Chrome, VS Code...): standard size
            bx, by, bh = frame.right - int(138 * dpr), frame.top, int(32 * dpr)
        bh = max(int(26 * dpr), min(bh, int(48 * dpr)))
        right = bx - int(6 * dpr)
        if right - frame.left < MIN_TITLE_SLOT * dpr:
            return None
        key = (frame.left, frame.top, frame.right, right, width)
        now = time.monotonic()
        cached = self._slots.get(hwnd)
        if cached is not None and cached[0] == key and now - cached[1] < 2.0:
            return cached[2]
        found = self._scan(right, by, bh, width, dpr, frame.left)
        if len(self._slots) > 32:
            self._slots.clear()
        self._slots[hwnd] = (key, now, found)
        return found

    def _scan(self, right: int, top: int, h: int, width: int, dpr: float, left_limit: int) -> Optional[QRect]:
        """Widest free run of title bar near the buttons, e.g. between Chrome's
        last tab and its buttons (physical px in, logical out)."""
        look = min(int((width + 160) * dpr), right - left_limit)
        x0 = right - look
        screen = QGuiApplication.primaryScreen()
        pix = screen.grabWindow(0, round(x0 / dpr), round(top / dpr), round(look / dpr), round(h / dpr))
        img = pix.toImage().convertToFormat(QImage.Format_Grayscale8)
        iw, ih, bpl = img.width(), img.height(), img.bytesPerLine()
        if iw < 20 or ih < 8:
            return None
        g = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * ih).reshape(ih, bpl)[:, :iw]
        band = g[int(ih * 0.2): int(ih * 0.8)].astype(np.float32)
        busy = (band.std(axis=0) + np.abs(np.diff(band.mean(axis=0), prepend=band[:, :1].mean())) * 0.6) > 6.0
        scale = iw / (look / dpr)                     # captured px per logical px
        pad = max(1, int(6 * scale))
        busy = np.convolve(busy.astype(np.float32), np.ones(2 * pad + 1), mode="same") > 0
        best = None
        start = None
        for x, b in enumerate(list(busy) + [True]):
            if not b and start is None:
                start = x
            elif b and start is not None:
                if (x - start) / scale >= MIN_TITLE_SLOT:
                    best = (start, x)                 # keep the right-most big enough run
                start = None
        if best is None:
            return None
        a, b = best
        slot_w = int(min(width, (b - a) / scale))
        right_l = round(x0 / dpr + b / scale)
        return QRect(right_l - slot_w, round(top / dpr), slot_w, round(h / dpr))
