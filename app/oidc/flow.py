"""OIDC authorization-code + PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core import auth_store
from app.core.logging import logger
from app.core.settings import clean_secret, settings
from app.oidc import config as oidc_config

STATE_COOKIE = "music_play_oidc"
STATE_MAX_AGE = 600
_discovery_cache: dict[str, tuple[float, dict]] = {}
_serializer = URLSafeTimedSerializer(settings.session_secret, salt="music-play-oidc")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def dump_state(payload: dict) -> str:
    return _serializer.dumps(payload)


def load_state(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired, Exception):
        return None
    return data if isinstance(data, dict) else None


def peer_ip(request: Any) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return request.client.host if request.client else ""


def parse_networks(raw: str | None) -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            if "/" in text:
                networks.append(ipaddress.ip_network(text, strict=False))
            else:
                networks.append(ipaddress.ip_network(f"{text}/32" if ":" not in text else f"{text}/128", strict=False))
        except ValueError:
            logger.warning("OIDC: ungültiges internes Netz ignoriert: {}", text)
    return networks


def client_is_internal(request: Any, networks_raw: str | None = None) -> bool:
    data = oidc_config.load()
    networks = parse_networks(networks_raw if networks_raw is not None else data.get("internal_networks"))
    if not networks:
        return False
    ip_text = peer_ip(request)
    if not ip_text:
        return False
    try:
        address = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return any(address in network for network in networks)


def redirect_uri_for(request: Any) -> str:
    data = oidc_config.load()
    configured = clean_secret(str(data.get("redirect_uri") or "")).rstrip("/")
    if configured:
        return configured
    if settings.server_url:
        return f"{settings.server_url.rstrip('/')}/auth/oidc/callback"
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/oidc/callback"


def discovery(issuer: str | None = None) -> dict:
    data = oidc_config.load()
    base = clean_secret(issuer or data.get("issuer") or "").rstrip("/")
    if not base:
        raise RuntimeError("OIDC-Issuer fehlt")
    cached = _discovery_cache.get(base)
    now = time.monotonic()
    if cached and now - cached[0] < 3600:
        return dict(cached[1])
    url = f"{base}/.well-known/openid-configuration"
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"OIDC Discovery fehlgeschlagen: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("authorization_endpoint"):
        raise RuntimeError("OIDC Discovery ohne authorization_endpoint")
    _discovery_cache[base] = (now, payload)
    return dict(payload)


def test_connection() -> dict:
    data = oidc_config.load()
    if not data.get("issuer"):
        return {"ok": False, "error": "Issuer fehlt"}
    try:
        meta = discovery(data.get("issuer"))
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "issuer": data.get("issuer"),
        "authorizationEndpoint": meta.get("authorization_endpoint") or "",
        "tokenEndpoint": meta.get("token_endpoint") or "",
        "userinfoEndpoint": meta.get("userinfo_endpoint") or "",
    }


def login_methods(request: Any) -> dict[str, Any]:
    """Which login options the login screen should offer for this client."""

    setup = auth_store.needs_setup()
    login_on = (not setup) and auth_store.login_required()
    data = oidc_config.load()
    oidc_ready = oidc_config.configured()
    mode = oidc_config._normalize_mode(data.get("auth_mode"))
    internal = client_is_internal(request)
    password = False
    oidc = False

    if setup:
        password = True
        oidc = False
    elif not login_on:
        password = False
        oidc = False
    elif mode == "password" or not oidc_ready:
        password = True
        oidc = False
    elif mode == "oidc":
        password = False
        oidc = True
    elif mode == "both":
        password = True
        oidc = True
    elif mode == "internal_both":
        oidc = True
        password = bool(internal)
    else:
        password = True
        oidc = False

    # Always keep password available during setup; if OIDC-only but misconfigured, fall back.
    if login_on and not password and not oidc:
        password = True

    return {
        "passwordLogin": password,
        "oidcLogin": oidc,
        "oidcConfigured": oidc_ready,
        "oidcLabel": data.get("display_name") or "OIDC",
        "oidcStartUrl": "/auth/oidc/start",
        "authMode": mode,
        "internalAccess": internal,
        "needsSetup": setup,
        "loginRequired": login_on,
    }


def password_login_allowed(request: Any) -> bool:
    return bool(login_methods(request).get("passwordLogin"))


def build_authorize_url(request: Any) -> tuple[str, str]:
    if not oidc_config.configured():
        raise RuntimeError("OIDC ist nicht konfiguriert")
    methods = login_methods(request)
    if not methods.get("oidcLogin"):
        raise RuntimeError("OIDC-Login ist für diese Adresse nicht freigeschaltet")

    data = oidc_config.load()
    meta = discovery()
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    redirect_uri = redirect_uri_for(request)
    scopes = data.get("scopes") or "openid profile email"
    params = {
        "response_type": "code",
        "client_id": data.get("client_id") or "",
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{meta['authorization_endpoint']}?{urlencode(params)}"
    cookie_payload = {
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "issuer": data.get("issuer") or "",
    }
    return url, dump_state(cookie_payload)


def _token_request(meta: dict, data: dict, code: str, verifier: str, redirect_uri: str) -> dict:
    token_url = meta.get("token_endpoint")
    if not token_url:
        raise RuntimeError("OIDC ohne token_endpoint")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": data.get("client_id") or "",
        "code_verifier": verifier,
    }
    secret = data.get("client_secret") or ""
    headers = {"Accept": "application/json"}
    auth = None
    if secret:
        # Pocket ID / most providers accept client_secret_post or basic.
        form["client_secret"] = secret
        auth = (data.get("client_id") or "", secret)
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        response = client.post(token_url, data=form, headers=headers, auth=auth)
        if response.status_code >= 400 and auth is not None:
            # Retry without HTTP Basic if the provider rejected it.
            response = client.post(token_url, data=form, headers=headers)
        if response.status_code >= 400:
            detail = response.text[:240]
            raise RuntimeError(f"Token-Austausch fehlgeschlagen ({response.status_code}): {detail}")
        payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("OIDC Token-Antwort ohne access_token")
    return payload


def _userinfo(meta: dict, access_token: str) -> dict:
    endpoint = meta.get("userinfo_endpoint")
    if not endpoint:
        raise RuntimeError("OIDC ohne userinfo_endpoint")
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        response = client.get(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"UserInfo fehlgeschlagen ({response.status_code})")
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("UserInfo ungültig")
    return payload


def _username_from_claims(claims: dict, preferred_claim: str) -> str:
    candidates = [
        claims.get(preferred_claim),
        claims.get("preferred_username"),
        claims.get("nickname"),
        claims.get("name"),
        claims.get("email"),
        claims.get("sub"),
    ]
    for raw in candidates:
        text = clean_secret(str(raw or ""))
        if not text:
            continue
        if "@" in text and preferred_claim != "email":
            text = text.split("@", 1)[0]
        text = text.replace(" ", ".")
        if text:
            return text[:64]
    raise RuntimeError("OIDC lieferte keinen Benutzernamen")


def finish_login(request: Any, *, code: str, state: str, state_cookie: str | None) -> dict:
    saved = load_state(state_cookie)
    if not saved or saved.get("state") != state:
        raise RuntimeError("OIDC-State ungültig oder abgelaufen – bitte erneut anmelden")
    data = oidc_config.load()
    meta = discovery(saved.get("issuer") or data.get("issuer"))
    tokens = _token_request(
        meta,
        data,
        code,
        str(saved.get("verifier") or ""),
        str(saved.get("redirect_uri") or redirect_uri_for(request)),
    )
    claims = _userinfo(meta, str(tokens.get("access_token") or ""))
    username = _username_from_claims(claims, str(data.get("username_claim") or "preferred_username"))
    oidc_sub = clean_secret(str(claims.get("sub") or "")) or username
    email = clean_secret(str(claims.get("email") or ""))
    user = auth_store.ensure_oidc_user(
        username,
        oidc_sub=oidc_sub,
        email=email,
        auto_create=bool(data.get("auto_create_users", True)),
    )
    return {
        "username": user["username"],
        "isAdmin": user.get("role") == "admin",
        "claims": {"sub": oidc_sub, "email": email},
    }
