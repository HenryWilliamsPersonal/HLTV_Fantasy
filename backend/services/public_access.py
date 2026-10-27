"""Public / operator split for a hosted backend.

The distributed app talks to a backend running on the operator's machine. Two
kinds of caller reach it:

* the operator — requests from this machine (loopback, not through the
  tunnel) or carrying the admin token (`X-Admin-Token`, see admin_token()) —
  who may do everything: imports, scheduler, runs, restarts;
* everyone else — treated as public — who only reaches the read-only surface
  the app needs: stored runs, valuations, Top 5 queries, player and team data,
  images. Anything that scrapes HLTV or rewrites stored state answers 403.

The public app always sends `X-Public-Client: 1`, so it is handled as public
even when it runs on the operator's own machine. Public query endpoints are
rate limited per client and capped in concurrency, since each one costs a
second or two of every core.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_RUNTIME_DIR = Path(__file__).resolve().parents[2] / ".runtime"
_TOKEN_FILE = _RUNTIME_DIR / "admin-token.txt"
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip")

# (method, path pattern) the public app may call. Paths are matched whole,
# without the query string.
# Everything the distributed app reads. Read-only GET data under the prefixes
# in _PUBLIC_GET_PREFIX (players, teams, events, assets) is public unless it is
# in PUBLIC_GET_DENY; the routes below live outside those prefixes.
PUBLIC_ROUTES: List[Tuple[str, str]] = [
    ("GET", r"/health"),
    ("GET", r"/public/config"),
    ("GET", r"/auth/.*"),
    ("POST", r"/auth/.*"),
    ("GET", r"/playoff/latest"),
    ("GET", r"/playoff/best-team/from-latest/latest"),
    ("GET", r"/playoff/best-team/bracket-from-latest/latest"),
    ("POST", r"/playoff/best-team/from-latest/query"),
    ("POST", r"/playoff/best-team/from-latest/completed-query"),
    ("POST", r"/playoff/outcome-player-detail"),
    ("POST", r"/playoff/winning-player-detail"),
    ("GET", r"/groups/latest"),
    ("GET", r"/groups/best-team/latest"),
    ("GET", r"/groups/event-autofill"),
    ("POST", r"/groups/best-team/query"),
    ("POST", r"/groups/best-team/completed-query"),
    ("POST", r"/groups/swiss/pairings"),
    ("GET", r"/live/events"),
    ("GET", r"/live/events/\d+"),
    ("GET", r"/live/stream"),
    # My Team: each signed-in user's own HLTV account (routes require a user)
    ("GET", r"/my-team(/.*)?"),
    ("POST", r"/my-team/session"),
    ("POST", r"/my-team/\d+/boosters/(apply|auto)"),
    ("DELETE", r"/my-team/session"),
]

# Read-only data the Database tab, the player/team modals and the Tournament
# page fetch: public by default so a new read-only endpoint does not silently
# 403 in the distributed app (the recent-form modal did exactly that)...
_PUBLIC_GET_PREFIX = re.compile(r"^/(players|teams|events|assets)(/|$)")
# ...except GETs that fetch from HLTV live, compute for a long time, or report
# operator import jobs. POST/DELETE, /admin, /schedule and the run starters are
# never public.
PUBLIC_GET_DENY: List[str] = [
    r"/events/hltv-recent-results",  # live HLTV fetch
    r"/events/hltv-results/match-details",  # live HLTV fetch + DB write (use /stored)
    r"/teams/(hltv|vrs)-ranking/by-date",  # live HLTV fetch
    r"/events/page-snapshots(/.*)?",
    r"/events/\d+/swiss-context",
    r".*/job/.*",  # operator job status
]
_PUBLIC_GET_DENY_COMPILED = [re.compile(f"^{p}$") for p in PUBLIC_GET_DENY]
_PUBLIC_COMPILED = [(m, re.compile(f"^{p}$")) for m, p in PUBLIC_ROUTES]

# Endpoints that compute on request: per-client rate limit + global concurrency cap.
_HEAVY = re.compile(r"^/(playoff/best-team/from-latest/(query|completed-query)|playoff/(outcome|winning)-player-detail|groups/best-team/(query|completed-query))$")
_RATE_WINDOW_SECONDS = 60.0
_RATE_LIMIT = int(os.getenv("HLTV_PUBLIC_RATE_LIMIT", "60"))
_MAX_CONCURRENT = int(os.getenv("HLTV_PUBLIC_MAX_CONCURRENT", "3"))
# Light routes (lists, modals, stored brackets): generous per-client cap so a
# flood is still bounded. Assets are exempt (Cloudflare caches them anyway).
_LIGHT_RATE_LIMIT = int(os.getenv("HLTV_PUBLIC_LIGHT_RATE_LIMIT", "600"))

_rate_lock = threading.Lock()
_rate_hits: Dict[str, List[float]] = {}
_inflight = threading.BoundedSemaphore(_MAX_CONCURRENT)


def admin_token() -> str:
    """The operator token: HLTV_ADMIN_TOKEN, else .runtime/admin-token.txt
    (generated once). Send it as X-Admin-Token to use the operator surface
    from another machine."""
    env = (os.getenv("HLTV_ADMIN_TOKEN") or "").strip()
    if env:
        return env
    try:
        stored = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    except Exception:
        pass
    token = secrets.token_urlsafe(32)
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(token, encoding="utf-8")
    except Exception:
        pass
    return token


_OVERRIDES_FILE = _TOKEN_FILE.parent / "public-config.json"
_overrides_cache: dict = {"mtime": None, "data": {}}


def public_config_overrides() -> dict:
    """Operator overrides for /public/config (min_client_version, message),
    re-read whenever .runtime/public-config.json changes. Only those two keys
    are honoured; a malformed file is ignored."""
    try:
        mtime = _OVERRIDES_FILE.stat().st_mtime
    except OSError:
        _overrides_cache.update(mtime=None, data={})
        return {}
    if _overrides_cache["mtime"] != mtime:
        try:
            import json

            raw = json.loads(_OVERRIDES_FILE.read_text(encoding="utf-8"))
            data = {k: str(raw[k]) for k in ("min_client_version", "message") if raw.get(k) is not None}
        except Exception:
            data = {}
        _overrides_cache.update(mtime=mtime, data=data)
    return dict(_overrides_cache["data"])


def client_key(request) -> str:
    for header in _PROXY_HEADERS:
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_operator(request) -> bool:
    token = request.headers.get("x-admin-token")
    if token and secrets.compare_digest(token, admin_token()):
        return True
    # A signed-in admin account is the operator wherever it connects from.
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        from backend.data import auth_db

        info = auth_db.session_info(header[7:].strip())
        if info and info[1]:
            request.state.user_id = info[0]
            return True
    if request.headers.get("x-public-client"):
        return False
    if any(request.headers.get(h) for h in _PROXY_HEADERS):
        return False  # came through the tunnel / a proxy
    host = request.client.host if request.client else ""
    return host in _LOCAL_HOSTS


def is_public_route(method: str, path: str) -> bool:
    if method == "GET" and _PUBLIC_GET_PREFIX.match(path):
        return not any(rx.match(path) for rx in _PUBLIC_GET_DENY_COMPILED)
    return any(m == method and rx.match(path) for m, rx in _PUBLIC_COMPILED)


def _rate_limited(key: str, limit: int = _RATE_LIMIT) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < _RATE_WINDOW_SECONDS]
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return True
        hits.append(now)
        _rate_hits[key] = hits
        if len(_rate_hits) > 5000:  # forget idle clients
            for k in [k for k, v in _rate_hits.items() if not v or now - v[-1] > _RATE_WINDOW_SECONDS]:
                _rate_hits.pop(k, None)
    return False


# Public routes a signed-out app may still call: liveness, the server config
# (which says sign-in is required), the sign-in flow itself, and images
# (<img> tags cannot send a bearer header).
_AUTH_EXEMPT = re.compile(r"^/(health|public/config|auth(/.*)?|assets/.*)$")


def session_user_id(request) -> Optional[int]:
    """The signed-in user behind a public request's bearer token, else None."""
    from backend.data import auth_db

    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    return auth_db.user_id_for_token(token)


class PublicAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS" or is_operator(request):
            return await call_next(request)
        path = request.scope["path"]  # routed path, never rebuilt from the Host header
        if not is_public_route(request.method, path):
            return JSONResponse({"detail": "This action is only available to the server operator."}, status_code=403)
        from backend.routes.auth import sign_in_required

        if sign_in_required() and not _AUTH_EXEMPT.match(path):
            user_id = session_user_id(request)
            if not user_id:
                return JSONResponse({"detail": "Sign in to use CS Fantasy Toolkit."}, status_code=401)
            request.state.user_id = user_id
        if _HEAVY.match(path):
            if _rate_limited("heavy:" + client_key(request)):
                return JSONResponse({"detail": "Too many requests. Please wait a moment."}, status_code=429)
            if not _inflight.acquire(blocking=False):
                return JSONResponse({"detail": "The server is busy. Please try again in a few seconds."}, status_code=503)
            try:
                return await call_next(request)
            finally:
                _inflight.release()
        if not path.startswith("/assets/") and _rate_limited("light:" + client_key(request), _LIGHT_RATE_LIMIT):
            return JSONResponse({"detail": "Too many requests. Please wait a moment."}, status_code=429)
        return await call_next(request)
