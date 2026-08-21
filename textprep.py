#!/usr/bin/env python3
"""Turn a raw selection into speakable segments.

Selections are messy: PDF columns hyphenate across line breaks, web copy drags in markdown
and bare URLs, and a wall of text with no boundaries makes stopping feel unresponsive. This
module normalises the text, then cuts it into sentence-sized pieces so synthesis streams
and a stop lands within a sentence rather than at the end of the selection.

Test from a shell:
    python3 textprep.py "Das ist ein Test. Siehe https://example.com/a/b?c=1"
"""
import re
import sys

# Abbreviations whose trailing period is NOT a sentence end. Kept lowercase; matched against
# the lowercased token before the period.
ABBREV = {
    # German
    "z.b", "d.h", "u.a", "u.ä", "o.ä", "bzw", "ca", "evtl", "ggf", "inkl", "exkl", "vgl",
    "usw", "usf", "bspw", "abb", "nr", "str", "tel", "dr", "prof", "hr", "fr", "sog", "bes",
    "max", "min", "mind", "ehem", "urspr", "jhd", "jh", "mio", "mrd", "zzgl", "einschl",
    # English
    "mr", "mrs", "ms", "st", "jr", "sr", "vs", "e.g", "i.e", "etc", "fig", "no", "approx",
    "dept", "est", "al", "inc", "ltd", "co", "univ", "cf", "pp",
}

_MD_FENCE = re.compile(r"```.*?```", re.S)
_MD_INLINE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")          # [label](url) -> label
_MD_EMPH = re.compile(r"(\*{1,3}|_{1,3})(\S.*?\S|\S)\1")  # **bold**/_em_ -> inner text
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?")
_MD_BULLET = re.compile(r"^\s{0,3}[-*+]\s+")
# Numbered list items open their own block too, but the number is KEPT — "1." is meaningful
# when read aloud, and stripping it would also swallow a paragraph opening with a German
# ordinal date.
_MD_NUM = re.compile(r"^\s{0,3}\d{1,2}[.)]\s+")
_MD_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", re.M)

_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')\]]+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
# A line-break hyphenation: "Kon-\ntext" -> "Kontext". Only when the next line starts
# lowercase, so a real compound at a line end ("Nord-\nSüd") survives intact.
_HYPHEN_BREAK = re.compile(r"(\w)[-‐­]\s*\n\s*([a-zäöüß])")
_SOFT_HYPHEN = re.compile(r"­")


def _blocks(t: str) -> str:
    """Regroup lines into paragraphs separated by a blank line.

    A blank line is a paragraph break. Single newlines inside a paragraph are only wrapping
    and become spaces — but a heading or a list item is its OWN paragraph, otherwise a
    bullet list with no full stops is read as one endless run-on sentence.
    """
    out, cur = [], []

    def flush():
        if cur:
            out.append(" ".join(cur).strip())
            cur.clear()

    for line in t.split("\n"):
        if not line.strip():
            flush()
            continue
        own_block = bool(_MD_HEAD.match(line) or _MD_BULLET.match(line) or _MD_NUM.match(line))
        text = _MD_QUOTE.sub("", line)
        text = _MD_HEAD.sub("", text)
        text = _MD_BULLET.sub("", text)          # _MD_NUM is deliberately not stripped
        text = text.strip()
        if not text:
            continue
        if own_block:
            flush()
            # Headings and bullets rarely carry a full stop; without one Piper runs them
            # into the next block with no pause and a flat, unfinished intonation.
            out.append(text if text[-1] in ".!?:;,…" else text + ".")
        else:
            cur.append(text)
    flush()
    return "\n\n".join(b for b in out if b.strip())


def _say_url(m: re.Match) -> str:
    """A read-aloud URL is unbearable; the host is the part a listener actually wants."""
    url = m.group(0)
    host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].lstrip("www.")
    return f"(Link zu {host})" if host else "(Link)"


