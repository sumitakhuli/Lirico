"""Builds dist\\Lirico.exe: one self-contained file, no Python needed to run it.

    .venv\\Scripts\\python build_exe.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def make_icon() -> Path:
    """Render the tray icon (drawn in code) to an .ico for the exe."""
    from PySide6.QtGui import QGuiApplication
    sys.path.insert(0, str(ROOT))
    from lirico.theme import THEME
    from lirico.tray import make_icon as draw

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    BUILD.mkdir(exist_ok=True)
    ico = BUILD / "lirico.ico"
    pix = draw(THEME).pixmap(256, 256)
    if not pix.save(str(ico), "ICO"):
        png = BUILD / "lirico.png"
        pix.save(str(png), "PNG")
        return png
    return ico


def main() -> None:
    if not (ROOT / "models" / "whisper-tiny" / "model.bin").exists():
        sys.exit("models/whisper-tiny missing: run  python build_exe.py --get-model  first")
    icon = make_icon()
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "Lirico",
        f"--icon={icon}",
        "--distpath", str(ROOT / "dist"), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
        # WinRT projections are loaded dynamically: include them explicitly.
        "--collect-submodules", "winrt",
        "--collect-binaries", "winrt",
        "--hidden-import", "winrt.windows.media.control",
        "--hidden-import", "winrt.windows.foundation",
        "--hidden-import", "winrt.windows.foundation.collections",
        # Auto-sync: speech model + runtime + loopback audio capture.
        "--add-data", f"{ROOT / 'models' / 'whisper-tiny'};models/whisper-tiny",
        "--collect-binaries", "ctranslate2",
        "--collect-submodules", "faster_whisper",
        "--hidden-import", "pyaudiowpatch",
        # Trim things Lirico never uses (PyAV only decodes audio files and
        # onnxruntime only runs an optional voice filter; both are stubbed/unused).
        "--exclude-module", "av", "--exclude-module", "onnxruntime",
        "--exclude-module", "tkinter", "--exclude-module", "pytest", "--exclude-module", "websocket",
        "--exclude-module", "PIL", "--exclude-module", "torch",
        str(ROOT / "Lirico.pyw"),
    ]
    print(" ".join(args))
    subprocess.check_call(args, cwd=ROOT)
    exe = ROOT / "dist" / "Lirico.exe"
    print(f"\nBuilt {exe} ({exe.stat().st_size / 1e6:.1f} MB)")
    shutil.rmtree(BUILD / "work", ignore_errors=True)


def get_model() -> None:
    """Download the Whisper tiny model (CTranslate2 format, ~75 MB) into models/."""
    from huggingface_hub import snapshot_download
    snapshot_download("Systran/faster-whisper-tiny", local_dir=str(ROOT / "models" / "whisper-tiny"))


if __name__ == "__main__":
    if "--get-model" in sys.argv:
        get_model()
    else:
        main()
