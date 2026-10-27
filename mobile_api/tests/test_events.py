def test_list_events_unauthorized(client):
    resp = client.get("/api/v1/events")
    assert resp.status_code == 401


def test_list_events_all(client, auth_headers):
    resp = client.get("/api/v1/events", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["events"]) == 3
    # newest first
    assert body["events"][0]["id"] >= body["events"][1]["id"] or True  # date-ordered, not id-ordered
    assert body["events"][0]["timestamp"] >= body["events"][1]["timestamp"]


def test_list_events_pagination(client, auth_headers):
    resp = client.get("/api/v1/events?limit=1&offset=0", headers=auth_headers)
    body = resp.json()
    assert body["total"] == 3
    assert len(body["events"]) == 1
    assert body["limit"] == 1
    assert body["offset"] == 0


def test_event_shape(client, auth_headers):
    resp = client.get("/api/v1/events", headers=auth_headers)
    events = resp.json()["events"]
    ev1 = next(e for e in events if e["id"] == 1)
    assert ev1["device_id"] == "kitchen"
    assert ev1["location"] == "kitchen"
    assert ev1["transcription"]["text"] == "hello there"
    assert ev1["transcription"]["emotion"] == "HAPPY"
    labels = {d["label"] for d in ev1["detections"]}
    assert labels == {"Speech", "Music"}
    assert ev1["audio_available"] is True


def test_get_event_by_id(client, auth_headers):
    resp = client.get("/api/v1/events/1", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == 1


def test_get_event_not_found(client, auth_headers):
    resp = client.get("/api/v1/events/9999", headers=auth_headers)
    assert resp.status_code == 404


def test_list_events_filtered_by_label(client, auth_headers):
    resp = client.get("/api/v1/events?label=Speech", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    ids = {e["id"] for e in body["events"]}
    assert ids == {1, 2}
    assert body["total"] == 2


def test_list_events_filtered_by_label_case_insensitive(client, auth_headers):
    resp = client.get("/api/v1/events?label=speech", headers=auth_headers)
    ids = {e["id"] for e in resp.json()["events"]}
    assert ids == {1, 2}


def test_list_events_filtered_by_label_no_match(client, auth_headers):
    resp = client.get("/api/v1/events?label=Barking", headers=auth_headers)
    body = resp.json()
    assert body["total"] == 0
    assert body["events"] == []
