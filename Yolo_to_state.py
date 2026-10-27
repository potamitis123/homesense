# -*- coding: utf-8 -*-
"""
Created on Mon Aug 17 20:08:07 2026

@author: potam
"""

"""
house_state_from_yolo.py

Convert YOLO11 detections into a structured house-state JSON.

IMPORTANT:
YOLO detections identify objects/persons. They do NOT by themselves
determine semantic activities such as "studying" or "discussing".

This script creates the JSON/state structure that can subsequently
be populated by a VLM/activity-recognition stage.

Input can be:
    1. A Python dictionary representing YOLO output
    2. A JSON file containing the same structure
    3. A list of detection dictionaries

Example input:

{
    "room": "kitchen",
    "detections": [
        {
            "track_id": 1,
            "class": "person",
            "confidence": 0.97,
            "name": "Ilyas",
            "bbox": [100, 100, 400, 700]
        },
        {
            "track_id": 2,
            "class": "person",
            "confidence": 0.95,
            "name": "Voula",
            "bbox": [500, 120, 800, 700]
        }
    ]
}

Output:

{
    "timestamp": "...",
    "room": "kitchen",
    "persons": [...],
    "group_activity": "discussing"
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# RESIDENTS
# ============================================================

RESIDENTS = {
    "Ilyas": {
        "sex": "male",
        "role": "husband",
        "activities": [
            "reading",
            "programming",
            "tablet_use",
            "phone_use",
            "watching_tv",
            "walking",
            "sitting",
            "standing",
            "lying_down",
            "eating",
            "drinking",
            "cooking",
            "washing_dishes",
            "cleaning",
            "working",
            "writing",
            "talking",
            "resting",
            "sleeping",
            "dressing",
            "exercising",
            "using_computer",
            "using_laptop",
            "unknown",
        ],
    },

    "Voula": {
        "sex": "female",
        "role": "wife",
        "activities": [
            "painting",
            "cooking",
            "washing_dishes",
            "home_cleaning",
            "tidying",
            "reading",
            "phone_use",
            "tablet_use",
            "watching_tv",
            "walking",
            "sitting",
            "standing",
            "lying_down",
            "eating",
            "drinking",
            "talking",
            "resting",
            "sleeping",
            "writing",
            "working",
            "dressing",
            "exercising",
            "unknown",
        ],
    },

    "Rafael": {
        "sex": "male",
        "role": "son",
        "age_group": "child",
        "activities": [
            "studying",
            "reading",
            "writing",
            "playing",
            "laptop_use",
            "computer_use",
            "phone_use",
            "tablet_use",
            "watching_tv",
            "gaming",
            "walking",
            "sitting",
            "standing",
            "lying_down",
            "eating",
            "drinking",
            "talking",
            "resting",
            "sleeping",
            "homework",
            "drawing",
            "exercising",
            "unknown",
        ],
    },
}


# ============================================================
# ROOMS
# ============================================================

ROOMS = {
    "bedroom": [
        "sleeping",
        "lying_down",
        "reading",
        "watching_tv",
        "phone_use",
        "tablet_use",
        "dressing",
        "resting",
        "talking",
        "walking",
        "standing",
        "sitting",
    ],

    "kitchen": [
        "cooking",
        "eating",
        "drinking",
        "washing_dishes",
        "cleaning",
        "home_cleaning",
        "tidying",
        "talking",
        "walking",
        "standing",
        "sitting",
    ],

    "living_room": [
        "watching_tv",
        "reading",
        "eating",
        "drinking",
        "talking",
        "discussing",
        "playing",
        "resting",
        "phone_use",
        "tablet_use",
        "walking",
        "sitting",
        "standing",
        "lying_down",
    ],

    "rafael_room": [
        "studying",
        "homework",
        "reading",
        "writing",
        "drawing",
        "playing",
        "gaming",
        "laptop_use",
        "computer_use",
        "phone_use",
        "tablet_use",
        "watching_tv",
        "talking",
        "walking",
        "sitting",
        "standing",
        "lying_down",
        "resting",
    ],

    "office": [
        "programming",
        "working",
        "reading",
        "writing",
        "laptop_use",
        "computer_use",
        "tablet_use",
        "phone_use",
        "talking",
        "sitting",
        "standing",
        "walking",
    ],

    "bathroom": [
        "washing",
        "showering",
        "dressing",
        "cleaning",
        "standing",
        "sitting",
        "walking",
        "unknown",
    ],

    "hallway": [
        "walking",
        "standing",
        "talking",
        "waiting",
        "unknown",
    ],
}


# ============================================================
# SHARED ACTIVITIES
# ============================================================

# Each tuple is sorted alphabetically internally when matching.
# The explicit combinations make the intended family semantics clear.

SHARED_ACTIVITY_RULES = {
    frozenset({"Ilyas", "Voula"}): {
        "discussing",
        "eating_together",
        "watching_tv_together",
        "talking_together",
        "walking_together",
        "working_together",
        "cleaning_together",
    },

    frozenset({"Ilyas", "Rafael"}): {
        "playing_together",
        "eating_together",
        "discussing",
        "watching_tv_together",
        "talking_together",
        "walking_together",
    },

    frozenset({"Voula", "Rafael"}): {
        "discussing",
        "eating_together",
        "watching_tv_together",
        "talking_together",
        "walking_together",
        "cleaning_together",
    },

    frozenset({"Ilyas", "Voula", "Rafael"}): {
        "eating_together",
        "watching_tv_together",
        "talking_together",
        "playing_together",
        "walking_together",
    },
}


# ============================================================
# HELPERS
# ============================================================

def timestamp_now() -> str:
    """Return ISO-8601 timestamp with UTC timezone."""
    return datetime.now(timezone.utc).isoformat()


def normalize_name(name: str | None) -> str | None:
    if not name:
        return None

    name_lower = name.strip().lower()

    aliases = {
        "ilyas": "Ilyas",
        "voula": "Voula",
        "rafael": "Rafael",
    }

    return aliases.get(name_lower)


def extract_persons(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract recognized residents from YOLO detections.
    """

    persons = []

    for detection in detections:

        cls = str(detection.get("class", "")).lower()

        # Accept several common representations.
        if cls not in {"person", "0"}:
            continue

        name = normalize_name(
            detection.get("name")
            or detection.get("identity")
            or detection.get("person")
        )

        # Unknown people are retained but marked unknown.
        if name is None:
            name = "Unknown"

        person = {
            "name": name,
            "track_id": detection.get("track_id"),
            "detection_confidence": detection.get("confidence"),
            "activity": detection.get("activity", "unknown"),
            "activity_confidence": detection.get(
                "activity_confidence", 0.0
            ),
        }

        persons.append(person)

    return persons


