"""Encrypt secrets at rest and hash the app password.

Navidrome/MediaSync and Subsonic still need the real password in memory.
JSON files on the config share only store ciphertext (AES via Fernet) plus
an scrypt hash for the Music Play login.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.settings import settings

SEAL_PREFIX = "enc:v1:"
HASH_PREFIX = "scrypt:v1:"
MIN_PASSWORD_LENGTH = 8


def _fernet() -> Fernet:
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"music-play-secrets-v1",
        info=b"config-at-rest",
    ).derive(settings.session_secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(material))


def is_sealed(value: str | None) -> bool:
    return str(value or "").startswith(SEAL_PREFIX)


def seal(plaintext: str | None) -> str:
    secret = plaintext or ""
    if not secret:
        return ""
    if is_sealed(secret):
        return secret
    token = _fernet().encrypt(secret.encode("utf-8")).decode("ascii")
    return f"{SEAL_PREFIX}{token}"


def unseal(value: str | None) -> str:
    raw = value or ""
    if not raw:
        return ""
    if not is_sealed(raw):
        return raw
    try:
        return _fernet().decrypt(raw[len(SEAL_PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return ""


def hash_password(password: str) -> str:
    if not password:
        return ""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return (
        HASH_PREFIX
        + base64.urlsafe_b64encode(salt).decode("ascii")
        + ":"
        + base64.urlsafe_b64encode(digest).decode("ascii")
    )


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash or not stored_hash.startswith(HASH_PREFIX):
        return False
    try:
        salt_b64, digest_b64 = stored_hash[len(HASH_PREFIX) :].split(":", 1)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


def needs_migration(value: str | None) -> bool:
    raw = value or ""
    return bool(raw) and not is_sealed(raw)


def write_private_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
