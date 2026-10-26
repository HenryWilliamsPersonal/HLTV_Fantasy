"""The My Team page: each app user's own HLTV fantasy team per event
(services/my_team). A signed-in app user is keyed "user:<id>"; the server
operator, who has no app session locally, is "operator". The browser reads
run in the background: a response carries `pending` while one is under way
and the page asks again."""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from backend.services import live_state, my_team, public_access

router = APIRouter()
_COOKIE_ALLOWLIST = ("autologin", "PHPSESSID", "cf_clearance", "_cfuvid", "__cflb", "__cf_bm")


def _user_key(request: Request) -> str:
    uid = public_access.session_user_id(request)
    if uid:
        return f"user:{int(uid)}"
    if public_access.is_operator(request):
        return my_team.OPERATOR_KEY
    raise HTTPException(status_code=401, detail="Sign in to use My Team.")


def _cookies_from(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cookies from the app's hand-over ({cookies: [...]}) or a pasted
    autologin value ({autologin: "...", phpsessid: "..."})."""
    raw = list((payload or {}).get("cookies") or [])
    if (payload or {}).get("autologin"):
        raw.append({"name": "autologin", "value": str(payload["autologin"]).strip()})
    if (payload or {}).get("phpsessid"):
        raw.append({"name": "PHPSESSID", "value": str(payload["phpsessid"]).strip()})
    cookies: List[Dict[str, Any]] = []
    for item in raw:
        item = item or {}
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name not in _COOKIE_ALLOWLIST or not value:
            continue
        cookie: Dict[str, Any] = {"name": name, "value": value, "domain": str(item.get("domain") or ".hltv.org"), "path": str(item.get("path") or "/")}
        try:
            if item.get("expiry"):
                cookie["expiry"] = int(float(item["expiry"]))
        except (TypeError, ValueError):
            pass
        if item.get("secure"):
            cookie["secure"] = True
        if item.get("httpOnly"):
            cookie["httpOnly"] = True
        cookies.append(cookie)
    if "autologin" not in {c["name"] for c in cookies}:
        raise HTTPException(status_code=400, detail="HLTV's autologin cookie is required")
    return cookies


@router.get("/session")
def get_session(request: Request) -> dict:
    return my_team.status_for(_user_key(request))


@router.post("/session")
def connect_session(request: Request, payload: Dict[str, Any]) -> dict:
    """Connect the user's HLTV account: the cookies are stored at once and
    verified in the background (the status says `verifying` meanwhile)."""
    key = _user_key(request)
    cookies = _cookies_from(payload)
    return {"status": "ok", **my_team.connect_account(key, cookies)}


@router.delete("/session")
def disconnect_session(request: Request) -> dict:
    key = _user_key(request)
    my_team.forget_account(key)
    return {"status": "ok", "connected": False}


@router.get("/events")
def list_my_team_events(request: Request) -> dict:
    """The current events (live, upcoming, recent) in which the user's
    account has a team, with the account's connection state. `pending`
    while the login is being verified or the events are being read."""
    key = _user_key(request)
    status = my_team.status_for(key)
    if not status.get("connected"):
        return {**status, "events": [], "pending": False}
    if status.get("verifying"):
        return {**status, "events": [], "pending": True}
    current = live_state.live_events()
    job = my_team.events_for(key, [int(ev["event_id"]) for ev in current])
    if job.get("error") and job.get("value") is None:
        return {**status, "connected": False, "error": job["error"], "events": [], "pending": False}
    teams_by_event = job.get("value") or {}
    out = []
    for ev in current:
        teams = teams_by_event.get(int(ev["event_id"])) or []
        if teams:
            out.append({**live_state.summary(ev), "team_name": teams[0]["team_name"], "league_name": teams[0]["league_name"]})
    return {**status, "events": out, "pending": bool(job.get("pending"))}


@router.get("/{event_id}")
def get_my_team(request: Request, event_id: int, refresh: bool = False) -> dict:
    """The report, or {pending: true} while it is being read (a stale report
    comes with pending: true while a refresh is under way)."""
    key = _user_key(request)
    job = my_team.report_for(int(event_id), key, refresh=refresh)
    if job.get("value"):
        return {**job["value"], "pending": bool(job.get("pending"))}
    if job.get("pending"):
        return {"pending": True}
    if job.get("error"):
        raise HTTPException(status_code=409, detail=str(job["error"]))
    raise HTTPException(status_code=404, detail="No team of yours in this event")
