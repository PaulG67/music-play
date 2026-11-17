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


def _wait_for_scan(client: NavidromeClient, timeout: float = 8) -> None:
    """
    Kick a Navidrome scan and wait briefly for it to finish.

    Watcher-based libraries often never report scanning=true — bail out
    quickly instead of burning ~90s. Callers should poll for the new
    song with search, not wait for a full library scan.
    """

    client.start_scan()
    deadline = time.time() + max(1.0, float(timeout))
    saw_scan = False
    idle_rounds = 0
    while time.time() < deadline:
        status = client.scan_status()
        if status.get("scanning"):
            saw_scan = True
            idle_rounds = 0
        elif saw_scan:
            return
        else:
            idle_rounds += 1
            # Never saw an active scan after a few polls → watcher / already done.
            if idle_rounds >= 3:
                return
        time.sleep(0.45)


def _locate(track: dict, *, force_refresh: bool = False) -> dict | None:
    if force_refresh:
        nd_match.refresh_index(force=True)
    return nd_match.find_in_index(track) or nd_match.find_live(track)


def _refresh_index_async() -> None:
    threading.Thread(
        target=nd_match.refresh_index,
        kwargs={"force": True},
        daemon=True,
        name="nd-index-after-import",
    ).start()


def _await_indexed(
    track: dict,
    client: NavidromeClient,
    timeout: float = 40,
) -> dict | None:
    """
    Trigger a scan, then poll live search until the track appears.

    Avoids a forced full-library index dump on every import attempt.
    """

    found = _locate(track, force_refresh=False)
    if found and found.get("id"):
        return found

    _wait_for_scan(client, timeout=6)
    deadline = time.time() + max(5.0, float(timeout))
    force_after = time.time() + 8
    forced = False
    while time.time() < deadline:
        found = _locate(track, force_refresh=False)
        if found and found.get("id"):
            _refresh_index_async()
            return found
        if not forced and time.time() >= force_after:
            forced = True
            found = _locate(track, force_refresh=True)
            if found and found.get("id"):
                return found
        time.sleep(0.55)

    found = _locate(track, force_refresh=True)
    if found and found.get("id"):
        return found
    return None


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
        found = _locate(track, force_refresh=False)
        if not (found and found.get("id")):
            with NavidromeClient() as client:
                found = _await_indexed(track, client, timeout=25)
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
        found = _await_indexed(track, client, timeout=40)

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
        "message": job.get("message") or "",
    }


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return job_snapshot(job) if job else None


