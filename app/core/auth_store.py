"""Multi-user credentials stored in the config share.

Each Music Play / Subsonic account can carry its own Navidrome login.
Passwords are encrypted on disk; the web login uses an scrypt hash.
"""

from __future__ import annotations

import hashlib
import hmac
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


def _key(name: str) -> str:
    return clean_secret(name).casefold()


def _navidrome_blob(raw: dict | None) -> dict:
    data = raw if isinstance(raw, dict) else {}
    password = data.get("password") or ""
    return {
        "url": clean_secret(str(data.get("url") or "")).rstrip("/"),
        "username": clean_secret(str(data.get("username") or "")),
        "password": secret_box.unseal(str(password)) if secret_box.is_sealed(str(password)) else clean_secret(str(password)),
    }


def _seal_navidrome(data: dict | None) -> dict:
    blob = data if isinstance(data, dict) else {}
    return {
        "url": clean_secret(str(blob.get("url") or "")).rstrip("/"),
        "username": clean_secret(str(blob.get("username") or "")),
        "password": secret_box.seal(clean_secret(str(blob.get("password") or ""))),
    }


def _normalize_user(raw: dict, *, admin: bool = False) -> dict:
    username = clean_secret(str(raw.get("username") or "")) or "musicplay"
    password = raw.get("password") or ""
    if secret_box.is_sealed(str(password)):
        password = secret_box.unseal(str(password))
    else:
        password = clean_secret(str(password))
    hashed = str(raw.get("password_hash") or "")
    if password and not hashed:
        hashed = secret_box.hash_password(password)
    role = "admin" if admin or str(raw.get("role") or "") == "admin" else "user"
    return {
        "username": username,
        "password": password,
        "password_hash": hashed,
        "role": role,
        "navidrome": _navidrome_blob(raw.get("navidrome")),
    }


def _dump_user(user: dict) -> dict:
    return {
        "username": user["username"],
        "password": secret_box.seal(user.get("password") or ""),
        "password_hash": user.get("password_hash") or "",
        "role": user.get("role") or "user",
        "navidrome": _seal_navidrome(user.get("navidrome")),
    }


def _seed_navidrome() -> dict:
    path = CONFIG_DIR / "navidrome.json"
    if not path.exists():
        return {"url": "", "username": "", "password": ""}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"url": "", "username": "", "password": ""}
    if not isinstance(loaded, dict):
        return {"url": "", "username": "", "password": ""}
    password = str(loaded.get("password") or "")
    return {
        "url": clean_secret(str(loaded.get("url") or "")).rstrip("/"),
        "username": clean_secret(str(loaded.get("username") or "")),
        "password": secret_box.unseal(password) if secret_box.is_sealed(password) else clean_secret(password),
    }


def _legacy_user(loaded: dict) -> dict:
    return _normalize_user(
        {
            "username": loaded.get("username") or settings.subsonic_user or "musicplay",
            "password": loaded.get("password") or "",
            "password_hash": loaded.get("password_hash") or "",
            "role": "admin",
            "navidrome": loaded.get("navidrome") or _seed_navidrome(),
        },
        admin=True,
    )


def _write_unlocked(users: list[dict]) -> None:
    payload = {"users": [_dump_user(user) for user in users]}
    secret_box.write_private_file(AUTH_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_unlocked() -> list[dict]:
    global _cache
    if _cache is not None:
        return [dict(user) for user in _cache]

    users: list[dict] = []
    migrate = not AUTH_FILE.exists()
    if AUTH_FILE.exists():
        try:
            loaded = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("users"), list):
                users = [_normalize_user(item, admin=index == 0) for index, item in enumerate(loaded["users"]) if isinstance(item, dict)]
                if any(secret_box.needs_migration(str((item.get("password") or ""))) for item in loaded["users"] if isinstance(item, dict)):
                    migrate = True
            elif isinstance(loaded, dict) and loaded.get("username"):
                users = [_legacy_user(loaded)]
                migrate = True
        except (OSError, json.JSONDecodeError, TypeError):
            migrate = True

    if not users:
        seeded = _env_password()
        users = [
            _normalize_user(
                {
                    "username": settings.subsonic_user or "musicplay",
                    "password": seeded,
                    "role": "admin",
                    "navidrome": _seed_navidrome() if seeded else {},
                },
                admin=True,
            )
        ]
        migrate = True

    if not any(user.get("role") == "admin" for user in users):
        users[0]["role"] = "admin"
        migrate = True

    if migrate:
        try:
            _write_unlocked(users)
        except OSError:
            pass

    _cache = [dict(user) for user in users]
    return [dict(user) for user in users]


