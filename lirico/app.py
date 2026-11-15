"""Controller: connects media detection, lyrics lookup, clock, overlay, tray,
hotkeys and the settings window."""

from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPoint, QTimer, Signal
from PySide6.QtWidgets import QApplication

from . import winutil
from .autosync import AutoSyncEngine, guess_language, lyric_words
from .clock import PlaybackClock, TimelineGuard
from .lrc import LyricTimeline
from .lyrics_source import (ERROR, INSTRUMENTAL, NOT_FOUND, PLAIN, SYNCED, LrclibClient, LyricsCache,
                            LyricsResult)
from .media_watch import MediaSnapshot, MediaWatcher
from .overlay import GAP, INTRO, LYRIC, STATUS, Overlay
from .paths import cache_dir, config_dir, speech_model_dir
from .settings import HOTKEY_ACTIONS, Settings
from .theme import PRESETS, Theme, base_theme, make_theme
from .title_clean import CleanTrack, clean_track
from .tray import Tray

log = logging.getLogger(__name__)

MAX_LOOKUP_WAIT = 3.0         # s to wait for the timeline duration to settle before searching
STATUS_SECONDS = 6.0          # how long "not found" style messages stay visible
FLASH_SECONDS = 1.6           # how long "Sync +0.25 s" stays visible
TAP_REACTION = 0.2            # typical human reaction time when tapping to a line
RETRY_DELAYS_MS = (10_000, 30_000, 90_000)
HOLD_LIMIT = 90.0             # s of playback after which we stop waiting for the singer
VERIFY_FOR = 60.0             # s after locking during which auto-sync keeps double-checking
SYNC_CHECK_MS = 6000          # how often to analyse the latest audio

# auto-sync states for the current lyrics
SYNC_OFF, SYNC_LISTENING, SYNC_LOCKED, SYNC_MANUAL = "off", "listening", "locked", "manual"

WAITING, SEARCHING, READY, NO_LYRICS, SKIPPED, FAILED = (
    "waiting", "searching", "ready", "no_lyrics", "skipped", "failed")


