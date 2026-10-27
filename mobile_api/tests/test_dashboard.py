def test_dashboard_today(client, auth_headers):
    resp = client.get("/api/v1/dashboard/today", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    # sample data has 2 events today (ids 1, 2), one yesterday (id 3)
    assert body["total_events"] == 2
    assert body["devices_total"] == 2
    assert body["devices_online"] == 1
    assert body["last_event"]["device"] in ("kitchen", "living_room")
    assert body["most_active_location"] in ("kitchen", "living_room")
