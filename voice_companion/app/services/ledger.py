"""The topic ledger: what the agent leads with, and in what order.

Measured over three live calls, the agent asked six open memory questions
in five minutes -- "do you have a favourite childhood memory?", "any
particular story that stood out?" -- and twice fell back to "is there
anything else on your mind?". Both are the same failure wearing
different clothes: the lead is handed to the person, and the person is
someone with stage-2 dementia who has just been asked to search an empty
room and come back with something.

The insight this module is built on is not that open questions are
impolite. It is that they do not start the search at all. A memory does
not surface because somebody was invited to look for one; it surfaces
because a name arrived and dragged it up unbidden. And the names that do
that are intimate ones -- a wife, a sister, a mother, the house, the
town they grew up in. "Do you have a favourite memory" reaches nobody.
"Do you remember your friend George?" arrives already connected to
something, and the rest follows on its own.

So an anchor is not a topic. It is the thing that starts the engine.
Once it has fired, an open tail is welcome and even wanted:

    "Do you remember your friend George -- or any favourite memory of
    those days?"

Anchor first, door open after. Never the door alone.

What this module provides is the inventory and the running order: which
names are available, which are intimate enough to work, and which have
been used recently enough to be stale. Choosing the words stays with the
model; choosing what to aim at does not.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# Intimacy, which here means how reliably a name starts the search. This
# is the whole ranking, and it is not a guess about what is interesting:
# it is about what arrives already attached to something.
#
#   3  the people and places somebody carries with them always --
#      spouse, children, siblings, parents, the home, the town they grew
#      up in. These work even on a bad day.
#   2  people who are really in their life but at one remove -- a
#      cousin, the caregiver who comes daily, the hairdresser. Warm, and
#      concrete enough to catch.
#   1  interests and habits. Fine for keeping a conversation moving,
#      too thin to start one from cold.
INTIMATE = 3
FAMILIAR = 2
LIGHT = 1

# Roles that mark the word after them as a person worth asking about.
# Written out rather than inferred, because a capitalised word in these
# files is as likely to be a town or a brand of blood-pressure tablet.
_ROLE_WORDS = (
    "wife|husband|son|daughter|sister|brother|mother|father|mum|dad|"
    "cousin|niece|nephew|aunt|uncle|grandson|granddaughter|grandmother|"
    "grandfather|friend|neighbour|neighbor|caregiver|carer|guardian|"
    "hairdresser|physiotherapist|nurse|doctor|priest"
)
_ROLE_RE = re.compile(
    rf"\b({_ROLE_WORDS})s?\b(?:\s+(?:named|called))?\s*\(?([A-Z][a-z]+)")

# Relatives who appear without a name -- "he likes talking about his
# past, his mother, grand mother". Nameless, and among the strongest
# anchors there are: nobody needs telling which mother is meant.
_NAMELESS_RE = re.compile(
    r"\bhis\s+(mother|father|mum|dad|grand\s?mother|grand\s?father|wife|"
    r"sister|brother|son|daughter)\b", re.I)

# "name: Voula | relationship: [wife]"
_RELATIVE_RE = re.compile(
    r"name:\s*([^|\[\]]+?)\s*\|\s*relationship:\s*\[?([^\]\n]+?)\]?\s*$")


@dataclass(frozen=True)
class Anchor:
    """One thing the agent may lead with.

    ``label`` is written to be said out loud as-is -- "your sister Ann",
    not "Ann (sister)" -- because the model is being handed a phrase to
    build a question around, not a database row.
    """
    label: str
    kind: str
    intimacy: int
    source: str = ""

    @property
    def key(self) -> str:
        """Identity for rotation. Two spellings of one person are one."""
        return re.sub(r"[^a-z0-9]+", " ", self.label.lower()).strip()


def _clean(text: str) -> str:
    """Strip the profile's [square brackets] and trailing noise."""
    return text.strip().strip("[]").strip().rstrip(".,;")


def anchors_from_profile(profile) -> list[Anchor]:
    """Every name and place in the profile worth leading with.

    Deliberately generous. A thin inventory is what produced "is there
    anything else on your mind?" in the first place: when the model has
    nothing concrete to reach for, it reaches for the open field.
    """
    out: list[Anchor] = []
    lists = getattr(profile, "lists", {}) or {}

    # Closest first: whoever the household wrote down as immediate family.
    for raw in lists.get("close_relatives", []):
        m = _RELATIVE_RE.search(raw.strip())
        if not m:
            continue
        name, rel = _clean(m.group(1)), _clean(m.group(2)).split(",")[0]
        out.append(Anchor(f"your {rel} {name}", "person", INTIMATE,
                          "close_relatives"))

    # People mentioned in passing anywhere else: the cousin who visits,
    # the caregiver, the hairdresser. The profile records these as prose,
    # so they are pulled out by role word rather than by field.
    for field_name, entries in lists.items():
        if field_name in ("close_relatives", "avoid_topics"):
            continue
        for raw in entries:
            for rel, name in _ROLE_RE.findall(raw):
                rel = rel.lower()
                out.append(Anchor(f"your {rel} {name}", "person",
                                  FAMILIAR, field_name))

    # Relatives referred to only by role. The household writes "he likes
    # talking about his mother" and never names her, but "do you
    # remember your mother?" needs no name to land.
    for field_name, entries in lists.items():
        if field_name == "avoid_topics":
            continue
        for raw in entries:
            for rel in _NAMELESS_RE.findall(raw):
                rel = re.sub(r"\s+", "", rel.lower())
                out.append(Anchor(f"your {rel}", "person", INTIMATE,
                                  field_name))

    # Home and hometown. Named explicitly because they belong in the top
    # rank with the people: for somebody who cannot leave the house, the
    # house is not scenery, it is most of the remaining world.
    out.append(Anchor("this house, where you have lived all these years",
                      "place", INTIMATE, "known_facts"))
    out.append(Anchor("the home you grew up in", "place", INTIMATE,
                      "known_facts"))

    # Interests: enough to keep a conversation going, not to start one.
    for raw in lists.get("interests", []):
        text = _clean(raw.split("e.g.")[0])
        if text:
            out.append(Anchor(text, "interest", LIGHT, "interests"))

    return _dedupe(out)


