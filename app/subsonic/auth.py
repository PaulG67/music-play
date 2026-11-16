"""Subsonic credential handling.

Supports the three schemes clients use in practice: plaintext `p`, hex-encoded
`p=enc:...`, and the salted token `t`/`s` that Amperfy sends by default.
Each Music Play user authenticates with their own Subsonic password.
"""

import hashlib
from typing import Protocol

from fastapi import Request

from app.api.session import secrets_equal
from app.core import auth_store
from app.navidrome import context as nd_context
from app.subsonic.response import (
    ERROR_BAD_CREDENTIALS,
    ERROR_MISSING_PARAM,
    SubsonicError,
)


class ParamSource(Protocol):
    def get(self, name: str, default: str | None = ...) -> str | None: ...


def _accept(request: Request, user: dict) -> str:
    request.state.session_user = user["username"]
    nd_context.bind(auth_store.navidrome_credentials(user["username"]))
    return user["username"]


def _decode_password(raw: str) -> str:
    if raw.startswith("enc:"):
        try:
            return bytes.fromhex(raw[4:]).decode("utf-8")
        except ValueError:
            return raw
    return raw


def authenticate(request: Request, params: ParamSource) -> str:
    """`params` covers query string *and* form body, so POSTed credentials
    (the `formPost` extension) authenticate the same way."""
    username = params.get("u")

    session_user = getattr(request.state, "session_user", None)
    if not username and session_user:
        return session_user

    if auth_store.allows_anonymous() or not auth_store.password_required():
        if username:
            named = auth_store.get_user(username)
            if named:
                return _accept(request, named)
        admin = auth_store.get_user(auth_store.username())
        if admin:
            return _accept(request, admin)
        return auth_store.username()

    if not username:
        raise SubsonicError(ERROR_MISSING_PARAM, "Erforderlicher Parameter 'u' fehlt")

    user = auth_store.get_user(username)
    if user is None:
        raise SubsonicError(ERROR_BAD_CREDENTIALS, "Benutzername oder Passwort falsch")

    token = params.get("t")
    salt = params.get("s")
    password = params.get("p")
    expected_password = user.get("password") or ""

    if token and salt:
        expected = hashlib.md5((expected_password + salt).encode("utf-8")).hexdigest()
        if not secrets_equal(expected, token.lower()):
            raise SubsonicError(ERROR_BAD_CREDENTIALS, "Benutzername oder Passwort falsch")
        return _accept(request, user)

    if password is not None:
        if not secrets_equal(_decode_password(password), expected_password):
            raise SubsonicError(ERROR_BAD_CREDENTIALS, "Benutzername oder Passwort falsch")
        return _accept(request, user)

    if params.get("apiKey"):
        if not secrets_equal(params["apiKey"], expected_password):
            raise SubsonicError(ERROR_BAD_CREDENTIALS, "API-Key ungültig")
        return _accept(request, user)

    raise SubsonicError(ERROR_MISSING_PARAM, "Erforderlicher Parameter 'p' oder 't'/'s' fehlt")
