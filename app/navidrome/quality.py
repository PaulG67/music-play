"""Find higher-bitrate YouTube Music versions of a Navidrome track."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.core.logging import logger
from app.navidrome import config
from app.navidrome import ids as nd_ids
from app.navidrome import match as nd_match
from app.navidrome.client import NavidromeClient
from app.ytm import client as ytm
from app.ytm import streams

_MAX_CANDIDATES = 10
_PROBE_WORKERS = 6


def _codec_label(acodec: str, ext: str) -> str:
    text = f"{acodec or ''} {ext or ''}".lower()
    if "mp4a" in text or "aac" in text or ext in {"m4a", "mp4"}:
        return "AAC"
    if "opus" in text:
        return "Opus"
    if "mp3" in text or ext == "mp3":
        return "MP3"
    if "flac" in text or ext == "flac":
        return "FLAC"
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


def _collect_candidates(track: dict) -> list[dict]:
    query = " ".join(part for part in (track.get("artist"), track.get("title")) if part).strip()
    if not query:
        return []
    pool: list[dict] = []
    seen: set[str] = set()
    for scope, limit in (("songs", 12), ("videos", 8)):
        try:
            results = ytm.search(query, scope=scope, limit=limit)
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
        "title": item.get("title") or probe.get("title") or "",
        "artist": item.get("artist") or "",
        "album": item.get("album") or "",
        "duration": item.get("duration") or 0,
        "bitrate": int(probe["bitrate"]),
        "codec": codec,
        "ext": probe.get("ext") or "",
        "formatId": probe.get("format_id") or "",
    }


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
        for future in as_completed(futures):
            try:
                option = future.result()
            except Exception as exc:
                logger.debug("Bitrate-Probe übersprungen: {}", exc)
                continue
            if option:
                options.append(option)

    options.sort(key=lambda item: (int(item.get("bitrate") or 0), item.get("ext") == "m4a"), reverse=True)
    for option in options:
        bitrate = int(option.get("bitrate") or 0)
        option["current"] = bool(current and abs(bitrate - current) <= 4)
        option["better"] = bool(current and bitrate > current)

    return {
        "title": title,
        "artist": artist,
        "album": data.get("album") or "",
        "bitrate": current,
        "navidromeId": _navidrome_id(data),
        "path": data.get("path") or "",
        "query": " ".join(part for part in (artist, title) if part),
        "options": options,
    }
