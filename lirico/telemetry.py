"""Anonymous usage counts, so we know whether Lirico actually helps people.

Sent: a random install id (made on this PC, linked to nobody), app version,
Windows version, and counters such as minutes with lyrics on screen, songs
played, lyrics found / not found, auto-sync locked / gave up, and which
position (taskbar / title bar / floating) was used.

Never sent: song titles, artists, lyrics, audio, file paths, your name or IP
(the server stores no IPs). Turn it off in Settings → Behavior.
"""

from __future__ import annotations

import logging
import platform
import threading
import uuid
from collections import defaultdict

import requests

from . import __version__

log = logging.getLogger(__name__)

ENDPOINT = "https://getlirico.pages.dev/api/events"


class Telemetry:
    def __init__(self, settings):
        self.settings = settings
        if not settings.install_id:
            settings.install_id = uuid.uuid4().hex
            settings.save()
        self._counts: dict = defaultdict(float)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telemetry)

    def count(self, name: str, n: float = 1) -> None:
        if self.enabled:
            with self._lock:
                self._counts[name] += n

    def start(self) -> None:
        self._send("start", {})

    def flush(self, wait: bool = False) -> None:
        with self._lock:
            data = {k: round(v, 2) for k, v in self._counts.items() if v}
            self._counts.clear()
        if data:
            self._send("usage", data, wait)

    def _send(self, kind: str, data: dict, wait: bool = False) -> None:
        if not self.enabled:
            return
        payload = {"install": self.settings.install_id, "version": __version__,
                   "os": f"Windows {platform.release()} ({platform.version()})"[:40],
                   "kind": kind, "data": data}

        def post():
            try:
                requests.post(ENDPOINT, json=payload, timeout=5,
                              headers={"User-Agent": f"Lirico/{__version__}"})
            except Exception as exc:          # offline etc.: stats are best-effort
                log.debug("telemetry not sent: %s", exc)

        if wait:
            post()
        else:
            threading.Thread(target=post, name="lirico-telemetry", daemon=True).start()