def load_users() -> list[dict]:
    with _lock:
        return _read_unlocked()


def _find(users: list[dict], username: str) -> dict | None:
    needle = _key(username)
    for user in users:
        if _key(user.get("username") or "") == needle:
            return user
    return None


def get_user(username: str | None) -> dict | None:
    if not username:
        return None
    return _find(load_users(), username)


def list_public() -> list[dict]:
    rows = []
    for user in load_users():
        nd = user.get("navidrome") or {}
        rows.append(
            {
                "username": user["username"],
                "role": user.get("role") or "user",
                "navidromeConfigured": bool(nd.get("url") and nd.get("username") and nd.get("password")),
                "navidromeUser": nd.get("username") or "",
            }
        )
    return rows


def is_admin(username: str | None) -> bool:
    user = get_user(username)
    return bool(user and user.get("role") == "admin")


def navidrome_credentials(username: str | None) -> dict:
    user = get_user(username)
    if not user:
        return {"url": "", "username": "", "password": ""}
    return dict(user.get("navidrome") or {"url": "", "username": "", "password": ""})


def verify(username: str, password: str) -> bool:
    user = get_user(username)
    if not user:
        return False
    cleaned = clean_secret(password)
    hashed = user.get("password_hash") or ""
    if hashed:
        return secret_box.verify_password(cleaned, hashed)
    stored = user.get("password") or ""
    if not stored:
        return False
    digest = hashlib.sha256
    ok = hmac.compare_digest(digest(cleaned.encode("utf-8")).digest(), digest(stored.encode("utf-8")).digest())
    if ok:
        save_user(user["username"], password=cleaned)
    return ok


def save_user(
    username: str,
    *,
    password: str | None = None,
    role: str | None = None,
    navidrome: dict | None = None,
    new_username: str | None = None,
) -> dict:
    global _cache
    with _lock:
        users = _read_unlocked()
        current = _find(users, username)
        if current is None:
            raise KeyError("Benutzer nicht gefunden")
        if new_username:
            renamed = clean_secret(new_username)
            if not renamed:
                raise ValueError("Benutzername fehlt")
            other = _find(users, renamed)
            if other is not None and other is not current:
                raise ValueError("Benutzername ist vergeben")
            current["username"] = renamed
        if password is not None:
            cleaned = clean_secret(password)
            if not cleaned:
                raise ValueError("Passwort darf nicht leer sein")
            current["password"] = cleaned
            current["password_hash"] = secret_box.hash_password(cleaned)
        if role in {"admin", "user"}:
            current["role"] = role
            if role != "admin" and not any(item.get("role") == "admin" and item is not current for item in users):
                current["role"] = "admin"
        if navidrome is not None:
            merged = dict(current.get("navidrome") or {})
            if navidrome.get("url") is not None:
                merged["url"] = clean_secret(str(navidrome.get("url") or "")).rstrip("/")
            if navidrome.get("username") is not None:
                merged["username"] = clean_secret(str(navidrome.get("username") or ""))
            if navidrome.get("password"):
                merged["password"] = clean_secret(str(navidrome.get("password") or ""))
            current["navidrome"] = merged
        _write_unlocked(users)
        _cache = [dict(item) for item in users]
        return dict(current)


def create_user(username: str, password: str, *, role: str = "user") -> dict:
    global _cache
    name = clean_secret(username)
    cleaned = clean_secret(password)
    if not name:
        raise ValueError("Benutzername fehlt")
    if len(cleaned) < secret_box.MIN_PASSWORD_LENGTH:
        raise ValueError(f"Passwort mindestens {secret_box.MIN_PASSWORD_LENGTH} Zeichen")
    with _lock:
        users = _read_unlocked()
        if _find(users, name):
            raise ValueError("Benutzername ist vergeben")
        user = _normalize_user({"username": name, "password": cleaned, "role": role, "navidrome": {}})
        users.append(user)
        _write_unlocked(users)
        _cache = [dict(item) for item in users]
        return dict(user)