def determine_group_activity(
    persons: list[dict[str, Any]]
) -> str | None:
    """
    Determine a shared activity when the activity recognizer has
    provided compatible evidence.

    At this stage this function does NOT invent 'discussing' simply
    because two people are present.

    It looks for explicit activity evidence such as:

        talking
        discussing
        eating
        playing

    and converts it to a shared activity where appropriate.
    """

    names = {
        p["name"]
        for p in persons
        if p["name"] in RESIDENTS
    }

    if len(names) < 2:
        return None

    allowed = SHARED_ACTIVITY_RULES.get(frozenset(names), set())

    activities = {
        p.get("activity", "unknown")
        for p in persons
    }

    # Strong semantic evidence first.

    if "discussing" in activities and "discussing" in allowed:
        return "discussing"

    if "talking" in activities and "talking_together" in allowed:
        return "talking_together"

    if "eating" in activities and "eating_together" in allowed:
        return "eating_together"

    if "playing" in activities and "playing_together" in allowed:
        return "playing_together"

    if "watching_tv" in activities and "watching_tv_together" in allowed:
        return "watching_tv_together"

    if "walking" in activities and "walking_together" in allowed:
        return "walking_together"

    if "cleaning" in activities and "cleaning_together" in allowed:
        return "cleaning_together"

    return None


# ============================================================
# MAIN CONVERSION
# ============================================================

def yolo_to_house_state(
    room: str,
    detections: list[dict[str, Any]],
    timestamp: str | None = None,
) -> dict[str, Any]:

    room = room.strip().lower()

    persons = extract_persons(detections)

    group_activity = determine_group_activity(persons)

    return {
        "timestamp": timestamp or timestamp_now(),
        "room": room,

        "persons": persons,

        "group_activity": group_activity,

        "resident_count": len(
            [
                p for p in persons
                if p["name"] in RESIDENTS
            ]
        ),
    }


# ============================================================
# FILE INTERFACE
# ============================================================

def process_json_file(
    input_file: str,
    output_file: str,
) -> None:

    input_path = Path(input_file)
    output_path = Path(output_file)

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    room = data["room"]
    detections = data.get("detections", [])

    result = yolo_to_house_state(
        room=room,
        detections=detections,
        timestamp=data.get("timestamp"),
    )

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            result,
            f,
            indent=2,
            ensure_ascii=False,
        )


# ============================================================
# EXAMPLE
# ============================================================

if __name__ == "__main__":

    example_detections = [
        {
            "track_id": 17,
            "class": "person",
            "confidence": 0.97,
            "name": "Ilyas",
            "activity": "discussing",
            "activity_confidence": 0.89,
            "bbox": [100, 100, 400, 700],
        },

        {
            "track_id": 23,
            "class": "person",
            "confidence": 0.95,
            "name": "Voula",
            "activity": "discussing",
            "activity_confidence": 0.91,
            "bbox": [500, 120, 800, 700],
        },
    ]

    result = yolo_to_house_state(
        room="kitchen",
        detections=example_detections,
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )