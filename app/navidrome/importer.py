"""Download a YouTube Music track into the Navidrome music folder."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import yt_dlp

from app.core.logging import logger
from app.core.settings import settings
from app.db import repo
from app.db.database import session_scope
from app.navidrome import config
from app.navidrome.client import NavidromeClient
from app.navidrome import match as nd_match
from app.ytm import streams

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_inflight: dict[str, str] = {}


def _safe(value: str | None, fallback: str = "Unbekannt", limit: int = 80) -> str:
    text = _UNSAFE.sub("_", (value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:limit]


def _destination(track: dict) -> Path:
    artist = _safe(track.get("artist"), "Unbekannt")
    album = _safe(track.get("album"), "Singles")
    title = _safe(track.get("title"), track.get("id") or "Titel")
    number = track.get("track_no")
    filename = f"{int(number):02d} - {title}.m4a" if number else f"{title}.m4a"
    return config.import_root() / artist / album / filename


def _ydl_options(output_template: str) -> dict:
    options = {
        "format": streams.FORMAT_SELECTOR,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "writethumbnail": True,
        "embedthumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ],
        "postprocessor_args": {"ffmpeg": ["-movflags", "+faststart"]},
        "socket_timeout": 20,
        "retries": 2,
        "cachedir": str(settings.audio_cache_dir.parent / "ytdlp"),
    }
    if settings.ytdlp_cookie_file and Path(settings.ytdlp_cookie_file).exists():
        options["cookiefile"] = settings.ytdlp_cookie_file
    if settings.ytdlp_player_clients:
        clients = [c.strip() for c in settings.ytdlp_player_clients.split(",") if c.strip()]
        options["extractor_args"] = {"youtube": {"player_client": clients}}
    return options


def _write_ffmpeg_tags(source: Path, dest: Path, track: dict) -> None:
    if shutil.which("ffmpeg") is None:
        shutil.copy2(source, dest)
        return
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-metadata",
        f"title={track.get('title') or ''}",
        "-metadata",
        f"artist={track.get('artist') or ''}",
        "-metadata",
        f"album={track.get('album') or ''}",
        "-metadata",
        f"album_artist={track.get('artist') or ''}",
        "-metadata",
        f"date={track.get('year') or ''}",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    if track.get("track_no"):
        index = command.index("-movflags")
        command[index:index] = ["-metadata", f"track={track['track_no']}"]
    try:
        subprocess.run(command, check=True, timeout=120)
    except Exception as exc:
        logger.warning("Metadaten für {} nicht gesetzt, kopiere Datei: {}", dest.name, exc)
        shutil.copy2(source, dest)


def _wait_for_scan(client: NavidromeClient, timeout: int = 90) -> None:
    client.start_scan()
    deadline = time.time() + timeout
    saw_scan = False
    while time.time() < deadline:
        status = client.scan_status()
        if status.get("scanning"):
            saw_scan = True
        elif saw_scan:
            return
        time.sleep(1.5)
    # Watcher-based libraries may never report scanning=true.
    time.sleep(2)


def _locate(track: dict) -> dict | None:
    nd_match.refresh_index(force=True)
    found = nd_match.find_in_index(track) or nd_match.find_live(track)
    return found


def import_track(track: dict) -> dict:
    video_id = track.get("id") or ""
    if not video_id:
        raise RuntimeError("Titel ohne YouTube-Id")
    if video_id.startswith("nd:"):
        return {"navidromeId": video_id[3:], "path": "", "skipped": True}

    existing = nd_match.resolve(track)
    if existing and existing.get("id"):
        return {"navidromeId": existing["id"], "path": existing.get("path") or "", "skipped": True}

    if not config.configured():
        raise RuntimeError("Navidrome ist nicht konfiguriert")
    if not config.music_dir_writable():
        raise RuntimeError(
            f"Musikordner nicht beschreibbar: {config.music_dir()}. "
            "Denselben Ordner wie Navidrome nach /music einhängen."
        )

    dest = _destination(track)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and dest.stat().st_size > 1024:
        logger.info("Datei existiert bereits, überspringe Download: {}", dest)
        with NavidromeClient() as client:
            _wait_for_scan(client)
            found = _locate(track)
        if found and found.get("id"):
            with session_scope() as session:
                repo.upsert_navidrome_link(
                    session,
                    video_id,
                    str(found["id"]),
                    title=track.get("title") or "",
                    artist=track.get("artist") or "",
                    path=str(dest),
                )
            return {"navidromeId": str(found["id"]), "path": str(dest), "skipped": True}

    tmp_dir = settings.audio_cache_dir / "import"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Importiere {} nach {}", video_id, dest)
    try:
        with yt_dlp.YoutubeDL(_ydl_options(str(tmp_dir / f"{video_id}.%(ext)s"))) as ydl:
            ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=True)
    except Exception as exc:
        raise RuntimeError(f"Download fehlgeschlagen: {exc}") from exc

    produced = next((path for path in tmp_dir.glob(f"{video_id}*") if path.suffix.lower() in {".m4a", ".mp4", ".mp3"}), None)
    if produced is None:
        raise RuntimeError("yt-dlp hat keine Audiodatei erzeugt")

    _write_ffmpeg_tags(produced, dest, track)
    sidecar = next(
        (
            path
            for path in tmp_dir.glob(f"{video_id}*")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ),
        None,
    )
    if sidecar:
        try:
            from app.navidrome import artwork

            artwork.embed_image_file(dest, sidecar)
        except Exception as exc:
            logger.warning("Cover für {} nicht eingebettet: {}", dest.name, exc)
    for leftover in tmp_dir.glob(f"{video_id}*"):
        leftover.unlink(missing_ok=True)

    with NavidromeClient() as client:
        _wait_for_scan(client)
        found = _locate(track)
        if found is None:
            time.sleep(3)
            found = _locate(track)

    if not found or not found.get("id"):
        raise RuntimeError(
            "Datei liegt in der Bibliothek, Navidrome hat sie aber noch nicht indiziert. "
            "Später erneut versuchen oder in Navidrome einen Scan starten."
        )

    with session_scope() as session:
        repo.upsert_navidrome_link(
            session,
            video_id,
            str(found["id"]),
            title=track.get("title") or "",
            artist=track.get("artist") or "",
            path=str(dest),
        )
    return {"navidromeId": str(found["id"]), "path": str(dest), "skipped": False}


def job_snapshot(job: dict) -> dict:
    return {
        "id": job["id"],
        "videoId": job["videoId"],
        "status": job["status"],
        "error": job.get("error"),
        "navidromeId": job.get("navidromeId"),
        "title": job.get("title") or "",
        "bitrate": job.get("bitrate") or 0,
    }


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return job_snapshot(job) if job else None


def _run(job_id: str, track: dict) -> None:
    video_id = track.get("id") or ""
    try:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
        result = import_track(track)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["navidromeId"] = result.get("navidromeId")
    except Exception as exc:
        logger.error("Navidrome-Import von {} fehlgeschlagen: {}", video_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
    finally:
        with _jobs_lock:
            _inflight.pop(video_id, None)


def start_job(track: dict) -> dict:
    video_id = track.get("id") or ""
    with _jobs_lock:
        existing_id = _inflight.get(video_id)
        if existing_id and existing_id in _jobs:
            return job_snapshot(_jobs[existing_id])
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "videoId": video_id,
            "status": "queued",
            "error": None,
            "navidromeId": None,
            "title": track.get("title") or "",
        }
        _jobs[job_id] = job
        _inflight[video_id] = job_id

    thread = threading.Thread(target=_run, args=(job_id, track), daemon=True, name=f"nd-import-{video_id}")
    thread.start()
    return job_snapshot(job)


_AUDIO_EXTS = {".m4a", ".mp4", ".mp3", ".webm", ".opus", ".ogg", ".flac", ".wav", ".aac"}


def _ydl_best_options(output_template: str, format_id: str | None = None) -> dict:
    options = _ydl_options(output_template)
    options["format"] = format_id or "bestaudio/best"
    options["writethumbnail"] = False
    options["embedthumbnail"] = False
    options["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]
    options.pop("postprocessor_args", None)
    return options


def _audio_encode_args(suffix: str) -> list[str]:
    ext = (suffix or "").lower()
    if ext in {".m4a", ".mp4", ".aac"}:
        return ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", "320k"]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext in {".ogg", ".opus"}:
        return ["-c:a", "libopus", "-b:a", "192k"]
    return ["-c:a", "aac", "-b:a", "256k"]


def _can_copy_audio(source: Path, dest: Path) -> bool:
    src = source.suffix.lower()
    dst = dest.suffix.lower()
    if src == dst:
        return True
    return src in {".m4a", ".mp4", ".aac"} and dst in {".m4a", ".mp4", ".aac"}


def _metadata_args(track: dict) -> list[str]:
    args = [
        "-metadata",
        f"title={track.get('title') or ''}",
        "-metadata",
        f"artist={track.get('artist') or ''}",
        "-metadata",
        f"album={track.get('album') or ''}",
        "-metadata",
        f"album_artist={track.get('artist') or ''}",
        "-metadata",
        f"date={track.get('year') or ''}",
    ]
    if track.get("track_no"):
        args.extend(["-metadata", f"track={track['track_no']}"])
    return args


def _extract_cover(path: Path, dest: Path) -> Path | None:
    if shutil.which("ffmpeg") is None or not path.is_file():
        return None
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-an", "-c:v", "mjpeg", str(dest)],
            check=True,
            timeout=30,
        )
    except Exception:
        dest.unlink(missing_ok=True)
        return None
    return dest if dest.is_file() and dest.stat().st_size > 32 else None


def _probe_file_bitrate(path: Path) -> int:
    if shutil.which("ffprobe") is None or not path.is_file():
        return 0
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=bit_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = (result.stdout or "").strip().splitlines()[0] if result.stdout else ""
        bps = int(float(raw))
        return max(1, round(bps / 1000)) if bps > 0 else 0
    except Exception:
        return 0


def _encode_into(source: Path, dest: Path, track: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.unlink(missing_ok=True)
    if shutil.which("ffmpeg") is None:
        if dest.suffix.lower() != source.suffix.lower():
            raise RuntimeError("ffmpeg fehlt, die Datei kann nicht in das bestehende Format gewandelt werden")
        shutil.copy2(source, dest)
        return

    copy_audio = _can_copy_audio(source, dest)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        *(["-c:a", "copy"] if copy_audio else _audio_encode_args(dest.suffix)),
        *_metadata_args(track),
        str(tmp),
    ]
    if dest.suffix.lower() in {".m4a", ".mp4", ".aac"} and "-movflags" not in command:
        command[command.index(str(tmp)):command.index(str(tmp))] = ["-movflags", "+faststart"]
    try:
        subprocess.run(command, check=True, timeout=180)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        if copy_audio:
            logger.warning("Stream-Kopie nach {} fehlgeschlagen, kodiere neu: {}", dest.name, exc)
            retry = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                *_audio_encode_args(dest.suffix),
                *_metadata_args(track),
                str(tmp),
            ]
            try:
                subprocess.run(retry, check=True, timeout=180)
            except Exception as retry_exc:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"Ersetzen fehlgeschlagen: {retry_exc}") from retry_exc
        else:
            raise RuntimeError(f"Ersetzen fehlgeschlagen: {exc}") from exc
    tmp.replace(dest)


def replace_track(navidrome_id: str, video_id: str, format_id: str | None = None) -> dict:
    nd_id = (navidrome_id or "").strip()
    video_id = (video_id or "").strip()
    if not nd_id:
        raise RuntimeError("Navidrome-Id fehlt")
    if not video_id or video_id.startswith("nd:"):
        raise RuntimeError("YouTube-Id fehlt")
    if not config.configured():
        raise RuntimeError("Navidrome ist nicht konfiguriert")
    if not config.music_dir_writable():
        raise RuntimeError(
            f"Musikordner nicht beschreibbar: {config.music_dir()}. "
            "Denselben Ordner wie Navidrome nach /music einhängen."
        )

    with NavidromeClient() as client:
        song = client.get_song(nd_id)
    if not song:
        raise RuntimeError("Titel nicht in Navidrome gefunden")

    from app.navidrome import artwork

    dest = artwork.resolve_audio_path({**song, "navidromeId": nd_id})
    if dest is None or not dest.is_file():
        raise RuntimeError("Audiodatei in der Navidrome-Quelle nicht gefunden")

    tmp_dir = settings.audio_cache_dir / "quality"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cover_tmp = tmp_dir / f"{nd_id}.cover.jpg"
    _extract_cover(dest, cover_tmp)

    logger.info("Ersetze Navidrome-Datei {} durch YouTube {}", dest, video_id)
    try:
        with yt_dlp.YoutubeDL(_ydl_best_options(str(tmp_dir / f"{video_id}.%(ext)s"), format_id)) as ydl:
            ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=True)
    except Exception as exc:
        raise RuntimeError(f"Download fehlgeschlagen: {exc}") from exc

    produced = next(
        (path for path in tmp_dir.glob(f"{video_id}*") if path.suffix.lower() in _AUDIO_EXTS),
        None,
    )
    if produced is None:
        raise RuntimeError("yt-dlp hat keine Audiodatei erzeugt")

    backup = dest.with_name(dest.name + ".bak")
    try:
        shutil.copy2(dest, backup)
        _encode_into(produced, dest, song)
        if cover_tmp.is_file():
            try:
                artwork.embed_image_file(dest, cover_tmp)
            except Exception as exc:
                logger.warning("Cover nach Qualitäts-Tausch nicht eingebettet: {}", exc)
    except Exception:
        if backup.is_file():
            shutil.move(str(backup), str(dest))
        raise
    backup.unlink(missing_ok=True)
    for leftover in tmp_dir.glob(f"{video_id}*"):
        leftover.unlink(missing_ok=True)
    cover_tmp.unlink(missing_ok=True)

    bitrate = _probe_file_bitrate(dest)
    with NavidromeClient() as client:
        _wait_for_scan(client)
        updated = client.get_song(nd_id)
        if updated and int(updated.get("bitrate") or 0) in (0, song.get("bitrate")):
            time.sleep(3)
            updated = client.get_song(nd_id) or updated

    if updated and int(updated.get("bitrate") or 0):
        bitrate = int(updated.get("bitrate") or bitrate)

    nd_match.refresh_index(force=True)
    with session_scope() as session:
        repo.upsert_navidrome_link(
            session,
            video_id,
            nd_id,
            title=song.get("title") or "",
            artist=song.get("artist") or "",
            path=str(dest),
        )
    return {
        "navidromeId": nd_id,
        "path": str(dest),
        "bitrate": bitrate,
        "title": (updated or song).get("title") or song.get("title") or "",
        "artist": (updated or song).get("artist") or song.get("artist") or "",
    }


def _run_replace(job_id: str, navidrome_id: str, video_id: str, format_id: str | None, inflight_key: str) -> None:
    try:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
        result = replace_track(navidrome_id, video_id, format_id)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["navidromeId"] = result.get("navidromeId")
            _jobs[job_id]["bitrate"] = result.get("bitrate") or 0
    except Exception as exc:
        logger.error("Qualitäts-Tausch von {} fehlgeschlagen: {}", navidrome_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
    finally:
        with _jobs_lock:
            _inflight.pop(inflight_key, None)


def start_replace_job(navidrome_id: str, video_id: str, format_id: str | None = None) -> dict:
    nd_id = (navidrome_id or "").strip()
    video_id = (video_id or "").strip()
    inflight_key = f"replace:{nd_id}:{video_id}"
    with _jobs_lock:
        existing_id = _inflight.get(inflight_key)
        if existing_id and existing_id in _jobs:
            return job_snapshot(_jobs[existing_id])
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "videoId": video_id,
            "status": "queued",
            "error": None,
            "navidromeId": nd_id,
            "title": "",
            "bitrate": 0,
        }
        _jobs[job_id] = job
        _inflight[inflight_key] = job_id

    thread = threading.Thread(
        target=_run_replace,
        args=(job_id, nd_id, video_id, format_id, inflight_key),
        daemon=True,
        name=f"nd-quality-{nd_id[:8]}",
    )
    thread.start()
    return job_snapshot(job)
