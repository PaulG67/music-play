"""Find higher-bitrate versions of a Navidrome track (YouTube Music + MediaSync)."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.core.logging import logger
from app.navidrome import config
from app.navidrome import ids as nd_ids
from app.navidrome import match as nd_match
from app.navidrome.client import NavidromeClient
from app.ytm import client as ytm
from app.ytm import streams

_MAX_CANDIDATES = 14
_PROBE_WORKERS = 6
_LOSSLESS = 1411
_PLACEHOLDER_ALBUMS = {"", "singles", "unknown", "unbekannt", "single", "various", "va"}
_RELEASE_HINT = re.compile(
    r"\b(flac|alac|wav|lossless|320|256|v0|kbps|web[- ]?rip|cdrip|album)\b",
    re.IGNORECASE,
)

_SOURCE_LABELS = {
    "ytm": "YouTube Music",
    "youtube": "MediaSync · YouTube",
    "musikat": "MediaSync · MusiKat",
    "usenet": "MediaSync · Usenet",
}


def _codec_label(acodec: str, ext: str) -> str:
    text = f"{acodec or ''} {ext or ''}".lower()
    if "flac" in text:
        return "FLAC"
    if "mp4a" in text or "aac" in text or ext in {"m4a", "mp4"}:
        return "AAC"
    if "opus" in text:
        return "Opus"
    if "mp3" in text or ext == "mp3":
        return "MP3"
    if "alac" in text:
        return "ALAC"
    if "vorbis" in text:
        return "Vorbis"
    return (acodec or ext or "Audio").split(".")[0].upper() or "Audio"


def _navidrome_id(track: dict) -> str:
    return str(track.get("navidromeId") or nd_ids.unwrap(track.get("id")) or "")


def _current_song(track: dict) -> dict:
    data = dict(track or {})
    nd_id = _navidrome_id(data)
    if not nd_id or not config.configured():
        return data
    try:
        with NavidromeClient() as client:
            song = client.get_song(nd_id)
    except Exception as exc:
        logger.debug("Aktuelle Navidrome-Datei nicht lesbar: {}", exc)
        return data
    if not song:
        return data
    for key in ("title", "artist", "album", "duration", "path", "bitrate", "year", "track_no"):
        if song.get(key) not in (None, ""):
            data[key] = song[key]
    data["navidromeId"] = nd_id
    return data


def _same_song(track: dict, candidate: dict) -> bool:
    if not nd_match.titles_match(track.get("title") or "", candidate.get("title") or ""):
        return False
    artist = track.get("artist") or ""
    if artist and not nd_match.artists_match(artist, candidate.get("artist") or ""):
        return False
    return nd_match.duration_close(track.get("duration"), candidate.get("duration"))


def _album_status(expected: str, got: str) -> str:
    left, right = nd_match.fold(expected), nd_match.fold(got)
    if left in _PLACEHOLDER_ALBUMS or right in _PLACEHOLDER_ALBUMS:
        return "unknown"
    if left == right or left in right or right in left:
        return "same"
    return "other"


def _annotate_match(option: dict, track: dict) -> None:
    option["sameTitle"] = nd_match.titles_match(track.get("title") or "", option.get("title") or "")
    option["sameArtist"] = nd_match.artists_match(track.get("artist") or "", option.get("artist") or "")
    option["albumStatus"] = _album_status(track.get("album") or "", option.get("album") or "")
    option["sameAlbum"] = option["albumStatus"] == "same"
    option["sameDuration"] = nd_match.duration_close(track.get("duration"), option.get("duration"))


def _looks_like_release(value: str) -> bool:
    text = (value or "").strip()
    if len(text) > 80:
        return True
    return bool(_RELEASE_HINT.search(text))


def _collect_candidates(track: dict) -> list[dict]:
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "").strip()
    query = " ".join(part for part in (artist, title) if part).strip()
    if not query:
        return []
    pool: list[dict] = []
    seen: set[str] = set()
    searches = (
        ("songs", query, 12),
        ("videos", query, 8),
        ("songs", f"{query} official audio", 6),
    )
    for scope, term, limit in searches:
        try:
            results = ytm.search(term, scope=scope, limit=limit)
        except Exception as exc:
            logger.debug("Qualitätssuche ({}) fehlgeschlagen: {}", scope, exc)
            continue
        for item in results or []:
            video_id = str(item.get("id") or "")
            if not video_id or video_id in seen or video_id.startswith("nd:"):
                continue
            if not _same_song(track, item):
                continue
            seen.add(video_id)
            pool.append(item)
            if len(pool) >= _MAX_CANDIDATES:
                return pool
    return pool


def _probe_item(item: dict) -> dict | None:
    video_id = str(item.get("id") or "")
    probe = streams.probe_audio(video_id)
    if not probe or not probe.get("bitrate"):
        return None
    codec = _codec_label(probe.get("acodec") or "", probe.get("ext") or "")
    return {
        "videoId": video_id,
        "source": "ytm",
        "sourceLabel": _SOURCE_LABELS["ytm"],
        "title": item.get("title") or probe.get("title") or "",
        "artist": item.get("artist") or "",
        "album": item.get("album") or "",
        "detail": "",
        "duration": item.get("duration") or 0,
        "bitrate": int(probe["bitrate"]),
        "codec": codec,
        "ext": probe.get("ext") or "",
        "formatId": probe.get("format_id") or "",
        "candidate": None,
    }


def _bitrate_from_text(*parts: object) -> tuple[int, str]:
    blob = " ".join(str(part or "") for part in parts)
    text = blob.casefold()
    if re.search(r"(?<![a-z0-9])(flac|alac|wav|lossless)(?![a-z0-9])", text):
        codec = "FLAC" if "flac" in text or "lossless" in text else "ALAC" if "alac" in text else "WAV"
        return _LOSSLESS, codec
    match = re.search(r"(\d{2,4})\s*kb/?s", text)
    if match:
        value = int(match.group(1))
        codec = "MP3" if "mp3" in text else "AAC" if "aac" in text or "m4a" in text else "Audio"
        return value, codec
    for token, value, codec in (
        ("320", 320, "MP3"),
        ("256", 256, "AAC"),
        ("192", 192, "MP3"),
        ("v0", 245, "MP3"),
    ):
        if re.search(rf"(?<![a-z0-9]){token}(?![a-z0-9])", text):
            return value, codec
    if "mp3" in text:
        return 320, "MP3"
    if "aac" in text or "m4a" in text:
        return 256, "AAC"
    if "beste verfügbare" in text or "best" in text:
        return 256, "AAC"
    return 0, "Audio"


def _option_from_mediasync(raw: dict, source: str, query_title: str, query_artist: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    score = int(raw.get("score") or 0)
    if score and score < 45:
        return None
    title = (raw.get("title") or query_title or "").strip()
    artist = (raw.get("artist") or raw.get("channel") or query_artist or "").strip()
    if not title:
        return None
    bitrate, codec = _bitrate_from_text(
        raw.get("format"),
        raw.get("quality"),
        raw.get("bitrate_label"),
        raw.get("quality_label"),
        raw.get("release_title"),
        raw.get("title"),
        raw.get("category"),
        raw.get("album"),
    )
    if source == "youtube" and not bitrate:
        bitrate, codec = 256, "AAC"
    if source == "musikat" and not bitrate:
        bitrate, codec = 320, "MP3"
    video_id = str(raw.get("video_id") or "")
    if source == "youtube":
        song_title = query_title or title
        release = (raw.get("video_title") or raw.get("release_title") or title).strip()
    elif query_title and not nd_match.titles_match(query_title, title) and _looks_like_release(title):
        song_title = query_title
        release = (raw.get("release_title") or title).strip()
    else:
        song_title = title
        release = (raw.get("release_title") or raw.get("video_title") or "").strip()
    album = (raw.get("album") or "").strip()
    extra = release if release and nd_match.fold(release) != nd_match.fold(song_title) else ""
    match = {
        key: value
        for key, value in raw.items()
        if value is not None and isinstance(value, (str, int, float, bool, dict, list))
    }
    if raw.get("index") is not None:
        try:
            match["index"] = int(raw["index"])
        except (TypeError, ValueError):
            pass
    candidate = {
        "id": f"{source}:{video_id or raw.get('guid') or raw.get('index') or title}"[:120],
        "source": source,
        "source_label": _SOURCE_LABELS.get(source, source),
        "artist": artist,
        "title": song_title,
        "album": album,
        "score": score,
        "format": codec,
        "bitrate_label": f"{bitrate} kbps" if bitrate and bitrate < 1000 else codec,
        "match": match,
    }
    return {
        "videoId": video_id,
        "source": source,
        "sourceLabel": _SOURCE_LABELS.get(source, source),
        "title": song_title,
        "artist": artist,
        "album": album,
        "detail": extra,
        "duration": raw.get("duration") or 0,
        "bitrate": bitrate,
        "codec": codec,
        "ext": codec.lower(),
        "formatId": "",
        "candidate": candidate,
    }


def _mediasync_options(track: dict) -> list[dict]:
    from app.mediasync import config as ms_config
    from app.mediasync.client import MediaSyncClient

    if not ms_config.configured():
        return []
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "").strip()
    if not title:
        return []
    try:
        with MediaSyncClient() as client:
            buckets = client.search_sources(artist, title, track.get("album") or "")
    except Exception as exc:
        logger.warning("MediaSync-Qualitätssuche fehlgeschlagen: {}", exc)
        return []

    options: list[dict] = []
    for source, payload in buckets.items():
        rows = []
        if isinstance(payload, dict):
            if payload.get("best_match"):
                rows.append(payload["best_match"])
            rows.extend(payload.get("tracks") or [])
        seen: set[str] = set()
        for raw in rows:
            option = _option_from_mediasync(raw, source, title, artist)
            if not option:
                continue
            key = str((option.get("candidate") or {}).get("id") or option.get("title"))
            if key in seen:
                continue
            seen.add(key)
            options.append(option)
    return options


def search_quality(track: dict) -> dict:
    data = _current_song(track)
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    if not title:
        raise RuntimeError("Titel fehlt")

    current = int(data.get("bitrate") or 0)
    options: list[dict] = []
    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
        futures = [pool.submit(_probe_item, item) for item in _collect_candidates(data)]
        futures.append(pool.submit(_mediasync_options, data))
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                logger.debug("Bitrate-Probe übersprungen: {}", exc)
                continue
            if isinstance(result, list):
                options.extend(result)
            elif result:
                options.append(result)

    by_key: dict[str, dict] = {}
    for option in options:
        key = str(option.get("videoId") or (option.get("candidate") or {}).get("id") or option.get("title"))
        previous = by_key.get(key)
        if not previous or int(option.get("bitrate") or 0) > int(previous.get("bitrate") or 0):
            by_key[key] = option
    unique = list(by_key.values())
    unique.sort(
        key=lambda item: (int(item.get("bitrate") or 0), item.get("codec") == "FLAC", item.get("ext") == "m4a"),
        reverse=True,
    )
    for option in unique:
        bitrate = int(option.get("bitrate") or 0)
        option["current"] = bool(current and abs(bitrate - current) <= 4)
        option["better"] = bool(current and bitrate > current)
        _annotate_match(option, data)

    return {
        "title": title,
        "artist": artist,
        "album": data.get("album") or "",
        "duration": data.get("duration") or 0,
        "bitrate": current,
        "navidromeId": _navidrome_id(data),
        "path": data.get("path") or "",
        "query": " ".join(part for part in (artist, title) if part),
        "options": unique,
    }


def _library_rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(config.music_dir().resolve())).replace("\\", "/")
    except ValueError:
        text = str(path).replace("\\", "/")
        if "/music/" in text:
            return text.split("/music/", 1)[-1]
        return path.name


def _wait_mediasync_job(client, job_id: str, timeout: int = 300, progress=None) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.song_check_job(job_id) or {}
        status = str(last.get("status") or "").lower()
        if callable(progress):
            text = last.get("message") or last.get("detail")
            nested = last.get("result")
            if not text and isinstance(nested, dict):
                text = nested.get("message") or nested.get("status")
            progress(str(text or f"MediaSync lädt … ({status or 'läuft'})"))
        if status in {"done", "error", "failed"}:
            return last
        result = last.get("result")
        if isinstance(result, dict) and result.get("status") in {"needs_selection", "not_found", "error"}:
            return last
        time.sleep(2)
    raise RuntimeError("MediaSync braucht zu lange für den Download. Im Hub prüfen, ob der Auftrag noch läuft.")


def _mediasync_failure_text(payload: dict | None) -> str:
    data = payload if isinstance(payload, dict) else {}
    parts = [
        data.get("error"),
        data.get("message"),
        data.get("reason"),
        data.get("status"),
    ]
    nested = data.get("result")
    if isinstance(nested, dict):
        parts.extend([nested.get("error"), nested.get("reason"), nested.get("status")])
    return " ".join(str(part) for part in parts if part).strip()


def _recover_mediasync_replace(song: dict, dest, nd_id: str, snapshot: tuple) -> dict | None:
    from app.navidrome import artwork
    from app.navidrome.importer import _locate, _probe_file_bitrate, _wait_for_scan

    mtime, size, old_bitrate = snapshot
    with NavidromeClient() as client:
        _wait_for_scan(client, timeout=6)
        updated = client.get_song(nd_id)
    found = _locate(song, force_refresh=False)
    if updated is None and found:
        updated = found
        nd_id = str(found.get("id") or found.get("navidromeId") or nd_id)

    changed = False
    bitrate = old_bitrate
    if dest.is_file():
        stat = dest.stat()
        bitrate = _probe_file_bitrate(dest) or bitrate
        if stat.st_mtime > mtime + 0.4 or stat.st_size != size:
            changed = True
        if bitrate and old_bitrate and bitrate != old_bitrate:
            changed = True
    if updated and int(updated.get("bitrate") or 0) and int(updated.get("bitrate") or 0) != old_bitrate:
        bitrate = int(updated.get("bitrate") or bitrate)
        changed = True
    if found:
        other = artwork.resolve_audio_path(found)
        if other is not None and dest.exists() and other.resolve() != dest.resolve() and other.is_file():
            changed = True
            bitrate = int(found.get("bitrate") or _probe_file_bitrate(other) or bitrate)

    if not changed:
        return None
    return {
        "navidromeId": nd_id,
        "path": str(dest),
        "bitrate": bitrate,
        "title": (updated or song).get("title") or song.get("title") or "",
        "artist": (updated or song).get("artist") or song.get("artist") or "",
    }


def _hub_candidate(candidate: dict) -> dict:
    data = dict(candidate or {})
    match = data.get("match")
    if isinstance(match, dict):
        merged = {**match, **data}
        merged["match"] = match
        return merged
    return data


def replace_via_mediasync(navidrome_id: str, candidate: dict, progress=None) -> dict:
    from app.mediasync import config as ms_config
    from app.mediasync.client import MediaSyncClient
    from app.navidrome import artwork

    nd_id = (navidrome_id or "").strip()
    if not nd_id:
        raise RuntimeError("Navidrome-Id fehlt")
    if not ms_config.configured():
        raise RuntimeError("MediaSync-Hub ist nicht konfiguriert")
    if not isinstance(candidate, dict) or not candidate.get("source"):
        raise RuntimeError("Kein MediaSync-Treffer ausgewählt")

    with NavidromeClient() as client:
        song = client.get_song(nd_id)
    if not song:
        raise RuntimeError("Titel nicht in Navidrome gefunden")

    dest = artwork.resolve_audio_path({**song, "navidromeId": nd_id})
    rel = artwork.library_relative_path({**song, "navidromeId": nd_id}, dest)
    if not rel and dest is not None:
        rel = _library_rel_path(dest)
    if not rel:
        raise RuntimeError(
            "Audiodatei in der Navidrome-Quelle nicht gefunden. "
            f"Musikordner prüfen: {config.music_dir()}."
        )
    if dest is None or not dest.is_file():
        dest = config.music_dir() / rel
        snapshot = (0.0, 0, int(song.get("bitrate") or 0))
        logger.warning(
            "Navidrome-Datei lokal nicht sichtbar, MediaSync bekommt relativen Pfad {}",
            rel,
        )
    else:
        snapshot = (
            dest.stat().st_mtime,
            dest.stat().st_size,
            int(song.get("bitrate") or 0),
        )
    source = str(candidate.get("source") or "")
    artist = str(candidate.get("artist") or song.get("artist") or "").strip()
    title = str(candidate.get("title") or song.get("title") or "").strip()
    wait_s = 720 if source == "musikat" else 300

    def note(text: str) -> None:
        logger.info("Qualität: {}", text)
        if callable(progress):
            progress(text)

    note(f"Starte Download im Hub ({source or 'MediaSync'}) …")
    logger.info("Ersetze {} über MediaSync ({})", dest, source)
    outcome: dict = {}
    failure = ""
    hub_item = _hub_candidate(candidate)
    with MediaSyncClient() as client:
        started = client.start_redownload(
            rel,
            artist,
            title,
            hub_item,
            playlist_item_id=nd_id,
        )
        job_id = str(started.get("job_id") or started.get("id") or "")
        if not job_id:
            if started.get("success") is True:
                outcome = started.get("result") or started
            else:
                failure = _mediasync_failure_text(started) or "MediaSync hat keinen Download-Auftrag gestartet"
        else:
            note("Warte auf den Hub-Download …")
            body = _wait_mediasync_job(client, job_id, timeout=wait_s, progress=progress)
            if str(body.get("status") or "").lower() in {"error", "failed"}:
                failure = _mediasync_failure_text(body) or "MediaSync-Download fehlgeschlagen"
            else:
                outcome = body.get("result") or body
        if not failure and isinstance(outcome, dict) and outcome.get("success") is False:
            failure = (
                outcome.get("reason")
                or outcome.get("error")
                or "MediaSync hat nichts heruntergeladen"
            )

    if failure:
        recovered = _recover_mediasync_replace(song, dest, nd_id, snapshot)
        if recovered:
            logger.info("MediaSync meldete Fehler, Datei wurde trotzdem ersetzt: {}", failure)
            return recovered
        raise RuntimeError(failure)

    note("Prüfe, ob die Datei in Navidrome ankam …")
    recovered = _recover_mediasync_replace(song, dest, nd_id, snapshot)
    if recovered:
        from app.navidrome.importer import _refresh_index_async

        _refresh_index_async()
        return recovered
    raise RuntimeError(
        "MediaSync hat die Datei nicht ersetzt. "
        "Im Hub prüfen, ob der Download dort wirklich gelaufen ist."
    )
