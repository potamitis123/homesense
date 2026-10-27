def test_statistics_today_all_devices(client, auth_headers):
    resp = client.get("/api/v1/statistics/today", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"] == "all"
    assert body["activity"] == {"Speech": 2, "Music": 1}
    assert set(body["available_classes"]) >= {"Speech", "Music", "Dishes"}


def test_statistics_today_filtered_by_device(client, auth_headers):
    resp = client.get("/api/v1/statistics/today?device=kitchen", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"] == "kitchen"
    # only id1 (kitchen, today) counts: Speech + Music
    assert body["activity"] == {"Speech": 1, "Music": 1}


def test_statistics_requires_auth(client):
    resp = client.get("/api/v1/statistics/today")
    assert resp.status_code == 401
