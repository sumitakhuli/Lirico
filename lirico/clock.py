"""Smooth local playback clock driven by sparse SMTC position reports.

SMTC tells us "position P was true at time T". Between reports we run a local
monotonic clock. We re-anchor immediately on play/pause, rate change, track
change or a jump (seek); small drift is corrected gently so lyrics never
jitter backwards.
"""

from __future__ import annotations

import time


class TimelineGuard:
    """Rejects timelines that don't belong to the song being followed.

    Measured on real YouTube in Chrome: around pause/resume Chrome briefly
    publishes a *different* player's timeline under the same title (e.g. a
    43 s preview element at position ~0), and right after a video change it can
    still report the previous video's timeline. Both show up as a sudden change
    of duration, so we lock onto the song's duration and only accept a new one
    once it has been stable while playing for `adopt_after` seconds."""

    def __init__(self, tolerance: float = 1.5, adopt_after: float = 2.5):
        self.tolerance, self.adopt_after = tolerance, adopt_after
        self.reset()

    def reset(self) -> None:
        self.duration = 0.0          # locked duration (0 = none yet)
        self._alt = 0.0
        self._alt_since = 0.0
        self._first_seen = 0.0

    @property
    def settled(self) -> bool:
        """True once the duration has been stable long enough to trust for lookups."""
        return self.duration > 0 and time.monotonic() - self._first_seen >= 1.2

    def accept(self, duration: float, playing: bool, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if duration <= 0:
            return True                      # player exposes no duration: nothing to compare
        if self.duration <= 0:
            self.duration, self._first_seen = duration, now
            return True
        if abs(duration - self.duration) <= self.tolerance:
            self._alt = 0.0
            return True
        if now - self._first_seen < 1.5:
            # The lock is brand new, probably the previous video's stale timeline
            # right after a track change: switch straight away.
            self.duration, self._first_seen, self._alt = duration, now, 0.0
            return True
        if abs(duration - self._alt) > self.tolerance or not playing:
            # (re)start the stability timer; only continuous *playing* time counts,
            # so a foreign timeline seen during a pause is never adopted on resume.
            self._alt, self._alt_since = duration, now
            return False
        if playing and now - self._alt_since >= self.adopt_after:
            self.duration, self._first_seen, self._alt = duration, self._alt_since, 0.0
            return True
        return False


class PlaybackClock:
    def __init__(self, seek_threshold: float = 0.6, nudge: float = 0.25):
        self.seek_threshold = seek_threshold  # seconds of error treated as a seek
        self.nudge = nudge                    # fraction of small error corrected per report
        self.reset()

    def reset(self) -> None:
        self._anchor_pos = 0.0
        self._anchor_time = time.monotonic()
        self._playing = False
        self._rate = 1.0
        self._key: str | None = None
        self._has_timeline = True

    @property
    def playing(self) -> bool:
        return self._playing

    def position(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if not self._playing:
            return self._anchor_pos
        return self._anchor_pos + (now - self._anchor_time) * self._rate

    def _anchor(self, pos: float, at: float) -> None:
        self._anchor_pos, self._anchor_time = pos, at

    def update(self, key: str, position: float, playing: bool, rate: float,
               captured_at: float, duration: float = 0.0) -> str:
        """Feed one observation. Returns what happened: 'track', 'state',
        'seek', 'nudge' or 'none' (useful for logging/tests)."""
        rate = rate or 1.0
        # Some players expose no timeline at all (always 0/0): free-run locally.
        has_timeline = not (duration <= 0 and position <= 0)

        if key != self._key:
            self._key, self._playing, self._rate = key, playing, rate
            self._has_timeline = has_timeline
            self._anchor(position, captured_at)
            return "track"

        if playing != self._playing or abs(rate - self._rate) > 1e-3:
            current = self.position(captured_at)
            self._playing, self._rate = playing, rate
            self._anchor(position if has_timeline else current, captured_at)
            return "state"

        if not has_timeline:
            return "none"

        if not playing:
            if abs(position - self._anchor_pos) > 0.05:  # seek while paused
                self._anchor(position, captured_at)
                return "seek"
            return "none"

        err = position - self.position(captured_at)
        if abs(err) >= self.seek_threshold:
            self._anchor(position, captured_at)
            return "seek"
        if abs(err) > 0.04:
            # Correct gently; cap backward steps so a line never flickers back.
            corr = max(err * self.nudge, -0.05)
            self._anchor(self.position(captured_at) + corr, captured_at)
            return "nudge"
        return "none"
