#!/usr/bin/env python3
"""Sinread's main window — live status and settings.

Not shown on each read: closing it only hides it, the app stays resident and the hotkey keeps
working. The Status tab exists mainly to make the model's lifetime visible — you can watch
the unload countdown run out and the memory come back.
"""
import tkinter as tk
from tkinter import ttk

import hotkey
from config import available_voices
from theme import ACCENT, ACCENT2, BG, CARD, FG, OKC, REC, SUBTLE, Theme

# tkinter event.state bits -> GNOME accelerator modifiers (Mod2 = NumLock etc. are ignored)
_MOD_BITS = ((0x0004, "<Ctrl>"), (0x0001, "<Shift>"), (0x0008, "<Alt>"), (0x0040, "<Super>"))
_PURE_MODIFIERS = {
    "Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Super_L", "Super_R",
    "Meta_L", "Meta_R", "Hyper_L", "Hyper_R", "ISO_Level3_Shift", "Caps_Lock", "Num_Lock",
}

SAMPLE = {
    "de": "Dies ist eine Probe der deutschen Stimme bei dieser Geschwindigkeit.",
    "en": "This is a sample of the English voice at this speed.",
}


def confirm(parent, th: Theme, title: str, message: str,
            ok_text: str = "Use it anyway", cancel_text: str = "Pick another") -> bool:
    """Small themed modal — a raw Tk messagebox would look foreign next to the rest."""
    S = th.S
    top = parent.winfo_toplevel()
    dlg = tk.Toplevel(top)
    dlg.title(title)
    dlg.configure(bg=BG)
    dlg.transient(top)
    dlg.resizable(False, False)

    frm = ttk.Frame(dlg, padding=(S(22), S(18)))
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=title, style="H1.TLabel").pack(anchor="w")
    ttk.Label(frm, text=message, style="Sub.TLabel", justify="left",
              wraplength=S(400)).pack(anchor="w", pady=(S(10), S(18)))

    result = {"ok": False}
    btns = ttk.Frame(frm)
    btns.pack(anchor="e")

    def accept():
        result["ok"] = True
        dlg.destroy()

    ttk.Button(btns, text=cancel_text, command=dlg.destroy).pack(side="left", padx=(0, S(8)))
    ttk.Button(btns, text=ok_text, style="Accent.TButton", command=accept).pack(side="left")

    dlg.update_idletasks()
    x = top.winfo_rootx() + (top.winfo_width() - dlg.winfo_width()) // 2
    y = top.winfo_rooty() + (top.winfo_height() - dlg.winfo_height()) // 3
    dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    dlg.grab_set()
    dlg.wait_window()
    return result["ok"]


class HotkeyCapture(ttk.Frame):
    """Click, press any key or combination, and it becomes the GNOME shortcut.

    A single key (F8, Pause, Insert …) is just as valid as a combination — no modifier is
    required. Keys that normal typing depends on still work, but ask for confirmation first.
    """

    def __init__(self, master, th: Theme, initial: str, on_change):
        super().__init__(master)
        self.th, self.on_change = th, on_change
        self.value = initial
        self.capturing = False
        self.var = tk.StringVar(value=hotkey.pretty(initial))
        self.entry = ttk.Entry(self, textvariable=self.var, state="readonly",
                               width=24, font=th.F_UIB, justify="center")
        self.entry.pack(side="left", ipady=th.S(2))
        self.btn = ttk.Button(self, text="Change…", command=self.start)
        self.btn.pack(side="left", padx=(th.S(8), 0))
        self.hint = ttk.Label(self, text="", style="Sub.TLabel")
        self.hint.pack(side="left", padx=(th.S(10), 0))

    def start(self):
        if self.capturing:
            return
        self.capturing = True
        self.var.set("Press a key…")
        self.hint.configure(text="single key or combination · Esc cancels", foreground=SUBTLE)
        self.btn.state(["disabled"])
        self.entry.focus_set()
        self.entry.grab_set()
        self.entry.bind("<KeyPress>", self._on_key)

    def _teardown(self):
        """Release the keyboard before anything else can open a window."""
        self.entry.grab_release()
        self.entry.unbind("<KeyPress>")
        self.btn.state(["!disabled"])
        self.capturing = False

    def _report(self, value: str | None, message: str = "", colour: str = SUBTLE):
        if value:
            self.value = value
        self.var.set(hotkey.pretty(self.value))
        self.hint.configure(text=message, foreground=colour)
        if message:
            self.after(4000, lambda: self.hint.configure(text=""))

    def _on_key(self, event):
        keysym = event.keysym
        if keysym == "Escape":
            self._teardown()
            self._report(None, "cancelled")
            return "break"
        if keysym in _PURE_MODIFIERS:
            return "break"                      # a modifier alone cannot be a shortcut

        mods = [name for bit, name in _MOD_BITS if event.state & bit]
        key = keysym.lower() if len(keysym) == 1 and keysym.isalpha() else keysym
        combo = "".join(mods) + key

        self._teardown()

        clash = hotkey.conflict(combo)
        if clash:
            self._report(None, f"already used by {clash}", REC)
            return "break"

        warning = hotkey.risky(combo)
        if warning and not confirm(
                self, self.th, "Use a typing key as the hotkey?",
                f"{warning}. If you bind it, pressing it will read the selection instead of "
                f"typing — in every app, not just this one.\n\n"
                f"Keys like F8, Pause, Insert or Menu are safer single-key choices."):
            self._report(None, "cancelled")
            return "break"

        try:
            hotkey.set_binding_only(combo)
        except Exception as e:  # noqa: BLE001
            self._report(None, f"could not set: {e}", REC)
            return "break"
        self._report(combo, "saved", OKC)
        self.on_change(combo)
        return "break"


