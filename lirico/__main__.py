"""Entry point:  python -m lirico   (or double-click Lirico.pyw)"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler


def _setup_logging(debug: bool) -> None:
    from .paths import log_file
    handlers: list[logging.Handler] = [RotatingFileHandler(log_file(), maxBytes=512_000, backupCount=1,
                                                           encoding="utf-8")]
    if sys.stderr is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    debug = "--debug" in sys.argv
    _setup_logging(debug)

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    from .app import Controller
    from .paths import config_dir
    from .settings import Settings

    app = QApplication(sys.argv)
    app.setApplicationName("Lirico")
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(config_dir() / "lirico.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, "Lirico", "Lirico is already running (look for it in the system tray).")
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        logging.warning("system tray not available")

    ctl = Controller(app, Settings.load())  # noqa: F841 (kept alive for the app's lifetime)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
