"""Accounts: sign in with Google, sessions for the distributed app.

The app never sees Google tokens and the client secret never leaves this
server:

1. The app makes a random `state` and `verifier`, sends
   challenge = sha256(verifier) with the state to /auth/google/start in the
   user's browser.
2. This server redirects to Google; Google sends the code back to
   /auth/google/callback; the server exchanges it, checks the ID token,
   upserts the user, creates a session and parks its token under the state.
3. The app polls POST /auth/poll {state, verifier}; on a matching verifier
   the token is handed over once and the parking entry deleted.
4. The app sends Authorization: Bearer <token>; the public-access middleware
   requires it on every public data route (see public_access.py).

Google OAuth client (Web application, redirect URI
<redirect_base>/auth/google/callback): .runtime/google-oauth.json
    {"client_id": "...", "client_secret": "...", "redirect_base": "https://api.csfantasy.co.uk"}
or HLTV_GOOGLE_CLIENT_ID / HLTV_GOOGLE_CLIENT_SECRET / HLTV_PUBLIC_API_BASE.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.data import auth_db
from backend.data.db import ROOT_DIR

logger = logging.getLogger(__name__)
router = APIRouter()

_CONFIG_PATH = Path(ROOT_DIR) / ".runtime" / "google-oauth.json"
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


_config_cache: Dict[str, Any] = {"mtime": None, "raw": {}}


def _config_file() -> Dict[str, Any]:
    """The JSON config, re-read only when the file changes (checked per call)."""
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        _config_cache.update(mtime=None, raw={})
        return {}
    if _config_cache["mtime"] != mtime:
        try:
            _config_cache["raw"] = json.loads(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - malformed file = not configured
            _config_cache["raw"] = {}
        _config_cache["mtime"] = mtime
    return dict(_config_cache["raw"])


def google_config() -> Dict[str, str]:
    cfg: Dict[str, str] = {"client_id": "", "client_secret": "", "redirect_base": "https://api.csfantasy.co.uk"}
    raw = _config_file()
    for key in cfg:
        if raw.get(key):
            cfg[key] = str(raw[key]).strip()
    cfg["client_id"] = os.getenv("HLTV_GOOGLE_CLIENT_ID") or cfg["client_id"]
    cfg["client_secret"] = os.getenv("HLTV_GOOGLE_CLIENT_SECRET") or cfg["client_secret"]
    cfg["redirect_base"] = (os.getenv("HLTV_PUBLIC_API_BASE") or cfg["redirect_base"]).rstrip("/")
    return cfg


def google_configured() -> bool:
    cfg = google_config()
    return bool(cfg["client_id"] and cfg["client_secret"])


def sign_in_required() -> bool:
    """Sessions are enforced on public data routes only once Google sign-in
    is configured, so switching accounts on is: add the credentials, ship
    the app version that can sign in. Nothing breaks in between."""
    return google_configured()


def _redirect_uri() -> str:
    return google_config()["redirect_base"] + "/auth/google/callback"


def _bearer(request: Request) -> Optional[str]:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _page(title: str, body: str, ok: bool = True) -> HTMLResponse:
    accent = "#ff6b1a" if ok else "#ff5c5c"
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - CS Fantasy Toolkit</title>
<style>body{{margin:0;background:#0f1216;color:#e9edf3;font:16px/1.5 "Segoe UI",system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:20px;box-sizing:border-box}}
.card{{max-width:480px;background:#171c24;border:1px solid #2c3441;border-top:3px solid {accent};padding:26px 30px}}
.k{{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:{accent};font-weight:700;margin:0 0 8px}}
h1{{font-size:26px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.02em}}p{{margin:0 0 8px;color:#c6d0dc}}</style></head>
<body><div class="card"><p class="k">CS Fantasy Toolkit</p><h1>{title}</h1>{body}</div></body></html>"""
    return HTMLResponse(html, status_code=200 if ok else 400)