class StatusTab(ttk.Frame):
    """What Sinread is doing, and what the model is costing right now."""

    def __init__(self, master, th: Theme, app):
        super().__init__(master, padding=(th.S(22), th.S(18)))
        self.th, self.app = th, app
        S = th.S

        self.state_lbl = ttk.Label(self, text="Idle", style="H1.TLabel")
        self.state_lbl.grid(row=0, column=0, columnspan=3, sticky="w")

        self.detail_lbl = ttk.Label(self, text="", style="Sub.TLabel", justify="left",
                                    wraplength=S(620))
        self.detail_lbl.grid(row=1, column=0, columnspan=3, sticky="w", pady=(S(4), S(12)))

        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1000,
                                   style="TProgressbar")
        self.bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, S(18)))

        ttk.Label(self, text="Model", style="H1.TLabel").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(0, S(6)))
        self.model_lbl = ttk.Label(self, text="", style="Sub.TLabel", justify="left",
                                   wraplength=S(620))
        self.model_lbl.grid(row=4, column=0, columnspan=3, sticky="w")

        btns = ttk.Frame(self)
        btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(S(16), 0))
        self.read_btn = ttk.Button(btns, text="▶  Read selection", style="Accent.TButton",
                                   command=app.on_trigger)
        self.read_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="■  Stop", command=app.on_stop)
        self.stop_btn.pack(side="left", padx=(S(8), 0))
        self.pause_btn = ttk.Button(btns, text="❚❚  Pause", command=app.on_pause)
        self.pause_btn.pack(side="left", padx=(S(8), 0))
        self.unload_btn = ttk.Button(btns, text="Unload model now", command=app.on_unload)
        self.unload_btn.pack(side="left", padx=(S(20), 0))

        self.columnconfigure(2, weight=1)

    def refresh(self, st: dict, progress: tuple, lang: str, sentence: str):
        S = self.th.S
        index, total = progress
        if st["speaking"]:
            if st["paused"]:
                self.state_lbl.configure(text="Paused", foreground=SUBTLE)
            else:
                self.state_lbl.configure(text="Reading", foreground=ACCENT2)
            self.detail_lbl.configure(
                text=f"{('Deutsch' if lang == 'de' else 'English')}  ·  sentence "
                     f"{index + 1} of {total}\n{sentence[:220]}")
            self.bar.configure(value=int(1000 * (index + 1) / max(1, total)))
        else:
            self.state_lbl.configure(text="Idle", foreground=FG)
            self.detail_lbl.configure(
                text=f"Select text anywhere, then press "
                     f"{hotkey.pretty(self.app.cfg['hotkey'])}.")
            self.bar.configure(value=0)

        if st["model_loaded"]:
            if st["speaking"]:
                self.model_lbl.configure(
                    text="Piper is loaded and speaking.  ~450 MB RAM, 0 MB VRAM.",
                    foreground=OKC)
            else:
                left = max(0.0, st["unload_after"] - st["idle"])
                self.model_lbl.configure(
                    text=f"Piper is loaded and idle — unloading in {left:.0f}s.\n"
                         f"~450 MB RAM, 0 MB VRAM (Piper runs on the CPU).",
                    foreground=SUBTLE)
        else:
            self.model_lbl.configure(
                text="Not loaded — nothing resident. It loads again on the next read "
                     "(about half a second).",
                foreground=SUBTLE)

        self.stop_btn.state(["!disabled"] if st["speaking"] else ["disabled"])
        self.pause_btn.state(["!disabled"] if st["speaking"] else ["disabled"])
        self.pause_btn.configure(text="▶  Resume" if st["paused"] else "❚❚  Pause")
        self.unload_btn.state(["!disabled"] if st["model_loaded"] and not st["speaking"]
                              else ["disabled"])


