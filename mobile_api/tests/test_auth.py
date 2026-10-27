from mobile_api.tests.conftest import TEST_PASSWORD, TEST_USERNAME


def test_login_success(client):
    resp = client.post("/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_login_wrong_password(client):
    resp = client.post("/api/v1/auth/login", json={"username": TEST_USERNAME, "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


def test_refresh_token_flow(client):
    login = client.post("/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    refresh_token = login.json()["refresh_token"]

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_refresh_with_invalid_token(client):
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": "garbage"})
    assert resp.status_code == 401


def test_protected_route_without_token(client):
    resp = client.get("/api/v1/dashboard/today")
    assert resp.status_code == 401


def test_protected_route_with_invalid_token(client):
    resp = client.get("/api/v1/dashboard/today", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
