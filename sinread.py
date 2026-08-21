#!/usr/bin/env python3
"""Sinread — read the text you have selected out loud, with one hotkey, fully offline.

Architecture: one resident tkinter process. The GNOME hotkey runs `sinread-trigger`, which
sends "toggle" over a Unix socket. A worker thread grabs the primary selection, splits it
into sentences, detects German or English per sentence, and streams Piper audio to the
speakers while the pill on the right edge follows along.

The Piper model lives in a separate process (`tts_worker.py`) so that unloading it is a
kill, not a hopeful `del` — after `idle_seconds` without a read the watchdog kills it and
every byte comes back. Piper runs on the CPU, so no VRAM is held at any point.

Tk is not thread-safe, so the socket, speaker and watchdog threads only put events on a
queue; a root.after() pump drains it and does every UI call on the main thread.

Usage:  sinread.py [--show | --hidden]
"""
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk

import config
import hotkey
import speaker
from config import load_config, save_config
from mainwin import MainWindow
from popup import ReadingPopup
from theme import OKC, REC, SUBTLE, Theme, apply_ttk_style

RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCKET_PATH = os.path.join(RUNTIME, "sinread.sock")
WATCHDOG_TICK = 2.0        # how often the idle timer is checked
# If preparation has not produced a sentence within this long something is wedged, and the
# next hotkey press should reset rather than be swallowed.
BUSY_RESET = 25.0
UI_TICK = 500              # ms between Status-tab refreshes


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ping_existing(command: str = "ping") -> str:
    """Talk to an already-running instance; '' if there is none."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(SOCKET_PATH)
        s.sendall(command.encode())
        reply = s.recv(4096).decode("utf-8", "replace")
        s.close()
        return reply
    except OSError:
        return ""


class SinreadApp:
    def __init__(self, root: tk.Tk, th: Theme, start_hidden: bool):
        self.root, self.th = root, th
        self.cfg = load_config()
        self.events: queue.Queue = queue.Queue()
        self._srv = None
        self._stopping = False
        self._progress = (0, 1)
        # Set the moment a read is requested, cleared when the session ends. `speaking` only
        # becomes true once the worker thread has actually grabbed the selection, so without
        # this a second press during that window would start a second read instead of
        # stopping the first.
        self._busy = False
        self._busy_since = 0.0
        self._gen = 0          # bumped by every read/stop; cancels an in-flight grab
        self._lang = self.cfg["default_lang"]
        self._sentence = ""

        self.speaker = speaker.Speaker(self.cfg, on_state=self._on_state)
        self.popup = ReadingPopup(root, th, self.cfg)
        self.win = MainWindow(root, th, self)
        if not start_hidden:
            self.win.show()

        threading.Thread(target=self._serve, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        self.root.after(50, self._pump)
        self.root.after(UI_TICK, self._tick_ui)

    # -- helpers used by the settings tab -------------------------------------
    def voice_path(self, lang: str) -> str:
        return config.voice_path(self.cfg, lang)

    def persist(self, message: str = ""):
        try:
            save_config(self.cfg)
        except Exception as e:  # noqa: BLE001
            self.win.set_status(f"could not save settings: {e}", REC)
            return
        self.win.refresh_hotkey()
        if message:
            self.win.set_status(message, OKC)

    # -- socket ---------------------------------------------------------------
    def _serve(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)          # stale socket from a crash
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(8)
        self._srv = srv
        log(f"listening on {SOCKET_PATH}")
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                if self._stopping:
                    break
                continue                     # survive transient errors
            with conn:
                try:
                    cmd = conn.recv(4096).decode("utf-8", "replace").strip()
                    reply = "ok"
                    if cmd == "ping":
                        reply = "pong"
                    elif cmd in ("toggle", "read", "stop", "pause", "unload", "show"):
                        self.events.put((cmd,))
                    elif cmd == "status":
                        st = self.speaker.status()
                        reply = " ".join(f"{k}={v}" for k, v in st.items())
                    elif cmd == "quit":
                        reply = "bye"
                        self.events.put(("quit",))
                    else:
                        reply = f"unknown command {cmd!r}"
                    conn.sendall(reply.encode())
                except OSError:
                    pass

    # -- watchdog -------------------------------------------------------------
    def _watchdog(self):
        """Kills the Piper worker once the idle window has passed. A thread rather than a
        root.after() chain so the timer keeps running even while the UI is busy."""
        while not self._stopping:
            time.sleep(WATCHDOG_TICK)
            try:
                self.speaker.maybe_unload()
            except Exception as e:  # noqa: BLE001
                log(f"watchdog: {e!r}")

    # -- speaker callbacks (any thread) ---------------------------------------
    def _on_state(self, kind: str, **info):
        self.events.put(("state", kind, info))

    # -- event pump (main thread) ---------------------------------------------
    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "toggle":
                    self.on_trigger()
                elif kind == "read":
                    self.on_read()
                elif kind == "stop":
                    self.on_stop()
                elif kind == "pause":
                    self.on_pause()
                elif kind == "unload":
                    self.on_unload()
                elif kind == "show":
                    self.win.show()
                elif kind == "state":
                    self._handle_state(ev[1], ev[2])
                elif kind == "quit":
                    self.shutdown()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _tick_ui(self):
        """Keeps the Status tab (and its unload countdown) live while it is visible."""
        try:
            if self.win.win.winfo_viewable():
                self.win.status_tab.refresh(self.speaker.status(), self._progress,
                                            self._lang, self._sentence)
        except tk.TclError:
            pass
        self.root.after(UI_TICK, self._tick_ui)

    def _handle_state(self, kind: str, info: dict):
        if kind == "grabbed":
            self.popup.show_busy("Preparing…")
        elif kind == "loading":
            self.popup.show_busy("Loading voice…")
        elif kind == "start":
            self._lang = info.get("lang", self._lang)
            self._progress = (0, info.get("total", 1))
        elif kind == "segment":
            self._lang = info.get("lang", self._lang)
            self._sentence = info.get("text", "")
            self._progress = (info["index"], info["total"])
            self.popup.show_reading(info["index"], info["total"], self._lang,
                                    self._sentence, hotkey.pretty(self.cfg["hotkey"]),
                                    paused=self.speaker.paused)
        elif kind in ("paused", "resumed"):
            i, total = self._progress
            self.popup.show_reading(i, total, self._lang, self._sentence,
                                    hotkey.pretty(self.cfg["hotkey"]),
                                    paused=(kind == "paused"))
        elif kind in ("done", "stopped"):
            self._sentence = ""
            self._busy = False
            self.popup.show_done(info.get("spoken", 0), info.get("total", 0))
        elif kind == "error":
            self._sentence = ""
            self._busy = False
            self.popup.show_error(info.get("message", "Something went wrong."))
        elif kind == "model":
            if info.get("loaded"):
                self.win.set_status("model loaded", SUBTLE)
            else:
                reason = info.get("reason", "")
                self.win.set_status(f"model unloaded ({reason})", SUBTLE)
                if reason.startswith("idle"):
                    log(f"model unloaded after {reason}")

    # -- actions --------------------------------------------------------------
    def on_trigger(self):
        """The hotkey. Reading -> stop. Paused -> resume. Otherwise read the selection."""
        if self.speaker.paused:
            self.speaker.toggle_pause()
        elif self.speaker.speaking:
            self.on_stop()
        elif self._busy:
            # Still preparing — no audio has started yet. A second press here means "come
            # on", not "stop": people press again because nothing has happened, and
            # throwing the work away leaves them with silence and no explanation. So keep
            # preparing. The age check is the safety valve, so a wedged prepare can never
            # make the hotkey feel dead.
            if time.time() - self._busy_since > BUSY_RESET:
                log("preparation wedged — resetting on second press")
                self.on_stop()
        else:
            self.on_read()

    def on_read(self):
        self._busy = True
        self._busy_since = time.time()
        self._gen += 1
        gen = self._gen
        self.popup.show_busy("Preparing…")
        threading.Thread(target=self.speaker.speak_selection,
                         args=(lambda: gen != self._gen,), daemon=True).start()

    def on_stop(self):
        was_busy, speaking = self._busy, self.speaker.speaking
        self._busy = False
        self._gen += 1         # cancels a read still grabbing the selection
        self.speaker.stop()
        if was_busy and not speaking:
            # Cancelled before the first sentence. The worker thread notices only after its
            # current step returns (grabbing the selection can take a second), so take the
            # spinner down here rather than leaving it spinning on "Preparing…".
            self.popup.show_done(0, 0)

    def on_pause(self):
        self.speaker.toggle_pause()

    def on_unload(self):
        if self.speaker.unload("you asked"):
            self.popup.show_note("Model unloaded — memory free")

    def preview(self, text: str):
        """Speak a sample at the current settings (the Settings tab's Test buttons)."""
        threading.Thread(target=self.speaker.speak, args=(text,), daemon=True).start()

    # -- lifecycle ------------------------------------------------------------
    def shutdown(self):
        self._stopping = True
        try:
            self.speaker.shutdown()
        except Exception as e:  # noqa: BLE001
            log(f"speaker shutdown: {e!r}")
        try:
            if self._srv is not None:
                self._srv.close()
        except OSError:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)          # decisive: no daemon thread can keep the process alive


