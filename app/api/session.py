"""Cookie sessions for the built-in web player.

Subsonic clients authenticate per request with `u`/`t`/`s`; the browser instead
gets a signed cookie so the `<audio>` element can hit the stream endpoint
without credentials in the URL.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from itsdangerous import URLSafeTimedSerializer

from app.core import auth_store
from app.core.settings import clean_secret, settings

COOKIE_NAME = "music_play_session"
# Custom header so reverse proxies do not treat the app token as HTTP Basic/JWT.
TOKEN_HEADER = "x-music-play-token"
# Browsers cap persistent cookies around 400 days; refresh on each app open.
MAX_AGE = 60 * 60 * 24 * 400

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="music-play-session")


def secrets_equal(left: str | None, right: str | None) -> bool:
    """Length-safe comparison. hmac.compare_digest raises if sizes differ."""
    digest = hashlib.sha256
    a = digest((left or "").encode("utf-8")).digest()
    b = digest((right or "").encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


def check_password(username: str, password: str) -> bool:
    if auth_store.needs_setup() or auth_store.allows_anonymous():
        return True
    return auth_store.verify(username, password)


def issue(username: str) -> str:
    return _serializer.dumps({"u": username})


def _load(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=MAX_AGE)
    except Exception:
        return None
    username = data.get("u")
    if username and not auth_store.get_user(username):
        return None
    return username


def read(request: Request) -> str | None:
    user = _load(request.cookies.get(COOKIE_NAME))
    if user:
        return user
    custom = (request.headers.get(TOKEN_HEADER) or "").strip()
    if custom.lower().startswith("bearer "):
        custom = custom.split(" ", 1)[1].strip()
    user = _load(custom)
    if user:
        return user
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return _load(header.split(" ", 1)[1].strip())
    return None


def request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    return "proto=https" in (request.headers.get("forwarded") or "").lower()


def apply_cookie(response: Response, token: str, request: Request) -> None:
    """HttpOnly session cookie. SameSite=None on HTTPS so the iPhone PWA keeps it."""
    secure = request_is_https(request)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=MAX_AGE,
        expires=datetime.now(timezone.utc) + timedelta(seconds=MAX_AGE),
        httponly=True,
        samesite="none" if secure else "lax",
        path="/",
        secure=secure,
    )


def clear_cookie(response: Response) -> None:
    for secure in (False, True):
        for samesite in ("lax", "none"):
            response.delete_cookie(
                COOKIE_NAME,
                path="/",
                httponly=True,
                samesite=samesite,
                secure=secure,
            )
