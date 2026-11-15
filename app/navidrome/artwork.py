"""Find album artwork and embed it into downloaded audio files."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.logging import logger
from app.core.settings import APP_VERSION, settings
from app.db import repo
from app.db.database import session_scope
from app.navidrome import config
from app.navidrome import ids as nd_ids
from app.navidrome.client import NavidromeClient
from app.ytm import client as ytm
from app.ytm import mapper

_AUDIO = {".m4a", ".mp4", ".mp3", ".flac", ".ogg", ".opus", ".aac"}
_HEADERS = {"User-Agent": f"MusicPlay/{APP_VERSION} (https://github.com/PaulG67/music-play)"}


def _music_root() -> Path:
    return config.music_dir().resolve()


def _inside_library(path: Path) -> bool:
    try:
        path.resolve().relative_to(_music_root())
        return True
    except (ValueError, OSError):
        return False


def resolve_audio_path(track: dict) -> Path | None:
    """Locate the file on disk for a downloaded / Navidrome track."""
    from app.navidrome import importer

    candidates: list[Path] = []
    for raw in (track.get("path"), track.get("filePath")):
        if raw:
            candidates.append(Path(str(raw)))

    nd_id = str(track.get("navidromeId") or nd_ids.unwrap(track.get("id")) or "")
    if nd_id:
        try:
            with NavidromeClient() as client:
                song = client.get_song(nd_id)
            if song and song.get("path"):
                candidates.append(Path(str(song["path"])))
        except Exception as exc:
            logger.debug("Song-Pfad von Navidrome nicht lesbar: {}", exc)

    video_id = str(track.get("videoId") or track.get("id") or "")
    if video_id and not video_id.startswith("nd:"):
        with session_scope() as session:
            link = repo.get_navidrome_link(session, video_id)
        if link and link.get("path"):
            candidates.append(Path(str(link["path"])))
        try:
            candidates.append(importer._destination(track))
        except Exception:
            pass

    root = _music_root()
    seen: set[str] = set()
    for path in candidates:
        options = [path]
        if not path.is_absolute():
            options.append(root / path)
        elif str(path).startswith("/music/") and root != Path("/music"):
            options.append(root / str(path).lstrip("/").split("/", 1)[-1])
        for option in options:
            try:
                resolved = option.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            if resolved.is_file() and resolved.suffix.lower() in _AUDIO and _inside_library(resolved):
                return resolved
    return None


def _candidate(url: str, source: str, label: str) -> dict | None:
    url = (url or "").strip()
    if not url or url.startswith("data:"):
        return None
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return {"id": digest, "url": url, "source": source, "label": label}


def _itunes_covers(artist: str, album: str) -> list[dict]:
    query = " ".join(part for part in (artist, album) if part).strip()
    if not query:
        return []
    try:
        response = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": query, "entity": "album", "limit": 8},
            headers=_HEADERS,
            timeout=12.0,
        )
        response.raise_for_status()
        rows = response.json().get("results") or []
    except Exception as exc:
        logger.debug("iTunes-Cover-Suche fehlgeschlagen: {}", exc)
        return []
    found = []
    for row in rows:
        art = str(row.get("artworkUrl100") or "")
        if not art:
            continue
        art = art.replace("100x100bb", "1200x1200bb").replace("100x100", "1200x1200")
        name = row.get("collectionName") or album or "Album"
        found.append(_candidate(art, "iTunes", name))
    return [item for item in found if item]


def _deezer_covers(artist: str, album: str) -> list[dict]:
    query = " ".join(part for part in (artist, album) if part).strip()
    if not query:
        return []
    try:
        response = httpx.get(
            "https://api.deezer.com/search/album",
            params={"q": query, "limit": 8},
            headers=_HEADERS,
            timeout=12.0,
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
    except Exception as exc:
        logger.debug("Deezer-Cover-Suche fehlgeschlagen: {}", exc)
        return []
    found = []
    for row in rows:
        art = str(row.get("cover_xl") or row.get("cover_big") or "")
        if not art:
            continue
        name = row.get("title") or album or "Album"
        found.append(_candidate(art, "Deezer", name))
    return [item for item in found if item]


def _youtube_covers(track: dict) -> list[dict]:
    found = []
    thumb = mapper.upscale_thumbnail(track.get("thumbnail"), 1200) if track.get("thumbnail") else None
    if thumb and not str(thumb).startswith("/"):
        found.append(_candidate(thumb, "YouTube Music", "Video-/Albumvorschaubild"))
    album_id = track.get("albumId") or track.get("album_id")
    if album_id and not str(album_id).startswith("nd:"):
        try:
            album = ytm.album(str(album_id))
        except Exception:
            album = None
        if album and album.get("thumbnail"):
            found.append(_candidate(mapper.upscale_thumbnail(album["thumbnail"], 1200) or album["thumbnail"], "YouTube Music", album.get("name") or "Album"))
    query = " ".join(part for part in (track.get("artist"), track.get("album") or track.get("title")) if part).strip()
    if query:
        try:
            albums = ytm.search(query, scope="albums", limit=8) or []
        except Exception as exc:
            logger.debug("YouTube-Album-Suche fehlgeschlagen: {}", exc)
            albums = []
        for album in albums:
            url = album.get("thumbnail")
            if url:
                found.append(
                    _candidate(
                        mapper.upscale_thumbnail(url, 1200) or url,
                        "YouTube Music",
                        album.get("name") or album.get("title") or "Album",
                    )
                )
    return [item for item in found if item]


def search_covers(track: dict) -> list[dict]:
    artist = str(track.get("artist") or "").strip()
    album = str(track.get("album") or "").strip()
    items = []
    items.extend(_youtube_covers(track))
    items.extend(_itunes_covers(artist, album or str(track.get("title") or "")))
    items.extend(_deezer_covers(artist, album or str(track.get("title") or "")))
    unique = []
    seen = set()
    for item in items:
        key = item["url"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= 12:
            break
    return unique


def _fetch_image(url: str) -> tuple[bytes, str]:
    raw = (url or "").strip()
    if raw.startswith("/api/cover"):
        parsed = urlparse(raw)
        nested = (parse_qs(parsed.query).get("u") or [""])[0]
        if nested:
            raw = nested
    if raw.startswith("/"):
        raise RuntimeError("Cover-URL nicht erreichbar")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("Ungültige Cover-URL")
    response = httpx.get(raw, headers=_HEADERS, timeout=20.0, follow_redirects=True)
    response.raise_for_status()
    data = response.content
    if len(data) < 64:
        raise RuntimeError("Cover-Datei ist leer")
    mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}:
        if data[:2] == b"\xff\xd8":
            mime = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:4] == b"RIFF":
            mime = "image/webp"
        else:
            raise RuntimeError("Kein Bild unter dieser Adresse")
    return data, mime


def _to_jpeg(data: bytes, mime: str) -> bytes:
    if mime in {"image/jpeg", "image/jpg"} and data[:2] == b"\xff\xd8":
        return data
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "in.bin"
        dest = Path(folder) / "out.jpg"
        source.write_bytes(data)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(dest),
        ]
        subprocess.run(command, check=True, timeout=30)
        if not dest.exists() or dest.stat().st_size < 32:
            raise RuntimeError("Cover konnte nicht nach JPEG gewandelt werden")
        return dest.read_bytes()


def embed_into_file(path: Path, jpeg: bytes) -> None:
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover

    suffix = path.suffix.lower()
    if suffix in {".m4a", ".mp4", ".aac"}:
        audio = MP4(path)
        audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return
    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        tags.save(path)
        return
    if suffix == ".flac":
        audio = FLAC(path)
        picture = Picture()
        picture.type = 3
        picture.mime = "image/jpeg"
        picture.desc = "Cover"
        picture.data = jpeg
        audio.clear_pictures()
        audio.add_picture(picture)
        audio.save()
        return
    raise RuntimeError(f"Cover-Einbettung für {suffix or 'diese Datei'} nicht unterstützt")


def _write_folder_image(folder: Path, jpeg: bytes) -> None:
    for name in ("cover.jpg", "folder.jpg"):
        target = folder / name
        try:
            target.write_bytes(jpeg)
        except OSError as exc:
            logger.debug("Cover-Datei {} nicht schreibbar: {}", target, exc)


def embed_cover(track: dict, url: str, *, album: bool = False) -> dict:
    path = resolve_audio_path(track)
    if path is None:
        raise RuntimeError(
            "Audiodatei nicht gefunden. Der Titel muss in der Musikbibliothek liegen "
            f"({config.music_dir()})."
        )
    data, mime = _fetch_image(url)
    jpeg = _to_jpeg(data, mime)
    targets = [path]
    if album:
        targets = [
            item
            for item in sorted(path.parent.iterdir())
            if item.is_file() and item.suffix.lower() in _AUDIO and _inside_library(item)
        ] or [path]
    embedded = 0
    for target in targets:
        try:
            embed_into_file(target, jpeg)
            embedded += 1
        except Exception as exc:
            logger.warning("Cover in {} nicht eingebettet: {}", target.name, exc)
            raise RuntimeError(f"Cover konnte nicht in {target.name} geschrieben werden: {exc}") from exc
    _write_folder_image(path.parent, jpeg)
    try:
        with NavidromeClient() as client:
            client.start_scan()
    except Exception as exc:
        logger.debug("Navidrome-Scan nach Cover: {}", exc)
    return {
        "ok": True,
        "path": str(path),
        "files": embedded,
        "folder": str(path.parent),
        "folderHost": f"{settings.navidrome_music_host.rstrip('/')}/{path.parent.relative_to(_music_root()).as_posix()}"
        if _inside_library(path.parent)
        else str(path.parent),
    }


def embed_image_file(audio_path: Path, image_path: Path) -> None:
    if not audio_path.exists() or not image_path.exists():
        return
    data = image_path.read_bytes()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(image_path.suffix.lower(), "image/jpeg")
    jpeg = _to_jpeg(data, mime)
    embed_into_file(audio_path, jpeg)
    _write_folder_image(audio_path.parent, jpeg)
