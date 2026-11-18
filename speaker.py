#!/usr/bin/env python3
"""Sinread's reading engine: selection -> segments -> Piper worker -> speakers.

Standard library only — the daemon must start without a venv. Piper itself lives in
`tts_worker.py`, behind a pipe, in an interpreter this module locates at runtime.

Flow control is the pipes. One thread reads PCM frames from the worker and writes them
straight into the player's stdin; when the player's buffer is full that write blocks, which
stalls the reader, which stalls the worker. No queue, no timers, no drift. Piper synthesises
at ~8x realtime, so the ~1.4 s of audio the pipes hold is a comfortable cushion — it covers
both the next sentence and the ~0.5 s reload when the text switches language mid-read.

Test from a shell:
    python3 speaker.py "Guten Tag. This sentence is English."
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import config
import langdetect
import textprep

APP_DIR = Path(__file__).resolve().parent
WORKER = APP_DIR / "tts_worker.py"
LOG_DIR = Path.home() / ".cache" / "sinread"
SAMPLE_RATE = 22050
BYTES_PER_SEC = SAMPLE_RATE * 2

# Interpreters tried in order when config["python"] is empty.
CANDIDATE_PYTHONS = (
    APP_DIR / ".venv" / "bin" / "python",
    Path.home() / "Nexus-Agentic-Coding-Setup" / "app" / ".venv" / "bin" / "python",
    Path("/usr/bin/python3"),
)

# Raw-PCM players, best first. pw-play is PipeWire's own client and is what this desktop
# runs; aplay and ffplay are there so a missing PipeWire never leaves the app mute.
PLAYERS = (
    ["pw-play", "--rate=22050", "--channels=1", "--format=s16", "--raw", "-"],
    ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-"],
    ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet",
     "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
)


class SpeakError(Exception):
    """Message safe to put in front of the user."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [speaker] {msg}", flush=True)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def grab_selection() -> tuple[str, str]:
    """(text, source). The primary selection is whatever is highlighted right now; the
    clipboard is the fallback for apps that do not export a primary selection."""
    for args, name in ((["wl-paste", "--primary", "--no-newline"], "primary"),
                       (["wl-paste", "--no-newline"], "clipboard")):
        try:
            p = subprocess.run(args, capture_output=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0:
            text = p.stdout.decode("utf-8", "replace").strip()
            if text:
                return text, name
    return "", ""


# ---------------------------------------------------------------------------
# interpreter discovery
# ---------------------------------------------------------------------------
def _has_piper(python: Path) -> bool:
    try:
        r = subprocess.run([str(python), "-c", "import piper.voice"],
                           capture_output=True, timeout=25)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_python(cfg: dict) -> str:
    """Interpreter that can run tts_worker.py, or raise with a fixable message."""
    explicit = (cfg.get("python") or "").strip()
    if explicit:
        if not Path(explicit).exists():
            raise SpeakError(f"Configured interpreter does not exist:\n{explicit}")
        if not _has_piper(Path(explicit)):
            raise SpeakError(f"Piper is not installed in:\n{explicit}")
        return explicit
    for cand in CANDIDATE_PYTHONS:
        if cand.exists() and _has_piper(cand):
            return str(cand)
    raise SpeakError("No Python with Piper found.\nRun ./install.sh in ~/AI/coding-private/Sinread.")


def pick_player() -> list:
    for cmd in PLAYERS:
        if shutil.which(cmd[0]):
            return cmd
    raise SpeakError("No audio player found (pw-play, aplay or ffplay).")


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class Speaker:
    """Owns the Piper worker, the player, and the reading thread.

    Every public method is safe to call from any thread. `on_state(kind, **info)` is invoked
    from the reading thread — the GUI marshals it onto its own loop.
    """

    def __init__(self, cfg: dict, on_state=None):
        self.cfg = cfg
        self.on_state = on_state or (lambda kind, **info: None)

        self._proc: subprocess.Popen | None = None   # Piper worker
        self._player: subprocess.Popen | None = None
        self._lock = threading.RLock()               # guards process handles
        # Serialises whole reading sessions. Without it two hotkey presses landing inside
        # the same moment can each start a session, and the two players talk over each other.
        self._session_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = False
        self._speaking = False
        self._last_use = 0.0
        self._python: str | None = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    # -- state ---------------------------------------------------------------
    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def paused(self) -> bool:
        return self._paused

    def model_loaded(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def idle_seconds(self) -> float:
        return 0.0 if self._speaking or not self._last_use else time.time() - self._last_use

    def status(self) -> dict:
        return {
            "speaking": self._speaking,
            "paused": self._paused,
            "model_loaded": self.model_loaded(),
            "idle": round(self.idle_seconds(), 1),
            "unload_after": self.cfg["idle_seconds"],
        }

    # -- worker lifecycle ----------------------------------------------------
    def _spawn_worker(self) -> subprocess.Popen:
        python = self._python or find_python(self.cfg)
        self._python = python
        # Worker stderr goes to a real file, never devnull: a respawn loop has to stay
        # diagnosable (Nexus audit lesson).
        errlog = open(LOG_DIR / "worker.log", "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                [python, str(WORKER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                cwd=str(APP_DIR), bufsize=0)
        finally:
            errlog.close()          # the child holds its own dup of the fd
        kind, payload = _read_frame(proc.stdout)
        if kind != b"J" or json.loads(payload).get("event") != "ready":
            proc.kill()
            raise SpeakError("The Piper worker did not start.\n"
                             f"See {LOG_DIR / 'worker.log'}")
        log(f"worker up (pid {proc.pid}) via {python}")
        self.on_state("model", loaded=True)
        return proc

    def _ensure_worker(self) -> subprocess.Popen:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._spawn_worker()
            return self._proc

    def unload(self, reason: str = "idle") -> bool:
        """Kill the worker. Returns True if a model was actually resident.

        Killing rather than unloading in-process is the point: process death is the only
        thing that reliably returns the ONNX Runtime's arenas (and a CUDA context, on a GPU
        build) to the system.
        """
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return False
        log(f"unloading Piper ({reason})")
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
        self.on_state("model", loaded=False, reason=reason)
        return True

    def maybe_unload(self) -> bool:
        """Called by the daemon's watchdog — unload once the idle window has passed."""
        if self._speaking or not self.model_loaded():
            return False
        if self._last_use and (time.time() - self._last_use) >= self.cfg["idle_seconds"]:
            return self.unload(f"idle {self.cfg['idle_seconds']:.0f}s")
        return False

    # -- playback ------------------------------------------------------------
    def _start_player(self) -> subprocess.Popen:
        cmd = pick_player()
        # bufsize=0: writes go straight to the pipe, so pausing/killing the player can
        # never leave buffered audio behind to be flushed into a dead pipe at GC time.
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, bufsize=0)
        with self._lock:
            self._player = proc
        return proc

    def _kill_player(self) -> None:
        with self._lock:
            proc, self._player = self._player, None
        if proc is None:
            return
        try:
            if self._paused:
                proc.send_signal(signal.SIGCONT)   # a stopped process cannot flush or exit
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if proc.stdin:
                proc.stdin.close()   # explicit: the writer thread may still hold a memoryview
        except OSError:
            pass                     # EPIPE here is the normal case — the player is dead

    # -- public controls -----------------------------------------------------
    def speak(self, text: str) -> None:
        """Read `text` aloud, replacing whatever is currently being read."""
        with self._session_lock:
            self.stop()
            segs = textprep.segments(text[:self.cfg["max_chars"]], self.cfg["segment_chars"])
            if not segs:
                self.on_state("error", message="Nothing readable in that selection.")
                return
            self._stop.clear()
            self._paused = False
            self._speaking = True
            self._last_use = time.time()
            self._thread = threading.Thread(target=self._run, args=(segs,), daemon=True)
            self._thread.start()

    def speak_selection(self, should_cancel=None) -> None:
        """Grab the selection and read it. `should_cancel()` is polled around the grab —
        it can take up to two seconds, and a stop pressed inside that window has to win
        rather than be overtaken by the read it was cancelling."""
        cancelled = should_cancel or (lambda: False)
        text, source = grab_selection()
        if cancelled():
            # Announce it: the caller is showing a "Preparing…" spinner that only a
            # terminal state takes down.
            self.on_state("stopped", spoken=0, total=0)
            return
        if not text:
            self.on_state("error", message="No text selected.\n"
                                           "Highlight something, then press the hotkey.")
            return
        truncated = len(text) > self.cfg["max_chars"]
        log(f"{source}: {len(text)} chars{' (truncated)' if truncated else ''}")
        if cancelled():
            self.on_state("stopped", spoken=0, total=0)
            return
        self.on_state("grabbed", chars=len(text), source=source, truncated=truncated)
        self.speak(text)

    def stop(self) -> None:
        """Silence immediately and end the session. The model stays loaded."""
        if not self._speaking:
            return
        self._stop.set()
        self._kill_player()                 # unblocks the reading thread's write with EPIPE
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        self._speaking = False
        self._paused = False
        self._last_use = time.time()

    def toggle_pause(self) -> bool:
        """Pause/resume by stopping the player process: it stops draining its pipe, so the
        reader and the worker block behind it and the whole chain freezes in place."""
        with self._lock:
            proc = self._player
        if proc is None or proc.poll() is not None:
            return False
        try:
            if self._paused:
                proc.send_signal(signal.SIGCONT)
                self._paused = False
            else:
                proc.send_signal(signal.SIGSTOP)
                self._paused = True
        except OSError:
            return False
        self.on_state("paused" if self._paused else "resumed")
        return True

    def shutdown(self) -> None:
        self.stop()
        self.unload("shutting down")

    # -- the reading thread --------------------------------------------------
    def _run(self, segs: list) -> None:
        spoken = 0
        try:
            dominant, _ = langdetect.detect(" ".join(segs[:40]), self.cfg["default_lang"])
            self.on_state("start", total=len(segs), lang=dominant)

            # Spawning the worker and loading a voice takes a second or two. Say so —
            # a spinner that sits on "Preparing…" for that long reads as a hang.
            if not self.model_loaded():
                self.on_state("loading")
            proc = self._ensure_worker()
            player = self._start_player()

            for i, seg in enumerate(segs):
                if self._stop.is_set():
                    break
                lang = (langdetect.detect_sticky(seg, dominant, self.cfg["default_lang"])
                        if self.cfg["detect_per_segment"] else dominant)
                self.on_state("segment", index=i, total=len(segs), lang=lang, text=seg)

                req = {"cmd": "speak", "text": seg, "speed": float(self.cfg["speed"]),
                       "voice": config.voice_path(self.cfg, lang)}
                _write_all(proc.stdin, (json.dumps(req) + "\n").encode("utf-8"))

                if not self._pump(proc, player):
                    break
                spoken += 1

            if not self._stop.is_set():
                # Close the player's stdin and let it drain: killing it here would clip the
                # last word, which is exactly when it is most noticeable.
                try:
                    player.stdin.close()
                except OSError:
                    pass
                try:
                    player.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    pass
                self.on_state("done", spoken=spoken, total=len(segs))
            else:
                self.on_state("stopped", spoken=spoken, total=len(segs))

        except SpeakError as e:
            self.on_state("error", message=str(e))
        except Exception as e:  # noqa: BLE001
            log(f"reading failed: {e!r}")
            self.on_state("error", message=f"Reading failed: {e.__class__.__name__}: {e}")
        finally:
            self._kill_player()
            self._speaking = False
            self._paused = False
            self._last_use = time.time()

    def _pump(self, proc: subprocess.Popen, player: subprocess.Popen) -> bool:
        """Copy one segment's audio from the worker into the player.

        Returns False if the session should end. Always consumes frames up to the worker's
        terminating 'J' event — leaving audio in the pipe would desynchronise the next read.
        """
        dropping = False
        while True:
            kind, payload = _read_frame(proc.stdout)
            if kind is None:
                raise SpeakError("The Piper worker exited unexpectedly.\n"
                                 f"See {LOG_DIR / 'worker.log'}")
            if kind == b"J":
                msg = json.loads(payload)
                if msg.get("event") == "error":
                    # One unpronounceable segment must not end the reading: skip it.
                    log(f"worker error: {msg.get('message')}")
                    return not self._stop.is_set()
                return not (dropping or self._stop.is_set())

            if dropping or self._stop.is_set():
                dropping = True          # keep reading to the 'done' frame, but play nothing
                continue
            try:
                _write_all(player.stdin, payload)
            except (BrokenPipeError, ValueError, OSError):
                # The player was killed (stop pressed) or died; drain, then end the session.
                dropping = True


def _write_all(stream, data: bytes) -> None:
    """Write every byte. An unbuffered pipe may accept only part of a large chunk, and a
    short write here would silently drop audio out of the middle of a sentence."""
    view = memoryview(data)
    while view:
        n = stream.write(view)
        if n:
            view = view[n:]


def _read_frame(stream) -> tuple:
    """Read one worker frame -> (b'A'|b'J', payload), or (None, b'') at EOF."""
    header = _read_exact(stream, 9)
    if header is None:
        return None, b""
    kind, length = header[:1], int(header[1:9], 16)
    payload = _read_exact(stream, length) if length else b""
    if payload is None:
        return None, b""
    return kind, payload


def _read_exact(stream, n: int) -> bytes | None:
    """Pipes hand back short reads; loop until n bytes or EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


if __name__ == "__main__":
    cfg = config.load_config()
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else grab_selection()[0]
    if not text:
        print("nothing selected and no text given", file=sys.stderr)
        raise SystemExit(1)
    sp = Speaker(cfg, on_state=lambda kind, **i: log(f"{kind} {i.get('lang','')} "
                                                     f"{str(i.get('text',''))[:60]}"))
    sp.speak(text)
    while sp.speaking:
        time.sleep(0.1)
    print(json.dumps(sp.status(), indent=2))
    sp.shutdown()
