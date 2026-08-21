#!/usr/bin/env python3
"""The reading pill — right edge, vertically centred, follows the read and then fades out.

One persistent Toplevel that is re-filled as the read progresses, so a new hotkey press
replaces the pill instead of stacking a second window.

Borderless and non-focus-stealing via X11 override-redirect (through XWayland), the same
technique local-wisprflow's listening pill and Sinlate's popup use — the text you selected
keeps its selection and its window keeps focus while Sinread reads.

Standalone demo:  python3 popup.py
"""
import time
import tkinter as tk

from theme import (ACCENT, ACCENT2, BG, BORDER, CARD, CARD_HI, FG, FPS, OKC, REC, SUBTLE,
                   Theme, apply_ttk_style, round_rect)

LANG_LABEL = {"de": "Deutsch", "en": "English"}
MAX_BODY_CHARS = 400           # the sentence being spoken, not the whole selection


class ReadingPopup:
    def __init__(self, root: tk.Tk, th: Theme, cfg: dict):
        self.root, self.th, self.cfg = root, th, cfg
        w = tk.Toplevel(root)
        w.withdraw()               # stay hidden until fully configured -> never flash
        w.overrideredirect(True)   # borderless AND never takes focus
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.98)
        except tk.TclError:
            pass
        w.configure(bg=BG)
        self.win = w
        self.cv = tk.Canvas(w, bg=BG, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Button-1>", lambda _e: self.hide())
        self.cv.bind("<Enter>", self._on_enter)
        self.cv.bind("<Leave>", self._on_leave)
        self._hide_after = None
        self._spin_after = None
        self._hoverable = False

    # -- timers ---------------------------------------------------------------
    def _cancel_hide(self):
        if self._hide_after is not None:
            try:
                self.root.after_cancel(self._hide_after)
            except Exception:  # noqa: BLE001
                pass
            self._hide_after = None

    def _arm_hide(self, seconds: float):
        self._cancel_hide()
        self._hide_after = self.root.after(int(seconds * 1000), self.hide)

    def _stop_spin(self):
        if self._spin_after is not None:
            try:
                self.root.after_cancel(self._spin_after)
            except Exception:  # noqa: BLE001
                pass
            self._spin_after = None

    def _on_enter(self, _e):
        if self._hoverable:
            self._cancel_hide()

    def _on_leave(self, _e):
        if self._hoverable:
            self._arm_hide(1.5)

    # -- geometry -------------------------------------------------------------
    def _place(self, w: int, h: int):
        th = self.th
        th.refresh_monitor()       # monitor layout can change mid-session
        x = th.OX + th.MW - w - th.S(24)
        y = th.OY + (th.MH - h) // 2
        self.cv.configure(width=w, height=h)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _card(self, w: int, h: int):
        S = self.th.S
        pad, r, lw = S(2), S(14), max(1, S(1))
        round_rect(self.cv, pad, pad, w - pad, h - pad, r,
                   fill=CARD, outline=BORDER, width=lw)
        self.cv.create_line(pad + r, pad + lw, w - pad - r, pad + lw, fill=CARD_HI, width=lw)

    def _measure(self, text: str, wrap: int, font) -> int:
        probe = self.cv.create_text(0, 0, text=text, anchor="nw", width=wrap, font=font)
        bb = self.cv.bbox(probe)
        self.cv.delete(probe)
        return (bb[3] - bb[1]) if bb else 0

    def _show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)   # re-assert; some WMs drop it across withdraw

    # -- states ---------------------------------------------------------------
    def show_busy(self, label: str = "Preparing…"):
        """Spinner for the gap before the first syllable — the ~0.5 s Piper cold load, or
        a little longer when the worker itself has to be respawned after an idle unload."""
        S, th = self.th.S, self.th
        self._stop_spin()
        self._cancel_hide()
        self._hoverable = False
        self.cv.delete("all")
        w, h = S(250), S(64)
        self._place(w, h)
        self._card(w, h)
        cx, cy, rr = S(32), h // 2, S(11)
        self.cv.create_text(S(56), cy, text=label, anchor="w", fill=FG, font=th.F_TITLE)
        t0 = time.time()

        def spin():
            self.cv.delete("spin")
            ang = (time.time() - t0) * 320.0 % 360
            self.cv.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=ang, extent=270,
                               style="arc", outline=ACCENT, width=max(2, S(2)), tags="spin")
            self._spin_after = self.root.after(int(1000 / FPS), spin)

        spin()
        self._show()
        # Safety net: never leave a spinner on screen if the worker never reports back.
        self._hide_after = self.root.after(60000, self.hide)

    def show_reading(self, index: int, total: int, lang: str, sentence: str,
                     hotkey: str, paused: bool = False):
        head = f"{'Paused' if paused else 'Reading'}  ·  {LANG_LABEL.get(lang, lang)}"
        self._render(head, SUBTLE if paused else ACCENT2, sentence,
                     SUBTLE if paused else FG,
                     progress=((index + 1) / total if total else 0.0),
                     footer=f"{index + 1} / {total}   ·   {hotkey} = "
                            f"{'resume' if paused else 'stop'}")
        self._hoverable = False
        self._cancel_hide()        # a read stays on screen for as long as it is speaking

    def show_done(self, spoken: int, total: int):
        note = "Finished" if spoken >= total else f"Stopped  ·  {spoken} of {total}"
        self._render(note, OKC if spoken >= total else SUBTLE, "", FG, progress=1.0)
        self._arm_hide(min(2.5, self.cfg["popup_seconds"]))

    def show_error(self, message: str):
        self._render("⚠  Sinread", REC, message, SUBTLE)
        self._arm_hide(4.0)

    def show_note(self, message: str, color: str = SUBTLE):
        """Small transient line — used for 'model unloaded, VRAM free'."""
        self._render("Sinread", color, message, SUBTLE)
        self._arm_hide(2.5)

    def _render(self, header: str, header_color: str, body: str, body_color: str,
                progress: float | None = None, footer: str = ""):
        S, th = self.th.S, self.th
        self._stop_spin()
        self.cv.delete("all")
        th.refresh_monitor()

        w = S(380)
        pad = S(15)
        wrap = w - 2 * pad
        head_y = pad
        body_y = pad + S(22)
        max_h = int(th.MH * 0.5)

        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rstrip() + "…"
        body_h = self._measure(body, wrap, th.F_BODY) if body else 0
        while body and body_y + body_h + pad > max_h and len(body) > 120:
            body = body[:int(len(body) * 0.8)].rstrip() + "…"
            body_h = self._measure(body, wrap, th.F_BODY)

        bottom = body_y + body_h + (S(6) if body else 0)
        bar_y = bottom + S(6) if progress is not None else bottom
        foot_y = bar_y + (S(12) if progress is not None else 0)
        h = max(S(58), min(max_h, foot_y + (S(16) if footer else 0) + pad // 2))

        self._place(w, h)
        self._card(w, h)
        self.cv.create_text(pad, head_y, text=header, anchor="nw",
                            fill=header_color, font=th.F_SUB)
        if body:
            self.cv.create_text(pad, body_y, text=body, anchor="nw", width=wrap,
                                fill=body_color, font=th.F_BODY)
        if progress is not None:
            bh = max(2, S(3))
            self.cv.create_rectangle(pad, bar_y, w - pad, bar_y + bh,
                                     fill=BG, outline="")
            filled = pad + int((w - 2 * pad) * max(0.0, min(1.0, progress)))
            if filled > pad:
                self.cv.create_rectangle(pad, bar_y, filled, bar_y + bh,
                                         fill=ACCENT, outline="")
        if footer:
            self.cv.create_text(pad, foot_y, text=footer, anchor="nw",
                                fill=SUBTLE, font=th.F_SUB)
        self._show()

    def hide(self):
        self._stop_spin()
        self._cancel_hide()
        self._hoverable = False
        self.win.withdraw()


if __name__ == "__main__":
    from config import load_config

    root = tk.Tk()
    root.withdraw()
    th = Theme(root)
    apply_ttk_style(root, th)
    cfg = load_config()
    p = ReadingPopup(root, th, cfg)

    SENTS = [("de", "Guten Morgen, dies ist der erste Satz dieser Vorlesung."),
             ("de", "Der zweite Satz ist etwas länger und beschreibt, was gerade passiert."),
             ("en", "And this sentence is English, so the voice switches over."),
             ("de", "Zum Schluss geht es wieder auf Deutsch weiter.")]
    demo = [(0, lambda: p.show_busy())]
    for i, (lang, s) in enumerate(SENTS):
        demo.append((1500 + i * 1800,
                     lambda i=i, lang=lang, s=s: p.show_reading(i, len(SENTS), lang, s, "F8")))
    demo += [
        (1500 + len(SENTS) * 1800, lambda: p.show_reading(2, len(SENTS), "en", SENTS[2][1],
                                                          "F8", paused=True)),
        (1500 + len(SENTS) * 1800 + 1800, lambda: p.show_done(len(SENTS), len(SENTS))),
        (1500 + len(SENTS) * 1800 + 4200, lambda: p.show_note("Model unloaded — VRAM free")),
        (1500 + len(SENTS) * 1800 + 7000, lambda: p.show_error("No text selected.\n"
                                                               "Highlight something, then press F8.")),
        (1500 + len(SENTS) * 1800 + 11000, root.destroy),
    ]
    for ms, fn in demo:
        root.after(ms, fn)
    print("popup demo: spinner -> reading -> paused -> done -> note -> error (~20s)")
    root.mainloop()
