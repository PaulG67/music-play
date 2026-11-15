"""Per-request Navidrome credentials for the signed-in Music Play user."""

from __future__ import annotations

from contextvars import ContextVar, Token

_overlay: ContextVar[dict | None] = ContextVar("navidrome_user_overlay", default=None)


def bind(credentials: dict | None) -> Token:
    return _overlay.set(credentials if credentials else None)


def reset(token: Token) -> None:
    _overlay.reset(token)


def current() -> dict | None:
    return _overlay.get()
