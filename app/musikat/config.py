"""MusiKat URL, stored like Navidrome/MediaSync in the config share."""

from __future__ import annotations

import json
import threading

from app.core import secret_box
from app.core.settings import CONFIG_DIR, clean_secret, settings

CONFIG_FILE = CONFIG_DIR / "musikat.json"
_lock = threading.Lock()
_cache: dict | None = None


def _defaults() -> dict:
    return {"url": (getattr(settings, "musikat_url", "") or "").rstrip("/")}


def _write_unlocked(data: dict) -> None:
    payload = {"url": data.get("url") or ""}
    secret_box.write_private_file(CONFIG_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_unlocked() -> dict:
    global _cache
    if _cache is not None:
        return dict(_cache)

    data = _defaults()
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("url") is not None:
                data["url"] = clean_secret(str(loaded.get("url") or "")).rstrip("/")
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    _cache = dict(data)
    return dict(_cache)


def load() -> dict:
    with _lock:
        return _read_unlocked()


def save(payload: dict) -> dict:
    global _cache
    data = {"url": clean_secret(str(payload.get("url") or "")).rstrip("/")}
    with _lock:
        _write_unlocked(data)
        _cache = dict(data)
    return dict(data)


def configured() -> bool:
    return bool(resolve_url())


def stored_url() -> str:
    return (load().get("url") or "").rstrip("/")


def resolve_url() -> str:
    """Prefer the saved URL, then env, then the Hub config."""
    url = stored_url()
    if url:
        return url
    env = (getattr(settings, "musikat_url", "") or "").rstrip("/")
    if env:
        return env
    try:
        from app.mediasync import config as ms_config
        from app.mediasync.client import MediaSyncClient

        if not ms_config.configured():
            return ""
        with MediaSyncClient() as client:
            body = client.hub_config()
        found = str((body or {}).get("musikat_url") or "").rstrip("/")
        return found
    except Exception:
        return ""


def public_view() -> dict:
    url = stored_url() or (getattr(settings, "musikat_url", "") or "").rstrip("/")
    return {
        "configured": bool(url),
        "url": url,
    }