# ---- Google calls (stdlib only) --------------------------------------------------
def _post_form(url: str, data: Dict[str, str], timeout: float = 20) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _get_json(url: str, timeout: float = 20) -> Dict[str, Any]:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def exchange_code(code: str) -> Dict[str, Any]:
    cfg = google_config()
    return _post_form(
        _GOOGLE_TOKEN,
        {
            "code": code,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
    )


def verify_id_token(id_token: str) -> Dict[str, Any]:
    """Claims of a Google ID token, checked by Google's tokeninfo endpoint
    plus our own audience/issuer/expiry checks. Raises ValueError."""
    claims = _get_json(_GOOGLE_TOKENINFO + "?" + urllib.parse.urlencode({"id_token": id_token}))
    cfg = google_config()
    if claims.get("aud") != cfg["client_id"]:
        raise ValueError("token audience mismatch")
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("token issuer mismatch")
    if float(claims.get("exp") or 0) < time.time():
        raise ValueError("token expired")
    if not claims.get("sub"):
        raise ValueError("token has no subject")
    if str(claims.get("email_verified")).lower() != "true":
        raise ValueError("Google account email is not verified")
    return claims


# ---- endpoints ----------------------------------------------------------------------
@router.get("/google/start")
def google_start(state: str, challenge: str):
    """Opened in the user's browser by the app. Parks the handshake and sends
    the browser to Google's consent page."""
    if not google_configured():
        return _page("Sign-in not set up", "<p>This server has no Google sign-in configured yet.</p>", ok=False)
    if not _TOKEN_RE.match(state or "") or not _TOKEN_RE.match(challenge or ""):
        raise HTTPException(status_code=400, detail="bad state or challenge")
    auth_db.create_pending(state, challenge)
    cfg = google_config()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "access_type": "online",
    }
    return RedirectResponse(_GOOGLE_AUTH + "?" + urllib.parse.urlencode(params), status_code=302)


@router.get("/google/callback")
def google_callback(state: str = "", code: str = "", error: str = ""):
    if error:
        return _page("Sign-in cancelled", f"<p>Google reported: {urllib.parse.quote(error)}. You can close this tab and try again in the app.</p>", ok=False)
    pending = auth_db.get_pending(state) if state else None
    if not pending:
        return _page("Sign-in expired", "<p>This sign-in link is no longer valid. Go back to the app and press Continue with Google again.</p>", ok=False)
    try:
        tokens = exchange_code(code)
        claims = verify_id_token(str(tokens.get("id_token") or ""))
    except urllib.error.HTTPError as exc:
        logger.warning("Google token exchange failed: %s", exc)
        return _page("Sign-in failed", "<p>Google did not accept the sign-in. Go back to the app and try again.</p>", ok=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google sign-in rejected: %s", exc)
        return _page("Sign-in failed", "<p>The sign-in could not be verified. Go back to the app and try again.</p>", ok=False)
    user = auth_db.upsert_user(str(claims["sub"]), claims.get("email"), claims.get("name"), claims.get("picture"))
    token = auth_db.create_session(int(user["id"]), client="app")
    if not auth_db.complete_pending(state, token, int(user["id"])):
        auth_db.revoke_session(token)
        return _page("Sign-in expired", "<p>This sign-in link was already used. Go back to the app and try again.</p>", ok=False)
    name = user.get("name") or user.get("email") or "you"
    return _page("You're signed in", f"<p>Signed in as <b>{name}</b>.</p><p>You can close this tab and return to CS Fantasy Toolkit.</p>")


@router.post("/poll")
def auth_poll(payload: dict | None = None) -> dict:
    body = payload or {}
    state = str(body.get("state") or "")
    verifier = str(body.get("verifier") or "")
    if not _TOKEN_RE.match(state) or not _TOKEN_RE.match(verifier):
        raise HTTPException(status_code=400, detail="bad state or verifier")
    pending = auth_db.get_pending(state)
    if not pending:
        return {"status": "unknown"}
    if not pending.get("token"):
        return {"status": "pending"}
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("ascii").rstrip("=")
    if digest != str(pending.get("challenge")).rstrip("="):
        raise HTTPException(status_code=403, detail="verifier mismatch")
    taken = auth_db.take_pending(state)
    if not taken:
        return {"status": "unknown"}
    user = auth_db.get_user(int(taken["user_id"]))
    return {"status": "ok", "token": taken["token"], "user": auth_db.public_user(user)}


@router.get("/me")
def auth_me(request: Request) -> dict:
    user_id = auth_db.user_id_for_token(_bearer(request))
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to use CS Fantasy Toolkit.")
    return {"user": auth_db.public_user(auth_db.get_user(user_id))}


@router.post("/signout")
def auth_signout(request: Request) -> dict:
    return {"status": "ok", "revoked": auth_db.revoke_session(_bearer(request))}
