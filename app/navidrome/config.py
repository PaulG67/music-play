"""Navidrome connection stored in the config share, same idea as auth.json.

Env vars are the defaults. Saving under Mehr overwrites them so the Unraid
template does not have to be recreated after the first setup.
Passwords are encrypted on disk.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.core import secret_box
from app.core.settings import CONFIG_DIR, clean_secret, settings
from app.navidrome import context as nd_context

CONFIG_FILE = CONFIG_DIR / "navidrome.json"
_lock = threading.Lock()
_cache: dict | None = None
ROOT_FOLDER = "."


def normalize_import_folder(value: str | None) -> str:
    """`.` or `/` (or empty) means the music library root, not a YouTube subfolder."""
    text = clean_secret(str(value or "")).replace("\\", "/").strip()
    lowered = text.rstrip("/")
    if lowered in {"", ".", "./", "/"}:
        return ROOT_FOLDER
    parts = [part for part in lowered.split("/") if part and part not in {".", ".."}]
    return "/".join(parts) or ROOT_FOLDER


def is_import_root(folder: str | None) -> bool:
    return normalize_import_folder(folder) == ROOT_FOLDER


def _host_music() -> str:
    return (settings.navidrome_music_host or "/mnt/user/music").rstrip("/")


def import_paths(folder: str | None, music_dir: str | None = None) -> dict:
    container = (music_dir or "/music").rstrip("/") or "/music"
    host = _host_music()
    normalized = normalize_import_folder(folder)
    if normalized == ROOT_FOLDER:
        return {
            "importFolder": ROOT_FOLDER,
            "importFolderIsRoot": True,
            "importPath": container,
            "importPathUnraid": host,
        }
    return {
        "importFolder": normalized,
        "importFolderIsRoot": False,
        "importPath": f"{container}/{normalized}",
        "importPathUnraid": f"{host}/{normalized}",
    }


def _defaults() -> dict:
    return {
        "url": (settings.navidrome_url or "").rstrip("/"),
        "username": clean_secret(settings.navidrome_user),
        "password": clean_secret(settings.navidrome_password),
        "music_dir": settings.navidrome_music_dir or "/music",
        "import_folder": settings.navidrome_import_folder or "YouTube",
    }


def _write_unlocked(data: dict) -> None:
    payload = {
        "url": data.get("url") or "",
        "username": data.get("username") or "",
        "password": secret_box.seal(data.get("password") or ""),
        "music_dir": data.get("music_dir") or "/music",
        "import_folder": normalize_import_folder(data.get("import_folder")),
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
                if loaded.get("url") is not None:
                    data["url"] = clean_secret(str(loaded.get("url") or "")).rstrip("/")
                if loaded.get("username") is not None:
                    data["username"] = clean_secret(str(loaded.get("username") or ""))
                if loaded.get("password") is not None:
                    stored = str(loaded.get("password") or "")
                    data["password"] = secret_box.unseal(stored)
                    migrate = secret_box.needs_migration(stored)
                if loaded.get("music_dir") is not None:
                    data["music_dir"] = clean_secret(str(loaded.get("music_dir") or data["music_dir"])) or data["music_dir"]
                if loaded.get("import_folder") is not None:
                    data["import_folder"] = normalize_import_folder(loaded.get("import_folder"))
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    elif data.get("password"):
        migrate = True

    if migrate:
        try:
            _write_unlocked(data)
        except OSError:
            pass

    _cache = dict(data)
    return dict(_cache)


def server_settings() -> dict:
    with _lock:
        return _read_unlocked()


def load() -> dict:
    data = server_settings()
    overlay = nd_context.current()
    if not overlay:
        return data
    merged = dict(data)
    for key in ("url", "username", "password"):
        value = overlay.get(key)
        if value:
            merged[key] = value
    return merged


def save(payload: dict) -> dict:
    global _cache
    current = server_settings()
    password = payload.get("password")
    if password is None:
        password = current.get("password") or ""
    data = {
        "url": clean_secret(str(payload.get("url") or "")).rstrip("/"),
        "username": clean_secret(str(payload.get("username") or "")),
        "password": clean_secret(str(password or "")),
        "music_dir": clean_secret(str(payload.get("music_dir") or current.get("music_dir") or "/music")) or "/music",
        "import_folder": normalize_import_folder(
            payload["import_folder"] if "import_folder" in payload else current.get("import_folder")
        ),
    }
    with _lock:
        _write_unlocked(data)
        _cache = dict(data)
    return dict(data)


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def configured() -> bool:
    data = load()
    return bool(data.get("url") and data.get("username") and data.get("password"))


def music_dir() -> Path:
    return Path(load().get("music_dir") or "/music")


def import_root() -> Path:
    root = music_dir()
    folder = normalize_import_folder(load().get("import_folder"))
    if folder == ROOT_FOLDER:
        return root
    return root / folder


def music_dir_writable() -> bool:
    path = music_dir()
    try:
        if not path.exists() or not path.is_dir():
            return False
        return os.access(path, os.W_OK)
    except OSError:
        return False


def public_view() -> dict:
    data = load()
    music = data.get("music_dir") or "/music"
    paths = import_paths(data.get("import_folder"), music)
    return {
        "configured": configured(),
        "url": data.get("url") or "",
        "username": data.get("username") or "",
        "passwordSet": bool(data.get("password")),
        "musicDir": music,
        "musicHostDir": _host_music(),
        "importFolder": paths["importFolder"],
        "importFolderIsRoot": paths["importFolderIsRoot"],
        "importPath": paths["importPath"],
        "importPathUnraid": paths["importPathUnraid"],
        "musicDirWritable": music_dir_writable(),
    }
