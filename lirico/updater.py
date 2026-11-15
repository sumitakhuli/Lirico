"""Automatic updates for the single-file Lirico.exe.

Twice a day Lirico asks GitHub for the latest release. If it's newer, the new
Lirico.exe is downloaded in the background and checked (size and SHA-256 from
GitHub). Then it swaps itself in place: Windows lets a running .exe be
*renamed* (not deleted), so the old file becomes Lirico.old.exe and the new one
takes its name. Lirico restarts into the new version the next time nothing is
playing, so a song is never interrupted. The old file is removed on the next
start. Only the packaged exe updates itself; running from source never does.
Turn it off in Settings → Behavior.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import requests

from . import __version__

log = logging.getLogger(__name__)

LATEST_URL = "https://api.github.com/repos/sumitakhuli/Lirico/releases/latest"
ASSET_NAME = "Lirico.exe"


def parse_version(v: str) -> tuple:
    v = (v or "").strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))[:3]


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def exe_path() -> Optional[Path]:
    """The running Lirico.exe, or None when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def cleanup_old() -> None:
    exe = exe_path()
    if exe is None:
        return
    for p in (exe.with_name(exe.stem + ".old.exe"), exe.with_name(exe.stem + ".download")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


class Updater:
    def __init__(self, on_ready: Callable[[str], None]):
        self.on_ready = on_ready          # called (from a worker thread) with the new version
        self.ready_version: Optional[str] = None
        self._busy = threading.Lock()

    @property
    def supported(self) -> bool:
        return exe_path() is not None

    def check_async(self) -> None:
        if not self.supported or self.ready_version or self._busy.locked():
            return
        threading.Thread(target=self._check, name="lirico-update", daemon=True).start()

    def _check(self) -> None:
        with self._busy:
            try:
                self._check_inner()
            except Exception as exc:          # offline, rate limited, disk full...
                log.info("update check failed: %s", exc)

    def _check_inner(self) -> None:
        r = requests.get(LATEST_URL, timeout=10, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": f"Lirico/{__version__}"})
        r.raise_for_status()
        rel = r.json()
        tag = rel.get("tag_name", "")
        if rel.get("draft") or rel.get("prerelease") or not is_newer(tag):
            log.debug("up to date (%s, latest %s)", __version__, tag)
            return
        asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET_NAME), None)
        if asset is None:
            return
        url, size = asset["browser_download_url"], int(asset.get("size") or 0)
        digest = (asset.get("digest") or "").lower()
        exe = exe_path()
        tmp = exe.with_name(exe.stem + ".download")
        log.info("downloading update %s (%d bytes)", tag, size)
        h = hashlib.sha256()
        got = 0
        with requests.get(url, stream=True, timeout=30, headers={"User-Agent": f"Lirico/{__version__}"}) as d:
            d.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in d.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
        if size and got != size:
            raise IOError(f"incomplete download ({got} of {size} bytes)")
        if digest.startswith("sha256:") and h.hexdigest() != digest.split(":", 1)[1]:
            raise IOError("checksum mismatch")
        if got < 5_000_000:
            raise IOError("download too small to be Lirico")
        # Swap: running exe -> .old.exe, new file -> Lirico.exe
        old = exe.with_name(exe.stem + ".old.exe")
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
        os.replace(exe, old)
        try:
            os.replace(tmp, exe)
        except OSError:
            os.replace(old, exe)               # put the working version back
            raise
        self.ready_version = tag.lstrip("vV")
        log.info("update %s installed; restarting when idle", self.ready_version)
        self.on_ready(self.ready_version)

    def restart(self) -> bool:
        """Start the new exe (it waits for us to exit) and tell the caller to quit."""
        exe = exe_path()
        if exe is None or not self.ready_version:
            return False
        import subprocess
        env = dict(os.environ)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # the new exe must unpack itself, not reuse our files
        try:
            subprocess.Popen([str(exe), "--after-update"], close_fds=True, env=env,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return True
        except OSError as exc:
            log.warning("could not start the updated Lirico: %s", exc)
            return False
