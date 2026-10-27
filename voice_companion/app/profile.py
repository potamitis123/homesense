"""Reads data/person_ilyas.txt into a structured profile.

The file is plain text on purpose -- the person who maintains it is not
necessarily a programmer, and it must be obvious how to correct a wrong
fact. Parsing is forgiving: unknown keys are kept, blank sections are
empty lists, and a malformed line is skipped rather than fatal.

Placeholder handling matters. Anything still containing [PLACEHOLDER] is
dropped before it reaches the prompt: the agent saying an invented
relative's name aloud to an elderly person is the specific failure this
guards against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER = re.compile(r"\[PLACEHOLDER[^\]]*\]", re.IGNORECASE)

LIST_KEYS = {
    "close_relatives", "health_context", "interests",
    "conversation_preferences", "avoid_topics", "known_facts",
    "open_follow_ups",
}


@dataclass
class PersonProfile:
    person_id: str = "unknown"
    name: str = "there"
    preferred_name: str | None = None
    age: int | None = None
    preferred_language: str = "English"
    sentinel_id: str | None = None
    room: str | None = None
    guardian_name: str | None = None
    guardian_relationship: str | None = None
    lists: dict[str, list[str]] = field(default_factory=dict)
    # Fields whose value was still a placeholder, so the reader can be told.
    unfilled: list[str] = field(default_factory=list)

    @property
    def address_as(self) -> str:
        return self.preferred_name or self.name

    def get(self, key: str) -> list[str]:
        return self.lists.get(key, [])


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.search(value))


def load_profile(path: str | Path) -> PersonProfile:
    path = Path(path)
    profile = PersonProfile()
    if not path.exists():
        raise FileNotFoundError(f"person profile not found: {path}")

    current_list: str | None = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue

        stripped = line.strip()

        # "  - item" continues the list opened by the last "key:" line
        if stripped.startswith("- "):
            if current_list is None:
                continue
            item = stripped[2:].strip()
            if not item or _is_placeholder(item):
                continue
            profile.lists.setdefault(current_list, []).append(item)
            continue

        if ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key in LIST_KEYS:
            current_list = key
            profile.lists.setdefault(key, [])
            if value and not _is_placeholder(value):
                profile.lists[key].append(value)
            continue

        current_list = None
        if _is_placeholder(value) or not value:
            profile.unfilled.append(key)
            continue

        if key == "person_id":
            profile.person_id = value
        elif key == "name":
            profile.name = value
        elif key == "preferred_name":
            profile.preferred_name = value
        elif key == "age":
            try:
                profile.age = int(value)
            except ValueError:
                profile.unfilled.append(key)
        elif key == "preferred_language":
            profile.preferred_language = value
        elif key == "sentinel_id":
            profile.sentinel_id = value
        elif key == "room":
            profile.room = value
        elif key == "guardian_name":
            profile.guardian_name = value
        elif key == "guardian_relationship":
            profile.guardian_relationship = value

    return profile


def profile_to_prompt_block(profile: PersonProfile) -> str:
    """Compact, human-readable facts for the system prompt.

    Deliberately small: dumping everything known into every turn is what
    makes agents sound like they are reading a file aloud.
    """
    lines = [f"Name: {profile.address_as}"]
    if profile.age:
        lines.append(f"Age: {profile.age}")
    lines.append(f"Language: {profile.preferred_language}")

    def block(title: str, key: str, limit: int = 6):
        items = profile.get(key)[:limit]
        if items:
            lines.append(f"{title}: " + "; ".join(items))

    block("Family", "close_relatives")
    block("Health context", "health_context")
    block("Interests", "interests")
    block("Known already (do not ask again)", "known_facts")
    block("Open follow-ups", "open_follow_ups")
    block("Do not raise unless he does", "avoid_topics")
    block("Style", "conversation_preferences")
    return "\n".join(lines)