def update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)


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
    # Prefer true best audio (often Opus ~160–256). `bestaudio[ext=m4a]` is YouTube 128k.
    options["format"] = format_id or "bestaudio/best"
    options["overwrites"] = True
    options["writethumbnail"] = False
    options["embedthumbnail"] = False
    options["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]
    options.pop("postprocessor_args", None)
    return options


def _bitrate_is_upgrade(got: int, current: int, wanted: int) -> bool:
    if got <= 0:
        return False
    if current and got + 20 < current:
        return False
    if wanted >= 280 and got < 200:
        return False
    return True


def _bitrate_reject_message(got: int, current: int, wanted: int) -> str:
    if wanted >= 280 and got < 200:
        return (
            f"Gewählt waren {wanted} kbit/s, der Download liefert nur {got} kbit/s. "
            "Die Navidrome-Datei wurde nicht ersetzt."
        )
    if current and got + 20 < current:
        return (
            f"Download wäre schlechter ({got} statt {current} kbit/s). "
            "Die Navidrome-Datei wurde nicht ersetzt."
        )
    return f"Download mit {got} kbit/s ist unbrauchbar. Datei nicht ersetzt."


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


_DUP_STEM = re.compile(r"\s*\(\d+\)$")
_WEAK_AUDIO = {".opus", ".webm", ".ogg"}
_KEEP_AUDIO = {".m4a", ".mp4", ".mp3", ".flac", ".aac"}


def _part_path(dest: Path, folder: Path | None = None) -> Path:
    """Keep the real audio suffix so ffmpeg can pick a muxer (not `.m4a.part`)."""
    parent = folder or dest.parent
    return parent / f".{dest.stem}.part{dest.suffix}"


def _canonical_replace_path(dest: Path, song: dict | None = None) -> Path:
    """Keep the original library file; skip Hub duplicates like `Titel (2).opus`."""
    if dest is None:
        return dest
    parent = dest.parent
    stem = _DUP_STEM.sub("", dest.stem).strip() or dest.stem
    reported = Path(str((song or {}).get("path") or (song or {}).get("filePath") or "")).suffix.lower()
    preferred = []
    if reported:
        preferred.append(reported)
    preferred.extend([".m4a", ".mp3", ".flac", ".mp4", ".aac", dest.suffix.lower()])
    seen: set[str] = set()
    for suffix in preferred:
        if not suffix or suffix in seen:
            continue
        seen.add(suffix)
        candidate = parent / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    if dest.is_file() and dest.suffix.lower() not in _WEAK_AUDIO:
        return dest
    return dest


def _container_args(suffix: str) -> list[str]:
    ext = (suffix or "").lower()
    if ext in {".m4a", ".aac"}:
        return ["-f", "ipod"]
    if ext == ".mp4":
        return ["-f", "mp4"]
    if ext == ".mp3":
        return ["-f", "mp3"]
    return []


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode == 0:
        return
    err = (result.stderr or result.stdout or "").strip()
    raise RuntimeError(err[-800:] if err else f"ffmpeg exit {result.returncode}")


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


def _encode_into(
    source: Path,
    dest: Path,
    track: dict,
    *,
    work_dir: Path | None = None,
    source_bitrate: int = 0,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _part_path(dest, work_dir)
    tmp.unlink(missing_ok=True)
    if shutil.which("ffmpeg") is None:
        if dest.suffix.lower() != source.suffix.lower():
            raise RuntimeError("ffmpeg fehlt, die Datei kann nicht in das bestehende Format gewandelt werden")
        shutil.copy2(source, dest)
        return

    def command(copy_audio: bool, mapped: bool) -> list[str]:
        audio = ["-c:a", "copy"] if copy_audio else _audio_encode_args(dest.suffix)
        mapping = ["-map", "0:a:0", "-vn"] if mapped else ["-vn"]
        extra = []
        if dest.suffix.lower() in {".m4a", ".mp4", ".aac"} and "-movflags" not in audio:
            extra = ["-movflags", "+faststart"]
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            *mapping,
            *audio,
            *_metadata_args(track),
            *extra,
            *_container_args(dest.suffix),
            str(tmp),
        ]

    attempts = []
    if _can_copy_audio(source, dest) and (not source_bitrate or source_bitrate >= 200):
        attempts.append(command(True, True))
    attempts.append(command(False, True))
    attempts.append(command(False, False))

    last_error: Exception | None = None
    for index, ffmpeg_cmd in enumerate(attempts):
        try:
            _run_ffmpeg(ffmpeg_cmd)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            tmp.unlink(missing_ok=True)
            if index == 0:
                logger.warning("Stream-Kopie nach {} fehlgeschlagen, kodiere neu: {}", dest.name, exc)
    if last_error:
        raise RuntimeError(f"Ersetzen fehlgeschlagen: {last_error}") from last_error
    if not tmp.is_file() or tmp.stat().st_size < 1024:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg hat keine gültige Audiodatei erzeugt")
    try:
        tmp.replace(dest)
    except OSError:
        shutil.copy2(tmp, dest)
        tmp.unlink(missing_ok=True)


def replace_track(
    navidrome_id: str,
    video_id: str,
    format_id: str | None = None,
    *,
    wanted_bitrate: int = 0,
) -> dict:
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
        raise RuntimeError(
            "Audiodatei in der Navidrome-Quelle nicht gefunden. "
            f"Musikordner muss dieselbe Freigabe wie Navidrome sein ({config.music_dir()})."
        )
    dest = _canonical_replace_path(dest, song)
    original = dest
    if dest.suffix.lower() in _WEAK_AUDIO:
        dest = dest.with_suffix(".m4a")

    tmp_dir = settings.audio_cache_dir / "quality"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cover_tmp = tmp_dir / f"{nd_id}.cover.jpg"
    _extract_cover(original, cover_tmp)

    logger.info("Ersetze Navidrome-Datei {} durch YouTube {}", dest, video_id)
    try:
        with yt_dlp.YoutubeDL(_ydl_best_options(str(tmp_dir / f"{video_id}.%(ext)s"), format_id)) as ydl:
            ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=True)
    except Exception as exc:
        raise RuntimeError(f"Download fehlgeschlagen: {exc}") from exc

    audio_hits = [
        path for path in tmp_dir.glob(f"{video_id}*") if path.suffix.lower() in _AUDIO_EXTS
    ]
    audio_hits.sort(
        key=lambda path: (
            path.suffix.lower() not in {".opus", ".webm", ".ogg"},
            " (2)" in path.stem,
            path.name,
        )
    )
    produced = audio_hits[0] if audio_hits else None
    if produced is None:
        raise RuntimeError("yt-dlp hat keine Audiodatei erzeugt")

    source_bitrate = _probe_file_bitrate(produced)
    current_bitrate = int(song.get("bitrate") or 0)
    if not _bitrate_is_upgrade(source_bitrate, current_bitrate, wanted_bitrate):
        for leftover in tmp_dir.glob(f"{video_id}*"):
            leftover.unlink(missing_ok=True)
        raise RuntimeError(_bitrate_reject_message(source_bitrate, current_bitrate, wanted_bitrate))

    backup = tmp_dir / f"{nd_id}.bak{original.suffix}"
    try:
        shutil.copy2(original, backup)
        _encode_into(produced, dest, song, work_dir=tmp_dir, source_bitrate=source_bitrate)
        if cover_tmp.is_file():
            try:
                artwork.embed_image_file(dest, cover_tmp)
            except Exception as exc:
                logger.warning("Cover nach Qualitäts-Tausch nicht eingebettet: {}", exc)
    except Exception:
        if backup.is_file():
            shutil.copy2(backup, original)
        raise
    if dest != original and dest.is_file() and original.is_file():
        original.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)
    for leftover in tmp_dir.glob(f"{video_id}*"):
        leftover.unlink(missing_ok=True)
    cover_tmp.unlink(missing_ok=True)

    bitrate = _probe_file_bitrate(dest)
    updated = None
    with NavidromeClient() as client:
        _wait_for_scan(client, timeout=6)
        deadline = time.time() + 12
        while time.time() < deadline:
            updated = client.get_song(nd_id)
            new_bitrate = int((updated or {}).get("bitrate") or 0)
            if bitrate and (not new_bitrate or new_bitrate == bitrate):
                break
            if updated and new_bitrate and new_bitrate != int(song.get("bitrate") or 0):
                bitrate = new_bitrate
                break
            time.sleep(0.6)

    _refresh_index_async()
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


