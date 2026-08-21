#!/usr/bin/env python3
"""Sinread TTS worker — Piper synthesis, hosted in a killable subprocess.

Run by the Piper venv interpreter, never by the daemon's own python. It exists as a
SEPARATE PROCESS so that "unload the model" can be implemented as "kill the process":
dropping the Python reference leaves the ONNX Runtime arenas (and, on a CUDA build, the
context) allocated, which is the lesson Nexus's memory audit already paid for. Process
death returns every byte.

Protocol
--------
stdin   one JSON object per line:
          {"cmd": "speak", "text": "...", "voice": "/abs/voice.onnx", "speed": 1.3}
          {"cmd": "ping"}
stdout  binary frames: a 9-byte ASCII header (1 type char + 8 hex length) then the payload.
          'A' + len  raw PCM, signed 16-bit LE mono @ 22050 Hz
          'J' + len  UTF-8 JSON event: {"event": "done"|"pong"|"error"|"ready", ...}
        Every 'speak' ends with exactly one 'J' event, so the reader always resynchronises.
stderr  human-readable log (the daemon tees it to ~/.cache/sinread/worker.log).
"""
import json
import sys

from piper.voice import PiperVoice
from piper.config import SynthesisConfig

OUT = sys.stdout.buffer
SAMPLE_RATE = 22050

# Loaded voices, keyed by .onnx path. Both a German and an English voice fit in ~300 MB of
# RAM, so a mid-text language switch costs nothing — evicting one would put a ~0.5 s reload
# in the middle of a sentence every time the text alternates.
_voices: dict[str, PiperVoice] = {}
MAX_CACHED = 2


def log(msg: str) -> None:
    print(f"[tts_worker] {msg}", file=sys.stderr, flush=True)


def frame(kind: bytes, payload: bytes) -> None:
    OUT.write(kind + b"%08x" % len(payload))
    OUT.write(payload)
    OUT.flush()


def event(**payload) -> None:
    frame(b"J", json.dumps(payload).encode("utf-8"))


def get_voice(path: str) -> PiperVoice:
    v = _voices.get(path)
    if v is None:
        if len(_voices) >= MAX_CACHED:
            _voices.pop(next(iter(_voices)))
        log(f"loading {path}")
        v = PiperVoice.load(path, config_path=path + ".json")
        _voices[path] = v
        log(f"loaded {path}")
    return v


def speak(req: dict) -> None:
    text = (req.get("text") or "").strip()
    if not text:
        event(event="done", samples=0)
        return
    speed = float(req.get("speed") or 1.0)
    speed = min(2.0, max(0.5, speed))     # outside this Piper's prosody falls apart
    # Piper's length_scale is the inverse of speed: 1.3x faster == 0.769 length.
    cfg = SynthesisConfig(length_scale=1.0 / speed)

    voice = get_voice(req["voice"])
    total = 0
    for chunk in voice.synthesize(text, cfg):
        pcm = chunk.audio_int16_array.tobytes()
        total += len(pcm) // 2
        frame(b"A", pcm)
    event(event="done", samples=total)


def main() -> int:
    event(event="ready", sample_rate=SAMPLE_RATE)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            event(event="error", message=f"bad request: {e}")
            continue

        cmd = req.get("cmd")
        try:
            if cmd == "speak":
                speak(req)
            elif cmd == "ping":
                event(event="pong", loaded=sorted(_voices))
            elif cmd == "quit":
                return 0
            else:
                event(event="error", message=f"unknown command {cmd!r}")
        except Exception as e:  # noqa: BLE001
            # Never die on one bad segment: report it and stay ready for the next request,
            # otherwise a single unpronounceable chunk would end the whole reading session.
            log(f"{cmd} failed: {e!r}")
            event(event="error", message=f"{e.__class__.__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
