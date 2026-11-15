"""JSON API consumed by the built-in web player."""

import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from app.api import session as web_session
from app.core import auth_store
from app.core.logging import logger
from app.core.secret_box import MIN_PASSWORD_LENGTH
from app.core.settings import APP_VERSION, settings
from app.db import repo
from app.db.database import session_scope
from app.services import catalog, extras, stats
from app.services import translate as translate_service
from app.mediasync import bridge as mediasync
from app.mediasync import config as mediasync_config
from app.mediasync.client import MediaSyncError
from app.navidrome import bridge as navidrome
from app.navidrome import config as navidrome_config
from app.navidrome import ids as nd_ids
from app.navidrome.client import NavidromeError
from app.subsonic import media
from app.ytm import ids
from app.ytm import client as ytm
from app.ytm import streams

router = APIRouter(prefix="/api")

_login_attempts: dict[str, list[float]] = {}


def _peer(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _throttle_login(request: Request) -> None:
    now = time.monotonic()
    ip = _peer(request)
    recent = [stamp for stamp in _login_attempts.get(ip, []) if now - stamp < 900]
    _login_attempts[ip] = recent
    if len(recent) >= 8:
        raise HTTPException(status_code=429, detail="Zu viele Anmeldeversuche. Warte ein paar Minuten.")


def _note_login_failure(request: Request) -> None:
    ip = _peer(request)
    _login_attempts.setdefault(ip, []).append(time.monotonic())


def _clear_login_failures(request: Request) -> None:
    _login_attempts.pop(_peer(request), None)


def _require_session(request: Request) -> str:
    username = getattr(request.state, "session_user", None)
    if username:
        return username
    if not auth_store.password_required():
        raise HTTPException(status_code=401, detail="Zugang einrichten")
    raise HTTPException(status_code=401, detail="Nicht angemeldet")


def _require_admin(request: Request) -> str:
    username = _require_session(request)
    if not auth_store.is_admin(username):
        raise HTTPException(status_code=403, detail="Nur Administratoren können Benutzer verwalten")
    return username


def _subsonic_id(item: dict, kind: str) -> str:
    item_id = item.get("id") or ""
    if kind in {"song", "video", "episode"}:
        return ids.song_id(item_id)
    if kind == "album":
        return ids.album_id(item_id)
    if kind == "artist":
        return ids.artist_id(item.get("id"), item.get("name") or item.get("title") or "")
    if kind == "podcast":
        return ids.podcast_id(item_id)
    return item_id


def _json_clean(value):
    """Recursively drop values FastAPI cannot JSON-encode (YouTube leftovers)."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return 0 if value != value or abs(value) == float("inf") else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_clean(item) for item in value]
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or text in {"None", "null"} or len(text) > 2000:
        return None
    return text


def _json_item(item: dict) -> dict:
    cleaned = _json_clean(item)
    return cleaned if isinstance(cleaned, dict) else {}


def _decorate(items: list[dict], kind: str | None = None) -> list[dict]:
    """Attach the Subsonic id and favourite flag the frontend needs.

    With `kind` omitted, each item's own `kind` is used, which is how mixed
    home/chart sections arrive.
    """
    if not items:
        return []

    with session_scope() as session:
        starred = repo.starred_map(session)

    decorated = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_kind = kind or item.get("kind") or "song"
        try:
            subsonic_id = _subsonic_id(item, item_kind)
        except Exception as exc:
            logger.debug("Entdecken-Eintrag übersprungen ({}): {}", item_kind, exc)
            continue
        decorated.append({**item, "sid": subsonic_id, "starred": subsonic_id in starred, "kind": item_kind})
    try:
        enriched = navidrome.enrich_items(decorated, kind)
    except Exception as exc:
        logger.debug("Navidrome-Anreicherung übersprungen: {}", exc)
        enriched = decorated
    return [_json_item(item) for item in enriched if isinstance(item, dict)]


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@router.get("/login-hint")
async def login_hint():
    required = auth_store.password_required()
    return {
        "username": "" if required else (auth_store.username() or "musicplay"),
        "passwordRequired": required,
        "version": APP_VERSION,
    }


def _request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    return "proto=https" in (request.headers.get("forwarded") or "").lower()


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    secure = _request_is_https(request)
    response.set_cookie(
        web_session.COOKIE_NAME,
        token,
        max_age=web_session.MAX_AGE,
        expires=datetime.now(timezone.utc) + timedelta(seconds=web_session.MAX_AGE),
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
    )


def _clear_session_cookie(response: Response) -> None:
    for secure in (False, True):
        response.delete_cookie(
            web_session.COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )


def _session_payload(user: str | None) -> dict:
    required = auth_store.password_required()
    record = auth_store.get_user(user) if user else None
    return {
        "authenticated": bool(user),
        "passwordRequired": required,
        "needsSetup": not required,
        "user": user or (auth_store.username() if not required else None),
        "isAdmin": bool(record and record.get("role") == "admin"),
        "version": APP_VERSION,
        "ytmAccount": ytm.is_authenticated(),
        "location": settings.ytm_location,
    }


@router.post("/login")
async def login(request: Request):
    content_type = (request.headers.get("content-type") or "").lower()
    wants_json = "application/json" in content_type
    if wants_json:
        payload = await request.json()
    else:
        form = await request.form()
        payload = {
            "username": form.get("username") or form.get("mp_username"),
            "password": form.get("password") or form.get("mp_password"),
        }

    _throttle_login(request)
    username = web_session.clean_secret(
        str(payload.get("username") or payload.get("mp_username") or "")
    )
    password = str(payload.get("password") or payload.get("mp_password") or "")
    if not auth_store.password_required():
        detail = "Zuerst unter Zugang ein Passwort setzen."
        if wants_json:
            raise HTTPException(status_code=401, detail=detail)
        return RedirectResponse("/?login=fail", status_code=303)
    record = auth_store.get_user(username)
    if record is None or not web_session.check_password(username, password):
        _note_login_failure(request)
        logger.warning("Login fehlgeschlagen")
        detail = "Benutzername oder Passwort falsch."
        if wants_json:
            raise HTTPException(status_code=401, detail=detail)
        return RedirectResponse("/?login=fail", status_code=303)

    _clear_login_failures(request)
    token = web_session.issue(record["username"])
    logger.info("Web-Login erfolgreich")
    if wants_json:
        response = JSONResponse(
            {"user": record["username"], "token": token, "version": APP_VERSION, "isAdmin": record.get("role") == "admin"}
        )
        _set_session_cookie(response, token, request)
        return response

    redirect = RedirectResponse("/", status_code=303)
    _set_session_cookie(redirect, token, request)
    return redirect


@router.post("/logout")
async def logout(response: Response):
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/session")
async def whoami(request: Request, response: Response):
    user = getattr(request.state, "session_user", None)
    payload = _session_payload(user)
    if user:
        token = web_session.issue(user)
        _set_session_cookie(response, token, request)
        payload["token"] = token
    return payload


@router.post("/account")
async def save_account(request: Request, response: Response, payload: dict = Body(...)):
    """Create the first admin or update the signed-in user's Subsonic login."""
    if payload.get("password") is None:
        raise HTTPException(status_code=400, detail="Passwort fehlt")
    new_password = web_session.clean_secret(str(payload.get("password")))
    confirm = web_session.clean_secret(str(payload.get("passwordConfirm") or new_password))
    if not new_password:
        raise HTTPException(status_code=400, detail="Passwort darf nicht leer sein")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Passwort mindestens {MIN_PASSWORD_LENGTH} Zeichen")
    if new_password != confirm:
        raise HTTPException(status_code=400, detail="Passwörter stimmen nicht überein")

    wanted = web_session.clean_secret(payload.get("username") or "")
    try:
        if not auth_store.password_required():
            user = auth_store.ensure_setup_user(wanted or "musicplay", new_password)
        else:
            current = _require_session(request)
            user = auth_store.save_user(current, password=new_password, new_username=wanted or current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = web_session.issue(user["username"])
    _set_session_cookie(response, token, request)
    logger.info("Zugang aktualisiert für {}", user["username"])
    return {
        "ok": True,
        "user": user["username"],
        "token": token,
        "passwordRequired": True,
        "isAdmin": user.get("role") == "admin",
    }


@router.get("/users")
async def list_users(request: Request):
    _require_admin(request)
    return {"users": auth_store.list_public()}


@router.post("/users")
async def create_user(request: Request, payload: dict = Body(...)):
    _require_admin(request)
    username = web_session.clean_secret(payload.get("username") or "")
    password = web_session.clean_secret(str(payload.get("password") or ""))
    confirm = web_session.clean_secret(str(payload.get("passwordConfirm") or password))
    if password != confirm:
        raise HTTPException(status_code=400, detail="Passwörter stimmen nicht überein")
    try:
        user = auth_store.create_user(username, password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": user["username"]}


@router.put("/users/{username}")
async def update_user(request: Request, username: str, payload: dict = Body(...)):
    _require_admin(request)
    password = web_session.clean_secret(str(payload.get("password") or ""))
    confirm = web_session.clean_secret(str(payload.get("passwordConfirm") or password))
    if not password:
        raise HTTPException(status_code=400, detail="Passwort fehlt")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Passwort mindestens {MIN_PASSWORD_LENGTH} Zeichen")
    if password != confirm:
        raise HTTPException(status_code=400, detail="Passwörter stimmen nicht überein")
    try:
        auth_store.save_user(username, password=password)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "user": username}


@router.delete("/users/{username}")
async def delete_user(request: Request, username: str):
    admin = _require_admin(request)
    if web_session.secrets_equal(username, admin):
        raise HTTPException(status_code=400, detail="Eigenes Konto bitte über Abmelden nicht löschen – einen anderen Admin nutzen")
    try:
        auth_store.delete_user(username)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@router.get("/home")
async def home(request: Request):
    _require_session(request)
    try:
        sections = await catalog.home()
        if not sections:
            sections = await catalog.charts()
        return {"sections": [{"title": s["title"], "items": _decorate(s["items"])} for s in sections]}
    except Exception as exc:
        logger.error("Startseite fehlgeschlagen: {}", exc)
        return {"sections": []}


@router.get("/charts")
async def charts(request: Request):
    _require_session(request)
    try:
        sections = await catalog.charts()
        return {"sections": [{"title": s["title"], "items": _decorate(s["items"])} for s in sections]}
    except Exception as exc:
        logger.error("Charts fehlgeschlagen: {}", exc)
        return {"sections": []}


@router.get("/search")
async def search(request: Request, q: str = "", scope: str = "all", limit: int = 25):
    _require_session(request)
    if not q.strip():
        return {"songs": [], "albums": [], "artists": []}

    if scope == "all":
        try:
            results = await catalog.search(q, song_limit=limit, album_limit=12, artist_limit=12)
        except Exception as exc:
            logger.exception("Suche fehlgeschlagen: {}", exc)
            results = {}
        try:
            library = await navidrome.search(q, limit=limit)
        except Exception as exc:
            logger.debug("Navidrome-Suche übersprungen: {}", exc)
            library = {}
        ytm_songs = _decorate(results.get("songs") or [], "song")
        seen = {(song.get("navidromeId") or "").lower() for song in ytm_songs if song.get("navidromeId")}
        extra_songs = [
            song
            for song in library.get("songs") or []
            if (song.get("navidromeId") or "").lower() not in seen
        ]
        top = results.get("top")
        return _json_clean(
            {
                "top": (_decorate([top], top.get("kind") or "playlist") or [None])[0] if top else None,
                "items": _decorate(results.get("items") or []),
                "songs": ytm_songs,
                "videos": _decorate(results.get("videos") or [], "song"),
                "albums": _decorate(results.get("albums") or [], "album"),
                "artists": _decorate(results.get("artists") or [], "artist"),
                "playlists": _decorate(results.get("playlists") or [], "playlist"),
                "community_playlists": _decorate(results.get("community_playlists") or [], "playlist"),
                "podcasts": _decorate(results.get("podcasts") or [], "podcast"),
                "episodes": _decorate(results.get("episodes") or [], "song"),
                "library": {
                    "songs": _decorate(extra_songs, "song"),
                    "albums": library.get("albums") or [],
                    "artists": library.get("artists") or [],
                },
            }
        )

    items = await catalog.search_scope(q, scope, limit)
    kind = {"songs": "song", "albums": "album", "artists": "artist", "playlists": "playlist"}.get(scope, "song")
    return {scope: _decorate(items, kind)}


@router.get("/suggest")
async def suggest(request: Request, q: str = ""):
    _require_session(request)
    return {"suggestions": await catalog.suggestions(q)}


@router.get("/album/{album_id}")
async def album(request: Request, album_id: str):
    _require_session(request)
    if nd_ids.is_navidrome(album_id):
        data = await navidrome.get_album(album_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Album nicht gefunden")
        return _json_clean({**data, "sid": ids.album_id(album_id), "tracks": _decorate(data.get("tracks") or [], "song")})
    data = await catalog.get_album(album_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Album nicht gefunden")
    return _json_clean({**data, "sid": ids.album_id(album_id), "tracks": _decorate(data.get("tracks") or [], "song")})


@router.get("/artist/{artist_id}")
async def artist(request: Request, artist_id: str):
    _require_session(request)
    if nd_ids.is_navidrome(artist_id):
        data = await navidrome.get_artist(artist_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Interpret nicht gefunden")
        return _json_clean(
            {
                **data,
                "sid": ids.artist_id(artist_id, data.get("name", "")),
                "albums": _decorate(data.get("albums") or [], "album"),
                "top_songs": _decorate(data.get("top_songs") or [], "song"),
                "related": [],
            }
        )
    data = await catalog.get_artist(artist_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Interpret nicht gefunden")
    return _json_clean(
        {
            **data,
            "sid": ids.artist_id(artist_id, data.get("name", "")),
            "albums": _decorate(data.get("albums") or [], "album"),
            "top_songs": _decorate(data.get("top_songs") or [], "song"),
            "related": _decorate(data.get("related") or [], "artist"),
        }
    )


@router.get("/ytplaylist/{playlist_id}")
async def yt_playlist(request: Request, playlist_id: str):
    _require_session(request)
    try:
        data = await catalog.get_ytm_playlist(playlist_id)
    except Exception as exc:
        logger.exception("Playlist {} fehlgeschlagen: {}", playlist_id, exc)
        raise HTTPException(status_code=502, detail="Playlist konnte nicht geladen werden")
    if data is None:
        raise HTTPException(status_code=404, detail="Playlist nicht gefunden")
    payload = {k: v for k, v in data.items() if k != "tracks"}
    payload["tracks"] = _decorate(data.get("tracks") or [], "song")
    return _json_clean(payload)


@router.get("/radio/{video_id}")
async def radio(request: Request, video_id: str, limit: int = 30):
    _require_session(request)
    if nd_ids.is_navidrome(video_id):
        return {"tracks": []}
    return {"tracks": _decorate(await catalog.radio(video_id, limit), "song")}


@router.get("/lyrics/{video_id}")
async def lyrics(request: Request, video_id: str, translate: bool = False, lang: str = ""):
    _require_session(request)
    if nd_ids.is_navidrome(video_id):
        return {"text": "", "synced": None, "source": "", "translation": None}
    found = await catalog.lyrics(video_id)
    if not found:
        return {"text": "", "synced": None, "source": "", "translation": None}

    translation = None
    if translate:
        translation = await translate_service.translate_lyrics(video_id, found, lang or None)

    return {
        **found,
        "translation": translation,
        "translationAvailable": settings.translation_enabled,
        "translationLanguage": settings.translate_language,
    }


@router.get("/votes/{video_id}")
async def votes(request: Request, video_id: str):
    """Like/dislike counts via ReturnYouTubeDislike."""
    _require_session(request)
    if nd_ids.is_navidrome(video_id) or len(video_id) != 11:
        return {}
    return await extras.votes(video_id) or {}


# --------------------------------------------------------------------------
# Moods, genres and podcasts
# --------------------------------------------------------------------------


@router.get("/moods")
async def moods(request: Request):
    _require_session(request)
    return {"sections": await catalog.mood_categories()}


@router.get("/mood")
@router.post("/mood")
async def mood(request: Request, params: str = ""):
    _require_session(request)
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        params = (body.get("params") or params or "") if isinstance(body, dict) else params
    params = (params or "").strip()
    if not params:
        return {"playlists": []}
    try:
        items = await catalog.mood_playlists(params)
        return {"playlists": _decorate(items)}
    except Exception as exc:
        logger.exception("Playlists für Entdecken-Kategorie fehlgeschlagen")
        return {"playlists": []}


@router.get("/podcasts")
async def podcasts(request: Request):
    _require_session(request)
    with session_scope() as session:
        subscribed = repo.subscribed_podcasts(session)
    return {
        "subscribed": _decorate([{**item, "name": item["title"]} for item in subscribed], "podcast"),
        "newEpisodes": _decorate(await catalog.new_episodes(), "song"),
    }


@router.get("/podcasts/search")
async def search_podcasts(request: Request, q: str, limit: int = 20):
    _require_session(request)
    shows = await catalog.search_podcasts(q, limit)
    episodes = await catalog.search_episodes(q, limit)
    return {
        "podcasts": _decorate([{**item, "name": item["title"]} for item in shows], "podcast"),
        "episodes": _decorate(episodes, "song"),
    }


@router.get("/podcast/{podcast_id}")
async def podcast(request: Request, podcast_id: str):
    _require_session(request)
    found = await catalog.get_podcast(podcast_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Podcast nicht gefunden")

    subsonic_id = ids.podcast_id(podcast_id)
    with session_scope() as session:
        subscribed = repo.is_starred(session, subsonic_id, "podcast") is not None

    return {
        **found,
        "sid": subsonic_id,
        "subscribed": subscribed,
        "episodes": _decorate(found.get("episodes") or [], "song"),
    }


@router.post("/podcast/{podcast_id}/subscribe")
async def subscribe_podcast(request: Request, podcast_id: str, payload: dict = Body({})):
    _require_session(request)
    subscribe = payload.get("subscribed", True)
    subsonic_id = ids.podcast_id(podcast_id)

    if subscribe:
        await catalog.get_podcast(podcast_id)

    with session_scope() as session:
        if subscribe:
            repo.star(session, subsonic_id, "podcast")
        else:
            repo.unstar(session, subsonic_id, "podcast")

    return {"sid": subsonic_id, "subscribed": bool(subscribe)}


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


@router.get("/stats")
async def listening_stats(request: Request, limit: int = 10):
    _require_session(request)
    with session_scope() as session:
        return stats.overview(session, limit)


@router.get("/wrapped/{year}")
async def wrapped(request: Request, year: int):
    _require_session(request)
    with session_scope() as session:
        return stats.wrapped(session, year)


# --------------------------------------------------------------------------
# Player preferences (equaliser, crossfade, sleep timer)
# --------------------------------------------------------------------------


@router.get("/settings")
async def read_settings(request: Request):
    _require_session(request)
    with session_scope() as session:
        stored = repo.all_settings(session)
    return {
        "player": stored.get("player", {}),
        "equalizer": stored.get("equalizer", {}),
        "translationAvailable": settings.translation_enabled,
        "translationLanguage": settings.translate_language,
        "sponsorblock": settings.sponsorblock_enabled,
        "dislikes": settings.return_youtube_dislike,
    }


@router.put("/settings/{key}")
async def write_settings(request: Request, key: str, payload: dict = Body(...)):
    _require_session(request)
    if key not in ("player", "equalizer"):
        raise HTTPException(status_code=400, detail="Unbekannter Einstellungsbereich")
    with session_scope() as session:
        repo.set_setting(session, key, payload)
    return {"ok": True}


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------


@router.get("/favorites")
async def favorites(request: Request):
    _require_session(request)
    with session_scope() as session:
        songs = repo.starred_tracks(session)
        albums = repo.starred_albums(session)
        artists = repo.starred_artists(session)
        recent = repo.recently_played_tracks(session, 60)
        most = repo.most_played_tracks(session, 60)
    return {
        "songs": _decorate(songs, "song"),
        "albums": _decorate(albums, "album"),
        "artists": _decorate(artists, "artist"),
        "recent": _decorate(recent, "song"),
        "most": _decorate(most, "song"),
    }


@router.post("/favorite")
async def toggle_favorite(request: Request, payload: dict = Body(...)):
    _require_session(request)
    subsonic_id = payload.get("sid")
    if not subsonic_id:
        raise HTTPException(status_code=400, detail="sid fehlt")

    kind, value = ids.parse(subsonic_id)
    item_type = {ids.SONG: "song", ids.ALBUM: "album", ids.ARTIST: "artist"}.get(kind)
    if item_type is None:
        raise HTTPException(status_code=400, detail="Unbekannter Typ")

    starred = bool(payload.get("starred"))
    if starred:
        # Metadata must exist locally before the item joins the library.
        if item_type == "song":
            await catalog.get_song(value)
        elif item_type == "album":
            await catalog.get_album(value)
        else:
            channel_id, _ = ids.artist_payload(subsonic_id)
            if channel_id:
                await catalog.get_artist(channel_id)

    with session_scope() as session:
        if starred:
            repo.star(session, subsonic_id, item_type)
        else:
            repo.unstar(session, subsonic_id, item_type)

    return {"sid": subsonic_id, "starred": starred}


@router.get("/playlists")
async def playlists(request: Request):
    _require_session(request)
    with session_scope() as session:
        items = [repo.playlist_summary(session, playlist) for playlist in repo.list_playlists(session)]
    return {"playlists": items, "account": await catalog.account_playlists()}


@router.get("/playlist/{playlist_id}")
async def playlist(request: Request, playlist_id: int):
    _require_session(request)
    with session_scope() as session:
        found = repo.get_playlist(session, playlist_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Playlist nicht gefunden")
        summary = repo.playlist_summary(session, found)
        tracks = repo.playlist_tracks(session, playlist_id)
    return {**summary, "tracks": _decorate(tracks, "song")}


@router.post("/playlists")
async def create_playlist(request: Request, payload: dict = Body(...)):
    _require_session(request)
    video_ids = [ids.parse(sid)[1] for sid in payload.get("tracks", []) if ids.parse(sid)[0] == ids.SONG]
    await catalog.get_songs(video_ids)
    with session_scope() as session:
        created = repo.create_playlist(session, payload.get("name", "Neue Playlist"), video_ids)
        session.flush()
        summary = repo.playlist_summary(session, created)
    try:
        remote = await run_in_threadpool(navidrome.sync_playlist, summary["id"])
        if remote:
            summary["navidrome_id"] = remote
    except Exception as exc:
        logger.debug("Playlist nicht nach Navidrome gespiegelt: {}", exc)
    return summary


@router.post("/playlist/{playlist_id}/tracks")
async def add_to_playlist(request: Request, playlist_id: int, payload: dict = Body(...)):
    _require_session(request)
    video_ids = [ids.parse(sid)[1] for sid in payload.get("tracks", []) if ids.parse(sid)[0] == ids.SONG]
    await catalog.get_songs(video_ids)
    with session_scope() as session:
        if repo.get_playlist(session, playlist_id) is None:
            raise HTTPException(status_code=404, detail="Playlist nicht gefunden")
        if payload.get("replace"):
            repo.replace_playlist_tracks(session, playlist_id, video_ids)
        else:
            repo.append_playlist_tracks(session, playlist_id, video_ids)
    try:
        await run_in_threadpool(navidrome.sync_playlist, playlist_id)
    except Exception as exc:
        logger.debug("Playlist-Sync nach Navidrome: {}", exc)
    return {"ok": True}


@router.post("/playlist/{playlist_id}/remove-tracks")
async def remove_from_playlist(request: Request, playlist_id: int, payload: dict = Body(...)):
    _require_session(request)
    track_ids = [str(item) for item in (payload.get("trackIds") or []) if item]
    if payload.get("trackId"):
        track_ids.append(str(payload.get("trackId")))
    for sid in payload.get("tracks") or []:
        kind, video_id = ids.parse(sid)
        if kind == ids.SONG and video_id:
            track_ids.append(video_id)
    if payload.get("sid"):
        kind, video_id = ids.parse(str(payload.get("sid")))
        if kind == ids.SONG and video_id:
            track_ids.append(video_id)
    index = payload.get("index")
    try:
        index = int(index) if index is not None and str(index) != "" else None
    except (TypeError, ValueError):
        index = None
    with session_scope() as session:
        if repo.get_playlist(session, playlist_id) is None:
            raise HTTPException(status_code=404, detail="Playlist nicht gefunden")
        removed = repo.remove_playlist_entries(session, playlist_id, index=index, track_ids=track_ids)
    if not removed:
        raise HTTPException(status_code=404, detail="Titel nicht in der Playlist")
    try:
        await run_in_threadpool(navidrome.sync_playlist, playlist_id)
    except Exception as exc:
        logger.debug("Playlist-Sync nach Navidrome: {}", exc)
    return {"ok": True}


@router.delete("/playlist/{playlist_id}")
async def remove_playlist(request: Request, playlist_id: int):
    _require_session(request)
    remote_id = None
    with session_scope() as session:
        found = repo.get_playlist(session, playlist_id)
        if found is not None:
            remote_id = found.navidrome_id
        repo.delete_playlist(session, playlist_id)
    if remote_id:
        await run_in_threadpool(navidrome.delete_remote_playlist, remote_id)
    return {"ok": True}


@router.post("/playlist/{playlist_id}/navidrome")
async def push_playlist_to_navidrome(request: Request, playlist_id: int):
    """Import missing tracks into Navidrome, then replace the remote playlist."""
    _require_session(request)
    with session_scope() as session:
        if repo.get_playlist(session, playlist_id) is None:
            raise HTTPException(status_code=404, detail="Playlist nicht gefunden")
        video_ids = repo.playlist_track_ids(session, playlist_id)
    youtube_ids = [video_id for video_id in video_ids if not nd_ids.is_navidrome(video_id)]
    jobs = await navidrome.import_tracks(youtube_ids) if youtube_ids else []
    try:
        remote = await run_in_threadpool(navidrome.sync_playlist, playlist_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "navidromeId": remote, "jobs": jobs}


@router.post("/scrobble")
async def scrobble(request: Request, payload: dict = Body(...)):
    _require_session(request)
    video_id = payload.get("videoId")
    if not video_id:
        raise HTTPException(status_code=400, detail="videoId fehlt")
    await catalog.get_song(video_id)
    with session_scope() as session:
        repo.record_play(session, video_id, "web", seconds=int(payload.get("seconds") or 0))
    return {"ok": True}


# --------------------------------------------------------------------------
# Extras
# --------------------------------------------------------------------------


@router.get("/sponsorblock/{video_id}")
async def sponsorblock(request: Request, video_id: str):
    """Non-music segments (intros, outros) so the web player can skip them."""
    _require_session(request)
    if nd_ids.is_navidrome(video_id) or len(video_id) != 11:
        return {"segments": []}
    return {"segments": await extras.skip_segments(video_id)}


@router.get("/status")
async def status(request: Request):
    user = _require_session(request)
    with session_scope() as session:
        library = len(repo.library_track_ids(session))
    return {
        "version": APP_VERSION,
        "libraryTracks": library,
        "cache": streams.cache_stats(),
        "streamMode": settings.stream_mode,
        "subsonicUser": user,
        "isAdmin": auth_store.is_admin(user),
        "passwordRequired": auth_store.password_required(),
        "translation": {
            "enabled": settings.translation_enabled,
            "provider": settings.translate_provider,
            "language": settings.translate_language,
        },
        "sponsorblock": settings.sponsorblock_enabled,
        "dislikes": settings.return_youtube_dislike,
        "ytmAccount": ytm.is_authenticated(),
        "navidrome": navidrome.status(),
        "mediasync": mediasync.status(),
    }


@router.post("/cache/clear")
async def clear_cache(request: Request):
    _require_session(request)
    streams.clear_disk_cache()
    ytm.clear_cache()
    return {"ok": True}


# --------------------------------------------------------------------------
# Navidrome
# --------------------------------------------------------------------------


@router.get("/navidrome")
async def navidrome_status(request: Request):
    _require_session(request)
    return navidrome.status()


@router.post("/navidrome/test")
async def test_navidrome(request: Request, payload: dict | None = Body(None)):
    _require_session(request)
    payload = payload or {}
    stored = navidrome_config.load()
    url = (payload.get("url") or stored.get("url") or "").strip().rstrip("/")
    username = (payload.get("username") or stored.get("username") or "").strip()
    password = payload.get("password") or stored.get("password") or ""
    if not url or not username or not password:
        raise HTTPException(status_code=400, detail="URL, Benutzer und Passwort angeben")
    try:
        return await run_in_threadpool(navidrome.test_connection, url, username, password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Verbindung fehlgeschlagen: {exc}")


@router.put("/navidrome")
async def save_navidrome(request: Request, payload: dict = Body(...)):
    user = _require_session(request)
    url = (payload.get("url") or "").strip().rstrip("/")
    username = (payload.get("username") or "").strip()
    password = payload.get("password")
    if password == "":
        password = None
    stored = navidrome_config.load()
    secret = stored.get("password") if password is None else password
    if url and username and secret:
        try:
            await run_in_threadpool(navidrome.test_connection, url, username, secret)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Verbindung fehlgeschlagen: {exc}")
    try:
        auth_store.save_user(
            user,
            navidrome={"url": url, "username": username, "password": secret or stored.get("password") or ""},
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if auth_store.is_admin(user):
        navidrome_config.save(
            {
                "url": url or stored.get("url") or "",
                "username": stored.get("username") or "",
                "password": stored.get("password") or "",
                "music_dir": payload.get("musicDir") or stored.get("music_dir"),
                "import_folder": payload["importFolder"] if "importFolder" in payload else stored.get("import_folder"),
            }
        )
    from app.navidrome import context as nd_context

    nd_context.bind(auth_store.navidrome_credentials(user))
    navidrome.reload_index()
    return navidrome.status() | {"ok": True, "url": url}


@router.get("/navidrome/library")
async def navidrome_library(request: Request):
    _require_session(request)
    return await navidrome.library()


@router.delete("/navidrome/playlist/{playlist_id}")
async def delete_navidrome_playlist(request: Request, playlist_id: str):
    _require_session(request)
    try:
        await run_in_threadpool(navidrome.delete_navidrome_playlist, playlist_id)
    except NavidromeError as exc:
        raise HTTPException(status_code=getattr(exc, "status_code", 502), detail=str(exc)) from exc
    return {"ok": True}


@router.get("/navidrome/playlist/{playlist_id}")
async def navidrome_playlist(request: Request, playlist_id: str):
    _require_session(request)
    data = await navidrome.get_playlist(playlist_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Playlist nicht gefunden")
    return {**data, "tracks": _decorate(data.get("tracks") or [], "song")}


@router.get("/navidrome/cover/{cover_id}")
async def navidrome_cover(request: Request, cover_id: str, size: int = 544):
    _require_session(request)
    return await media.fetch_navidrome_cover(cover_id, size)


@router.post("/navidrome/playlists")
async def create_navidrome_playlist(request: Request, payload: dict = Body(...)):
    _require_session(request)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name fehlt")
    song_ids = [str(item) for item in (payload.get("songIds") or []) if item]
    try:
        created = await run_in_threadpool(navidrome.create_named_playlist, name, song_ids)
    except NavidromeError as exc:
        raise HTTPException(status_code=getattr(exc, "status_code", 502), detail=str(exc)) from exc
    return {"ok": True, "id": created, "name": name}


@router.post("/navidrome/playlist/{playlist_id}/tracks")
async def add_navidrome_playlist_tracks(request: Request, playlist_id: str, payload: dict = Body(...)):
    _require_session(request)
    song_ids = [str(item) for item in (payload.get("songIds") or []) if item]
    try:
        await run_in_threadpool(navidrome.add_songs_to_playlist, playlist_id, song_ids)
    except NavidromeError as exc:
        raise HTTPException(status_code=getattr(exc, "status_code", 502), detail=str(exc)) from exc
    return {"ok": True}


@router.post("/navidrome/playlist/{playlist_id}/remove-tracks")
async def remove_navidrome_playlist_tracks(request: Request, playlist_id: str, payload: dict = Body(...)):
    _require_session(request)
    song_ids = [str(item) for item in (payload.get("songIds") or []) if item]
    indexes = []
    for item in payload.get("indexes") or []:
        try:
            indexes.append(int(item))
        except (TypeError, ValueError):
            continue
    if payload.get("index") is not None and str(payload.get("index")) != "":
        try:
            indexes.append(int(payload.get("index")))
        except (TypeError, ValueError):
            pass
    try:
        await run_in_threadpool(
            lambda: navidrome.remove_songs_from_playlist(
                playlist_id, indexes=indexes or None, song_ids=song_ids or None
            )
        )
    except NavidromeError as exc:
        raise HTTPException(status_code=getattr(exc, "status_code", 502), detail=str(exc)) from exc
    return {"ok": True}


@router.post("/navidrome/import")
async def navidrome_import(request: Request, payload: dict = Body(...)):
    _require_session(request)
    if not navidrome.configured():
        raise HTTPException(status_code=400, detail="Navidrome ist nicht konfiguriert")
    video_ids = payload.get("videoIds") or []
    if payload.get("videoId"):
        video_ids = [payload["videoId"], *video_ids]
    video_ids = [item for item in video_ids if item]
    if not video_ids:
        raise HTTPException(status_code=400, detail="videoId fehlt")
    return {"jobs": await navidrome.import_tracks(video_ids)}


@router.get("/navidrome/job/{job_id}")
async def navidrome_job(request: Request, job_id: str):
    _require_session(request)
    found = navidrome.job(job_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Auftrag nicht gefunden")
    return found


# --------------------------------------------------------------------------
# MediaSync
# --------------------------------------------------------------------------


@router.get("/mediasync")
async def mediasync_status(request: Request):
    _require_session(request)
    return mediasync.status()


@router.put("/mediasync")
async def save_mediasync(request: Request, payload: dict = Body(...)):
    _require_session(request)
    url = (payload.get("url") or "").strip().rstrip("/")
    username = (payload.get("username") or "").strip()
    password = payload.get("password")
    if password == "":
        password = None
    stored = mediasync_config.load()
    secret = stored.get("password") if password is None else password
    if url:
        try:
            await run_in_threadpool(mediasync.test_connection, url, username, secret or "")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Verbindung fehlgeschlagen: {exc}")
    mediasync_config.save({"url": url, "username": username, "password": secret or ""})
    return mediasync.status() | {"ok": True}


@router.get("/mediasync/targets")
async def mediasync_targets(request: Request):
    _require_session(request)
    if not mediasync.configured():
        raise HTTPException(status_code=400, detail="MediaSync ist nicht konfiguriert")
    try:
        items = await run_in_threadpool(mediasync.targets)
    except MediaSyncError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"targets": items}


@router.post("/mediasync/send")
async def mediasync_send(request: Request, payload: dict = Body(...)):
    _require_session(request)
    if not mediasync.configured():
        raise HTTPException(status_code=400, detail="MediaSync ist nicht konfiguriert")

    raw_items = payload.get("tracks")
    if not isinstance(raw_items, list) or not raw_items:
        raw_items = [payload]

    tracks = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        video_id = item.get("videoId") or item.get("id") or payload.get("videoId") or payload.get("id")
        track = {
            "id": video_id or "",
            "title": item.get("title") or "",
            "artist": item.get("artist") or "",
            "album": item.get("album") or "",
        }
        if not track["title"] and video_id:
            found = await catalog.get_song(video_id)
            if found:
                track = found
        if not track.get("title"):
            continue
        tracks.append(track)

    if not tracks:
        raise HTTPException(status_code=400, detail="Titel fehlen")
    try:
        result = await run_in_threadpool(
            mediasync.send_many,
            tracks,
            payload.get("playlistId") or None,
            payload.get("playlistName") or None,
        )
    except MediaSyncError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return result


# --------------------------------------------------------------------------
# Playback for the web player (cookie authenticated)
# --------------------------------------------------------------------------


@router.get("/stream/{video_id}")
async def stream(request: Request, video_id: str):
    _require_session(request)
    return await media.stream_track(request, video_id)


@router.get("/cover")
async def cover(request: Request, u: str, size: int = 544):
    """Proxy artwork through the server.

    Two reasons: the browser never talks to Google directly, and same-origin
    responses can be stored in the Cache API for offline playback (opaque
    cross-origin responses cannot).
    """
    _require_session(request)
    if not u.startswith("https://"):
        raise HTTPException(status_code=400, detail="Ungültige Cover-URL")
    return await media.fetch_cover(u, size)
