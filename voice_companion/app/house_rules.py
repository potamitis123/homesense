"""Standing household instructions, read from a plain text file.

Deliberately not part of the prompt in code. The rules in data/
house_rules.txt are the household's -- what to say when someone asks
for shopping, what never to say about a symptom -- and the person
responsible for them should be able to change them without editing
Python, without a restart, and without a review from anyone.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Costs tokens on every turn of every conversation, and a long list of
# rules is followed worse than a short one. Truncation is loud rather
# than silent so an over-long file is noticed and trimmed.
MAX_CHARACTERS = 4000


def load_house_rules(path: str | Path) -> str:
    """Read the rules, or return empty if there are none.

    Never raises. A missing, unreadable or empty file means the built-in
    prompt stands on its own, which it is written to do -- a companion
    that will not start because a rules file is absent is worse than one
    running with its defaults.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        log.info("no house rules at %s; using the built-in prompt alone", path)
        return ""
    except Exception:
        log.exception("could not read house rules at %s; continuing without", path)
        return ""

    kept = [line.rstrip() for line in raw.splitlines()
            if not line.lstrip().startswith("#")]

    # Collapse the run of blank lines left behind by a comment header,
    # but keep single blanks: they are what separates one rule from the
    # next when the model reads it.
    text, blank = [], 0
    for line in kept:
        if line.strip():
            blank = 0
            text.append(line)
        else:
            blank += 1
            if blank == 1:
                text.append("")
    rules = chr(10).join(text).strip()

    if len(rules) > MAX_CHARACTERS:
        log.warning("house rules are %d characters; using the first %d. "
                    "Every line costs tokens on every turn -- shorten the "
                    "file rather than relying on this.",
                    len(rules), MAX_CHARACTERS)
        rules = rules[:MAX_CHARACTERS].rsplit(chr(10), 1)[0]

    if rules:
        log.info("house rules: %d lines from %s",
                 len(rules.splitlines()), Path(path).name)
    return rules
