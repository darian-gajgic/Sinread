# Sinread

Select text anywhere, press a key, and it is read out loud — in the language it is written
in. Everything runs locally on the CPU: no internet, no API keys, and **no VRAM at any
point**, so your GPU stays free for other models.

Default hotkey: **`F8`**  ·  press it again while it reads to stop.

## How it decides the language

Nothing to configure. Every sentence is classified as German or English from its own
function words and orthography, and the voice switches mid-text when the text does:

> „Guten Tag. **This sentence is English**, so the voice changes here. Danach wieder Deutsch."

A selection too short to call (`OK`, `Setup`) is read in your fallback language — German by
default, changeable in Settings.

| Language | Voice | Quality |
|---|---|---|
| German | `de_DE-thorsten-high` | high |
| English | `en_US-ryan-high` | high — the voice Nexus reads with |

Drop any other 22.05 kHz Piper voice into `voices/` and it appears in the Settings
dropdown. `en_US-ryan-medium` (the model Nexus actually loads as `piper_voice.onnx`) is
already there as a lighter alternative.

## Install

```bash
cd ~/Sinread
./install.sh
```

That creates the venv, fetches the voices, registers the GNOME shortcut (appending to the
shared list, so the wisprflow and Sinlate hotkeys are untouched), and adds a "Sinread" entry
to the app menu.

## Using it

- **Read**: highlight text in any app → press `F8`. A pill appears at the right edge showing
  the language, the sentence being spoken, and progress.
- **Stop**: press `F8` again. Audio cuts immediately.
- **Pause / resume**: the buttons in the main window, or `sinread-trigger pause`. `F8`
  resumes from a pause rather than stopping.
- **Main window**: launch *Sinread* from the app menu, or `./sinread-trigger show`.
  - **Status** — what it is doing, and whether the model is resident, with the unload
    countdown running down in front of you.
  - **Settings** — hotkey, speed (with German/English test buttons), voice per language,
    fallback language, mid-text switching, unload delay. Saved immediately to
    `~/.config/sinread/config.json`.
- Closing the window only hides it; the hotkey keeps working. Use **Quit Sinread** to stop
  it entirely. Pressing `F8` afterwards starts it again automatically.

## The model's lifetime

This is the part worth understanding, because it is what keeps the GPU free.

Piper is an **ONNX model that runs on the CPU** — the same engine Nexus uses for JARVIS's
voice. It holds about **300 MB of RAM and 0 MB of VRAM**, whether it is idle or speaking.
Your 12 GB card never sees it.

It still unloads when you stop using it. After **120 seconds** without a read the watchdog
**kills the worker process** rather than dropping a Python reference — process death is the
only thing that reliably returns the ONNX Runtime's memory arenas, which is the lesson
Nexus's own memory audit paid for. Reloading afterwards costs about half a second, hidden
behind the pill's spinner.

```
loaded, speaking   ~300 MB RAM   0 MB VRAM
loaded, idle       ~300 MB RAM   0 MB VRAM      <- unload countdown running
unloaded                 0 MB    0 MB VRAM      <- process gone
```

Change the delay in Settings → Memory, or unload on the spot with **Unload model now**.

## How it works

One resident Python process (standard library only — the venv is used exclusively by the
Piper worker):

```
GNOME hotkey → sinread-trigger → Unix socket → Sinread
                                                 ├─ wl-paste --primary   (what you highlighted)
                                                 ├─ textprep             (sentences, de-hyphenated)
                                                 ├─ langdetect           (German or English, per sentence)
                                                 ├─ tts_worker.py ──────► Piper, in its own killable process
                                                 ├─ pw-play              (streams while it synthesises)
                                                 └─ watchdog → kill after 120s idle
```

Flow control is the pipes themselves. Audio is written straight into the player as Piper
produces it; when the player's buffer is full the write blocks, which stalls the reader,
which stalls Piper. Synthesis runs at roughly **8× realtime**, so speech starts within about
half a second and never runs dry — including across a mid-text voice change.

The selection is cleaned before it is spoken: PDF line-break hyphenation is rejoined,
markdown markers are dropped, headings and list items become their own sentences, and a
naked URL is read as "Link zu example.com" instead of letter by letter.

## Files

| | |
|---|---|
| `sinread.py` | resident daemon — socket, popup, window, watchdog |
| `sinread-trigger` | what the hotkey runs; starts the daemon if it is not up |
| `speaker.py` | worker lifetime, streaming playback, idle unload |
| `tts_worker.py` | Piper synthesis, in the venv, in its own process |
| `langdetect.py` | German/English classifier |
| `textprep.py` | selection → speakable sentences |
| `config.py` `hotkey.py` | settings; the GNOME shortcut |
| `popup.py` `mainwin.py` `theme.py` | the reading pill, the window, the shared look |

## Troubleshooting

| Symptom | Check |
|---|---|
| Hotkey does nothing | `./sinread-trigger ping` → `pong`. Then `python3 hotkey.py show`. |
| Nothing is read | Is the text still highlighted? Sinread reads the primary selection, and falls back to the clipboard. |
| No sound | `./sinread-trigger status`, then `~/.cache/sinread/worker.log`. |
| Won't start | `~/.cache/sinread/sinread.log`. |
| Wrong language | Settings → *If unsure, read as*, for selections too short to classify. |

```bash
./sinread-trigger status        # speaking / model_loaded / idle seconds
python3 langdetect.py "Wo ist der Bahnhof?"
python3 textprep.py "Text to see how it gets split."
python3 speaker.py "Read this without the GUI."
```

## Uninstall

```bash
python3 ~/Sinread/hotkey.py remove
rm -f ~/.local/share/applications/sinread.desktop ~/Desktop/sinread.desktop
```
