"""Dev tool: creates a REAL Windows media session (SMTC) playing a silent, muted
WAV with the title/artist you choose, then runs a scripted timeline of seeks
and pauses. Lets you test Lirico end-to-end without playing audible music.

    .venv\\Scripts\\python tools\\fake_player.py --title "Tum Hi Ho | Aashiqui 2 | Arijit Singh | Full Video Song" --artist "T-Series" --script "0:play 12:seek=100 20:pause 26:play 34:stop"

Needs: pip install -r requirements-dev.txt
"""
import argparse
import os
import tempfile
import time
import wave
from datetime import timedelta


def make_silence(seconds: int) -> str:
    path = os.path.join(tempfile.gettempdir(), f"lirico_silence_{seconds}.wav")
    if not os.path.exists(path):
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(8000)
            w.writeframes(b"\x80" * 8000 * seconds)
    return path


def main() -> None:
    from winrt.windows.foundation import Uri
    from winrt.windows.media import MediaPlaybackType
    from winrt.windows.media.core import MediaSource
    from winrt.windows.media.playback import MediaPlaybackItem, MediaPlayer

    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--artist", default="")
    ap.add_argument("--length", type=int, default=262)
    ap.add_argument("--audio", default="", help="play this .wav instead of silence")
    ap.add_argument("--volume", type=float, default=0.0, help="0 = muted (default)")
    ap.add_argument("--script", default="0:play 60:stop",
                    help="space-separated '<t>:<action>' with actions play, pause, stop, seek=<s>")
    a = ap.parse_args()

    player = MediaPlayer()
    player.volume = a.volume
    player.is_muted = a.volume <= 0
    src = os.path.abspath(a.audio) if a.audio else make_silence(a.length)
    item = MediaPlaybackItem(MediaSource.create_from_uri(Uri("file:///" + src.replace("\\", "/"))))
    props = item.get_display_properties()
    props.type = MediaPlaybackType.MUSIC
    props.music_properties.title = a.title
    props.music_properties.artist = a.artist
    item.apply_display_properties(props)
    player.source = item

    steps = []
    for tok in a.script.split():
        t, action = tok.split(":", 1)
        steps.append((float(t), action))
    start = time.monotonic()
    for t, action in steps:
        delay = start + t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        stamp = time.monotonic() - start
        if action == "play":
            player.play()
        elif action == "pause":
            player.pause()
        elif action.startswith("seek="):
            player.playback_session.position = timedelta(seconds=float(action[5:]))
        elif action == "stop":
            print(f"[{stamp:5.1f}s] stop", flush=True)
            break
        pos = player.playback_session.position.total_seconds()
        print(f"[{stamp:5.1f}s] {action:10s} media position now {pos:6.2f}s", flush=True)
    player.close()


if __name__ == "__main__":
    main()
