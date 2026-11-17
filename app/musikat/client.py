"""HTTP client for the MusiKat FastAPI (search Deezer, download to Navidrome)."""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from app.core.logging import logger
from app.musikat import config
from app.navidrome import config as nd_config


class MusiKatError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, *, exists: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.exists = exists


class MusiKatClient:
    def __init__(self, base: str | None = None):
        self.base = (base or config.resolve_url()).rstrip("/")
        self._http = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(12.0, read=45.0),
            headers={"Accept": "application/json", "User-Agent": "MusicPlay"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MusiKatClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def ready(self) -> bool:
        return bool(self.base)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base}{path}"

    def _error(self, response: httpx.Response) -> str:
        try:
            body = response.json()
            detail = body.get("detail") or body.get("error") or body.get("message")
            if isinstance(detail, str) and detail:
                return detail
        except ValueError:
            pass
        return f"MusiKat HTTP {response.status_code}"

    def ping(self) -> dict:
        try:
            response = self._http.get(self._url("/api/metadata/providers"), timeout=8.0)
            response.raise_for_status()
            payload = response.json() if response.content else {}
        except httpx.HTTPError as exc:
            raise MusiKatError(f"MusiKat nicht erreichbar: {exc}") from exc
        return {"ok": True, "version": payload.get("default") or "ok"}

    def libraries(self) -> list[dict]:
        try:
            response = self._http.get(self._url("/api/navidrome/libraries"), timeout=8.0)
            if response.status_code >= 400:
                return []
            payload = response.json()
        except Exception:
            return []
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        rows = payload.get("libraries") or payload.get("items") or []
        return [row for row in rows if isinstance(row, dict)]

    def _navidrome_library(self) -> str | None:
        want = str(nd_config.music_dir()).replace("\\", "/").rstrip("/")
        for row in self.libraries():
            path = str(row.get("path") or row.get("id") or "").replace("\\", "/").rstrip("/")
            if path and (path == want or path.endswith("/music") and want.endswith("/music")):
                return path
        return None

    def search(self, query: str, limit: int = 8) -> list[dict]:
        if not query.strip():
            return []
        response = self._http.post(
            self._url("/api/search"),
            json={"query": query.strip(), "limit": max(1, min(limit, 20)), "provider": "deezer"},
        )
        if response.status_code >= 400:
            raise MusiKatError(self._error(response), response.status_code)
        payload = response.json() if response.content else []
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        rows = payload.get("tracks") or payload.get("results") or []
        return [row for row in rows if isinstance(row, dict)]

    def start_download(self, track_id: str, *, quality: str = "320", fmt: str = "mp3") -> dict:
        payload = {
            "track_id": str(track_id),
            "location": "navidrome",
            "format": fmt,
            "quality": str(quality),
            "provider": "deezer",
            "max_retries": 2,
        }
        library = self._navidrome_library()
        if library:
            payload["navidrome_library"] = library
        response = self._http.post(self._url("/api/download"), json=payload)
        if response.status_code == 409:
            raise MusiKatError(self._error(response), 409, exists=True)
        if response.status_code >= 400:
            raise MusiKatError(self._error(response), response.status_code)
        return response.json() if response.content else {}

    def download_status(self, track_id: str) -> dict:
        response = self._http.get(self._url(f"/api/download/status/{track_id}"))
        if response.status_code >= 400:
            raise MusiKatError(self._error(response), response.status_code)
        return response.json() if response.content else {}

    def wait_download(self, track_id: str, timeout: int = 240, progress=None) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            last = self.download_status(track_id)
            status = str(last.get("status") or "").lower()
            if callable(progress):
                progress(str(last.get("message") or f"MusiKat lädt … ({status or 'läuft'})"))
            if status in {"completed", "complete", "done", "error", "failed"}:
                return last
            time.sleep(2)
        raise MusiKatError("MusiKat braucht zu lange für den Download.")


def map_library_file(file_path: str) -> Path | None:
    raw = (file_path or "").strip()
    if not raw:
        return None
    direct = Path(raw)
    try:
        if direct.is_file():
            return direct
    except OSError:
        pass
    text = raw.replace("\\", "/")
    rel = ""
    if "/music/" in text:
        rel = text.split("/music/", 1)[-1]
    if rel:
        mapped = nd_config.music_dir() / rel
        if mapped.is_file():
            return mapped
    return None


def search_tracks(artist: str, title: str) -> list[dict]:
    query = " ".join(part for part in (artist, title) if part).strip()
    if not query or not config.resolve_url():
        return []
    try:
        with MusiKatClient() as client:
            return client.search(query, limit=8)
    except Exception as exc:
        logger.warning("MusiKat-Suche fehlgeschlagen: {}", exc)
        return []


def download_track(track_id: str, progress=None) -> dict:
    if not track_id:
        raise MusiKatError("MusiKat-Titel-Id fehlt")
    with MusiKatClient() as client:
        started = client.start_download(track_id)
        if callable(progress):
            progress(str(started.get("message") or "MusiKat-Download gestartet …"))
        return client.wait_download(track_id, progress=progress)