def clean(text: str) -> str:
    """Normalise a raw selection into plain prose."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _SOFT_HYPHEN.sub("", t)
    t = _HYPHEN_BREAK.sub(r"\1\2", t)

    t = _MD_FENCE.sub(" ", t)
    t = _MD_RULE.sub(" ", t)
    t = _blocks(t)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_INLINE.sub(r"\1", t)
    t = _MD_EMPH.sub(r"\2", t)

    t = _EMAIL.sub(lambda m: f"(E-Mail-Adresse bei {m.group(1)})", t)
    t = _URL.sub(_say_url, t)

    # Typography Piper reads better spelled out as plain punctuation.
    for a, b in (("—", " — "), ("–", " – "), ("…", "..."),
                 ("“", '"'), ("”", '"'), ("„", '"'), ("‘", "'"),
                 ("’", "'"), (" ", " "), ("​", "")):
        t = t.replace(a, b)

    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _ends_sentence(chunk: str) -> bool:
    """Does `chunk` end at a real sentence boundary?"""
    s = chunk.rstrip()
    if not s or s[-1] not in ".!?":
        return s.endswith(("”", '"')) and s[:-1].rstrip().endswith((".", "!", "?"))
    if s[-1] in "!?":
        return True
    # A trailing period: rule out abbreviations, initials and ordinals ("am 3. Januar").
    tail = re.split(r"[\s(\[]", s[:-1])[-1].lower()
    if tail in ABBREV:
        return False
    if len(tail) == 1 and tail.isalpha():        # "J. R. R. Tolkien"
        return False
    if tail.isdigit():                            # German ordinal
        return False
    return True


def _sentences(paragraph: str):
    """Split a paragraph on real sentence ends."""
    parts = re.split(r"(?<=[.!?”\"])\s+", paragraph)
    buf = ""
    for part in parts:
        buf = f"{buf} {part}".strip() if buf else part
        if _ends_sentence(buf):
            yield buf
            buf = ""
    if buf.strip():
        yield buf.strip()


def _split_long(sentence: str, limit: int):
    """Break an over-long sentence at the best punctuation available, then hard-wrap."""
    if len(sentence) <= limit:
        return [sentence]
    for pattern in (r"(?<=[;:])\s+", r"(?<=,)\s+", r"\s+(?=(?:und|oder|aber|denn|weil|dass|and|or|but|because|which|that)\b)"):
        pieces, buf, out = re.split(pattern, sentence), "", []
        if len(pieces) < 2:
            continue
        for p in pieces:
            cand = f"{buf} {p}".strip() if buf else p
            if buf and len(cand) > limit:
                out.append(buf)
                buf = p
            else:
                buf = cand
        if buf:
            out.append(buf)
        if all(len(o) <= limit for o in out):
            return out
        sentence_parts = out
        break
    else:
        sentence_parts = [sentence]

    # Still too long (one endless clause) — wrap on word boundaries so nothing is dropped.
    final, buf = [], ""
    for piece in sentence_parts:
        for word in piece.split(" "):
            cand = f"{buf} {word}".strip() if buf else word
            if buf and len(cand) > limit:
                final.append(buf)
                buf = word
            else:
                buf = cand
        if buf:
            final.append(buf)
            buf = ""
    return [f for f in final if f]


def segments(text: str, limit: int = 350) -> list[str]:
    """Cleaned selection -> the list of chunks handed to Piper, in order."""
    out = []
    for paragraph in clean(text).split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for sentence in _sentences(paragraph):
            sentence = sentence.strip()
            if sentence:
                out.extend(_split_long(sentence, limit))
    # Drop pieces with nothing pronounceable (a lone "---" or "•") — Piper emits silence
    # for them and they only add gaps.
    return [s for s in out if re.search(r"[^\W\d_]", s, re.UNICODE)]


if __name__ == "__main__":
    src = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    for i, seg in enumerate(segments(src), 1):
        print(f"{i:3d} [{len(seg):3d}] {seg}")
