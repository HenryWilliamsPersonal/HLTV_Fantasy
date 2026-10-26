"""The My Team page: the personal HLTV account's fantasy team per event
(services/my_team), operator-only."""
from fastapi import APIRouter, HTTPException

from backend.services import live_state, my_team

router = APIRouter()


@router.get("/events")
def list_my_team_events() -> dict:
    """The current events (live, upcoming, recent) in which the personal
    account has a team, with the account's connection state."""
    status = my_team.personal_status()
    if not status.get("connected"):
        return {**status, "events": []}
    current = live_state.live_events()
    try:
        teams_by_event = my_team.my_teams_for([int(ev["event_id"]) for ev in current], max_age=3600.0)
    except my_team.PersonalSessionError as exc:
        return {**status, "connected": False, "error": str(exc), "events": []}
    out = []
    for ev in current:
        teams = teams_by_event.get(int(ev["event_id"])) or []
        if teams:
            out.append({**live_state.summary(ev), "team_name": teams[0]["team_name"], "league_name": teams[0]["league_name"]})
    return {**status, "events": out}


@router.get("/{event_id}")
def get_my_team(event_id: int, refresh: bool = False) -> dict:
    try:
        report = my_team.team_report(int(event_id), max_age=0.0 if refresh else my_team.REFRESH)
    except my_team.PersonalSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not report:
        raise HTTPException(status_code=404, detail="No team of yours in this event")
    return report
