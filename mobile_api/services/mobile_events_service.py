"""Daily audio events for the mobile app: date filtering, pagination,
newest-first, trimmed to mobile-relevant fields only."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from mobile_api.adapters import existing_app_adapter as existing


def get_events(
    date: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    label: Optional[str] = None,
) -> dict:
    if date:
        df = existing.get_events_df(f"{date} 00:00:00", f"{date} 23:59:59")
    else:
        df = existing.get_events_df()

    df = existing.filter_events_by_label(df, label)

    total = 0 if df.empty else len(df)
    if df.empty:
        return {"date": date, "total": 0, "limit": limit, "offset": offset, "events": []}

    df = df.copy()
    # errors="coerce": production data has at least one malformed date
    # value (a leftover test/mock row); such rows become NaT and sort
    # last rather than crashing the whole endpoint.
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date", ascending=False, na_position="last")
    page = df.iloc[offset: offset + limit]

    events = [existing.event_row_to_dict(row.to_dict()) for _, row in page.iterrows()]
    return {"date": date, "total": total, "limit": limit, "offset": offset, "events": events}


def get_event(event_id: int) -> Optional[dict]:
    row = existing.get_event_by_id(event_id)
    if row is None:
        return None
    return existing.event_row_to_dict(row)
