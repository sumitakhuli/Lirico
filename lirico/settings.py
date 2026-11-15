"""User settings persisted to %USERPROFILE%\\.lirico\\settings.json."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .paths import config_dir

log = logging.getLogger(__name__)

# action id -> (label, default shortcut). Edited in Settings → Hotkeys.
HOTKEY_ACTIONS = {
    "toggle": ("Show / hide lyrics", "Ctrl+Alt+Space"),
    "tap_sync": ("Sync: next line starts now", "Ctrl+Alt+Up"),
    "earlier": ("Lyrics earlier (+0.25 s)", "Ctrl+Alt+Right"),
    "later": ("Lyrics later (−0.25 s)", "Ctrl+Alt+Left"),
    "reset_offset": ("Reset this song's sync", "Ctrl+Alt+Down"),
    "next_version": ("Try another lyrics version", "Ctrl+Alt+N"),
    "settings": ("Open settings", ""),
}


SETTINGS_VERSION = 2


@dataclass
class Settings:
    version: int = SETTINGS_VERSION
    overlay_visible: bool = True
    position: Optional[list] = None     # [x, y] top-left when floating; None = just above the taskbar
    position_mode: str = "taskbar"      # "taskbar" = in the taskbar's free space, "titlebar", "float" = anywhere
    titlebar_app: str = ""              # exe of the window the lyrics are attached to (title-bar mode)
    theme_preset: str = "classic"       # theme.PRESETS key; `theme` holds the user's tweaks on top
    autostart_setup: bool = False       # first-run "start with Windows" has been applied
    auto_update: bool = True            # download new versions and restart into them when idle
    telemetry: bool = True              # anonymous usage counts (Settings -> Behavior to turn off)
    telemetry_notice: bool = False      # the one-time "we share anonymous counts" note was shown
    install_id: str = ""                # random id for the counts; linked to nobody
    script_pref: str = "latin"          # "latin" (English letters, default) | "devanagari"
    lead: float = 0.15                  # show every line this many seconds early (reading time)
    offsets: dict = field(default_factory=dict)   # per-song extra offset, seconds
    pins: dict = field(default_factory=dict)      # per-song chosen LRCLIB id
    theme: dict = field(default_factory=dict)     # overrides of the preset (theme.make_theme)
    hotkeys: dict = field(default_factory=dict)   # action -> shortcut ("" = disabled)
    auto_sync: bool = True              # listen to the music to find where the singing starts
    wait_for_singer: bool = True        # until synced, show the title instead of (early) lyrics
    hide_in_fullscreen: bool = True
    click_through: bool = False         # lock: clicks pass through, no dragging
    pause_hide_seconds: float = 20.0    # fade out after being paused this long (0 = never)
    max_media_minutes: float = 15.0     # skip lyrics lookups for longer media (movies, podcasts)

    @staticmethod
    def path() -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if data.get("script_pref") == "auto":
            known["script_pref"] = "latin"
        if int(data.get("version", 1)) < 2:
            # v1 settings window could save hotkeys as "" by accident: restore defaults.
            known["hotkeys"] = {k: v for k, v in (data.get("hotkeys") or {}).items() if v}
        known["version"] = SETTINGS_VERSION
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def save(self) -> None:
        p = self.path()
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:
            log.warning("could not save settings: %s", exc)

    def hotkey(self, action: str) -> str:
        if action in self.hotkeys:
            return self.hotkeys[action] or ""
        return HOTKEY_ACTIONS[action][1]

    # -- per-song values -----------------------------------------------------
    @staticmethod
    def song_id(title: str, artist: str) -> str:
        return hashlib.sha1(f"{title}\x1f{artist}".encode("utf-8")).hexdigest()[:16]

    def has_offset(self, title: str, artist: str) -> bool:
        return self.song_id(title, artist) in self.offsets

    def offset_for(self, title: str, artist: str) -> float:
        return float(self.offsets.get(self.song_id(title, artist), 0.0))

    def set_offset(self, title: str, artist: str, value) -> None:
        key = self.song_id(title, artist)
        if value is None:
            self.offsets.pop(key, None)
        else:
            self.offsets[key] = round(value, 2)   # 0.0 is kept: "checked, no shift needed"
        self.save()

    def pin_for(self, title: str, artist: str) -> Optional[int]:
        v = self.pins.get(self.song_id(title, artist))
        return int(v) if v is not None else None

    def set_pin(self, title: str, artist: str, lrclib_id: Optional[int]) -> None:
        key = self.song_id(title, artist)
        if lrclib_id is None:
            self.pins.pop(key, None)
        else:
            self.pins[key] = int(lrclib_id)
        self.save()
