"""
Local, isolated credential store for the mobile API.

Deliberately NOT the existing ast_sql/users.py table: that table hashes
passwords with unsalted SHA-256 (misc.hash_password) and looks purpose-built
for device/location registration (it carries latitude/longitude), not
dashboard-style login. Reusing it would mean real login passwords protected
by the weakest hashing in the codebase. This store is a small local JSON
file instead -- no new database, no external security library, just
hashlib + secrets from the standard library (see authentication.py).

File format (mobile_api/security/credentials.json, gitignored):
    {
        "<username>": {"salt": "<hex>", "hash": "<hex>"}
    }
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import threading

from .authentication import hash_password, verify_password

_STORE_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")


class CredentialsStore:
    def __init__(self, path: str = _STORE_PATH):
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self._path)

    def has_any_user(self) -> bool:
        return bool(self._load())

    def set_password(self, username: str, password: str) -> None:
        with self._lock:
            data = self._load()
            salt_hex, hash_hex = hash_password(password)
            data[username] = {"salt": salt_hex, "hash": hash_hex}
            self._save(data)

    def verify(self, username: str, password: str) -> bool:
        data = self._load()
        entry = data.get(username)
        if entry is None:
            return False
        return verify_password(password, entry["salt"], entry["hash"])

    def delete_user(self, username: str) -> None:
        with self._lock:
            data = self._load()
            data.pop(username, None)
            self._save(data)


credentials_store = CredentialsStore()


def _cli() -> None:
    """python -m mobile_api.security.credentials_store set <username>"""
    if len(sys.argv) < 3 or sys.argv[1] != "set":
        print("Usage: python -m mobile_api.security.credentials_store set <username>")
        sys.exit(1)
    username = sys.argv[2]
    password = getpass.getpass(f"Password for '{username}': ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.")
        sys.exit(1)
    credentials_store.set_password(username, password)
    print(f"Password set for '{username}'.")


if __name__ == "__main__":
    _cli()