class SettingsTab(ttk.Frame):
    def __init__(self, master, th: Theme, app):
        super().__init__(master, padding=(th.S(22), th.S(18)))
        self.th, self.app = th, app
        S, cfg = th.S, app.cfg
        row = 0

        def section(text):
            nonlocal row
            ttk.Label(self, text=text, style="H1.TLabel").grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(S(14) if row else 0, S(6)))
            row += 1

        def hint(text):
            nonlocal row
            ttk.Label(self, text=text, style="Sub.TLabel", justify="left").grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(0, S(8)))
            row += 1

        section("Hotkey")
        hint("Select text in any app, then press this to read it aloud.\n"
             "Press it again while reading to stop. A single key works — F8, Pause, "
             "Insert and Menu are free ones.")
        self.capture = HotkeyCapture(self, th, cfg["hotkey"], self._on_hotkey)
        self.capture.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, S(4)))
        row += 1

        section("Speed")
        self.speed = tk.DoubleVar(value=cfg["speed"])
        scale = ttk.Scale(self, from_=0.7, to=2.0, orient="horizontal",
                          variable=self.speed, command=self._on_speed_move,
                          style="Horizontal.TScale", length=S(300))
        scale.grid(row=row, column=0, columnspan=2, sticky="w")
        self.speed_lbl = ttk.Label(self, text="", style="CardAccent.TLabel")
        self.speed_lbl.grid(row=row, column=2, sticky="w", padx=(S(12), 0))
        scale.bind("<ButtonRelease-1>", lambda _e: self._on_speed_commit())
        row += 1
        prev = ttk.Frame(self)
        prev.grid(row=row, column=0, columnspan=3, sticky="w", pady=(S(8), 0))
        ttk.Button(prev, text="🔊  Test German",
                   command=lambda: self._preview("de")).pack(side="left")
        ttk.Button(prev, text="🔊  Test English",
                   command=lambda: self._preview("en")).pack(side="left", padx=(S(8), 0))
        ttk.Label(prev, text="  1.0 is the voice's own pace", style="Sub.TLabel").pack(
            side="left", padx=(S(10), 0))
        row += 1

        section("Voices")
        hint("The language is detected from the text itself — you never pick it by hand.")
        voices = available_voices()
        self.voice_vars = {}
        for lang, label in (("de", "German"), ("en", "English")):
            options = voices.get(lang) or []
            names = [n for n, _ in options]
            self.paths = getattr(self, "paths", {})
            self.paths.update({n: p for n, p in options})
            current = next((n for n, p in options if p == self.app.voice_path(lang)),
                           names[0] if names else "")
            var = tk.StringVar(value=current)
            self.voice_vars[lang] = var
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=S(4))
            cb = ttk.Combobox(self, textvariable=var, values=names, state="readonly",
                              width=26)
            cb.grid(row=row, column=1, sticky="w", padx=(S(10), 0))
            cb.bind("<<ComboboxSelected>>", lambda _e, l=lang: self._on_voice(l))
            row += 1

        self.fallback = tk.StringVar(value="German" if cfg["default_lang"] == "de" else "English")
        ttk.Label(self, text="If unsure, read as").grid(row=row, column=0, sticky="w", pady=S(4))
        cbf = ttk.Combobox(self, textvariable=self.fallback, values=["German", "English"],
                           state="readonly", width=26)
        cbf.grid(row=row, column=1, sticky="w", padx=(S(10), 0))
        cbf.bind("<<ComboboxSelected>>", lambda _e: self._on_fallback())
        ttk.Label(self, text="for selections too short to tell apart",
                  style="Sub.TLabel").grid(row=row, column=2, sticky="w", padx=(S(8), 0))
        row += 1

        self.per_seg = tk.BooleanVar(value=cfg["detect_per_segment"])
        ttk.Checkbutton(self, text="Switch voice mid-text when the language changes",
                        variable=self.per_seg, command=self._on_per_seg).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(S(8), 0))
        row += 1

        section("Memory")
        hint("When you stop using it, the model is unloaded by killing its process — that is\n"
             "what actually returns the memory. Piper runs on the CPU, so it holds no VRAM\n"
             "at any point; your GPU stays free for other models the whole time.")
        self.idle = tk.DoubleVar(value=cfg["idle_seconds"])
        ttk.Label(self, text="Unload after").grid(row=row, column=0, sticky="w", pady=S(4))
        sp = ttk.Spinbox(self, from_=10, to=3600, increment=30, width=8,
                         textvariable=self.idle, command=self._on_idle)
        sp.grid(row=row, column=1, sticky="w", padx=(S(10), 0))
        sp.bind("<FocusOut>", lambda _e: self._on_idle())
        sp.bind("<Return>", lambda _e: self._on_idle())
        ttk.Label(self, text="seconds of not reading", style="Sub.TLabel").grid(
            row=row, column=2, sticky="w", padx=(S(8), 0))
        row += 1

        self.columnconfigure(2, weight=1)
        self._refresh_speed_label()

    # -- handlers ------------------------------------------------------------
    def _refresh_speed_label(self):
        self.speed_lbl.configure(text=f"{self.speed.get():.2f}×")

    def _on_speed_move(self, _v):
        self._refresh_speed_label()

    def _on_speed_commit(self):
        self.app.cfg["speed"] = round(float(self.speed.get()), 2)
        self._refresh_speed_label()
        self.app.persist(f"speed {self.app.cfg['speed']:.2f}×")

    def _preview(self, lang: str):
        self.app.cfg["speed"] = round(float(self.speed.get()), 2)
        self.app.preview(SAMPLE[lang])

    def _on_voice(self, lang: str):
        name = self.voice_vars[lang].get()
        path = self.paths.get(name)
        if path:
            self.app.cfg[f"voice_{lang}"] = path
            self.app.persist(f"{lang} voice: {name}")

    def _on_fallback(self):
        self.app.cfg["default_lang"] = "de" if self.fallback.get() == "German" else "en"
        self.app.persist("fallback language saved")

    def _on_per_seg(self):
        self.app.cfg["detect_per_segment"] = bool(self.per_seg.get())
        self.app.persist("language switching saved")

    def _on_idle(self):
        try:
            v = float(self.idle.get())
        except (tk.TclError, ValueError):
            return
        self.app.cfg["idle_seconds"] = max(10.0, min(3600.0, v))
        self.app.persist(f"unload after {self.app.cfg['idle_seconds']:.0f}s")

    def _on_hotkey(self, combo: str):
        self.app.cfg["hotkey"] = combo
        self.app.persist(f"hotkey set to {combo}")


