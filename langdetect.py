#!/usr/bin/env python3
"""German / English detection for the text Sinread is about to read aloud.

Deliberately a stopword-and-orthography scorer, not a model: it decides in microseconds
with no load cost, and the only question it has to answer is "de or en?" — a two-way call
that function words settle almost perfectly. Anything heavier would put a model load in
front of the very first syllable.

Test from a shell:
    python3 langdetect.py "Wo ist der Bahnhof?"
"""
import re
import sys

# Function words are the signal: they are the most frequent tokens in any real sentence and
# they barely overlap between the two languages. Words that exist in BOTH (e.g. "in", "so",
# "was", "man", "die"/"die", "hat"/"hat") are left out on purpose — a shared token adds
# noise to both sides and can only blur the decision.
GERMAN = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer",
    "eines", "und", "oder", "aber", "nicht", "kein", "keine", "ist", "sind", "war", "waren",
    "wird", "werden", "wurde", "wurden", "haben", "habe", "hatte", "hatten", "sein", "ich",
    "du", "er", "wir", "ihr", "sie", "mich", "mir", "dich", "dir", "sich", "uns", "euch",
    "mit", "auf", "für", "von", "zu", "zum", "zur", "aus", "bei", "nach", "vor", "über",
    "unter", "durch", "gegen", "ohne", "um", "zwischen", "auch", "noch", "nur", "schon",
    "immer", "sehr", "mehr", "wenn", "dass", "weil", "damit", "als", "wie", "wo", "warum",
    "welche", "welcher", "welches", "kann", "können", "muss", "müssen", "soll", "sollen",
    "will", "wollen", "darf", "dürfen", "möchte", "hier", "dort", "jetzt", "heute", "gestern",
    "morgen", "wieder", "ja", "nein", "danke", "bitte", "hallo", "guten", "viel", "viele",
    "alle", "alles", "andere", "diese", "dieser", "dieses", "seine", "ihre", "unser", "man",
    "es", "im", "am", "vom", "beim", "ins", "ans", "doch", "denn", "sondern", "etwa", "etwas",
    "nichts", "jeder", "jede", "zwar", "bereits", "sowie", "bzw", "usw",
}
ENGLISH = {
    "the", "a", "an", "and", "or", "but", "not", "is", "are", "was", "were", "be", "been",
    "being", "am", "do", "does", "did", "have", "has", "had", "i", "you", "he", "she", "it",
    "we", "they", "me", "him", "her", "us", "them", "my", "your", "his", "its", "our",
    "their", "this", "that", "these", "those", "of", "to", "in", "on", "for", "with", "from",
    "at", "by", "about", "into", "over", "under", "through", "between", "against", "without",
    "also", "still", "only", "just", "always", "very", "more", "most", "if", "because",
    "when", "while", "where", "why", "how", "which", "who", "what", "can", "could", "would",
    "should", "will", "shall", "may", "might", "must", "here", "there", "now", "today",
    "yesterday", "tomorrow", "again", "yes", "no", "thanks", "please", "hello", "many",
    "all", "every", "some", "any", "other", "than", "then", "so", "such", "each", "both",
    "get", "got", "make", "made", "use", "used", "like", "want", "need", "know", "think",
    "see", "says", "said", "let", "does", "doesn", "isn", "don", "didn", "can't", "it's",
}

# Orthography: substrings that are near-impossible in the other language. Weaker than a
# function word (they also fire inside loanwords), hence the smaller weight below.
GERMAN_SUBSTR = ("sch", "tsch", "ung", "keit", "heit", "lich", "isch", "chen", "zw", "pf")
ENGLISH_SUBSTR = ("th", "wh", "ough", "tion", "ing", "ness", "ally", "aw")

WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

W_STOP = 3.0        # a function-word hit
W_UMLAUT = 2.5      # ä/ö/ü/ß — English has none of these natively
W_SUBSTR = 0.6      # orthographic hint


def _score(text: str) -> tuple[float, float, int]:
    """(german_score, english_score, word_count) for one piece of text."""
    low = text.lower()
    words = WORD_RE.findall(low)
    de = en = 0.0

    for w in words:
        if w in GERMAN:
            de += W_STOP
        if w in ENGLISH:
            en += W_STOP
        for s in GERMAN_SUBSTR:
            if s in w:
                de += W_SUBSTR
                break
        for s in ENGLISH_SUBSTR:
            if s in w:
                en += W_SUBSTR
                break

    # Umlauts and ß are the single strongest German tell; capped so that one ä in a long
    # English text cannot outvote its function words.
    umlauts = sum(low.count(c) for c in "äöüß")
    de += W_UMLAUT * min(umlauts, 4)

    # A capitalised noun mid-sentence is very German, but it is also how English writes
    # names and titles — so it only breaks ties, at a deliberately small weight.
    inner_caps = len(re.findall(r"(?<=[a-zäöüß] )[A-ZÄÖÜ][a-zäöüß]{3,}", text))
    de += 0.4 * min(inner_caps, 6)

    return de, en, len(words)


def detect(text: str, default: str = "de") -> tuple[str, float]:
    """Return (lang, confidence 0..1). Falls back to `default` when the text carries too
    little evidence — one or two content words genuinely cannot be told apart, and guessing
    there just makes the wrong voice read your selection."""
    de, en, n = _score(text)
    total = de + en
    if n == 0 or total < 2.0:
        return default, 0.0
    lang = "de" if de >= en else "en"
    lead = abs(de - en) / total                    # 0 = tie, 1 = one-sided
    # Confidence grows with both the margin AND how much text backed it up.
    confidence = lead * min(1.0, (n / 8.0) ** 0.5)
    return lang, round(confidence, 3)


def detect_sticky(text: str, dominant: str, default: str = "de",
                  threshold: float = 0.30) -> str:
    """Language for one segment, biased toward the selection's dominant language.

    Short segments ("Ja.", "See below.") carry almost no evidence, and letting them flip the
    voice makes a read stutter between two speakers. So a segment only switches away from
    the dominant language when it says so clearly.
    """
    lang, conf = detect(text, default=dominant)
    if lang == dominant:
        return dominant
    return lang if conf >= threshold else dominant


if __name__ == "__main__":
    if len(sys.argv) > 1:
        t = " ".join(sys.argv[1:])
        de, en, n = _score(t)
        lang, conf = detect(t)
        print(f"de={de:.1f} en={en:.1f} words={n} -> {lang} (confidence {conf})")
    else:
        print(__doc__)
