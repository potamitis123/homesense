"""Matches ASR transcription text against the 'Kokora' wake word + commands.

Matching is diacritic-insensitive (Greek tonos marks are stripped before
comparison) and substring-based, since ASR output doesn't reliably preserve
accents or exact word boundaries. Only "stop" is implemented for now; more
commands can be added to STOP_TRIGGERS-style lists later without touching
callers -- detect_command() already returns a command name, not a bool.
"""

import unicodedata


def _normalize(text):
    lowered = text.lower()
    decomposed = unicodedata.normalize('NFD', lowered)
    stripped = ''.join(ch for ch in decomposed if unicodedata.category(ch) != 'Mn')
    return unicodedata.normalize('NFC', stripped)


# Written with their accents so they stay readable, then normalized once at
# import. Comparing a raw accented pattern against normalized text can never
# match -- "κόκκορα" is not a substring of "κοκκορα" -- so the normalization
# has to be applied to both sides, not just the transcription.
_WAKE_WORDS_RAW = ["kokora", "κόκκορα", "κοκόρα", "κόκορα", "κόκκορας", "κόκορας"]

_STOP_TRIGGERS_RAW = [
    # Spoken as intended.
    "stop", "σταμάτα", "μη γράφεις", "κοιμήσου",

    # Observed ASR mishearings of the above. These are not new commands: the
    # household says the same phrases, Whisper writes them down differently,
    # and without these the halt simply does not fire. Each was confirmed
    # against a real failed attempt from the Commands tab.
    "στοπ",         # "stop" transcribed in Greek script
    "μι γράφεις",   # "μη" heard as "μι"
    "στα μάτα",     # "σταμάτα" split across a word boundary
]

WAKE_WORDS = sorted({_normalize(w) for w in _WAKE_WORDS_RAW})

STOP_TRIGGERS = sorted({_normalize(t) for t in _STOP_TRIGGERS_RAW})


def detect_command(transcription_text):
    """The command name heard in this transcription, or None.

    Currently only returns "stop". Both the wake word ("Kokora"/"Κόκκορα"/
    "κοκόρα") and a recognised trigger phrase must appear somewhere in the
    text -- not necessarily adjacent, since ASR segmentation is not exact.
    """
    if not transcription_text:
        return None
    normalized = _normalize(transcription_text)
    if not any(word in normalized for word in WAKE_WORDS):
        return None
    if any(trigger in normalized for trigger in STOP_TRIGGERS):
        return "stop"
    return None