def main() -> int:
    start_hidden = "--hidden" in sys.argv

    # Single instance: hand the request to the running copy instead of starting a second one.
    if ping_existing() == "pong":
        if not start_hidden:
            ping_existing("show")
            log("already running — raised the existing window")
        return 0

    root = tk.Tk()
    root.withdraw()                # the root only owns fonts/styles; real windows are Toplevels
    root.title("Sinread")
    th = Theme(root)
    apply_ttk_style(root, th)

    app = SinreadApp(root, th, start_hidden)

    # Reconcile the shortcut. gsettings is the source of truth for what the desktop actually
    # does, so an existing binding is adopted rather than overwritten — otherwise a stale
    # config would silently undo a change made in GNOME's own Settings.
    try:
        existing = hotkey.current()
        if not existing:
            hotkey.apply(app.cfg["hotkey"])
            log(f"registered hotkey {app.cfg['hotkey']}")
        elif existing != app.cfg["hotkey"]:
            log(f"adopting hotkey {existing} from GNOME (config had {app.cfg['hotkey']})")
            app.cfg["hotkey"] = existing
            save_config(app.cfg)
            app.win.refresh_hotkey()
    except Exception as e:  # noqa: BLE001
        log(f"could not register hotkey: {e!r}")

    log(f"ready — hotkey {app.cfg['hotkey']}, {app.cfg['speed']:.2f}x, "
        f"unload after {app.cfg['idle_seconds']:.0f}s")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
