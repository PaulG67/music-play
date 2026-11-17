"""Find album artwork and embed it into downloaded audio files."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
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
from app.navidrome.paths import AUDIO_SUFFIXES, try_resolve_library_rel_path
from app.ytm import client as ytm
from app.ytm import mapper

_AUDIO = AUDIO_SUFFIXES
_HEADERS = {"User-Agent": f"MusicPlay/{APP_VERSION} (https://github.com/PaulG67/music-play)"}
_MB_HEADERS = {
    "User-Agent": f"MusicPlay/{APP_VERSION} ( https://github.com/PaulG67/music-play )",
    "Accept": "application/json",
}
_PLACEHOLDER_ALBUMS = {"", "singles", "unknown", "unbekannt", "single", "various", "va"}


def _music_root() -> Path:
    return config.music_dir().resolve()


def _inside_library(path: Path) -> bool:
    try:
        path.resolve().relative_to(_music_root())
        return True
    except (ValueError, OSError):
        return False


def _posix(value: Path | str) -> str:
    return str(value or "").replace("\\", "/").strip()


def library_relative_path(track: dict, dest: Path | None = None) -> str:
    """Relative path under the shared music folder (for MediaSync / Navidrome)."""
    if dest is not None:
        try:
            return dest.resolve().relative_to(_music_root()).as_posix()
        except (ValueError, OSError):
            pass
    for raw in (track.get("filePath"), track.get("path")):
        relative = _reported_to_relative(raw)
        if relative:
            return relative
    return dest.name if dest is not None else ""


def _reported_to_relative(raw: object) -> str:
    text = _posix(raw)
    if not text:
        return ""
    if re.match(r"^[A-Za-z]:/", text):
        text = text[2:]
    lowered = text.casefold()
    for marker in ("/music/", "/musik/"):
        index = lowered.rfind(marker)
        if index >= 0:
            return text[index + len(marker) :].lstrip("/")
    host = _posix(settings.navidrome_music_host).rstrip("/")
    if host and (text == host or text.startswith(host + "/")):
        return text[len(host) :].lstrip("/")
    root = _posix(config.music_dir()).rstrip("/")
    if root and (text == root or text.startswith(root + "/")):
        return text[len(root) :].lstrip("/")
    if not text.startswith("/") and not text.startswith("mnt/"):
        return text.lstrip("./")
    return ""


def _mapped_audio_options(path: Path, root: Path) -> list[Path]:
    options = [path]
    text = _posix(path)
    relative = _reported_to_relative(text)
    if relative:
        options.append(root / relative)
    if not path.is_absolute():
        options.append(root / path)
    host = _posix(settings.navidrome_music_host).rstrip("/")
    if host and text.startswith(host + "/"):
        options.append(root / text[len(host) + 1 :])
    parts = Path(text).parts
    folded = [part.casefold() for part in parts]
    for folder in ("music", "musik"):
        if folder in folded:
            index = folded.index(folder)
            if index + 1 < len(parts):
                options.append(root.joinpath(*parts[index + 1 :]))
            break
    return options


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
            if song:
                for key in ("filePath", "path"):
                    if song.get(key):
                        candidates.append(Path(str(song[key])))
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
    relative_candidates: list[str] = []
    for path in candidates:
        relative = _reported_to_relative(path)
        if relative:
            relative_candidates.append(relative)
        for option in _mapped_audio_options(path, root):
            try:
                resolved = option.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            try:
                relative_candidates.append(
                    resolved.relative_to(root).as_posix()
                )
            except (ValueError, OSError):
                option_rel = _reported_to_relative(option)
                if option_rel:
                    relative_candidates.append(option_rel)
            audio = resolved.is_file() and (
                resolved.suffix.lower() in _AUDIO or not resolved.suffix
            )
            if audio and (_inside_library(resolved) or resolved.is_file()):
                if not _inside_library(resolved):
                    logger.warning("Audiodatei außerhalb von {}: {}", root, resolved)
                return resolved

    # Tolerant resolve: case, Unicode, track prefix, fuzzy album/filename
    seen_rel: set[str] = set()
    for relative in relative_candidates:
        key = relative.casefold()
        if key in seen_rel:
            continue
        seen_rel.add(key)
        found = try_resolve_library_rel_path(root, relative)
        if found is not None and found.is_file():
            return found

    return _guess_audio_path(track)


def _guess_audio_path(track: dict) -> Path | None:
    from app.navidrome.importer import _safe

    root = _music_root()
    artist = _safe(track.get("artist"), "")
    album = _safe(track.get("album"), "")
    title = _safe(track.get("title"), "")
    folders = []
    if artist and album:
        folders.append(root / artist / album)
        folders.append(config.import_root() / artist / album)
    if artist:
        artist_dir = root / artist
        if artist_dir.is_dir():
            folders.extend(sorted(p for p in artist_dir.iterdir() if p.is_dir())[:12])
    needle = title.casefold()
    for folder in folders:
        if not folder.is_dir():
            continue
        files = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in _AUDIO]
        files.sort(
            key=lambda path: (
                bool(re.search(r"\(\d+\)$", path.stem)),
                path.suffix.lower() in {".opus", ".webm", ".ogg"},
                path.name.casefold(),
            )
        )
        if needle:
            match = next((path for path in files if needle in path.stem.casefold()), None)
            if match and _inside_library(match):
                return match
        if len(files) == 1 and _inside_library(files[0]):
            return files[0]
    return None


def _candidate(url: str, source: str, label: str, *, original: bool = True, artist: str = "") -> dict | None:
    url = (url or "").strip()
    if not url or url.startswith("data:"):
        return None
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return {"id": digest, "url": url, "source": source, "label": label, "original": original, "artist": artist}


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def _tokens(text: str) -> list[str]:
    stop = {
        "the", "a", "an", "and", "feat", "ft", "official", "video", "audio", "lyric", "lyrics",
        "single", "ep", "album", "deluxe", "remastered", "vol", "volume", "ost", "soundtrack",
    }
    return [token for token in _norm(text).split() if len(token) > 1 and token not in stop]


def _artist_matches(expected: str, *got: str) -> bool:
    want = _tokens(expected)
    if not want:
        return True
    hay = set()
    for value in got:
        hay.update(_tokens(value))
    hits = 0
    for token in want:
        if token in hay or any(token in other or other in token for other in hay if min(len(token), len(other)) >= 4):
            hits += 1
    return hits >= max(1, (len(want) + 1) // 2)


def _itunes_art(url: str) -> str:
    art = url or ""
    for size in ("100x100bb", "60x60bb", "100x100", "60x60"):
        art = art.replace(size, "1200x1200bb" if "bb" in size else "1200x1200")
    return art


def _itunes_covers(artist: str, album: str, title: str = "") -> list[dict]:
    terms = []
    for term in (f"{artist} {title}".strip(), f"{artist} {album}".strip(), artist):
        if term and term not in terms:
            terms.append(term)
    countries = []
    for country in ("US", (settings.ytm_location or "DE")[:2].upper(), "GB"):
        if country and country not in countries:
            countries.append(country)
    found = []
    seen = set()
    for entity in ("song", "album"):
        for country in countries[:2]:
            for term in terms[:2]:
                try:
                    response = httpx.get(
                        "https://itunes.apple.com/search",
                        params={"term": term, "limit": 12, "country": country, "entity": entity, "media": "music"},
                        headers=_HEADERS,
                        timeout=12.0,
                    )
                    response.raise_for_status()
                    rows = response.json().get("results") or []
                except Exception as exc:
                    logger.warning("iTunes-Cover-Suche fehlgeschlagen: {}", exc)
                    continue
                for row in rows:
                    who = str(row.get("artistName") or "")
                    if not _artist_matches(artist, who, row.get("collectionName") or "", row.get("trackName") or ""):
                        continue
                    art = _itunes_art(str(row.get("artworkUrl100") or row.get("artworkUrl60") or ""))
                    if not art or art in seen:
                        continue
                    seen.add(art)
                    name = row.get("collectionName") or row.get("trackName") or album or title or "Cover"
                    found.append(_candidate(art, "iTunes", f"{who} – {name}" if who else name, artist=who))
    return [item for item in found if item]


def _deezer_covers(artist: str, album: str, title: str = "") -> list[dict]:
    queries = []
    if artist and title:
        queries.append(f'artist:"{artist}" track:"{title}"')
        queries.append(f"{artist} {title}")
    if artist and album and album.casefold() not in _PLACEHOLDER_ALBUMS:
        queries.append(f'artist:"{artist}" album:"{album}"')
        queries.append(f"{artist} {album}")
    found = []
    seen = set()
    endpoints = (
        ("https://api.deezer.com/search/track", "track"),
        ("https://api.deezer.com/search/album", "album"),
    )
    for url, kind in endpoints:
        for query in queries[:3]:
            try:
                response = httpx.get(url, params={"q": query, "limit": 8}, headers=_HEADERS, timeout=12.0)
                response.raise_for_status()
                rows = response.json().get("data") or []
            except Exception as exc:
                logger.warning("Deezer-Cover-Suche fehlgeschlagen: {}", exc)
                continue
            for row in rows:
                if kind == "track":
                    who = str((row.get("artist") or {}).get("name") or "")
                    album_info = row.get("album") or {}
                    art = str(album_info.get("cover_xl") or album_info.get("cover_big") or "")
                    name = album_info.get("title") or row.get("title") or title
                else:
                    who = str((row.get("artist") or {}).get("name") or "")
                    art = str(row.get("cover_xl") or row.get("cover_big") or "")
                    name = row.get("title") or album
                if not art or art in seen or not _artist_matches(artist, who, name):
                    continue
                seen.add(art)
                found.append(_candidate(art, "Deezer", f"{who} – {name}" if who else name, artist=who))
    return [item for item in found if item]


def _audiodb_covers(artist: str, album: str, title: str) -> list[dict]:
    if not artist:
        return []
    found = []
    params_list = []
    if album and album.casefold() not in _PLACEHOLDER_ALBUMS:
        params_list.append({"s": artist, "a": album})
    if title:
        params_list.append({"s": artist, "a": title})
    params_list.append({"s": artist})
    for params in params_list[:2]:
        try:
            response = httpx.get(
                "https://www.theaudiodb.com/api/v1/json/2/searchalbum.php",
                params=params,
                headers=_HEADERS,
                timeout=12.0,
            )
            response.raise_for_status()
            rows = response.json().get("album") or []
        except Exception as exc:
            logger.debug("TheAudioDB-Suche fehlgeschlagen: {}", exc)
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            who = str(row.get("strArtist") or "")
            if not _artist_matches(artist, who):
                continue
            art = str(row.get("strAlbumThumbHQ") or row.get("strAlbumThumb") or "")
            name = row.get("strAlbum") or album or title
            if art:
                found.append(_candidate(art, "TheAudioDB", f"{who} – {name}" if who else name, artist=who))
    return [item for item in found if item]


def _video_id(track: dict) -> str:
    for key in ("videoId", "id"):
        value = str(track.get(key) or "").strip()
        if value and not value.startswith("nd:") and ":" not in value and len(value) >= 8:
            return value
    return ""


def _youtube_frame_covers(video_id: str) -> list[dict]:
    if not video_id:
        return []
    found = []
    for name, suffix in (
        ("YouTube max", "maxresdefault.jpg"),
        ("YouTube HQ", "hqdefault.jpg"),
        ("YouTube SD", "sddefault.jpg"),
    ):
        found.append(_candidate(f"https://i.ytimg.com/vi/{video_id}/{suffix}", "YouTube-Video", name, original=False))
    return [item for item in found if item]


def _youtube_covers(track: dict) -> list[dict]:
    found = []
    thumb = track.get("thumbnail") or ""
    if thumb and not str(thumb).startswith("/"):
        scaled = mapper.upscale_thumbnail(thumb, 1200) or thumb
        found.append(_candidate(scaled, "YouTube Music", "Aktuelles Vorschaubild"))
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
            logger.warning("YouTube-Album-Suche fehlgeschlagen: {}", exc)
            albums = []
        for album in albums:
            url = album.get("thumbnail")
            who = album.get("artist") or ""
            if url and _artist_matches(track.get("artist") or "", who, album.get("name") or album.get("title") or ""):
                found.append(
                    _candidate(
                        mapper.upscale_thumbnail(url, 1200) or url,
                        "YouTube Music",
                        album.get("name") or album.get("title") or "Album",
                        artist=who,
                    )
                )
        try:
            songs = ytm.search(query, scope="songs", limit=6) or []
        except Exception:
            songs = []
        for song in songs:
            url = song.get("thumbnail")
            who = song.get("artist") or ""
            if url and _artist_matches(track.get("artist") or "", who, song.get("title") or ""):
                found.append(
                    _candidate(
                        mapper.upscale_thumbnail(url, 1200) or url,
                        "YouTube Music",
                        song.get("title") or "Titel",
                        original=False,
                        artist=who,
                    )
                )
    found.extend(_youtube_frame_covers(_video_id(track)))
    return [item for item in found if item]


def _enrich_track(track: dict) -> dict:
    data = dict(track or {})
    video_id = _video_id(data)
    if video_id:
        data["videoId"] = video_id
        try:
            song = ytm.song(video_id)
        except Exception:
            song = None
        if song:
            data["title"] = data.get("title") or song.get("title") or ""
            data["artist"] = data.get("artist") or song.get("artist") or ""
            data["album"] = data.get("album") or (song.get("album") if isinstance(song.get("album"), str) else "") or ""
            data["album_id"] = data.get("album_id") or data.get("albumId") or song.get("album_id")
            data["thumbnail"] = data.get("thumbnail") or song.get("thumbnail") or ""
    nd_id = str(data.get("navidromeId") or nd_ids.unwrap(data.get("id")) or "")
    if nd_id:
        data["navidromeId"] = nd_id
        try:
            with NavidromeClient() as client:
                song = client.get_song(nd_id)
        except Exception:
            song = None
        if song:
            data["title"] = song.get("title") or data.get("title") or ""
            data["artist"] = song.get("artist") or data.get("artist") or ""
            data["album"] = song.get("album") or data.get("album") or ""
            data["path"] = song.get("path") or data.get("path") or ""
            data["thumbnail"] = data.get("thumbnail") or song.get("thumbnail") or ""
        with session_scope() as session:
            link = repo.navidrome_link_by_song(session, nd_id)
        if link:
            data["videoId"] = data.get("videoId") or link.get("video_id") or ""
            data["title"] = data.get("title") or link.get("title") or ""
            data["artist"] = data.get("artist") or link.get("artist") or ""
            data["path"] = data.get("path") or link.get("path") or ""
    return data


def _mb_term(value: str) -> str:
    cleaned = re.sub(r'[+\-!(){}\[\]^"~*?:\\/]', " ", value or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _coverart_images(mbid: str, label: str) -> list[dict]:
    try:
        response = httpx.get(
            f"https://coverartarchive.org/release/{mbid}",
            headers=_HEADERS,
            timeout=15.0,
            follow_redirects=True,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        images = response.json().get("images") or []
    except Exception as exc:
        logger.debug("Cover Art Archive {}: {}", mbid, exc)
        return []
    found = []
    for image in images:
        thumbs = image.get("thumbnails") or {}
        url = thumbs.get("1200") or thumbs.get("large") or image.get("image")
        if not url:
            continue
        kind = "Vorderseite" if image.get("front") else (image.get("types") or ["Cover"])[0]
        found.append(_candidate(url, "Cover Art Archive", f"{label} · {kind}".strip(" ·")))
        if image.get("front"):
            break
    return [item for item in found if item]


def _musicbrainz_covers(artist: str, album: str, title: str) -> list[dict]:
    artist_q = _mb_term(artist)
    album_q = _mb_term(album)
    title_q = _mb_term(title)
    if not artist_q:
        return []
    queries = []
    if title_q:
        queries.append(("recording", f'recording:"{title_q}" AND artist:"{artist_q}"'))
    if album_q and album_q.casefold() not in _PLACEHOLDER_ALBUMS:
        queries.append(("release", f'release:"{album_q}" AND artist:"{artist_q}"'))
    if not queries:
        return []
    mbids: list[tuple[str, str]] = []
    for index, (kind, query) in enumerate(queries[:2]):
        if index:
            time.sleep(1.1)
        try:
            response = httpx.get(
                f"https://musicbrainz.org/ws/2/{kind}/",
                params={"query": query, "fmt": "json", "limit": 5},
                headers=_MB_HEADERS,
                timeout=15.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("MusicBrainz-Suche fehlgeschlagen: {}", exc)
            continue
        if kind == "release":
            for row in payload.get("releases") or []:
                mbid = str(row.get("id") or "")
                name = row.get("title") or album_q or "Album"
                if mbid:
                    mbids.append((mbid, name))
        else:
            for row in payload.get("recordings") or []:
                for release in row.get("releases") or []:
                    mbid = str(release.get("id") or "")
                    name = release.get("title") or album_q or "Album"
                    if mbid:
                        mbids.append((mbid, name))
        if len(mbids) >= 5:
            break
    unique_ids = []
    seen = set()
    for mbid, name in mbids:
        if mbid in seen:
            continue
        seen.add(mbid)
        unique_ids.append((mbid, name))
        if len(unique_ids) >= 5:
            break
    found = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch in pool.map(lambda item: _coverart_images(item[0], item[1]), unique_ids):
            found.extend(batch)
    return found


def search_covers(track: dict) -> dict:
    data = _enrich_track(track)
    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()
    title = str(data.get("title") or "").strip()
    album_query = album if album.casefold() not in _PLACEHOLDER_ALBUMS else title
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = [
            pool.submit(_musicbrainz_covers, artist, album_query, title),
            pool.submit(_itunes_covers, artist, album_query, title),
            pool.submit(_deezer_covers, artist, album_query, title),
            pool.submit(_audiodb_covers, artist, album_query, title),
            pool.submit(_youtube_covers, data),
        ]
        for job in jobs:
            try:
                items.extend(job.result())
            except Exception as exc:
                logger.warning("Cover-Quelle fehlgeschlagen: {}", exc)
    unique = []
    seen = set()
    filtered = [
        row
        for row in items
        if row and (not artist or _artist_matches(artist, row.get("artist") or "", row.get("label") or ""))
    ]
    if not filtered:
        filtered = [row for row in items if row]
    for item in sorted(
        filtered,
        key=lambda row: (not row.get("original", True), row.get("source") == "YouTube-Video"),
    ):
        key = item["url"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= 12:
            break
    originals = [item for item in unique if item.get("original")]
    if originals:
        unique = originals + [item for item in unique if not item.get("original")][:3]
        unique = unique[:12]
    path = resolve_audio_path(data)
    return {
        "candidates": unique,
        "artist": artist,
        "album": album or album_query,
        "title": title,
        "query": " – ".join(part for part in (artist, album_query or title) if part),
        "file": str(path) if path else "",
        "fileFound": bool(path),
        "navidromeId": data.get("navidromeId") or "",
        "path": data.get("path") or (str(path) if path else ""),
    }


def _fetch_image(url: str) -> tuple[bytes, str]:
    raw = (url or "").strip()
    if raw.startswith("/api/cover"):
        parsed = urlparse(raw)
        nested = (parse_qs(parsed.query).get("u") or [""])[0]
        if nested:
            raw = nested
    if raw.startswith("/api/navidrome/cover/"):
        cover_id = raw.split("/api/navidrome/cover/", 1)[1].split("?", 1)[0]
        with NavidromeClient() as client:
            raw = client.cover_url(cover_id, 1200)
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
    if suffix in {".opus", ".ogg"}:
        import base64

        from mutagen.oggopus import OggOpus
        from mutagen.oggvorbis import OggVorbis

        audio = OggOpus(path) if suffix == ".opus" else OggVorbis(path)
        picture = Picture()
        picture.type = 3
        picture.mime = "image/jpeg"
        picture.desc = "Cover"
        picture.data = jpeg
        audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
        audio.save()
        return
    raise RuntimeError(f"Cover-Einbettung für {suffix or 'diese Datei'} nicht unterstützt")


def _write_folder_image(folder: Path, jpeg: bytes) -> None:
    for name in ("cover.jpg", "folder.jpg", "album.jpg", "front.jpg"):
        target = folder / name
        try:
            target.write_bytes(jpeg)
        except OSError as exc:
            logger.debug("Cover-Datei {} nicht schreibbar: {}", target, exc)


def embed_cover(track: dict, url: str, *, album: bool = False) -> dict:
    track = _enrich_track(track)
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
            target.touch(exist_ok=True)
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
