"""System tray icon and menu. No logic here: it calls back into the controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .theme import Theme

if TYPE_CHECKING:  # pragma: no cover
    from .app import Controller

MENU_QSS = """
QMenu { background: #16181e; color: #e6e8ee; border: 1px solid #2a2f3a; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 6px 26px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #262b35; }
QMenu::item:disabled { color: #8a90a0; }
QMenu::separator { height: 1px; background: #262b35; margin: 5px 8px; }
QMenu::indicator { width: 14px; height: 14px; left: 4px; }
"""


def make_icon(theme: Theme = None) -> QIcon:
    """The app icon, rendered from the single logo definition in logo.py."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer

    from .logo import svg_bytes

    renderer = QSvgRenderer(QByteArray(svg_bytes()))
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p, QRectF(0, 0, size, size))
        p.end()
        icon.addPixmap(pm)
    return icon


def _mmss(seconds) -> str:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?:??"
    return f"{s // 60}:{s % 60:02d}"


class Tray:
    def __init__(self, ctl: "Controller", theme: Theme):
        self.ctl = ctl
        self.icon = QSystemTrayIcon(make_icon(theme))
        self.icon.setToolTip("Lirico")
        self.menu = QMenu()
        self.menu.setStyleSheet(MENU_QSS)
        self.menu.aboutToShow.connect(self._rebuild)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self._rebuild()
        self.icon.show()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:        # left click
            self.ctl.toggle_overlay()
        elif reason == QSystemTrayIcon.DoubleClick:
            self.ctl.open_settings()

    def popup(self, pos) -> None:
        self._rebuild()
        self.menu.popup(pos)

    def set_tooltip(self, text: str) -> None:
        self.icon.setToolTip(text[:120])

    def notify(self, title: str, text: str) -> None:
        self.icon.showMessage(title, text, QSystemTrayIcon.Information, 2500)

    def _add(self, menu: QMenu, text: str, slot=None, checked=None, enabled=True) -> QAction:
        act = QAction(text, menu)
        if checked is not None:
            act.setCheckable(True)
            act.setChecked(checked)
        act.setEnabled(enabled)
        if slot is not None:
            act.triggered.connect(slot)
        menu.addAction(act)
        return act

    def _hk(self, action: str) -> str:
        k = self.ctl.settings.hotkey(action)
        return f"\t{k}" if k else ""

    def _rebuild(self) -> None:
        ctl, m = self.ctl, self.menu
        m.clear()
        now, status = ctl.describe()
        self._add(m, now, enabled=False)
        if status:
            self._add(m, status, enabled=False)
        m.addSeparator()
        self._add(m, "Show lyrics" + self._hk("toggle"), ctl.toggle_overlay, checked=ctl.overlay_shown())
        intb = ctl.settings.position_mode == "taskbar"
        self._add(m, "In the taskbar", lambda: ctl.set_position_mode("float" if intb else "taskbar"), checked=intb)

        has_song = ctl.has_song()
        sync = m.addMenu(f"Sync  ({ctl.offset:+.2f} s)")
        sync.setStyleSheet(MENU_QSS)
        sync.setEnabled(has_song)
        self._add(sync, "Next line starts now" + self._hk("tap_sync"), ctl.tap_sync)
        sync.addSeparator()
        for label, delta, key in (("Earlier  +0.25 s", 0.25, "earlier"), ("Later  −0.25 s", -0.25, "later"),
                                  ("Earlier  +1 s", 1.0, ""), ("Later  −1 s", -1.0, "")):
            self._add(sync, label + (self._hk(key) if key else ""), lambda _=False, d=delta: ctl.nudge_offset(d))
        sync.addSeparator()
        self._add(sync, "Listen again (auto-sync)", ctl.listen_again,
                  enabled=ctl.settings.auto_sync and ctl.autosync.available)
        self._add(sync, "Reset" + self._hk("reset_offset"), lambda: ctl.nudge_offset(None))

        versions = m.addMenu("Lyrics version")
        versions.setStyleSheet(MENU_QSS)
        alts = ctl.alternatives()
        versions.setEnabled(has_song)
        media = ctl.media_duration()
        for alt in alts:
            d = alt.get("duration") or 0
            fit = ""
            if media and d:
                diff = d - media
                fit = "  · fits this video" if abs(diff) <= 1.6 else f"  · {diff:+.0f} s"
            kind = "" if alt.get("synced") else "  · unsynced"
            script = "  · हिन्दी" if alt.get("devanagari") else ""
            label = f"{(alt.get('track') or '?')[:34]} — {(alt.get('artist') or '?')[:22]}  ({_mmss(d)}){fit}{kind}{script}"
            self._add(versions, label, lambda _=False, i=alt.get("id"): ctl.choose_version(i),
                      checked=alt.get("id") == ctl.current_lyrics_id())
        if alts:
            versions.addSeparator()
        self._add(versions, "Search again", ctl.search_again)

        m.addSeparator()
        self._add(m, "Settings…" + self._hk("settings"), ctl.open_settings)
        self._add(m, "Quit Lirico", ctl.quit)
