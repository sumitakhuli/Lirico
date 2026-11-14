"""Reads "now playing" information from Windows media controls (SMTC).

Every app that shows up in the Windows volume flyout (Chrome/Edge tabs playing
YouTube, Spotify, VLC, Groove, ...) publishes a SMTC session. We poll the
sessions on a background thread with its own asyncio loop and hand immutable
`MediaSnapshot`s to a callback. Polling (instead of WinRT events) is simple,
robust across pywinrt versions, and cheap at a few Hz.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

try:  # Only importable on Windows with the winrt packages installed.
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _Status,
    )
except Exception:  # pragma: no cover - lets tests import this on any machine
    _SessionManager = None
    _Status = None


@dataclass(frozen=True)
class MediaSnapshot:
    """One observation of the active media session."""

    source_app: str          # e.g. "chrome.exe", "msedge.exe", "Spotify.exe"
    title: str
    artist: str
    album: str
    playing: bool
    position: float          # seconds, already extrapolated to `captured_at`
    duration: float          # seconds, 0 when unknown
    rate: float              # playback rate, 1.0 normally
    captured_at: float       # time.monotonic() when `position` was valid

    @property
    def track_key(self) -> str:
        """Identity of the track; changes whenever the song changes."""
        return f"{self.source_app}\x1f{self.title}\x1f{self.artist}"

    @property
    def is_browser(self) -> bool:
        app = self.source_app.lower()
        return any(b in app for b in ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc"))


SnapshotCallback = Callable[[Optional[MediaSnapshot]], None]


def _seconds(td) -> float:
    try:
        return max(0.0, td.total_seconds())
    except Exception:
        return 0.0


class MediaWatcher:
    """Background poller. Calls `on_snapshot` from the worker thread with a
    MediaSnapshot, or None when nothing is playing / no session exists."""

    def __init__(self, on_snapshot: SnapshotCallback, interval: float = 0.25):
        self._on_snapshot = on_snapshot
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_key: Optional[tuple] = None  # (app id, title) of the session we follow

    @staticmethod
    def available() -> bool:
        return _SessionManager is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="lirico-smtc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    # -- worker -------------------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._loop())
        except Exception:  # pragma: no cover
            log.exception("SMTC watcher crashed")

    async def _loop(self) -> None:
        manager = None
        while not self._stop.is_set():
            try:
                if manager is None:
                    manager = await _SessionManager.request_async()
                snap = await self._read(manager)
            except Exception as exc:
                log.debug("SMTC read failed: %s", exc)
                manager = None  # re-acquire next round (e.g. after explorer restart)
                snap = None
            try:
                self._on_snapshot(snap)
            except Exception:
                log.exception("snapshot callback failed")
            await asyncio.sleep(self._interval)

    async def _read(self, manager) -> Optional[MediaSnapshot]:
        sessions = list(manager.get_sessions() or [])
        if not sessions:
            return None
        current = manager.get_current_session()

        # IMPORTANT: every Chrome/Edge tab is its own session and they all share
        # one app id ("Chrome"). A session is identified by app id + title,
        # otherwise pausing a song can silently switch to another paused tab
        # (measured: it did, and the lyrics jumped to that tab's position).
        def is_playing(s) -> bool:
            info = s.get_playback_info()
            return info is not None and info.playback_status == _Status.PLAYING

        async def ident(s):
            props = await s.try_get_media_properties_async()
            title = (props.title or "").strip() if props is not None else ""
            return (s.source_app_user_model_id or "", title), props

        playing = [s for s in sessions if is_playing(s)]
        candidates = playing or sessions
        idents = [(s, *(await ident(s))) for s in candidates]
        followed = [x for x in idents if x[1] == self._last_key and x[1][1]]

        chosen = None
        if followed:
            chosen = followed[0]                       # keep following our song
        elif playing:
            # A new song is playing: prefer the one Windows calls "current".
            cur_key = (await ident(current))[0] if current is not None else None
            chosen = next((x for x in idents if x[1] == cur_key), None) or idents[0]
        elif self._last_key is None:
            cur_key = (await ident(current))[0] if current is not None else None
            chosen = next((x for x in idents if x[1] == cur_key), None) or idents[0]
        else:
            return None  # our song's session is gone and nothing plays: show nothing
        session, key, props = chosen
        if props is None or not key[1]:
            return None
        self._last_key = key
        chosen = session
        info = chosen.get_playback_info()
        timeline = chosen.get_timeline_properties()

        playing_now = info is not None and info.playback_status == _Status.PLAYING
        rate = 1.0
        try:
            if info is not None and info.playback_rate is not None:
                rate = float(info.playback_rate) or 1.0
        except Exception:
            rate = 1.0

        position = _seconds(timeline.position) - _seconds(timeline.start_time)
        duration = _seconds(timeline.end_time) - _seconds(timeline.start_time)

        # The reported position was true at `last_updated_time`; extrapolate to now.
        if playing_now:
            try:
                updated = timeline.last_updated_time
                if updated is not None and updated.year > 1601:
                    now = _dt.datetime.now(_dt.timezone.utc)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=_dt.timezone.utc)
                    elapsed = (now - updated).total_seconds()
                    if 0 <= elapsed < 6 * 3600:
                        position += elapsed * rate
            except Exception:
                pass
        if duration > 0:
            position = min(position, duration)

        return MediaSnapshot(
            source_app=chosen.source_app_user_model_id or "",
            title=(props.title or "").strip(),
            artist=(props.artist or "").strip(),
            album=(props.album_title or "").strip(),
            playing=playing_now,
            position=max(0.0, position),
            duration=max(0.0, duration),
            rate=rate,
            captured_at=time.monotonic(),
        )