def delete_user(username: str) -> None:
    global _cache
    with _lock:
        users = _read_unlocked()
        target = _find(users, username)
        if target is None:
            raise KeyError("Benutzer nicht gefunden")
        remaining = [item for item in users if item is not target]
        if not remaining:
            raise ValueError("Der letzte Benutzer kann nicht gelöscht werden")
        if target.get("role") == "admin" and not any(item.get("role") == "admin" for item in remaining):
            remaining[0]["role"] = "admin"
        _write_unlocked(remaining)
        _cache = [dict(item) for item in remaining]


def ensure_setup_user(username: str, password: str) -> dict:
    """First-run: create or replace the initial admin account."""
    global _cache
    name = clean_secret(username) or "musicplay"
    cleaned = clean_secret(password)
    if len(cleaned) < secret_box.MIN_PASSWORD_LENGTH:
        raise ValueError(f"Passwort mindestens {secret_box.MIN_PASSWORD_LENGTH} Zeichen")
    with _lock:
        users = _read_unlocked()
        if any((user.get("password") or user.get("password_hash")) for user in users):
            raise ValueError("Zugang ist bereits eingerichtet")
        admin = _normalize_user(
            {
                "username": name,
                "password": cleaned,
                "role": "admin",
                "navidrome": (users[0].get("navidrome") if users else {}) or _seed_navidrome(),
            },
            admin=True,
        )
        users = [admin]
        _write_unlocked(users)
        _cache = [dict(item) for item in users]
        return dict(admin)


def username() -> str:
    users = load_users()
    for user in users:
        if user.get("role") == "admin":
            return user["username"]
    return users[0]["username"] if users else "musicplay"


def password() -> str:
    user = get_user(username())
    return (user or {}).get("password") or ""


def password_hash() -> str:
    user = get_user(username())
    return (user or {}).get("password_hash") or ""


def password_required() -> bool:
    return any(user.get("password") or user.get("password_hash") for user in load_users())


def save(username_value: str, password_value: str) -> dict:
    """Compatibility helper used by first-run and single-user settings."""
    if not password_required():
        return ensure_setup_user(username_value, password_value)
    return save_user(username_value, password=password_value, new_username=username_value)


def _admin_user() -> dict | None:
    users = load_users()
    for user in users:
        if user.get("role") == "admin":
            return user
    return users[0] if users else None


def apply_env_overrides() -> None:
    """Map Unraid/Docker env fields onto the admin account.

    Empty Unraid boxes stay empty strings and are ignored, so an in-app
    password is not wiped on restart. Non-empty values win on every start.
    """
    from app.core.logging import logger

    env_user = clean_secret(os.getenv("SUBSONIC_USER"))
    env_pass = _env_password()
    nd_update = {}
    nd_url = clean_secret(os.getenv("NAVIDROME_URL") or "").rstrip("/")
    nd_name = clean_secret(os.getenv("NAVIDROME_USER") or "")
    nd_pass = clean_secret(os.getenv("NAVIDROME_PASSWORD") or "")
    if nd_url:
        nd_update["url"] = nd_url
    if nd_name:
        nd_update["username"] = nd_name
    if nd_pass:
        nd_update["password"] = nd_pass

    if not env_user and not env_pass and not nd_update:
        return

    admin = _admin_user()
    if admin is None:
        if env_pass:
            ensure_setup_user(env_user or "musicplay", env_pass)
            admin = _admin_user()
        else:
            return
    if admin is None:
        return

    changes = []
    try:
        kwargs: dict = {}
        if env_user and _key(env_user) != _key(admin["username"]):
            kwargs["new_username"] = env_user
            changes.append("Benutzername")
        if env_pass and env_pass != (admin.get("password") or ""):
            kwargs["password"] = env_pass
            changes.append("Passwort")
        if nd_update:
            kwargs["navidrome"] = nd_update
            changes.append("Navidrome")
        if kwargs:
            save_user(admin["username"], **kwargs)
            logger.info("Admin-Konto aus Container-Vorlage übernommen ({})", ", ".join(changes))
    except (KeyError, ValueError) as exc:
        logger.warning("Admin-Konto aus Vorlage nicht übernommen: {}", exc)
