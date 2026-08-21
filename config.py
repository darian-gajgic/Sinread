#!/usr/bin/env python3
"""Sinread settings — defaults plus load/save of ~/.config/sinread/config.json.

Only keys present in DEFAULTS are honoured from the user file, so a stale or hand-edited
config can never inject unknown keys into the running app.
"""
import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "sinread"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    # --- behaviour ---
    # A single key is a valid shortcut. Numpad + sits next to the numpad - that Sinlate
    # uses, and neither collides with typing (wisprflow holds <Ctrl><Super>space).
    "hotkey": "KP_Add",
    # Piper's native pace is 1.0; this is the pace the voice was tuned to by ear.
    "speed": 1.7,
    # Voice per language. Relative paths resolve against the app dir. Every voice here is
    # 22050 Hz — the rate the player is opened at, so a mismatched voice is refused rather
    # than played at the wrong pitch.
    "voice_de": "voices/de_DE-thorsten-high.onnx",
    "voice_en": "voices/en_US-ryan-high.onnx",
    # Used when a selection is too short or too ambiguous to call (e.g. "OK", "Setup").
    "default_lang": "de",
    # Re-detect the language for every sentence, so a German text quoting an English
    # sentence switches voice mid-read. Off = detect once over the whole selection.
    "detect_per_segment": True,

    # --- model lifetime ---
    # Seconds of no reading before the Piper worker is KILLED. Process death is what
    # actually returns the memory (Nexus's audit lesson: in-process unloading leaves the
    # runtime's arenas — and, on GPU, the CUDA context — behind).
    "idle_seconds": 120.0,

    # --- limits ---
    "max_chars": 20000,       # whole-selection cap; longer selections are read truncated
    "segment_chars": 350,     # target sentence-chunk size handed to Piper
    "popup_seconds": 4.0,     # how long the idle/finished popup lingers

    # --- engine ---
    # Interpreter that runs tts_worker.py. Empty = auto-detect (see speaker.find_python):
    # Sinread's own .venv first, then Nexus's, then any python3 that has piper installed.
    "python": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        except Exception as e:  # noqa: BLE001
            print(f"[sinread] WARNING: could not read {CONFIG_PATH}: {e!r}; using defaults",
                  flush=True)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    payload = {k: cfg[k] for k in DEFAULTS if k in cfg}
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, CONFIG_PATH)


def voice_path(cfg: dict, lang: str) -> str:
    """Absolute path of the .onnx for 'de' or 'en'."""
    raw = cfg.get(f"voice_{lang}") or DEFAULTS[f"voice_{lang}"]
    p = Path(raw)
    return str(p if p.is_absolute() else APP_DIR / p)


def available_voices() -> dict:
    """{'de': [(label, abspath), ...], 'en': [...]} — every voice shipped in voices/.

    The language comes from the voice's own config JSON, so dropping any Piper voice into
    voices/ makes it selectable without touching this file.
    """
    out = {"de": [], "en": []}
    vdir = APP_DIR / "voices"
    if not vdir.is_dir():
        return out
    for onnx in sorted(vdir.glob("*.onnx")):
        meta = onnx.with_suffix(".onnx.json")
        if not meta.exists():
            continue
        try:
            d = json.loads(meta.read_text())
            code = (d.get("language", {}) or {}).get("code", "") or onnx.name[:5]
            rate = (d.get("audio", {}) or {}).get("sample_rate")
        except Exception:  # noqa: BLE001
            continue
        if rate not in (None, 22050):
            continue                      # wrong rate would play at the wrong speed
        lang = "de" if code.lower().startswith("de") else "en" if code.lower().startswith("en") else ""
        if lang:
            out[lang].append((onnx.stem, str(onnx)))
    return out
