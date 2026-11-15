"""Settings window. Every control applies live to the overlay and is saved.

Its own look (the window chrome) is the small style sheet below; the overlay's
look is the Theme (theme.py) that these controls edit.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFontComboBox, QFrame,
                               QHBoxLayout, QKeySequenceEdit, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QSlider, QStackedWidget, QVBoxLayout, QWidget)

from .settings import HOTKEY_ACTIONS
from .theme import PRESETS, THEME, Theme, base_theme

if TYPE_CHECKING:  # pragma: no cover
    from .app import Controller

WINDOW_QSS = """
QWidget { background: #14161b; color: #e6e8ee; font-family: 'Segoe UI Variable Text', 'Segoe UI';
          font-size: 10pt; }
QWidget#side, QWidget#quick { background: #101216; }
QListWidget#nav { background: #101216; border: none; padding: 10px 6px; outline: none; }
QLabel#qhead { color: #5d6474; font-size: 8.5pt; letter-spacing: 1px; background: transparent; }
QCheckBox#qtoggle { color: #c3c8d3; spacing: 10px; background: transparent; }
QCheckBox#qtoggle::indicator { width: 28px; height: 15px; border-radius: 7px; }
QPushButton#coffee { background: #2a2416; color: #ffd27a; border: 1px solid #3d3420; border-radius: 9px; padding: 8px 10px; }
QPushButton#coffee:hover { background: #352d19; }
QListWidget#nav::item { padding: 9px 14px; border-radius: 8px; margin: 1px 4px; color: #aab0bd; }
QListWidget#nav::item:selected { background: #22262f; color: #ffffff; }
QListWidget#nav::item:hover:!selected { background: #1a1d24; }
QLabel#h1 { font-size: 15pt; font-weight: 600; color: #ffffff; padding-bottom: 2px; }
QLabel#hint { color: #8a90a0; font-size: 9pt; }
QLabel#err { color: #ff8a8a; font-size: 9pt; }
QLabel#value { color: #aab0bd; min-width: 46px; }
QFrame#row { border-bottom: 1px solid #1f232b; }
QSlider::groove:horizontal { height: 4px; background: #2a2f3a; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #7ee0c3; border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }
QComboBox, QFontComboBox, QDoubleSpinBox, QKeySequenceEdit, QLineEdit {
    background: #1c1f27; border: 1px solid #2a2f3a; border-radius: 7px; padding: 4px 8px; min-height: 22px; }
QComboBox:hover, QFontComboBox:hover, QDoubleSpinBox:hover { border-color: #3a4150; }
QPushButton#danger { color: #ff8a8a; border-color: #5a2a30; }
QPushButton#danger:hover { background: #3a1d22; }
QComboBox QAbstractItemView { background: #1c1f27; border: 1px solid #2a2f3a; selection-background-color: #2a3140; }
QPushButton { background: #22262f; border: 1px solid #2a2f3a; border-radius: 7px; padding: 6px 14px; }
QPushButton:hover { background: #2a2f3a; }
QPushButton#primary { background: #7ee0c3; color: #0e1014; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #9bead2; }
QPushButton#swatch { min-width: 54px; max-width: 54px; min-height: 22px; padding: 0; border-radius: 6px; }
QCheckBox::indicator { width: 34px; height: 18px; border-radius: 9px; background: #2a2f3a; }
QCheckBox::indicator:checked { background: #7ee0c3; }
QScrollArea { border: none; }
"""

WEIGHTS = [("Light", 300), ("Regular", 400), ("Medium", 500), ("Semibold", 600), ("Bold", 700), ("Black", 900)]


class SettingsWindow(QWidget):
    def __init__(self, ctl: "Controller"):
        super().__init__(None, Qt.Window)
        self.ctl = ctl
        self.setWindowTitle("Lirico · Settings")
        self.setWindowIcon(ctl.tray.icon.icon())
        self.setStyleSheet(WINDOW_QSS)
        self.resize(760, 560)
        self.theme: Theme = ctl.theme

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.nav = QListWidget(objectName="nav")
        self.pages = QStackedWidget()
        self._refreshers: list[Callable[[], None]] = []
        side = QWidget(objectName="side")
        side.setFixedWidth(190)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 16)
        sv.setSpacing(0)
        sv.addWidget(self.nav)
        sv.addStretch(1)
        sv.addWidget(self._quick_panel())
        root.addWidget(side)
        root.addWidget(self.pages, 1)

        self._page("Appearance", self._build_appearance)
        self._page("Motion", self._build_motion)
        self._page("Behavior", self._build_behavior)
        self._page("Sync & Lyrics", self._build_sync)
        self._page("Hotkeys", self._build_hotkeys)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setFixedHeight(5 * 38 + 24)
        self.nav.setCurrentRow(0)

        self._save_timer = QTimer(self, singleShot=True, interval=400)
        self._save_timer.timeout.connect(self.ctl.settings.save)

    def _quick_panel(self) -> QWidget:
        """Most-used switches, always one click away in the sidebar."""
        st, ctl = self.ctl.settings, self.ctl
        box = QWidget(objectName="quick")
        v = QVBoxLayout(box)
        v.setContentsMargins(16, 10, 12, 0)
        v.setSpacing(10)
        v.addWidget(QLabel("QUICK", objectName="qhead"))

        def quick(label, getter, setter):
            c = QCheckBox(label, objectName="qtoggle")
            c.setChecked(bool(getter()))
            c.toggled.connect(setter)
            self._refreshers.append(lambda: (c.blockSignals(True), c.setChecked(bool(getter())), c.blockSignals(False)))
            v.addWidget(c)

        quick("Show lyrics", lambda: st.overlay_visible,
              lambda on: (setattr(st, "overlay_visible", bool(on)), st.save()))
        quick("Stick to taskbar", lambda: st.position_mode != "float",
              lambda on: ctl.set_position_mode("taskbar" if on else "float"))
        quick("Auto-sync", lambda: st.auto_sync, ctl.set_auto_sync)
        quick("Start with Windows", ctl.autostart_enabled, ctl.set_autostart)
        from . import SUPPORT_URL
        if SUPPORT_URL:
            coffee = QPushButton("☕  Buy me a coffee", objectName="coffee")
            coffee.clicked.connect(ctl.open_support)
            v.addSpacing(6)
            v.addWidget(coffee)
        return box

    # ------------------------------------------------------------------ helpers
    def _page(self, name: str, builder) -> None:
        QListWidgetItem(name, self.nav).setSizeHint(QSize(150, 36))
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(0)
        title = QLabel(name, objectName="h1")
        lay.addWidget(title)
        lay.addSpacing(10)
        builder(lay)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.pages.addWidget(scroll)

    def _row(self, lay: QVBoxLayout, label: str, widget: QWidget, hint: str = "") -> None:
        row = QFrame(objectName="row")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 9, 0, 9)
        left = QVBoxLayout()
        left.setSpacing(1)
        left.addWidget(QLabel(label))
        if hint:
            lbl = QLabel(hint, objectName="hint")
            lbl.setWordWrap(True)
            left.addWidget(lbl)
        h.addLayout(left, 1)
        h.addWidget(widget, 0, Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(row)

    def _set_theme(self, **changes) -> None:
        self.theme = replace(self.ctl.theme, **changes)
        self.ctl.apply_theme(self.theme, save=False)
        self.ctl.settings.theme = self.theme.diff(base_theme(self.ctl.settings.theme_preset))
        self._save_timer.start()

    def _slider(self, field: str, lo: float, hi: float, step: float = 1.0, fmt: str = "{:.0f}",
                scale: float = 1.0) -> QWidget:
        """Slider bound to a theme field. `scale` maps stored value -> shown value."""
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        s = QSlider(Qt.Horizontal)
        s.setFixedWidth(200)
        n = int(round((hi - lo) / step))
        s.setRange(0, n)
        value = QLabel(objectName="value")
        typ = type(getattr(THEME, field))

        def refresh():
            v = getattr(self.theme, field) * scale
            s.blockSignals(True)
            s.setValue(int(round((v - lo) / step)))
            s.blockSignals(False)
            value.setText(fmt.format(v))

        def changed(i):
            v = lo + i * step
            value.setText(fmt.format(v))
            self._set_theme(**{field: typ(v / scale)})

        s.valueChanged.connect(changed)
        refresh()
        self._refreshers.append(refresh)
        h.addWidget(s)
        h.addWidget(value)
        return box

    def _toggle(self, getter, setter) -> QCheckBox:
        c = QCheckBox()
        c.setChecked(bool(getter()))
        c.toggled.connect(setter)
        self._refreshers.append(lambda: (c.blockSignals(True), c.setChecked(bool(getter())), c.blockSignals(False)))
        return c

    def _theme_toggle(self, field: str) -> QCheckBox:
        return self._toggle(lambda: getattr(self.theme, field), lambda v: self._set_theme(**{field: bool(v)}))

    def _color(self, field: str, alpha: bool = True) -> QPushButton:
        b = QPushButton(objectName="swatch")

        def paint():
            c = QColor(getattr(self.theme, field))
            b.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},{max(60, c.alpha())});"
                            f"border: 1px solid #3a4150;")

        def pick():
            opts = QColorDialog.ShowAlphaChannel if alpha else QColorDialog.ColorDialogOption(0)
            c = QColorDialog.getColor(QColor(getattr(self.theme, field)), self, "Choose color", opts)
            if c.isValid():
                self._set_theme(**{field: c.name(QColor.HexArgb)})
                paint()

        b.clicked.connect(pick)
        paint()
        self._refreshers.append(paint)
        return b

    def _combo(self, items: list[tuple[str, object]], getter, setter) -> QComboBox:
        c = QComboBox()
        for label, val in items:
            c.addItem(label, val)

        def refresh():
            c.blockSignals(True)
            idx = max(0, next((i for i, (_, v) in enumerate(items) if v == getter()), 0))
            c.setCurrentIndex(idx)
            c.blockSignals(False)

        c.currentIndexChanged.connect(lambda i: setter(items[i][1]))
        refresh()
        self._refreshers.append(refresh)
        return c

    def _refresh_all(self) -> None:
        for r in self._refreshers:
            r()

    # ------------------------------------------------------------------ pages
    def _build_appearance(self, lay: QVBoxLayout) -> None:
        st = self.ctl.settings

        def pick_preset(name):
            self.ctl.set_preset(name)
            self.theme = self.ctl.theme
            self._refresh_all()

        self._row(lay, "Theme", self._combo([(label, key) for key, (label, _) in PRESETS.items()],
                                            lambda: st.theme_preset, pick_preset),
                  "A ready-made look. Everything below still fine-tunes it")
        self._row(lay, "Little extras", self._combo(
            [("None", ""), ("Flowers & teddies 🌸🧸", "🌸 🧸 🎀 💗 🌷"), ("Stars ✨", "✨ ⭐ 💫"),
             ("Hearts 💗", "💗 💖 💕"), ("Music 🎵", "🎵 🎶 🎧")],
            lambda: self.theme.deco_glyphs, lambda v: self._set_theme(deco_glyphs=v)),
                  "A tiny picture rides the underline")
        font = QFontComboBox()
        font.setFixedWidth(240)
        font.setCurrentFont(QFont(self.theme.font_family))
        font.currentFontChanged.connect(lambda f: self._set_theme(font_family=f.family()))
        self._refreshers.append(lambda: (font.blockSignals(True), font.setCurrentFont(QFont(self.theme.font_family)),
                                         font.blockSignals(False)))
        self._row(lay, "Font", font, "Hindi (Devanagari) always falls back to Nirmala UI")
        self._row(lay, "Text size", self._slider("font_size", 9, 40, 0.5, "{:.1f} pt"))
        self._row(lay, "Weight", self._combo(WEIGHTS, lambda: self.theme.font_weight,
                                             lambda v: self._set_theme(font_weight=v)))
        self._row(lay, "Next line size", self._slider("next_size", 7, 30, 0.5, "{:.1f} pt"))
        self._row(lay, "Text color", self._color("text_color"))
        self._row(lay, "Next line color", self._color("next_color"))
        self._row(lay, "Accent color", self._color("accent_color"), "Underline and ♪")
        self._row(lay, "Background color", self._color("panel_color", alpha=False))
        self._row(lay, "Background opacity", self._slider("panel_opacity", 0, 100, 1, "{:.0f}%", scale=100))
        self._row(lay, "Corner radius", self._slider("panel_radius", 0, 30, 1, "{:.0f} px"))
        self._row(lay, "Box width", self._slider("box_width", 260, 900, 10, "{:.0f} px"),
                  "Fixed width of the floating lyrics box; long lines wrap inside it")
        self._row(lay, "Max rows per line", self._slider("max_lines", 1, 3, 1))
        self._row(lay, "Text shadow", self._theme_toggle("shadow"))
        self._row(lay, "Neon glow", self._theme_toggle("text_glow"))
        self._row(lay, "Capital letters", self._theme_toggle("uppercase"))
        self._row(lay, "Letter spacing", self._slider("letter_spacing", 0, 20, 1, "{:.0f}%"))
        self._row(lay, "Smooth edges", self._theme_toggle("font_antialias"), "Off gives a crisp pixel look")
        self._row(lay, "Dark text on a light taskbar", self._theme_toggle("adapt_to_taskbar"),
                  "With a see-through background, keeps lyrics readable on light taskbars")
        self._row(lay, "Show next line", self._theme_toggle("show_next_line"))
        self._row(lay, "Highlight sung words", self._combo(
            [("Moving word", "word"), ("Underline", "underline"), ("Off", "off")],
            lambda: self.theme.highlight, lambda v: self._set_theme(highlight=v)),
            "Shows exactly which word is being sung")
        self._row(lay, "Underline style", self._combo(
            [("Line", "line"), ("Glow", "glow"), ("Pixel blocks", "blocks")],
            lambda: self.theme.underline_style, lambda v: self._set_theme(underline_style=v)))
        self._row(lay, "Underline thickness", self._slider("underline_thickness", 1, 6, 0.5, "{:.1f} px"))
        self._row(lay, "Highlight strength", self._slider("highlight_strength", 0, 80, 5, "{:.0f}%", scale=100))
        self._row(lay, "Other words", self._slider("unsung_opacity", 20, 100, 5, "{:.0f}%", scale=100),
                  "Brightness of the words around the highlighted one")
        reset = QPushButton("Reset appearance & motion")
        reset.clicked.connect(self._reset_theme)
        lay.addSpacing(14)
        lay.addWidget(reset, 0, Qt.AlignLeft)

    def _build_motion(self, lay: QVBoxLayout) -> None:
        self._row(lay, "Line change", self._combo([("Slide up", "slide"), ("Fade", "fade"), ("None", "none")],
                                                  lambda: self.theme.animation,
                                                  lambda v: self._set_theme(animation=v)))
        self._row(lay, "Line change speed", self._slider("transition_ms", 0, 800, 10, "{:.0f} ms"))
        self._row(lay, "Show / hide fade", self._slider("fade_ms", 0, 1000, 10, "{:.0f} ms"))
        self._row(lay, "Slide distance", self._slider("slide_px", 0, 30, 1, "{:.0f} px"))
        self._row(lay, "Opacity while paused", self._slider("paused_opacity", 0, 100, 5, "{:.0f}%", scale=100))

    def _build_behavior(self, lay: QVBoxLayout) -> None:
        st = self.ctl.settings
        self._row(lay, "Hide in fullscreen", self._toggle(lambda: st.hide_in_fullscreen, self.ctl.set_hide_fullscreen),
                  f"Fullscreen videos and games. Bring lyrics back with {st.hotkey('toggle') or 'the show/hide hotkey'}")
        self._row(lay, "Lock overlay (click-through)", self._toggle(lambda: st.click_through, self.ctl.set_click_through),
                  "Clicks pass through the lyrics; dragging is disabled")
        hide = QDoubleSpinBox()
        hide.setRange(0, 600)
        hide.setDecimals(0)
        hide.setSuffix(" s")
        hide.setSpecialValueText("Never")
        hide.setValue(st.pause_hide_seconds)
        hide.valueChanged.connect(lambda v: (setattr(st, "pause_hide_seconds", float(v)), self._save_timer.start()))
        self._row(lay, "Hide after pausing for", hide)
        self._row(lay, "Position", self._combo(
            [("Attached to the taskbar", "taskbar"), ("Attached to the window title bar", "titlebar"),
             ("Floating", "float")], lambda: st.position_mode, self.ctl.set_position_mode),
            "Or just drag the lyrics: near the taskbar, or next to a window's minimise button, "
            "they snap on like a magnet")
        self._row(lay, "Share anonymous usage stats", self._toggle(lambda: st.telemetry, self.ctl.set_telemetry),
                  "Counts only (minutes used, songs found or not, app version). "
                  "Never song names, lyrics or audio")
        self._row(lay, "Media buttons on the lyrics", self._toggle(lambda: st.media_controls, self.ctl.set_media_controls),
                  "Previous, play / pause and next appear when you hover over the lyrics")
        self._row(lay, "Update automatically", self._toggle(lambda: st.auto_update, self.ctl.set_auto_update),
                  "New versions install in the background; Lirico restarts into them when no song is playing")
        self._row(lay, "Start with Windows", self._toggle(self.ctl.autostart_enabled, self.ctl.set_autostart),
                  "Lirico starts quietly in the tray when you sign in")
        uninstall = QPushButton("Uninstall Lirico…", objectName="danger")
        uninstall.clicked.connect(self._uninstall)
        self._row(lay, "Uninstall", uninstall,
                  "Removes Lirico, its settings and lyrics cache, and the start-with-Windows entry")

    def _build_sync(self, lay: QVBoxLayout) -> None:
        st = self.ctl.settings
        avail = self.ctl.autosync.available
        auto = self._toggle(lambda: st.auto_sync, self.ctl.set_auto_sync)
        auto.setEnabled(avail)
        self._row(lay, "Auto-sync by listening", auto,
                  "Recognises the singing to find where the song really starts (fixes video intros). "
                  "Audio is processed on this PC only and never saved or sent."
                  + ("" if avail else " (speech model not found)"))
        self._row(lay, "Wait for the singer", self._toggle(lambda: st.wait_for_singer, self.ctl.set_wait_for_singer),
                  "Until synced, show the song title instead of lyrics that might be early")
        lead = QDoubleSpinBox()
        lead.setRange(-2.0, 2.0)
        lead.setSingleStep(0.05)
        lead.setDecimals(2)
        lead.setSuffix(" s")
        lead.setValue(st.lead)
        lead.valueChanged.connect(lambda v: (setattr(st, "lead", float(v)), self._save_timer.start()))
        self._row(lay, "Show lines early by", lead,
                  "Applies to every song. A little early gives you time to read; raise it if you "
                  "use Bluetooth headphones (they delay the sound)")
        self.song_label = QLabel(objectName="value")
        reset = QPushButton("Reset")
        reset.clicked.connect(lambda: (self.ctl.nudge_offset(None), self._refresh_song()))
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.song_label)
        h.addWidget(reset)
        self._row(lay, "This song's adjustment", box,
                  f"Fastest fix: press {st.hotkey('tap_sync') or 'the sync hotkey'} the moment the singer starts the "
                  "upcoming line (the small one). Remembered for this song")
        self._refreshers.append(self._refresh_song)
        self._row(lay, "Lyrics in other scripts", self._combo(
            [("English letters", "latin"), ("Original script (हिन्दी, اردو, 한국어…)", "original")],
            lambda: st.script_pref, self.ctl.set_script_pref),
            "Hindi, Urdu, Punjabi, Korean, Russian… shown in English letters, or as written")
        self._refresh_song()

    def _refresh_song(self) -> None:
        if hasattr(self, "song_label"):
            self.song_label.setText(f"{self.ctl.offset:+.2f} s" if self.ctl.has_song() else "no song")

    def _build_hotkeys(self, lay: QVBoxLayout) -> None:
        hint = QLabel("Click a box and press the new combination. Hotkeys work in any app.", objectName="hint")
        lay.addWidget(hint)
        lay.addSpacing(6)
        self.hotkey_errors: dict[str, QLabel] = {}
        for action, (label, _default) in HOTKEY_ACTIONS.items():
            edit = QKeySequenceEdit(QKeySequence(self.ctl.settings.hotkey(action), QKeySequence.PortableText))
            try:
                edit.setMaximumSequenceLength(1)
            except AttributeError:
                pass
            edit.setFixedWidth(170)
            # Save only a *finished* combination. (keySequenceChanged also fires
            # with an empty sequence the moment recording starts; saving that
            # silently disabled the hotkey.)
            edit.editingFinished.connect(lambda a=action, e=edit: self._hotkey_changed(a, e))
            off = QPushButton("✕")
            off.setToolTip("Disable this hotkey")
            off.setFixedWidth(32)
            off.clicked.connect(lambda _=False, a=action, e=edit: self._hotkey_disable(a, e))
            err = QLabel("", objectName="err")
            self.hotkey_errors[action] = err
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(0, 0, 0, 0)
            line = QHBoxLayout()
            line.addWidget(edit)
            line.addWidget(off)
            v.addLayout(line)
            v.addWidget(err, 0, Qt.AlignRight)
            self._row(lay, label, box)
        reset = QPushButton("Restore default hotkeys")
        reset.clicked.connect(self._reset_hotkeys)
        lay.addSpacing(14)
        lay.addWidget(reset, 0, Qt.AlignLeft)
        self.show_hotkey_errors(self.ctl.hotkey_errors)

    def _hotkey_changed(self, action: str, edit: QKeySequenceEdit) -> None:
        seq = edit.keySequence().toString(QKeySequence.PortableText)
        if not seq:
            # Recording was abandoned: keep the current hotkey, show it again.
            edit.setKeySequence(QKeySequence(self.ctl.settings.hotkey(action), QKeySequence.PortableText))
            return
        if self.ctl.settings.hotkey(action) == seq:
            return
        self.ctl.settings.hotkeys[action] = seq
        self.ctl.settings.save()
        self.show_hotkey_errors(self.ctl.register_hotkeys())

    def _hotkey_disable(self, action: str, edit: QKeySequenceEdit) -> None:
        edit.clear()
        self.ctl.settings.hotkeys[action] = ""
        self.ctl.settings.save()
        self.show_hotkey_errors(self.ctl.register_hotkeys())

    def show_hotkey_errors(self, errors: dict) -> None:
        for action, lbl in getattr(self, "hotkey_errors", {}).items():
            lbl.setText(errors.get(action, ""))

    def _reset_hotkeys(self) -> None:
        self.ctl.settings.hotkeys = {}
        self.ctl.settings.save()
        errors = self.ctl.register_hotkeys()
        # rebuild the page so the boxes show the defaults
        idx = self.pages.currentIndex()
        old = self.pages.widget(4)
        body_parent = QWidget()
        lay = QVBoxLayout(body_parent)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.addWidget(QLabel("Hotkeys", objectName="h1"))
        lay.addSpacing(10)
        self._build_hotkeys(lay)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body_parent)
        self.pages.removeWidget(old)
        self.pages.insertWidget(4, scroll)
        self.pages.setCurrentIndex(idx)
        self.show_hotkey_errors(errors)

    def _uninstall(self) -> None:
        box = QMessageBox(QMessageBox.Warning, "Uninstall Lirico",
                          "Remove Lirico from this PC?\n\nThis deletes Lirico.exe, your settings, the lyrics "
                          "cache and the start-with-Windows entry. It can't be undone.", parent=self)
        yes = box.addButton("Uninstall", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self.hide()
            self.ctl.uninstall()

    def _reset_theme(self) -> None:
        self.theme = base_theme(self.ctl.settings.theme_preset)
        self.ctl.apply_theme(self.theme, save=False)
        self.ctl.settings.theme = {}
        self.ctl.settings.save()
        self._refresh_all()

    # ------------------------------------------------------------------ window events
    def showEvent(self, e):
        self.theme = self.ctl.theme
        self._refresh_all()
        self.ctl.overlay.set_preview(True)
        super().showEvent(e)

    def closeEvent(self, e):
        self.ctl.overlay.set_preview(False)
        self.ctl.settings.save()
        super().closeEvent(e)
