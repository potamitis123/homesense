import os

from mobile_api.adapters import recordings_files


def test_recording_not_found_no_db_row(client, auth_headers):
    resp = client.get("/api/v1/recordings/9999", headers=auth_headers)
    assert resp.status_code == 404


def test_recording_not_found_missing_file(client, auth_headers, monkeypatch, tmp_path):
    # id=3 exists in the fake DB but its file "c.wav" doesn't exist on disk
    # under the (redirected, empty) assets folder.
    monkeypatch.setattr(recordings_files, "_BASE_ASSETS", str(tmp_path))
    resp = client.get("/api/v1/recordings/3", headers=auth_headers)
    assert resp.status_code == 404


def test_recording_streams_with_range_support(client, auth_headers, monkeypatch, tmp_path):
    audios_dir = tmp_path / "audios"
    audios_dir.mkdir()
    content = b"0123456789" * 100  # 1000 bytes
    (audios_dir / "a.wav").write_bytes(content)
    monkeypatch.setattr(recordings_files, "_BASE_ASSETS", str(tmp_path))

    # Full file, no Range header
    resp = client.get("/api/v1/recordings/1", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.content == content

    # Partial range
    resp = client.get("/api/v1/recordings/1", headers={**auth_headers, "Range": "bytes=10-19"})
    assert resp.status_code == 206
    assert resp.content == content[10:20]
    assert resp.headers["Content-Range"] == f"bytes 10-19/{len(content)}"
