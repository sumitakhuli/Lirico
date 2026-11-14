"""Captures what the PC is playing (WASAPI loopback of the default speakers).

Never uses the microphone. Audio stays in memory, is only used to recognise
where in the song we are, and is never written to disk or sent anywhere.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

TARGET_RATE = 16000  # what the speech model expects


class LoopbackRecorder:
    """Keeps a rolling buffer of the last `seconds` of system audio (16 kHz mono)."""

    def __init__(self, seconds: float = 40.0):
        self.seconds = seconds
        self._chunks: deque = deque()
        self._samples = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self.end_time = 0.0            # time.monotonic() of the newest sample

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lirico-loopback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._samples = 0

    def snapshot(self, seconds: Optional[float] = None) -> tuple[np.ndarray, float]:
        """(audio, end_time): the most recent audio and the monotonic time of its last sample."""
        with self._lock:
            audio = np.concatenate(list(self._chunks)) if self._chunks else np.zeros(0, np.float32)
            end = self.end_time
        if seconds is not None:
            audio = audio[-int(seconds * TARGET_RATE):]
        return audio, end

    # -- worker -------------------------------------------------------------
    def _run(self) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except Exception as exc:  # pragma: no cover
            self.error = f"audio capture unavailable: {exc}"
            log.warning(self.error)
            return
        pa = pyaudio.PyAudio()
        try:
            while not self._stop.is_set():
                device = self._loopback_device(pa, pyaudio)
                if device is None:
                    self.error = "no loopback device"
                    time.sleep(2)
                    continue
                self._record(pa, pyaudio, device)
        finally:
            pa.terminate()

    @staticmethod
    def _loopback_device(pa, pyaudio):
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if speakers.get("isLoopbackDevice"):
                return speakers
            for dev in pa.get_loopback_device_info_generator():
                if speakers["name"] in dev["name"]:
                    return dev
        except Exception as exc:
            log.debug("loopback lookup failed: %s", exc)
        return None

    def _record(self, pa, pyaudio, device) -> None:
        rate = int(device["defaultSampleRate"])
        channels = max(1, int(device["maxInputChannels"]))
        frames = max(256, rate // 20)  # 50 ms reads
        try:
            stream = pa.open(format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
                             input_device_index=device["index"], frames_per_buffer=frames)
        except Exception as exc:
            self.error = f"could not open loopback: {exc}"
            log.warning(self.error)
            time.sleep(2)
            return
        log.info("listening to %s (%d Hz, %d ch)", device["name"], rate, channels)
        self.error = None
        keep = int(self.seconds * TARGET_RATE)
        started = time.monotonic()
        try:
            while not self._stop.is_set():
                # Loopback delivers nothing while the PC is silent; poll so a
                # default-device change is noticed.
                avail = stream.get_read_available()
                if avail < frames:
                    time.sleep(0.02)
                    if time.monotonic() - started > 5 and self._default_changed(pa, pyaudio, device):
                        return
                    continue
                data = stream.read(frames, exception_on_overflow=False)
                now = time.monotonic()
                x = np.frombuffer(data, dtype=np.float32)
                if channels > 1:
                    x = x.reshape(-1, channels).mean(axis=1)
                y = _resample(x, rate, TARGET_RATE)
                with self._lock:
                    # Silence produces no packets: pad the gap so the buffer
                    # stays aligned with real time.
                    if self.end_time:
                        expected = now - len(y) / TARGET_RATE
                        gap = expected - self.end_time
                        if 0.08 < gap < self.seconds:
                            pad = np.zeros(int(gap * TARGET_RATE), np.float32)
                            self._chunks.append(pad)
                            self._samples += len(pad)
                    self._chunks.append(y)
                    self._samples += len(y)
                    self.end_time = now
                    while self._samples - len(self._chunks[0]) > keep:
                        self._samples -= len(self._chunks.popleft())
        except Exception as exc:
            log.warning("loopback stopped: %s", exc)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

    def _default_changed(self, pa, pyaudio, device) -> bool:
        current = self._loopback_device(pa, pyaudio)
        return current is not None and current["index"] != device["index"]


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x.astype(np.float32, copy=False)
    n = int(round(len(x) * dst / src))
    if n <= 0:
        return np.zeros(0, np.float32)
    # Average-pool when downsampling (cheap anti-aliasing), then interpolate.
    if src > dst and src % dst == 0:
        f = src // dst
        m = len(x) - len(x) % f
        return x[:m].reshape(-1, f).mean(axis=1).astype(np.float32)
    pos = np.linspace(0, len(x) - 1, n)
    return np.interp(pos, np.arange(len(x)), x).astype(np.float32)