class Controller(QObject):
    snapshot_received = Signal(object)
    lyrics_ready = Signal(int, object)
    sync_ready = Signal(int, object)
    update_ready = Signal(str)

    def __init__(self, app: QApplication, settings: Settings):
        super().__init__()
        self.app, self.settings = app, settings
        self.theme: Theme = make_theme(settings.theme_preset, settings.theme)

        self.overlay = Overlay(self.theme, click_through=settings.click_through)
        pos = settings.position
        self.overlay.set_saved_position(QPoint(*pos) if pos else None)
        self.overlay.moved.connect(self._on_moved)
        from .taskbar_dock import TaskbarDock, TitleBarDock
        self.dock = TaskbarDock()
        self.titlebar = TitleBarDock(settings.titlebar_app)
        self._snap_kind: Optional[str] = None
        self._snap_hwnd: Optional[int] = None
        self.overlay.set_snap_provider(self._snap_target)
        self.dock_timer = QTimer(self, interval=400)
        self.dock_timer.timeout.connect(self._update_dock)

        self.clock = PlaybackClock()
        self.guard = TimelineGuard()
        self.client = LrclibClient(LyricsCache(cache_dir()))
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lirico-lyrics")

        self.snap: Optional[MediaSnapshot] = None
        self.track_key: Optional[str] = None
        self.track: Optional[CleanTrack] = None
        self.result: Optional[LyricsResult] = None
        self.timeline: Optional[LyricTimeline] = None
        self.state = WAITING
        self.offset = 0.0
        self.token = 0
        self.retries = 0
        self.track_started = 0.0
        self.lookup_duration = 0.0
        self.message_until = 0.0
        self.flash_text, self.flash_until = "", 0.0
        self.paused_since: Optional[float] = None
        self.fullscreen = False
        self.forced_in_fullscreen = False
        self._last_text: Optional[str] = None
        self.settings_window = None
        self.hotkey_errors: dict = {}
        # auto-sync (listen to the singer)
        self.sync_state = SYNC_OFF
        self.sync_gen = 0
        self.hold = False                 # show the title instead of lyrics until synced
        self.listen_play = 0.0            # seconds of playback while listening
        self.locked_play = 0.0            # seconds of playback since locking
        self.pending_shift: Optional[float] = None
        self.sync_lang = "en"
        self.sync_words: list = []
        self._last_tick = time.monotonic()
        from .telemetry import Telemetry
        self.telemetry = Telemetry(settings)
        self.tele_timer = QTimer(self, interval=30 * 60 * 1000)
        self.tele_timer.timeout.connect(self.telemetry.flush)
        self.autosync = AutoSyncEngine(speech_model_dir(), self.sync_ready.emit)
        self.sync_ready.connect(self._on_sync_result)
        self.sync_timer = QTimer(self, interval=SYNC_CHECK_MS)
        self.sync_timer.timeout.connect(self._sync_step)

        self.retry_timer = QTimer(self, singleShot=True)
        self.retry_timer.timeout.connect(lambda: self._start_lookup(use_cache=False))
        self.tick_timer = QTimer(self, interval=16)      # ~60 fps for a smooth underline
        self.tick_timer.timeout.connect(self._tick)
        self.fs_timer = QTimer(self, interval=600)
        self.fs_timer.timeout.connect(self._check_fullscreen)

        self.snapshot_received.connect(self._on_snapshot)
        self.lyrics_ready.connect(self._on_lyrics)

        self.tray = Tray(self, self.theme)
        self.overlay.context_menu.connect(self.tray.popup)

        from .hotkeys import HotkeyManager
        self.hotkeys = HotkeyManager()
        self.register_hotkeys()

        self.watcher = MediaWatcher(self.snapshot_received.emit)
        if not MediaWatcher.available():
            self.tray.notify("Lirico", "Windows media controls are unavailable.")
        self.watcher.start()
        self.tick_timer.start()
        self.fs_timer.start()
        self.dock_timer.start()
        self._update_dock()
        self._setup_autostart()
        self.telemetry.start()
        self.tele_timer.start()
        if self.settings.telemetry and not self.settings.telemetry_notice:
            self.settings.telemetry_notice = True
            self.settings.save()
            QTimer.singleShot(4000, lambda: self.tray.notify(
                "Lirico is running",
                "Find it in the tray. Lirico shares anonymous usage counts (no song names) "
                "to improve the app. Turn off in Settings → Behavior."))
        if settings.auto_sync:
            self.autosync.start()
        self.sync_timer.start()
        # auto-update: check shortly after start, then twice a day; restart when idle
        from .updater import Updater, cleanup_old
        cleanup_old()
        self.updater = Updater(self.update_ready.emit)
        self.update_ready.connect(self._on_update_ready)
        self.update_timer = QTimer(self, interval=12 * 3600 * 1000)
        self.update_timer.timeout.connect(self.check_for_update)
        self.update_timer.start()
        QTimer.singleShot(90_000, self.check_for_update)
        self.idle_timer = QTimer(self, interval=15_000)
        self.idle_timer.timeout.connect(self._restart_if_idle)
        self._idle_since: Optional[float] = None

    # ------------------------------------------------------------------ media
    def _on_snapshot(self, snap: Optional[MediaSnapshot]) -> None:
        if snap is None:
            self.snap = None
            return
        if snap.track_key != self.track_key:
            self._new_track(snap)
        self.snap = snap
        if self.guard.accept(snap.duration, snap.playing, snap.captured_at):
            mode = self.clock.update(snap.track_key, snap.position, snap.playing, snap.rate,
                                     snap.captured_at, snap.duration)
        else:
            # A timeline that doesn't belong to this song: keep our own position,
            # follow only play/pause.
            mode = self.clock.update(snap.track_key, self.clock.position(snap.captured_at), snap.playing,
                                     snap.rate, snap.captured_at, self.guard.duration)
        if mode in ("seek", "state", "track"):
            log.debug("clock %s -> %.2fs playing=%s", mode, self.clock.position(), snap.playing)
            # Buffered audio no longer maps cleanly onto song time.
            self.autosync.reset()
            self.sync_gen += 1

        # Search once the track plays and its duration has settled (the duration
        # picks the lyrics version timed to this exact recording).
        if self.state == WAITING and snap.playing:
            waited = time.monotonic() - self.track_started
            if self.guard.settled or snap.duration <= 0 and waited > 0.8 or waited > MAX_LOOKUP_WAIT:
                self._start_lookup(use_cache=True)
        elif self.state == READY and self.guard.duration and self.lookup_duration \
                and abs(self.guard.duration - self.lookup_duration) > 3:
            # Duration corrected after the lookup (stale timeline at start): re-pick.
            log.info("duration changed %.1f -> %.1f: re-ranking lyrics", self.lookup_duration, self.guard.duration)
            self._start_lookup(use_cache=True)

    def _new_track(self, snap: MediaSnapshot) -> None:
        self.track_key = snap.track_key
        self.track = clean_track(snap.title, snap.artist, snap.album, snap.source_app)
        self.telemetry.count("songs")
        self.result, self.timeline = None, None
        self.state = WAITING
        self.token += 1
        self.retries = 0
        self.retry_timer.stop()
        self.guard.reset()
        self.track_started = time.monotonic()
        self.offset = self.settings.offset_for(snap.title, snap.artist)
        self.sync_state, self.hold = SYNC_OFF, False
        self.sync_gen += 1
        self.autosync.forget()
        log.info("track: %r / %r [%s] -> %r by %r", snap.title, snap.artist, snap.source_app,
                 self.track.title, self.track.artist)
        self.tray.set_tooltip(f"Lirico · {self.track.title}" + (f" · {self.track.artist}" if self.track.artist else ""))

    def _start_lookup(self, use_cache: bool) -> None:
        snap, track = self.snap, self.track
        if snap is None or track is None or snap.track_key != self.track_key:
            return
        duration = self.guard.duration or snap.duration
        if duration > self.settings.max_media_minutes * 60:
            log.info("skipping lyrics: media is %.0f min long", duration / 60)
            self.state = SKIPPED
            return
        if self.state != READY:
            self.state = SEARCHING
        self.token += 1
        self.lookup_duration = duration
        token, pref = self.token, self.settings.script_pref
        pinned = self.settings.pin_for(snap.title, snap.artist)

        def work():
            try:
                res = self.client.find(track, duration, pref, use_cache=use_cache, pinned_id=pinned)
            except Exception as exc:  # never let a worker exception vanish silently
                log.exception("lookup failed")
                res = LyricsResult(ERROR, message=str(exc))
            self.lyrics_ready.emit(token, res)

        self.pool.submit(work)

    def _on_lyrics(self, token: int, res: LyricsResult) -> None:
        if token != self.token:
            return  # stale result for a previous song
        self.result = res
        log.info("lyrics: %s %r by %r Δ=%s romanized=%s (cache=%s)", res.status, res.track, res.artist,
                 None if res.duration_delta is None else round(res.duration_delta, 1), res.romanized, res.from_cache)
        if res.status != ERROR and not getattr(res, "_counted", False):
            self.telemetry.count("found" if res.status == SYNCED else "not_found")
        if res.status == SYNCED:
            self.timeline = LyricTimeline(res.lines)
            self.state = READY
            self._begin_autosync(res)
        elif res.status == ERROR and self.retries < len(RETRY_DELAYS_MS):
            self.state = FAILED
            self.retry_timer.start(RETRY_DELAYS_MS[self.retries])
            self.retries += 1
            self.message_until = time.monotonic() + STATUS_SECONDS
        else:
            self.state = NO_LYRICS if res.status != ERROR else FAILED
            self.message_until = time.monotonic() + STATUS_SECONDS

    # ------------------------------------------------------------------ auto-sync
    def _begin_autosync(self, res: LyricsResult) -> None:
        """Start listening for the singer, unless this song's timing is already known."""
        self.sync_gen += 1
        self.pending_shift = None
        self.listen_play = self.locked_play = 0.0
        known = self.snap is not None and self.settings.has_offset(self.snap.title, self.snap.artist)
        if not (self.settings.auto_sync and self.autosync.available) or known:
            self.sync_state = SYNC_MANUAL if known else SYNC_OFF
            self.hold = False
            return
        self.sync_state = SYNC_LISTENING
        self.autosync.forget()
        self.sync_lang = guess_language(res.lines)
        self.sync_words = lyric_words(res.lines)
        d = res.duration_delta
        # English lyrics timed to this exact recording are almost always right:
        # show them at once. Otherwise wait for the singer before showing lines.
        exact = d is not None and abs(d) <= 1.6 and self.sync_lang == "en"
        self.hold = self.settings.wait_for_singer and not exact
        log.info("autosync: listening (lang=%s, hold=%s)", self.sync_lang, self.hold)

    def _sync_step(self) -> None:
        snap = self.snap
        if snap is None or not snap.playing or self.state != READY:
            return
        if self.sync_state == SYNC_LISTENING or (self.sync_state == SYNC_LOCKED and self.locked_play < VERIFY_FOR):
            self.autosync.request(self.sync_gen, self.clock.position, self.sync_words, self.sync_lang,
                                  prior=self.offset if self.sync_state == SYNC_LOCKED else None)

    def _on_sync_result(self, gen: int, est) -> None:
        if gen != self.sync_gen or self.state != READY or est is None:
            return
        log.info("autosync: %s confident=%s", est, est.confident)
        if not est.confident:
            return
        if self.sync_state == SYNC_LISTENING:
            self._apply_auto_offset(est.offset)
            self.sync_state, self.hold = SYNC_LOCKED, False
            self.locked_play = 0.0
            self.telemetry.count("sync_locked")
            self._flash("Synced to the singer", 1.8)
        elif self.sync_state == SYNC_LOCKED and abs(est.offset - self.offset) > 1.2:
            # A different answer must show up twice before we change our mind.
            if self.pending_shift is not None and abs(self.pending_shift - est.offset) <= 1.0:
                self._apply_auto_offset(est.offset)
                self.pending_shift = None
            else:
                self.pending_shift = est.offset

    def _apply_auto_offset(self, value: float) -> None:
        self.offset = round(value, 2)
        if self.snap is not None:
            self.settings.set_offset(self.snap.title, self.snap.artist, self.offset)
        log.info("autosync: offset %+.2f s", self.offset)

    def listen_again(self) -> None:
        """Forget this song's timing and re-detect it by listening."""
        if not self.has_song() or self.result is None or self.state != READY:
            return
        self.settings.set_offset(self.snap.title, self.snap.artist, None)
        self.offset = 0.0
        self.autosync.reset()
        self._begin_autosync(self.result)

    def set_auto_sync(self, enabled: bool) -> None:
        self.settings.auto_sync = bool(enabled)
        self.settings.save()
        if enabled:
            self.autosync.start()
        else:
            self.autosync.recorder.stop()
            self.sync_state, self.hold = SYNC_OFF, False

    def check_for_update(self) -> None:
        if self.settings.auto_update:
            self.updater.check_async()

    def _on_update_ready(self, version: str) -> None:
        self.tray.notify("Lirico updated", f"Version {version} is installed. Lirico restarts into it "
                                           "the next time nothing is playing.")
        self._idle_since = None
        self.idle_timer.start()

    def _restart_if_idle(self) -> None:
        playing = self.snap is not None and self.snap.playing
        if playing or (self.settings_window is not None and self.settings_window.isVisible()):
            self._idle_since = None
            return
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= 60 and self.updater.restart():
            log.info("restarting into version %s", self.updater.ready_version)
            self.idle_timer.stop()
            self.quit()

    def set_auto_update(self, enabled: bool) -> None:
        self.settings.auto_update = bool(enabled)
        self.settings.save()
        if enabled:
            self.check_for_update()

    def set_telemetry(self, enabled: bool) -> None:
        if not enabled:
            self.telemetry.flush(wait=False)   # nothing more after this
        self.settings.telemetry = bool(enabled)
        self.settings.save()

    def open_support(self) -> None:
        from . import SUPPORT_URL
        if SUPPORT_URL:
            import webbrowser
            webbrowser.open(SUPPORT_URL)

    def set_wait_for_singer(self, enabled: bool) -> None:
        self.settings.wait_for_singer = bool(enabled)
        self.settings.save()
        if not enabled:
            self.hold = False

    # ------------------------------------------------------------------ render loop
    def lyric_position(self, now: Optional[float] = None) -> float:
        """Song time used to pick the lyric line: playback + lead + per-song offset."""
        return self.clock.position(now) + self.settings.lead + self.offset

    def overlay_shown(self) -> bool:
        if not self.settings.overlay_visible:
            return False
        return not (self.fullscreen and self.settings.hide_in_fullscreen and not self.forced_in_fullscreen)

    def _tick(self) -> None:
        ov, snap, now = self.overlay, self.snap, time.monotonic()
        dt, self._last_tick = now - self._last_tick, now
        if snap is not None and snap.playing and self.state == READY:
            if self.sync_state == SYNC_LISTENING:
                self.listen_play += dt
                if self.hold and self.listen_play > HOLD_LIMIT:
                    self.hold = False          # couldn't hear it: show the lyrics as they are
                    self.telemetry.count("sync_gave_up")
                    log.info("autosync: no lock after %.0f s, showing lyrics unshifted", HOLD_LIMIT)
            elif self.sync_state == SYNC_LOCKED:
                self.locked_play += dt
            if self.overlay_shown() and 0 < dt < 1:
                self.telemetry.count("minutes", dt / 60)
                self.telemetry.count("mode_" + self.settings.position_mode, dt / 60)
        if snap is None or not self.overlay_shown():
            ov.set_active(False)
            return
        if snap.playing:
            self.paused_since = None
        elif self.paused_since is None:
            self.paused_since = now
        limit = self.settings.pause_hide_seconds
        paused_long = limit > 0 and self.paused_since is not None and now - self.paused_since > limit
        ov.set_paused(not snap.playing)

        content = self._content(now)
        if now < self.flash_until:
            under = content[0] if content and content[1] == LYRIC else self._title_line()
            content = (self.flash_text, STATUS, under)
        if content is None or paused_long:
            ov.set_active(False)
            return
        text, kind, nxt, *rest = content
        if text != self._last_text:
            self._last_text = text
            log.debug("show @%.2fs [%s] %s", self.lyric_position(now), kind, text)
        ov.set_content(text, kind, nxt, rest[0] if rest else 0.0)
        ov.set_active(True)

    def _title_line(self) -> str:
        if self.result and self.result.track:
            return f"{self.result.track} — {self.result.artist}" if self.result.artist else self.result.track
        if self.track:
            return f"{self.track.title} — {self.track.artist}" if self.track.artist else self.track.title
        return ""

    def _content(self, now: float):
        """What to show right now, or None to hide: (text, kind, next_text[, sung_chars])."""
        if self.state in (WAITING, SKIPPED) or self.track is None:
            return None
        if self.state == SEARCHING:
            return ("Finding lyrics…", STATUS, self._title_line())
        if self.state in (NO_LYRICS, FAILED):
            if now > self.message_until:
                return None
            res = self.result
            msg = {NOT_FOUND: "No synced lyrics found", PLAIN: "Only unsynced lyrics available",
                   INSTRUMENTAL: f"{self.theme.gap_glyph}  Instrumental"}.get(res.status if res else "", "")
            if self.state == FAILED:
                msg = "Lyrics service unreachable" + (" · will retry" if self.retry_timer.isActive() else "")
            return (msg or "No synced lyrics found", STATUS, self._title_line())

        tl = self.timeline
        if tl is None or not len(tl):
            return None
        if self.hold:
            # Waiting for the singer: lyrics would otherwise start too early.
            return (f"{self.theme.gap_glyph}  {self._title_line()}", INTRO, "")
        pos = self.lyric_position(now)
        i = tl.index_at(pos)
        if i < 0:
            return (f"{self.theme.gap_glyph}  {self._title_line()}", INTRO, tl.next_text(-1))
        line = tl.line(i)
        if not line.text:
            return (self.theme.gap_glyph, GAP, tl.next_text(i))
        return (line.text, LYRIC, tl.next_text(i), tl.sung_chars(i, pos))

    def _flash(self, text: str, seconds: float = FLASH_SECONDS) -> None:
        self.flash_text, self.flash_until = text, time.monotonic() + seconds

    # ------------------------------------------------------------------ fullscreen
    def _check_fullscreen(self) -> None:
        fs = winutil.foreground_is_fullscreen((int(self.overlay.winId()),))
        if fs != self.fullscreen:
            self.fullscreen = fs
            if not fs:
                self.forced_in_fullscreen = False
            log.debug("fullscreen: %s", fs)

    # ------------------------------------------------------------------ actions (tray, hotkeys, settings)
    def has_song(self) -> bool:
        return self.track is not None and self.snap is not None

    def describe(self) -> tuple[str, str]:
        if not self.has_song():
            return ("Nothing playing", "")
        now = f"♪ {self._title_line()}"
        status = {WAITING: "Waiting for playback", SEARCHING: "Searching LRCLIB…",
                  NO_LYRICS: "No synced lyrics for this song", SKIPPED: "Long media: lyrics skipped",
                  FAILED: "Lyrics service unreachable"}.get(self.state, "")
        if self.state == READY and self.result is not None:
            status = {SYNC_LISTENING: "Listening for the singer…", SYNC_LOCKED: "Synced to the singer",
                      SYNC_MANUAL: "Synced (your adjustment)"}.get(self.sync_state, "Synced")
        return (now[:70], status)

    def toggle_overlay(self) -> None:
        if self.fullscreen and self.settings.hide_in_fullscreen and self.settings.overlay_visible:
            # In fullscreen: the hotkey brings lyrics to the front (or hides them again).
            self.forced_in_fullscreen = not self.forced_in_fullscreen
            return
        self.settings.overlay_visible = not self.settings.overlay_visible
        self.settings.save()

    def _on_moved(self, pos: QPoint) -> None:
        """Drag finished: attach to the magnet it was pulled onto, else float there."""
        kind, self._snap_kind = self._snap_kind, None
        if kind == "titlebar" and self._snap_hwnd and self.titlebar.attach(self._snap_hwnd):
            self._remember_titlebar_app()
            changed = self.settings.position_mode != "titlebar"
            self.settings.position_mode = "titlebar"
            self.settings.save()
            self._update_dock()
            if changed:
                self._flash("Attached to this window · in other apps they wait in the taskbar", 2.6)
            return
        if kind == "taskbar":
            if kind != self.settings.position_mode:
                self.set_position_mode(kind)
                self._flash("Attached to the taskbar", 1.6)
            else:
                self._update_dock()          # snap back into place
            return
        if self.settings.position_mode != "float":
            self.settings.position_mode = "float"
            self.overlay.set_dock(None)
            self._flash("Floating · drag it to the taskbar or a title bar to attach", 2.6)
        self.overlay.set_saved_position(pos)
        self.settings.position = [pos.x(), pos.y()]
        self.settings.save()

    # ------------------------------------------------------------------ magnets
    SNAP_TASKBAR = 70      # px above the taskbar where it starts pulling
    SNAP_TITLEBAR = 110    # px around the title-bar slot

    def _snap_target(self, center: QPoint):
        """While dragging: the rect the lyrics should pull onto, or None."""
        self._snap_kind, self._snap_hwnd = None, None
        try:
            from .taskbar_dock import taskbar_state
            st = taskbar_state()
            if st is not None and st.bottom and center.y() >= st.rect.top() - self.SNAP_TASKBAR:
                gap = self.dock.target(self.theme.box_width)
                if gap is not None:
                    self._snap_kind = "taskbar"
                    return gap
            hwnd = self.titlebar.window_at(center.x(), center.y())
            slot = self.titlebar.slot(hwnd, self.theme.box_width) if hwnd else None
            if slot is not None:
                dx, dy = center.x() - slot.center().x(), center.y() - slot.center().y()
                if abs(dy) <= self.SNAP_TITLEBAR * 0.6 and abs(dx) <= self.SNAP_TITLEBAR + slot.width() / 2:
                    self._snap_kind, self._snap_hwnd = "titlebar", hwnd
                    return slot
        except Exception:
            log.exception("snap target failed")
        return None

    # ------------------------------------------------------------------ taskbar dock
    def _update_dock(self) -> None:
        mode = self.settings.position_mode
        try:
            self.titlebar.poll()             # remember the app window you use, for "attach to title bar"
        except Exception:
            log.exception("foreground poll failed")
        if mode not in ("taskbar", "titlebar") or self.overlay._drag_offset is not None:
            if mode == "float" and self.overlay.docked:
                self.overlay.set_dock(None)
            return
        rect, on_taskbar = None, True
        try:
            if mode == "titlebar":
                # In the attached window's title bar while you use it; in the
                # taskbar meanwhile (back on top as soon as you return).
                rect = self.titlebar.target(self.theme.box_width)
                on_taskbar = rect is None
                if self.titlebar.app and self.titlebar.app != self.settings.titlebar_app:
                    self._remember_titlebar_app()
            if on_taskbar:
                rect = self.dock.target(self.theme.box_width)
        except Exception:
            log.exception("dock failed")
            rect = None
        self.overlay.set_light_surface(on_taskbar and self.dock.light)
        self.overlay.set_dock(rect)          # None: no room -> float just above the taskbar
        if rect is not None:
            self.overlay.raise_above_taskbar()  # the taskbar climbs above us when clicked

    def _remember_titlebar_app(self) -> None:
        self.settings.titlebar_app = self.titlebar.app
        self.settings.save()

    def set_position_mode(self, mode: str) -> None:
        mode = mode if mode in ("taskbar", "titlebar", "float") else "float"
        if mode == "titlebar":
            if self.titlebar.attach():         # the app window you used last
                self._remember_titlebar_app()
                self._flash("Attached to the title bar of the window you used last", 2.4)
            elif self.titlebar.anchor is None:
                self._flash("Click the window you want, or drag the lyrics onto its title bar", 2.8)
        self.settings.position_mode = mode
        self.settings.save()
        if mode == "float":
            self.overlay.set_dock(None)
        self._update_dock()

    def reset_position(self) -> None:
        self.settings.position_mode = "taskbar"
        self.settings.position = None
        self.settings.save()
        self.overlay.set_saved_position(None)
        self._update_dock()

    def set_offset(self, value: float) -> None:
        if not self.has_song():
            return
        if self.sync_state != SYNC_OFF:
            self.sync_state, self.hold = SYNC_MANUAL, False   # your adjustment wins
        self.offset = round(value, 2)
        self.settings.set_offset(self.snap.title, self.snap.artist, self.offset)
        self._flash(f"Sync {self.offset:+.2f} s")

    def nudge_offset(self, delta: Optional[float]) -> None:
        if not self.has_song():
            return
        if delta is None:          # reset: forget it and let auto-sync listen again
            if self.settings.auto_sync:
                self.listen_again()
            else:
                self.set_offset(0.0)
            self._flash("Sync reset")
            return
        self.set_offset(self.offset + delta)

    def tap_sync(self) -> None:
        """'The upcoming line starts now': align it to this moment."""
        tl = self.timeline
        if tl is None or not self.has_song():
            return
        now_pos = self.lyric_position()
        i = tl.index_at(now_pos)
        j = next((k for k in range(i + 1, len(tl)) if tl.lines[k].text), None)
        if j is None:
            return
        # pressed ~TAP_REACTION after hearing it: the line began slightly earlier
        target = tl.lines[j].start + TAP_REACTION
        self.set_offset(self.offset + (target - now_pos))

    def version_delta(self) -> Optional[float]:
        return self.result.duration_delta if self.result is not None else None

    def alternatives(self) -> list:
        return list(self.result.alternatives) if self.result is not None else []

    def current_lyrics_id(self):
        return self.result.lrclib_id if self.result is not None else None

    def media_duration(self) -> float:
        return self.guard.duration or (self.snap.duration if self.snap else 0.0)

    def choose_version(self, lrclib_id) -> None:
        if not self.has_song() or lrclib_id is None:
            return
        self.settings.set_pin(self.snap.title, self.snap.artist, lrclib_id)
        self.settings.set_offset(self.snap.title, self.snap.artist, None)  # new version: new timing
        self.offset = 0.0
        self._start_lookup(use_cache=True)

    def next_version(self) -> None:
        alts = [a for a in self.alternatives() if a.get("synced")]
        if not alts or not self.has_song():
            return
        ids = [a["id"] for a in alts]
        cur = self.current_lyrics_id()
        nxt = alts[(ids.index(cur) + 1) % len(alts)] if cur in ids else alts[0]
        self.choose_version(nxt["id"])
        media = self.media_duration()
        d = (nxt.get("duration") or 0) - media if media and nxt.get("duration") else None
        self._flash(f"Version {ids.index(nxt['id']) + 1}/{len(ids)} · {nxt.get('artist') or ''}"
                    + (f" · {d:+.0f} s" if d is not None else ""), 2.5)

    def search_again(self) -> None:
        if not self.has_song():
            return
        self.settings.set_pin(self.snap.title, self.snap.artist, None)
        self.retries = 0
        self.retry_timer.stop()
        self.state = SEARCHING
        self._start_lookup(use_cache=False)

    def set_script_pref(self, value: str) -> None:
        if value != self.settings.script_pref:
            self.settings.script_pref = value
            self.settings.save()
            if self.has_song():
                self._start_lookup(use_cache=True)

    def apply_theme(self, theme: Theme, save: bool = True) -> None:
        self.theme = theme
        self.overlay.apply_theme(theme)
        if save:
            self.settings.theme = theme.diff(base_theme(self.settings.theme_preset))
            self.settings.save()

    def set_preset(self, name: str) -> None:
        """Switch to a ready-made look (drops the tweaks made on the old one)."""
        if name not in PRESETS:
            return
        self.settings.theme_preset, self.settings.theme = name, {}
        self.settings.save()
        self.theme = base_theme(name)
        self.overlay.apply_theme(self.theme)

    def set_click_through(self, enabled: bool) -> None:
        self.settings.click_through = bool(enabled)
        self.settings.save()
        self.overlay.set_click_through(bool(enabled))

    def set_hide_fullscreen(self, enabled: bool) -> None:
        self.settings.hide_in_fullscreen = bool(enabled)
        self.settings.save()

    def register_hotkeys(self) -> dict:
        actions = {
            "toggle": self.toggle_overlay, "tap_sync": self.tap_sync,
            "earlier": lambda: self.nudge_offset(0.25), "later": lambda: self.nudge_offset(-0.25),
            "reset_offset": lambda: self.nudge_offset(None), "next_version": self.next_version,
            "settings": self.open_settings,
        }
        bindings = {a: (self.settings.hotkey(a), cb) for a, cb in actions.items() if a in HOTKEY_ACTIONS}
        self.hotkey_errors = self.hotkeys.register_all(bindings)
        return self.hotkey_errors

    def open_settings(self) -> None:
        if self.settings_window is None:
            from .settings_window import SettingsWindow
            self.settings_window = SettingsWindow(self)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    @staticmethod
    def _launch_command() -> Optional[str]:
        if getattr(sys, "frozen", False):          # packaged Lirico.exe
            return f'"{sys.executable}"'
        root = Path(__file__).resolve().parents[1]
        launcher = root / "Lirico.pyw"
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not launcher.exists() or not pythonw.exists():
            return None
        return f'"{pythonw}" "{launcher}"'

    def autostart_enabled(self) -> bool:
        return winutil.autostart_enabled()

    def _setup_autostart(self) -> None:
        """Packaged app: start with Windows by default (first run), and if it's
        enabled keep the registered path pointing at this exe (it may have been
        moved). Turn it off any time in Settings → Behavior."""
        if not getattr(sys, "frozen", False):
            return
        try:
            if not self.settings.autostart_setup:
                self.set_autostart(True)
                self.settings.autostart_setup = True
                self.settings.save()
            elif winutil.autostart_enabled():
                self.set_autostart(True)       # refresh the path
        except OSError as exc:
            log.warning("could not set up start with Windows: %s", exc)

    def set_autostart(self, enabled: bool) -> None:
        cmd = self._launch_command()
        if cmd:
            winutil.set_autostart(bool(enabled), cmd)

    def open_data_folder(self) -> None:
        try:
            os.startfile(str(config_dir()))  # type: ignore[attr-defined]
        except OSError:
            pass

    def quit(self) -> None:
        self.watcher.stop()
        self.tick_timer.stop()
        self.autosync.stop()
        self.telemetry.flush(wait=True)
        self.hotkeys.unregister_all()
        self.tray.icon.hide()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.app.quit()
