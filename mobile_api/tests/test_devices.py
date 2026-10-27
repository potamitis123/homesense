def test_list_devices(client, auth_headers):
    resp = client.get("/api/v1/devices", headers=auth_headers)
    assert resp.status_code == 200
    devices = resp.json()["devices"]
    ids = {d["device_id"] for d in devices}
    assert ids == {"kitchen", "living_room"}

    kitchen = next(d for d in devices if d["device_id"] == "kitchen")
    assert kitchen["online"] is True
    assert kitchen["capabilities"] == {"duplex": True, "video": False}

    living_room = next(d for d in devices if d["device_id"] == "living_room")
    assert living_room["online"] is False
    # not in the capability map -> defaults
    assert living_room["capabilities"] == {"duplex": False, "video": False}


def test_devices_never_expose_credentials(client, auth_headers):
    resp = client.get("/api/v1/devices", headers=auth_headers)
    body_text = resp.text
    assert "mqtt_password" not in body_text
    assert "wifi_password" not in body_text
    assert "super-secret" not in body_text
    assert "wifi-secret" not in body_text
