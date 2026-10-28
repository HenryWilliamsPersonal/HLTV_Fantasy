"""The My Team page: each app user's own HLTV fantasy team per event
(services/my_team). A signed-in app user is keyed "user:<id>"; the server
operator, who has no app session locally, is "operator". The browser reads
run in the background: a response carries `pending` while one is under way
and the page asks again."""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from backend.services import my_team, public_access

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


@router.get("/season")
def get_season(request: Request, refresh: bool = False) -> dict:
    """The HLTV fantasy season as the user's account sees it (services/
    fantasy_season): the stored copy at once, re-read in the background
    when stale or on refresh (`pending` meanwhile)."""
    from backend.services import fantasy_season

    key = _connected_key(request)
    job = fantasy_season.season_for(key, refresh=refresh)
    out: Dict[str, Any] = dict(job.get("value") or {})
    out.update({"pending": bool(job.get("pending")), "error": job.get("error")})
    return out


@router.get("/session")
def get_session(request: Request) -> dict:
    from backend.data import my_team_settings

    key = _user_key(request)
    out = dict(my_team.status_for(key))
    out["auto_all"] = my_team_settings.auto_all_enabled(key)
    return out


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


@router.post("/settings")
def set_my_team_settings(request: Request, payload: Dict[str, Any]) -> dict:
    """The user's booster settings. auto_all: the app sets the advised
    boosters on HLTV in every live or upcoming event the user has a team
    in, without switching it on per event; on → applied at once in a thread."""
    import threading

    from backend.data import my_team_settings
    from backend.services import booster_advisor

    key = _user_key(request)
    if "auto_all" in (payload or {}):
        enabled = bool(payload["auto_all"])
        my_team_settings.set_auto_all(key, enabled)
        if enabled:
            threading.Thread(target=lambda: booster_advisor.apply_everywhere(key), name=f"booster-auto-all-{key}", daemon=True).start()
    return {"status": "ok", "auto_all": my_team_settings.auto_all_enabled(key)}


def _connected_key(request: Request) -> str:
    key = _user_key(request)
    status = my_team.status_for(key)
    if not status.get("connected"):
        raise HTTPException(status_code=409, detail=str(status.get("error") or "No HLTV login connected"))
    return key


@router.get("/{event_id}/boosters")
def get_boosters(request: Request, event_id: int, refresh: bool = False) -> dict:
    """The booster advice for the user's team in the event (background read,
    `pending` meanwhile), with the auto-mode flag and the last actions."""
    from backend.data import my_team_settings
    from backend.services import booster_advisor

    key = _connected_key(request)
    job = booster_advisor.advice_for(int(event_id), key, refresh=refresh)
    out: Dict[str, Any] = dict(job.get("value") or {})
    auto_all = my_team_settings.auto_all_enabled(key)
    out.update({
        "pending": bool(job.get("pending")),
        "error": job.get("error"),
        "auto": auto_all or my_team_settings.auto_enabled(key, int(event_id)),
        "auto_all": auto_all,
        "actions": my_team_settings.actions(key, int(event_id)),
    })
    return out


@router.post("/{event_id}/boosters/apply")
def apply_boosters(request: Request, event_id: int, payload: Dict[str, Any]) -> dict:
    """Set boosters on HLTV: {assignments: [{player_id, match_id,
    booster_index}]} or {all: true} for every suggestion that differs."""
    from backend.services import booster_advisor

    key = _connected_key(request)
    try:
        if (payload or {}).get("all"):
            results = booster_advisor.apply_suggestions(int(event_id), key)
        else:
            assignments = [a for a in ((payload or {}).get("assignments") or []) if a.get("player_id") is not None and a.get("match_id") is not None]
            if not assignments:
                raise HTTPException(status_code=400, detail="Nothing to assign")
            results = booster_advisor.apply(int(event_id), key, assignments)
    except my_team.PersonalSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not set the boosters: {exc}") from exc
    with my_team._LOCK:
        my_team._RESULTS.pop(my_team._job_key("boosters", key, int(event_id)), None)
    return {"status": "ok", "results": results}


@router.post("/{event_id}/boosters/auto")
def set_auto_boosters(request: Request, event_id: int, payload: Dict[str, Any]) -> dict:
    """Switch auto mode: the app sets the advised boosters on HLTV for the
    rest of the event, starting now."""
    import threading

    from backend.data import my_team_settings
    from backend.services import booster_advisor

    key = _connected_key(request)
    enabled = bool((payload or {}).get("enabled"))
    my_team_settings.set_auto(key, int(event_id), enabled)
    if enabled:
        threading.Thread(target=lambda: booster_advisor.apply_suggestions(int(event_id), key, trigger="auto"), name=f"booster-auto-{key}", daemon=True).start()
    return {"status": "ok", "auto": enabled}


@router.get("/{event_id}")
def get_my_team(request: Request, event_id: int, refresh: bool = False) -> dict:
    """The user's team in the event, or {pending: true} while it is being
    read (a stale report comes with pending: true while a refresh is under
    way). 409 without a usable HLTV login."""
    key = _user_key(request)
    status = my_team.status_for(key)
    if not status.get("connected"):
        raise HTTPException(status_code=409, detail=str(status.get("error") or "No HLTV login connected"))
    if status.get("verifying"):
        return {"pending": True}
    job = my_team.report_for(int(event_id), key, refresh=refresh)
    if job.get("value"):
        return {**job["value"], "pending": bool(job.get("pending"))}
    if job.get("pending"):
        return {"pending": True}
    if job.get("error"):
        raise HTTPException(status_code=409, detail=str(job["error"]))
    raise HTTPException(status_code=404, detail="No team of yours in this event")
