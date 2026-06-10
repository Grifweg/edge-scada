"""
auth/users.py — Persistent user store backed by data/users.json.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import bcrypt

_USERS_FILE: Path = Path('/app/data/users.json')


class UserStore:
    _lock = threading.Lock()

    def __init__(self) -> None:
        _USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not _USERS_FILE.exists():
            self._init_defaults()

    # ── Public ────────────────────────────────────────────────────────────────

    def get_user(self, username: str) -> dict | None:
        return self._load().get(username)

    def verify_password(self, username: str, password: str) -> bool:
        record = self.get_user(username)
        if not record:
            return False
        try:
            return bcrypt.checkpw(password.encode(), record['password'].encode())
        except Exception:
            return False

    def set_password(self, username: str, new_password: str) -> None:
        with self._lock:
            data = self._load()
            if username not in data:
                raise KeyError(username)
            data[username]['password'] = bcrypt.hashpw(
                new_password.encode(), bcrypt.gensalt()
            ).decode()
            data[username]['must_change_password'] = False
            self._save(data)

    def create_user(self, username: str, password: str, role: str) -> None:
        with self._lock:
            data = self._load()
            if username in data:
                raise ValueError(f'User {username!r} already exists')
            data[username] = {
                'password': bcrypt.hashpw(
                    password.encode(), bcrypt.gensalt()
                ).decode(),
                'role':                role,
                'must_change_password': True,
            }
            self._save(data)

    def delete_user(self, username: str) -> None:
        with self._lock:
            data = self._load()
            data.pop(username, None)
            self._save(data)

    def list_users(self) -> dict[str, Any]:
        """Return all users without password hashes."""
        return {
            k: {kk: vv for kk, vv in v.items() if kk != 'password'}
            for k, v in self._load().items()
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    def _init_defaults(self) -> None:
        self._save({
            'admin': {
                'password': bcrypt.hashpw(b'admin123', bcrypt.gensalt()).decode(),
                'role':                'administrator',
                'must_change_password': True,
            }
        })

    def _load(self) -> dict:
        try:
            with _USERS_FILE.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        with _USERS_FILE.open('w') as f:
            json.dump(data, f, indent=2)
