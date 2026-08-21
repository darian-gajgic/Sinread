#!/usr/bin/env bash
# Set up Sinread: venv, voices, hotkey, app-menu entry, and a health check.
# No sudo, no systemd — Sinread is a plain user app.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_MODELS="$HOME/Nexus-Agentic-Coding-Setup/app/models"
VOICES="$HERE/voices"

echo "==> making scripts executable"
chmod +x "$HERE/sinread.py" "$HERE/sinread-trigger" "$HERE/hotkey.py" \
         "$HERE/speaker.py" "$HERE/tts_worker.py" "$HERE/langdetect.py" "$HERE/textprep.py"

echo "==> Piper runtime"
if [ -x "$HERE/.venv/bin/python" ] && "$HERE/.venv/bin/python" -c "import piper.voice" 2>/dev/null; then
  echo "    OK: $HERE/.venv already has Piper"
else
  echo "    creating $HERE/.venv (CPU-only onnxruntime, ~200 MB)"
  python3 -m venv "$HERE/.venv"
  "$HERE/.venv/bin/pip" install -q --upgrade pip
  "$HERE/.venv/bin/pip" install -q piper-tts
  "$HERE/.venv/bin/python" -c "import piper.voice" \
    || { echo "    FAILED to install piper-tts"; exit 1; }
  echo "    OK: Piper installed"
fi

echo "==> voices"
mkdir -p "$VOICES"
# English: the exact voice Nexus reads with, copied so Sinread does not depend on that repo.
if [ ! -f "$VOICES/en_US-ryan-high.onnx" ]; then
  if [ -f "$NEXUS_MODELS/voices/en_US-ryan-high.onnx" ]; then
    echo "    copying en_US-ryan-high from Nexus"
    cp "$NEXUS_MODELS/voices/en_US-ryan-high.onnx"      "$VOICES/"
    cp "$NEXUS_MODELS/voices/en_US-ryan-high.onnx.json" "$VOICES/"
  else
    echo "    downloading en_US-ryan-high"
    curl -sLf --max-time 600 -o "$VOICES/en_US-ryan-high.onnx" \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx"
    curl -sLf --max-time 60 -o "$VOICES/en_US-ryan-high.onnx.json" \
      "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx.json"
  fi
fi
# German: not part of Nexus (it only ever spoke English), so it comes from upstream.
if [ ! -f "$VOICES/de_DE-thorsten-high.onnx" ]; then
  echo "    downloading de_DE-thorsten-high"
  curl -sLf --max-time 600 -o "$VOICES/de_DE-thorsten-high.onnx" \
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx"
  curl -sLf --max-time 60 -o "$VOICES/de_DE-thorsten-high.onnx.json" \
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json"
fi
for v in de_DE-thorsten-high en_US-ryan-high; do
  [ -s "$VOICES/$v.onnx" ] && [ -s "$VOICES/$v.onnx.json" ] \
    && echo "    OK: $v" || { echo "    MISSING: $v"; exit 1; }
done

echo "==> registering the GNOME hotkey"
# Appends to the shared custom-keybindings list; wisprflow's and Sinlate's are preserved.
python3 "$HERE/hotkey.py" apply

echo "==> installing the app-menu entry"
mkdir -p "$HOME/.local/share/applications"
sed "s#/home/sinep/Sinread#$HERE#g" "$HERE/sinread.desktop" \
  > "$HOME/.local/share/applications/sinread.desktop"
chmod 644 "$HOME/.local/share/applications/sinread.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi
if [ -d "$HOME/Desktop" ]; then
  install -m 755 "$HOME/.local/share/applications/sinread.desktop" "$HOME/Desktop/sinread.desktop"
  gio set "$HOME/Desktop/sinread.desktop" metadata::trusted true 2>/dev/null || true
fi

echo "==> checking the desktop plumbing"
for tool in wl-paste pw-play; do
  command -v "$tool" >/dev/null 2>&1 \
    && echo "    OK: $tool" \
    || echo "    WARNING: $tool not found"
done

KEY="$(python3 -c "import sys;sys.path.insert(0,'$HERE');from config import load_config;print(load_config()['hotkey'])")"
cat <<EOF2

Done.

  Try it:    select any text, then press $KEY
  Again:     press $KEY while it reads to stop
  Window:    launch "Sinread" from the app menu (status + settings)
  Uninstall: python3 $HERE/hotkey.py remove && rm -f ~/.local/share/applications/sinread.desktop
EOF2
