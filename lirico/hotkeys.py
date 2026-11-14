"""System-wide hotkeys (work while any app is focused) via Win32 RegisterHotKey.

Shortcuts are stored as Qt portable strings ("Ctrl+Alt+Space"). Registration
fails if another app already owns the combination; `register_all` reports
which ones so the Settings window can show it.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_user32 = ctypes.windll.user32
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
_user32.VkKeyScanW.restype = ctypes.c_short

_SPECIAL_VK = {
    Qt.Key_Space: 0x20, Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
    Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22, Qt.Key_End: 0x23, Qt.Key_Home: 0x24,
    Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E, Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D,
    Qt.Key_Tab: 0x09, Qt.Key_Backspace: 0x08, Qt.Key_Escape: 0x1B, Qt.Key_Pause: 0x13,
    Qt.Key_Print: 0x2C, Qt.Key_ScrollLock: 0x91,
    Qt.Key_MediaPlay: 0xB3, Qt.Key_MediaNext: 0xB0, Qt.Key_MediaPrevious: 0xB1,
}


def to_native(sequence: str) -> tuple[int, int] | None:
    """'Ctrl+Alt+Space' -> (modifiers, virtual-key) or None if not mappable."""
    seq = QKeySequence(sequence, QKeySequence.PortableText)
    if seq.isEmpty():
        return None
    combo = seq[0]
    key, mods = combo.key(), combo.keyboardModifiers()
    native = MOD_NOREPEAT
    if mods & Qt.ControlModifier:
        native |= MOD_CONTROL
    if mods & Qt.AltModifier:
        native |= MOD_ALT
    if mods & Qt.ShiftModifier:
        native |= MOD_SHIFT
    if mods & Qt.MetaModifier:
        native |= MOD_WIN
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        vk = 0x70 + (key - Qt.Key_F1)
    elif Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        vk = int(key)
    elif key in _SPECIAL_VK:
        vk = _SPECIAL_VK[key]
    else:
        try:
            res = _user32.VkKeyScanW(chr(int(key)))
        except (ValueError, OverflowError):
            return None
        if res == -1:
            return None
        vk = res & 0xFF
    return native, vk


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, owner: "HotkeyManager"):
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):
        # PySide6 passes a QByteArray here, not bytes.
        kind = bytes(event_type.data()) if hasattr(event_type, "data") else event_type
        if kind in (b"windows_generic_MSG", "windows_generic_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self.owner._fire(int(msg.wParam))
                return True, 0
        return False, 0


class HotkeyManager:
    def __init__(self):
        self._window = QWidget()            # hidden; only provides an HWND
        self._hwnd = int(self._window.winId())
        self._filter = _Filter(self)
        QCoreApplication.instance().installNativeEventFilter(self._filter)
        self._actions: dict[int, Callable[[], None]] = {}
        self._next_id = 1

    def unregister_all(self) -> None:
        for hid in list(self._actions):
            _user32.UnregisterHotKey(self._hwnd, hid)
        self._actions.clear()

    def register_all(self, bindings: dict[str, tuple[str, Callable[[], None]]]) -> dict[str, str]:
        """bindings: action -> (shortcut, callback). Returns action -> error message
        for shortcuts that could not be registered."""
        self.unregister_all()
        errors = {}
        for action, (shortcut, callback) in bindings.items():
            if not shortcut:
                continue
            native = to_native(shortcut)
            if native is None:
                errors[action] = "unsupported key"
                continue
            hid = self._next_id
            self._next_id += 1
            if _user32.RegisterHotKey(self._hwnd, hid, native[0], native[1]):
                self._actions[hid] = callback
            else:
                errors[action] = "already used by another app"
                log.warning("hotkey %s (%s) could not be registered", shortcut, action)
        return errors

    def _fire(self, hid: int) -> None:
        cb = self._actions.get(hid)
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception("hotkey action failed")
