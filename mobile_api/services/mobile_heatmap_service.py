"""Daily event heatmap: class x hour activity for today, for one selected
device (a room) or all devices combined -- mirrors the actual "Daily
Events" Dash page (ast_dash/pages/date_sounds.py)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from mobile_api.adapters import existing_app_adapter as existing


def get_heatmap_today(device: Optional[str] = None) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    per_class = existing.compute_heatmap_for_date(today, device=device)
    classes = [
        {"class_name": class_name, "hourly_activity": hourly}
        for class_name, hourly in sorted(per_class.items())
    ]
    return {"date": today, "device": device or "all", "classes": classes}
