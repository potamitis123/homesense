"""Circular/radial activity statistics for today -- same source data as
the existing Statistics page's sunburst/top-classes chart, scoped to a
single day and optionally one device. Returns raw counts; the Android
app draws its own chart."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from mobile_api.adapters import existing_app_adapter as existing


def get_statistics_today(device: Optional[str] = None) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    activity = existing.compute_class_activity_for_date(today, device=device)
    available_classes = existing.list_available_classes()
    return {"date": today, "device": device or "all", "activity": activity, "available_classes": available_classes}
