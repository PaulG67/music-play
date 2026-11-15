"""App-managed credentials stored in the config share.

Passwords are encrypted on disk. The Music Play login also stores an scrypt
hash so the web UI does not need to compare against plaintext.
"""

from __future__ import annotations

import json
import os
import threading

from app.core import secret_box
from app.core.settings import CONFIG_DIR, clean_secret, settings

AUTH_FILE = CONFIG_DIR / "auth.json"
_lock = threading.Lock()
_cache: dict | None = None


def _env_password() -> str:
    raw = os.getenv("SUBSONIC_PASSWORD")
    return clean_secret(raw) if raw is not None else ""


def _defaults() -> dict:
    return {
        "username": settings.subsonic_user or "musicplay",
        "password": _env_password(),
        "password_hash": "",
    }


def _write_unlocked(data: dict) -> None:
    password = clean_secret(str(data.get("password") or ""))
    hashed = data.get("password_hash") or (secret_box.hash_password(password) if password else "")
    payload = {
        "username": clean_secret(str(data.get("username") or "")) or "musicplay",
        "password": secret_box.seal(password),
        "password_hash": hashed,
    }
    secret_box.write_private_file(AUTH_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_unlocked() -> dict:
    global _cache
    if _cache is not None:
        return dict(_cache)

    data = _defaults()
    migrate = not AUTH_FILE.exists()
    if AUTH_FILE.exists():
        try:
            loaded = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data["username"] = clean_secret(str(loaded.get("username") or data["username"])) or data["username"]
                stored = str(loaded.get("password") or "")
                data["password"] = secret_box.unseal(stored)
                data["password_hash"] = str(loaded.get("password_hash") or "")
                if secret_box.needs_migration(stored) or (data["password"] and not data["password_hash"]):
                    migrate = True
        except (OSError, json.JSONDecodeError, TypeError):
            migrate = True

    if data["password"] and not data.get("password_hash"):
        data["password_hash"] = secret_box.hash_password(data["password"])
        migrate = True

    if migrate:
        try:
            _write_unlocked(data)
        except OSError:
            pass

    _cache = dict(data)
    return dict(_cache)


def load() -> dict:
    with _lock:
        return _read_unlocked()


def save(username: str, password: str) -> dict:
    global _cache
    cleaned = clean_secret(password)
    data = {
        "username": clean_secret(username) or "musicplay",
        "password": cleaned,
        "password_hash": secret_box.hash_password(cleaned) if cleaned else "",
    }
    with _lock:
        _write_unlocked(data)
        _cache = dict(data)
    return dict(data)


def username() -> str:
    return load()["username"]


def password() -> str:
    return load()["password"]


def password_hash() -> str:
    return str(load().get("password_hash") or "")


def password_required() -> bool:
    data = load()
    return bool(data.get("password") or data.get("password_hash"))
