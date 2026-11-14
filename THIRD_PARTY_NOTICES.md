# Third-party software

Lirico.exe bundles the following open-source components. Thanks to their authors.

| Component | License | Used for |
|---|---|---|
| [Qt 6](https://www.qt.io) via [PySide6](https://pypi.org/project/PySide6/) | LGPL v3 | Overlay, tray, settings window |
| [Python](https://www.python.org) | PSF License | Runtime |
| [pywinrt](https://github.com/pywinrt/pywinrt) (`winrt-*`) | MIT | Windows media controls (now playing) |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Speech recognition for auto-sync |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT | Speech model runtime |
| [Whisper tiny](https://github.com/openai/whisper) model ([Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny)) | MIT | Speech model |
| [tokenizers](https://github.com/huggingface/tokenizers) | Apache 2.0 | Speech model tokenizer |
| [NumPy](https://numpy.org) | BSD-3-Clause | Audio processing |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) / PortAudio | MIT | System-audio (loopback) capture |
| [requests](https://github.com/psf/requests) | Apache 2.0 | Lyrics lookups |

Qt is used unmodified and is loaded as separate shared libraries, as the LGPL
requires; its source is available at https://download.qt.io/.

**Lyrics** are fetched at runtime from [LRCLIB](https://lrclib.net), a free,
community-maintained database. Lirico does not bundle or redistribute any
lyrics. Lyrics remain the property of their respective rights holders.
