def test_heatmap_today_all_devices(client, auth_headers):
    resp = client.get("/api/v1/heatmap/today", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"] == "all"
    by_class = {c["class_name"]: c["hourly_activity"] for c in body["classes"]}
    # today's events: id1 kitchen 08:00 (Speech, Music), id2 living_room 09:30 (Speech)
    assert by_class["Speech"][8] == 1
    assert by_class["Speech"][9] == 1
    assert by_class["Music"][8] == 1
    assert all(len(v) == 24 for v in by_class.values())


def test_heatmap_today_filtered_by_device(client, auth_headers):
    resp = client.get("/api/v1/heatmap/today?device=kitchen", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"] == "kitchen"
    by_class = {c["class_name"]: c["hourly_activity"] for c in body["classes"]}
    # only id1 (kitchen) should count -- id2 (living_room) excluded
    assert by_class["Speech"][8] == 1
    assert sum(by_class["Speech"]) == 1
    assert "Music" in by_class
