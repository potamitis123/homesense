"""
Thin adapters over the existing Sentinel Home Server functionality.

This module is the ONLY place mobile_api touches the existing app's data
layer. It calls real, existing functions/classes wherever they exist
(SQL_to_Pandas, Devices_SQL_to_Pandas, sentinel_status.json) and does not
re-run any queries those classes already run.

A few small aggregations (heatmap-by-device-hour, today's class activity
counts, emotion-string parsing) have no existing standalone function to
call: the code that produces them today lives inside ast_dash/pages/*.py
modules that call dash.register_page(...) at import time, which assumes
an active Dash app and would misbehave if imported from this separate
process. Per the architecture plan, those are re-expressed here as short,
clearly-labelled local equivalents of the *same* technique (pandas
groupby/value_counts over a SQL_to_Pandas dataframe) -- the real "engine"
(the SQL layer + pandas) is reused; only a couple of Dash-page-local
one-liners are mirrored rather than imported.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from typing import Optional

import pandas as pd

from ast_dash.sql_to_pandas import SQL_to_Pandas
from ast_dash.devices_sql_to_pandas import Devices_SQL_to_Pandas
from mobile_api.config import PROJECT_ROOT
from mobile_api.logging_setup import get_logger, log_extra

logger = get_logger()

_SENTINEL_STATUS_PATH = os.path.join(PROJECT_ROOT, "sentinel_status.json")

# Columns from remote_devices that must never reach a mobile client.
_DEVICE_SECRET_COLUMNS = {
    "mqtt_broker", "mqtt_port", "mqtt_client_id", "mqtt_password",
    "wifi_ssid", "wifi_password",
}

# Mirrors ast_dash/pages/statistics.py's emotion_map / parse_emotion (lines
# ~233-250): the DB stores emotion as a formatted string like "SAD (0.85)"
# or the short code "sad", never a structured {label, confidence} field.
_EMOTION_MAP = {
    "HAP": "HAPPY", "HAPPY": "HAPPY",
    "SAD": "SAD",
    "NEU": "NEUTRAL", "NEUTRAL": "NEUTRAL",
    "ANG": "ANGRY", "ANGRY": "ANGRY",
}


def _clean(value):
    """NaN/NaT -> None so pandas rows can go straight into Pydantic/JSON."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def parse_emotion_label(text: Optional[str]) -> Optional[str]:
    """Mirrors ast_dash/pages/statistics.py:242-250 parse_emotion()."""
    if not text or (isinstance(text, float) and math.isnan(text)):
        return None
    match = re.search(r"^([A-Za-z]+)", str(text))
    if not match:
        return None
    label = match.group(1).upper()
    return _EMOTION_MAP.get(label, label)


# ── Events / recordings (ast_dash/sql_to_pandas.py) ──────────────────

def get_events_df(date_from: Optional[str] = None, date_to: Optional[str] = None) -> pd.DataFrame:
    """Real query: SQL_to_Pandas.get_date_from_to() / get_all_rec()."""
    db = SQL_to_Pandas()
    if date_from or date_to:
        return db.get_date_from_to(date_from, date_to)
    return db.get_all_rec()


def get_event_by_id(event_id: int) -> Optional[dict]:
    """Real query: SQL_to_Pandas.get_by_id() (added alongside this module)."""
    db = SQL_to_Pandas()
    row = db.get_by_id(event_id)
    return row


def filter_events_by_label(df: pd.DataFrame, label: Optional[str]) -> pd.DataFrame:
    """Case-insensitive match against sound1/sound2/sound3, e.g. label="Speech"."""
    if not label or df.empty:
        return df
    target = label.strip().lower()
    mask = pd.Series(False, index=df.index)
    for col in ("sound1", "sound2", "sound3"):
        if col in df.columns:
            mask = mask | (df[col].astype(str).str.strip().str.lower() == target)
    return df[mask]


def event_row_to_dict(row: dict) -> dict:
    """Shared row -> mobile JSON shape used by both the events list and
    the single-event lookup, so the two endpoints can't drift apart."""
    detections = []
    for label_col, conf_col in (("sound1", "conf_sound1"), ("sound2", "conf_sound2"), ("sound3", "conf_sound3")):
        label = _clean(row.get(label_col))
        if not label or str(label).strip().lower() in ("none", ""):
            continue
        conf = _clean(row.get(conf_col))
        detections.append({"label": str(label), "confidence": float(conf) if conf is not None else 0.0})

    transcription_text = _clean(row.get("transcription"))
    emotion_raw = _clean(row.get("emotion"))

    return {
        "id": int(row["id"]),
        "timestamp": str(_clean(row.get("date")) or ""),
        "device_id": str(_clean(row.get("device_name")) or ""),
        "location": str(_clean(row.get("device_name")) or ""),
        "transcription": {
            "speaker": None,  # no speaker column exists on ast_table today
            "text": transcription_text,
            "emotion": parse_emotion_label(emotion_raw),
        } if transcription_text is not None else None,
        "detections": detections,
        "audio_available": bool(_clean(row.get("file_name"))),
    }


# ── Devices (ast_dash/devices_sql_to_pandas.py + sentinel_status.json) ─