class MainWindow:
    def __init__(self, root: tk.Tk, th: Theme, app):
        self.th, self.app = th, app
        S = th.S
        w = tk.Toplevel(root)
        w.title("Sinread")
        w.configure(bg=BG)
        w.geometry(f"{S(760)}x{S(600)}")
        w.minsize(S(560), S(420))
        # Closing hides the window; the app stays resident so the hotkey keeps working.
        w.protocol("WM_DELETE_WINDOW", self.hide)
        w.withdraw()
        self.win = w

        header = ttk.Frame(w, padding=(S(18), S(14), S(18), S(4)))
        header.pack(fill="x")
        ttk.Label(header, text="Sinread", style="H1.TLabel").pack(side="left")
        ttk.Label(header, text="  reads your selection aloud", style="Sub.TLabel").pack(
            side="left", padx=(S(6), 0))
        self.hotkey_lbl = ttk.Label(header, style="Sub.TLabel")
        self.hotkey_lbl.pack(side="right")

        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=S(10), pady=(0, S(6)))
        self.status_tab = StatusTab(nb, th, app)
        self.settings = SettingsTab(nb, th, app)
        nb.add(self.status_tab, text="Status")
        nb.add(self.settings, text="Settings")

        bar = ttk.Frame(w, padding=(S(18), S(6), S(18), S(12)))
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="", style="Sub.TLabel")
        self.status.pack(side="left")
        ttk.Button(bar, text="Quit Sinread", command=app.shutdown).pack(side="right")
        ttk.Button(bar, text="Hide", command=self.hide).pack(side="right", padx=(0, S(8)))
        self.refresh_hotkey()

    def refresh_hotkey(self):
        self.hotkey_lbl.configure(text=f"hotkey  {hotkey.pretty(self.app.cfg['hotkey'])}")

    def set_status(self, text: str, colour: str = SUBTLE):
        self.status.configure(text=text, foreground=colour)
        if text:
            self.status.after(3000, lambda: self.status.configure(text=""))

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def hide(self):
        self.win.withdraw()
