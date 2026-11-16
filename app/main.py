from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import session as web_session
from app.api.web import alias_router, router as web_router
from app.core import auth_store
from app.core.icons import ensure_icons
from app.core.logging import logger
from app.core.settings import APP_NAME, APP_VERSION, settings
from app.db.database import init_db
from app.navidrome import bridge as navidrome
from app.navidrome import config as navidrome_config
from app.navidrome import context as nd_context
from app.subsonic import media
from app.subsonic.router import router as subsonic_router

STATIC_DIR = "app/static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    ensure_icons()
    auth_store.apply_env_overrides()
    logger.info(
        "{} {} gestartet - Benutzer '{}', Streaming '{}', Passwort {}, Navidrome {}",
        APP_NAME,
        APP_VERSION,
        auth_store.username(),
        settings.stream_mode,
        "Proxy" if auth_store.allows_anonymous() else ("gesetzt" if auth_store.password_required() else "offen"),
        "verbunden" if navidrome_config.configured() else "aus",
    )
    token = nd_context.bind(auth_store.navidrome_credentials(auth_store.username()))
    try:
        navidrome.warm_index()
    finally:
        nd_context.reset(token)
    yield
    await media.aclose()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def attach_session(request: Request, call_next):
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto in {"http", "https"}:
        request.scope["scheme"] = proto
    request.state.session_user = auth_store.resolve_user(web_session.read(request))
    token = nd_context.bind(auth_store.navidrome_credentials(request.state.session_user))
    try:
        response = await call_next(request)
    finally:
        nd_context.reset(token)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


app.include_router(subsonic_router)
app.include_router(alias_router)
app.include_router(web_router)


def _render_app(request: Request):
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "version": APP_VERSION,
            "authenticated": False,
            "password_required": auth_store.password_required(),
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )
    user = getattr(request.state, "session_user", None)
    if user:
        web_session.apply_cookie(response, web_session.issue(user), request)
    return response


@app.get("/", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
async def index(request: Request):
    return _render_app(request)


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(f"{STATIC_DIR}/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    # Must be served from the root so its scope covers the whole app.
    return FileResponse(
        f"{STATIC_DIR}/sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
async def apple_icon():
    return FileResponse(f"{STATIC_DIR}/icons/icon-180.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(f"{STATIC_DIR}/icons/icon.svg", media_type="image/svg+xml")


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/version")
async def version():
    return {"name": APP_NAME, "version": APP_VERSION}