def get_registered_devices() -> pd.DataFrame:
    """Real query: Devices_SQL_to_Pandas.get_all_rec(). Caller must still
    strip _DEVICE_SECRET_COLUMNS before returning to a client."""
    db = Devices_SQL_to_Pandas()
    return db.get_all_rec()


def strip_device_secrets(df: pd.DataFrame) -> pd.DataFrame:
    cols_to_drop = [c for c in df.columns if c in _DEVICE_SECRET_COLUMNS]
    return df.drop(columns=cols_to_drop) if cols_to_drop else df


def get_sentinel_status() -> dict:
    """Same file the existing app already writes/reads
    (ast_src/communication/adapters.py's _update_sentinel_status_file)."""
    if not os.path.exists(_SENTINEL_STATUS_PATH):
        return {}
    try:
        with open(_SENTINEL_STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Could not read sentinel_status.json: {e}", extra=log_extra("MOBILE_API"))
        return {}


# ── Heatmap: class x hour for one day, one device (or all) ────────────

def compute_heatmap_for_date(date_str: str, device: Optional[str] = None) -> dict[str, list[int]]:
    """
    Mirrors ast_dash/pages/date_sounds.py's get_db_data_for_date(date,
    device) (lines ~71-112): query the day, filter by device if one was
    given, expand sound1/sound2 into a single "sound" column, parse hour,
    groupby(sound, hour) + count. This is genuinely the same shape the
    "Daily Events" heatmap page already produces (class x hour for a
    selected device) -- not reimplemented, faithfully mirrored, because
    date_sounds.py is a dash.register_page module (import-time side
    effects) that can't be imported from this separate process.

    device=None or "all" aggregates across every device, matching that
    page's own 'All' dropdown option.
    """
    db = SQL_to_Pandas()
    df = db.get_date_from_to(f"{date_str} 00:00:00", f"{date_str} 23:59:59")
    result: dict[str, list[int]] = {}
    if df.empty:
        return result

    df = df.copy()
    if device and device != "all":
        df = df[df["device_name"] == device]
        if df.empty:
            return result

    df_s1 = df[["sound1", "date"]].rename(columns={"sound1": "sound"})
    df_s2 = df[["sound2", "date"]].rename(columns={"sound2": "sound"})
    combined = pd.concat([df_s1, df_s2]).dropna(subset=["sound"])
    combined = combined[combined["sound"].astype(str).str.strip().str.lower() != "none"]
    if combined.empty:
        return result

    # errors="coerce": guard against malformed date values (seen in
    # production data) rather than raising out of this endpoint.
    combined["hour"] = pd.to_datetime(combined["date"], errors="coerce").dt.hour
    combined = combined.dropna(subset=["hour"])
    grouped = combined.groupby(["sound", "hour"]).size()
    for class_name in combined["sound"].dropna().unique():
        hourly = [0] * 24
        for hour in range(24):
            hourly[hour] = int(grouped.get((class_name, hour), 0))
        result[str(class_name)] = hourly
    return result


# ── Statistics: today's activity-by-class counts ──────────────────────

def compute_class_activity_for_date(date_str: str, device: Optional[str] = None) -> dict[str, int]:
    """
    Mirrors ast_dash/pages/statistics.py's update_graphs() data source for
    the circular/sunburst chart (df_expanded['sound'].value_counts(),
    lines ~148-151, 228-229), scoped to a single day instead of the
    page's default multi-day range, with an added device filter (that
    page has no device dimension at all -- new, but same technique).
    """
    db = SQL_to_Pandas()
    df = db.get_date_from_to(f"{date_str} 00:00:00", f"{date_str} 23:59:59")
    if df.empty:
        return {}

    if device and device != "all":
        df = df[df["device_name"] == device]
        if df.empty:
            return {}

    df_s1 = df[["sound1"]].rename(columns={"sound1": "sound"})
    df_s2 = df[["sound2"]].rename(columns={"sound2": "sound"})
    combined = pd.concat([df_s1, df_s2]).dropna()
    combined = combined[combined["sound"].astype(str).str.strip().str.lower() != "none"]
    if combined.empty:
        return {}

    counts = combined["sound"].value_counts()
    return {str(k): int(v) for k, v in counts.items()}


def list_available_classes() -> list[str]:
    """
    Mirrors ast_dash/pages/statistics.py's dropdown population
    (sorted(df_expanded['sound'].unique()), line ~291) so the Android
    class-selector reflects real data instead of a hardcoded list.
    Scans the full table (not scoped to today) so the list doesn't
    shrink to nothing on a quiet day.
    """
    db = SQL_to_Pandas()
    df = db.get_all_rec()
    if df.empty:
        return []
    df_s1 = df[["sound1"]].rename(columns={"sound1": "sound"})
    df_s2 = df[["sound2"]].rename(columns={"sound2": "sound"})
    combined = pd.concat([df_s1, df_s2]).dropna()
    combined = combined[combined["sound"].astype(str).str.strip().str.lower() != "none"]
    if combined.empty:
        return []
    return sorted(str(s) for s in combined["sound"].unique())
