"""Compact daily overview for the mobile dashboard. Composes existing data
(events DB + sentinel_status.json) -- no independent counting logic beyond
simple aggregation of what those sources already provide."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from mobile_api.adapters import existing_app_adapter as existing


def get_dashboard_today() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    df = existing.get_events_df(f"{today} 00:00:00", f"{today} 23:59:59")

    total_events = 0 if df.empty else len(df)

    last_event = None
    most_active_location = None
    if not df.empty:
        df = df.copy()
        # errors="coerce": guard against any malformed date value reaching
        # here (production data has had at least one), rather than 500ing
        # the whole dashboard.
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        dated = df.dropna(subset=["date"]).sort_values("date", ascending=False)
        if not dated.empty:
            top_row = dated.iloc[0]
            last_event = {
                "time": top_row["date"].isoformat(),
                "device": str(top_row.get("device_name") or ""),
                "type": str(top_row.get("sound1") or "") or None,
            }
        counts = df["device_name"].dropna().value_counts()
        if not counts.empty:
            most_active_location = str(counts.index[0])

    status = existing.get_sentinel_status()
    devices_total = len(status)
    devices_online = sum(1 for v in status.values() if v.get("status") == "online")

    return {
        "date": today,
        "total_events": total_events,
        "devices_online": devices_online,
        "devices_total": devices_total,
        "last_event": last_event,
        "most_active_location": most_active_location,
    }
