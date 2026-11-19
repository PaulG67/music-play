"""OIDC provider settings stored in the config share."""

from __future__ import annotations

import json
import threading

from app.core import secret_box
from app.core.settings import CONFIG_DIR, clean_secret, settings

CONFIG_FILE = CONFIG_DIR / "oidc.json"
_lock = threading.Lock()
_cache: dict | None = None

AUTH_MODES = ("password", "oidc", "both", "internal_both")
DEFAULT_INTERNAL = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128"


def _defaults() -> dict:
    return {
        "enabled": False,
        "issuer": clean_secret(getattr(settings, "oidc_issuer", "") or "").rstrip("/"),
        "client_id": clean_secret(getattr(settings, "oidc_client_id", "") or ""),
        "client_secret": clean_secret(getattr(settings, "oidc_client_secret", "") or ""),
        "redirect_uri": clean_secret(getattr(settings, "oidc_redirect_uri", "") or "").rstrip("/"),
        "display_name": clean_secret(getattr(settings, "oidc_display_name", "") or "") or "OIDC",
        "auth_mode": clean_secret(getattr(settings, "oidc_auth_mode", "") or "") or "password",
        "internal_networks": clean_secret(getattr(settings, "oidc_internal_networks", "") or "")
        or DEFAULT_INTERNAL,
        "scopes": clean_secret(getattr(settings, "oidc_scopes", "") or "") or "openid profile email",
        "username_claim": clean_secret(getattr(settings, "oidc_username_claim", "") or "")
        or "preferred_username",
        "auto_create_users": True,
    }


def _normalize_mode(value: object) -> str:
    mode = clean_secret(str(value or "")).casefold()
    if mode in AUTH_MODES:
        return mode
    return "password"


def _write_unlocked(data: dict) -> None:
    payload = {
        "enabled": bool(data.get("enabled")),
        "issuer": data.get("issuer") or "",
        "client_id": data.get("client_id") or "",
        "client_secret": secret_box.seal(data.get("client_secret") or ""),
        "redirect_uri": data.get("redirect_uri") or "",
        "display_name": data.get("display_name") or "OIDC",
        "auth_mode": _normalize_mode(data.get("auth_mode")),
        "internal_networks": data.get("internal_networks") or DEFAULT_INTERNAL,
        "scopes": data.get("scopes") or "openid profile email",
        "username_claim": data.get("username_claim") or "preferred_username",
        "auto_create_users": bool(data.get("auto_create_users", True)),
    }
    secret_box.write_private_file(CONFIG_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_unlocked() -> dict:
    global _cache
    if _cache is not None:
        return dict(_cache)

    data = _defaults()
    migrate = False
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                if "enabled" in loaded:
                    data["enabled"] = bool(loaded.get("enabled"))
                for key in (
                    "issuer",
                    "client_id",
                    "redirect_uri",
                    "display_name",
                    "internal_networks",
                    "scopes",
                    "username_claim",
                ):
                    if loaded.get(key) is not None:
                        value = clean_secret(str(loaded.get(key) or ""))
                        if key in {"issuer", "redirect_uri"}:
                            value = value.rstrip("/")
                        data[key] = value
                if loaded.get("client_secret") is not None:
                    stored = str(loaded.get("client_secret") or "")
                    data["client_secret"] = secret_box.unseal(stored)
                    migrate = secret_box.needs_migration(stored)
                if loaded.get("auth_mode") is not None:
                    data["auth_mode"] = _normalize_mode(loaded.get("auth_mode"))
                if "auto_create_users" in loaded:
                    data["auto_create_users"] = bool(loaded.get("auto_create_users"))
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    elif data.get("client_secret") or data.get("issuer"):
        migrate = True

    data["auth_mode"] = _normalize_mode(data.get("auth_mode"))
    if not data.get("display_name"):
        data["display_name"] = "OIDC"

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


def save(payload: dict) -> dict:
    global _cache
    current = load()
    secret = payload.get("client_secret")
    if secret is None or str(secret).strip() == "":
        secret = current.get("client_secret") or ""
    enabled = payload.get("enabled")
    if enabled is None:
        enabled = current.get("enabled")
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"0", "false", "off", "no"}
    auto_create = payload.get("auto_create_users")
    if auto_create is None:
        auto_create = current.get("auto_create_users", True)
    if isinstance(auto_create, str):
        auto_create = auto_create.strip().lower() not in {"0", "false", "off", "no"}

    data = {
        "enabled": bool(enabled),
        "issuer": clean_secret(str(payload.get("issuer") or "")).rstrip("/"),
        "client_id": clean_secret(str(payload.get("client_id") or "")),
        "client_secret": clean_secret(str(secret or "")),
        "redirect_uri": clean_secret(str(payload.get("redirect_uri") or "")).rstrip("/"),
        "display_name": clean_secret(str(payload.get("display_name") or "")) or "OIDC",
        "auth_mode": _normalize_mode(payload.get("auth_mode") or current.get("auth_mode")),
        "internal_networks": clean_secret(
            str(payload.get("internal_networks") or current.get("internal_networks") or DEFAULT_INTERNAL)
        )
        or DEFAULT_INTERNAL,
        "scopes": clean_secret(str(payload.get("scopes") or current.get("scopes") or ""))
        or "openid profile email",
        "username_claim": clean_secret(
            str(payload.get("username_claim") or current.get("username_claim") or "")
        )
        or "preferred_username",
        "auto_create_users": bool(auto_create),
    }
    with _lock:
        _write_unlocked(data)
        _cache = dict(data)
    return dict(data)


def configured() -> bool:
    data = load()
    return bool(data.get("enabled") and data.get("issuer") and data.get("client_id"))


def public_view() -> dict:
    data = load()
    return {
        "configured": configured(),
        "enabled": bool(data.get("enabled")),
        "issuer": data.get("issuer") or "",
        "clientId": data.get("client_id") or "",
        "clientSecretSet": bool(data.get("client_secret")),
        "redirectUri": data.get("redirect_uri") or "",
        "displayName": data.get("display_name") or "OIDC",
        "authMode": _normalize_mode(data.get("auth_mode")),
        "internalNetworks": data.get("internal_networks") or DEFAULT_INTERNAL,
        "scopes": data.get("scopes") or "openid profile email",
        "usernameClaim": data.get("username_claim") or "preferred_username",
        "autoCreateUsers": bool(data.get("auto_create_users", True)),
        "authModes": list(AUTH_MODES),
    }