def _run_replace(
    job_id: str,
    navidrome_id: str,
    video_id: str,
    format_id: str | None,
    inflight_key: str,
    source: str,
    candidate: dict | None,
) -> None:
    try:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["message"] = "Download wird vorbereitet …"
        from app.navidrome import quality as nd_quality

        def progress(text: str) -> None:
            update_job(job_id, status="running", message=text)

        wanted = int((candidate or {}).get("bitrate") or 0)
        hub_error = ""
        if source in {"musikat", "usenet"} and candidate:
            progress(
                f"Lade die gewählte {wanted} kbit/s-Datei über den Hub …"
                if wanted
                else "Lade die gewählte Datei über den Hub …"
            )
            try:
                result = nd_quality.replace_via_mediasync(navidrome_id, candidate, progress=progress)
                got = int(result.get("bitrate") or 0)
                if _bitrate_is_upgrade(got, 0, wanted) or (wanted and got + 24 >= wanted):
                    with _jobs_lock:
                        _jobs[job_id]["status"] = "done"
                        _jobs[job_id]["navidromeId"] = result.get("navidromeId")
                        _jobs[job_id]["bitrate"] = got
                        _jobs[job_id]["message"] = "Fertig"
                        _jobs[job_id]["title"] = result.get("title") or _jobs[job_id].get("title") or ""
                    return
                hub_error = f"Hub lieferte {got or 'keine'} kbit/s statt {wanted or 'der gewählten Qualität'}"
            except Exception as exc:
                hub_error = str(exc)
                logger.warning("Hub-Qualität {} nicht nutzbar: {}", source, exc)
            progress("Hub-Download ungeeignet, prüfe YouTube …")

        progress("Suche direkte YouTube-Quelle …")
        youtube = nd_quality.resolve_direct_video_id(navidrome_id, video_id, candidate)
        if not youtube:
            raise RuntimeError(
                hub_error
                or "Keine YouTube-Quelle für den direkten Download gefunden."
            )
        progress("Lade selbst nach Navidrome …")
        try:
            result = replace_track(navidrome_id, youtube, format_id, wanted_bitrate=wanted)
        except Exception as exc:
            if hub_error:
                raise RuntimeError(f"{exc} Hub: {hub_error}") from exc
            raise
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["navidromeId"] = result.get("navidromeId")
            _jobs[job_id]["bitrate"] = result.get("bitrate") or 0
            _jobs[job_id]["message"] = "Fertig"
            _jobs[job_id]["title"] = result.get("title") or _jobs[job_id].get("title") or ""
    except Exception as exc:
        logger.error("Qualitäts-Tausch von {} fehlgeschlagen: {}", navidrome_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["message"] = str(exc)
    finally:
        with _jobs_lock:
            _inflight.pop(inflight_key, None)


def start_replace_job(
    navidrome_id: str,
    video_id: str = "",
    format_id: str | None = None,
    *,
    source: str = "ytm",
    candidate: dict | None = None,
) -> dict:
    nd_id = (navidrome_id or "").strip()
    video_id = (video_id or "").strip()
    source = (source or "ytm").strip().lower() or "ytm"
    inflight_key = f"replace:{nd_id}:{video_id or source}:{str((candidate or {}).get('id') or '')[:24]}"
    with _jobs_lock:
        existing_id = _inflight.get(inflight_key)
        if existing_id and existing_id in _jobs:
            existing = _jobs[existing_id]
            started = float(existing.get("started_at") or 0)
            still_fresh = existing.get("status") in {"queued", "running"} and started and time.time() - started < 45
            if still_fresh:
                return job_snapshot(existing)
            existing["status"] = "error"
            existing["error"] = "Neuer direkter Download gestartet"
            existing["message"] = existing["error"]
            _inflight.pop(inflight_key, None)
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "videoId": video_id,
            "status": "queued",
            "error": None,
            "navidromeId": nd_id,
            "title": "",
            "bitrate": 0,
            "message": "Lade selbst nach Navidrome …",
            "started_at": time.time(),
        }
        _jobs[job_id] = job
        _inflight[inflight_key] = job_id

    thread = threading.Thread(
        target=_run_replace,
        args=(job_id, nd_id, video_id, format_id, inflight_key, source, candidate),
        daemon=True,
        name=f"nd-quality-{nd_id[:8]}",
    )
    thread.start()
    return job_snapshot(job)