def anchors_from_memory(recent_memory: str) -> list[Anchor]:
    """Anchors from the last few days of conversation.

    Ranked FAMILIAR rather than LIGHT: something they themselves brought
    up on Tuesday is already connected to something, which is the whole
    property being selected for. Asking after it also does the one thing
    a daily call can uniquely do -- show that the last one was heard.
    """
    out: list[Anchor] = []
    for line in (recent_memory or "").splitlines():
        text = line.strip().lstrip("-").strip()
        if not text:
            continue
        # Drop the "yesterday:" / "two days ago:" prefix the diary adds.
        text = re.sub(r"^[^:]{0,20}:\s*", "", text)
        if len(text) < 8:
            continue
        out.append(Anchor(text.rstrip("."), "recent", FAMILIAR,
                          "short_memory"))
    return _dedupe(out)


def _dedupe(anchors: list[Anchor]) -> list[Anchor]:
    """One entry per subject, keeping the most intimate framing of it."""
    best: dict[str, Anchor] = {}
    for a in anchors:
        prev = best.get(a.key)
        if prev is None or a.intimacy > prev.intimacy:
            best[a.key] = a
    return list(best.values())


@dataclass
class TopicLedger:
    """The running order, and what has already been spent.

    Rotation matters more than it sounds. Asking after Ann is warm;
    asking after Ann four turns running is an interrogation about Ann,
    and it is also the fastest way to exhaust the one subject that was
    working.
    """
    anchors: list[Anchor] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    # How many turns must pass before a subject may come round again.
    cooldown: int = 4

    @classmethod
    def build(cls, profile, recent_memory: str = "") -> "TopicLedger":
        anchors = _dedupe(anchors_from_profile(profile)
                          + anchors_from_memory(recent_memory))
        anchors.sort(key=lambda a: -a.intimacy)
        log.info("topic ledger: %d anchors (%d intimate, %d familiar)",
                 len(anchors),
                 sum(a.intimacy == INTIMATE for a in anchors),
                 sum(a.intimacy == FAMILIAR for a in anchors))
        return cls(anchors=anchors)

    def candidates(self, limit: int = 5) -> list[Anchor]:
        """The next few things to lead with, most intimate first."""
        recent = set(self.used[-self.cooldown:])
        fresh = [a for a in self.anchors if a.key not in recent]
        # Everything is on cooldown in a long call: rather than stall,
        # let the oldest come round again.
        return (fresh or self.anchors)[:limit]

    def mark_used(self, text: str) -> Anchor | None:
        """Record whichever anchor the agent's reply actually named."""
        hit = self.find_in(text)
        if hit is not None:
            self.used.append(hit.key)
        return hit

    def find_in(self, text: str) -> Anchor | None:
        """The anchor a reply names, if any.

        Matched on the distinctive words rather than the whole label,
        because the model will say "Ann" or "your sister" where the
        ledger holds "your sister Ann" -- and either one is a hit: the
        person heard something that belongs to them.

        A name needs one hit; a sentence needs two. Anchors drawn from
        the diary are whole sentences, and one shared ordinary word is
        not evidence of anything -- "he talked about paddling in Crete"
        and "what do you recall enjoying about those stories?" share the
        word "about", which is how an early version of this quietly
        passed the exact questions it was built to catch.
        """
        low = (text or "").lower()
        for a in self.anchors:
            tokens = _match_tokens(a)
            hits = sum(1 for t in tokens if t in low)
            if hits >= (2 if len(tokens) > 2 else 1):
                return a
        return None


# Words too common to prove an anchor was named. Without this, "your
# house" makes every mention of the word "house" a hit, and the check
# stops meaning anything.
_STOPWORDS = {
    "your", "the", "you", "have", "lived", "all", "these", "years", "in",
    "this", "where", "home", "grew", "up", "and", "with", "of", "a", "an",
    "he", "his", "she", "her", "they", "them", "it", "for", "to", "on",
    "at", "by", "some", "any", "that", "who", "had", "has", "was", "were",
    # Ordinary words that show up in diary sentences and prove nothing.
    "about", "those", "there", "here", "when", "then", "from", "into",
    "said", "says", "told", "talked", "asked", "wanted", "liked", "went",
    "just", "very", "much", "more", "like", "would", "could", "should",
    "today", "yesterday", "week", "day", "days", "time", "again", "still",
    "boy", "girl", "man", "old", "new", "good", "nice", "back", "over",
}


def _match_tokens(anchor: Anchor) -> list[str]:
    """The words whose presence shows this anchor was actually named."""
    words = re.findall(r"[a-z]+", anchor.label.lower())
    strong = [w for w in words if w not in _STOPWORDS and len(w) > 3]
    if strong:
        return strong
    # Nothing distinctive survived (the two generic place anchors): fall
    # back to the whole phrase, which will rarely match, and should.
    return [anchor.label.lower()]
