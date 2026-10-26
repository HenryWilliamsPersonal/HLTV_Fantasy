"""The My Team page: the operator's own HLTV fantasy team per event
(services/my_team), operator-only since it reads the logged-in account."""
from fastapi import APIRouter, HTTPException

from backend.services import live_state, my_team

router = APIRouter()


@router.get("/events")
def list_my_team_events() -> dict:
    """The current events (live, upcoming, recent) in which the account has a team."""
    out = []
    for ev in live_state.live_events():
        try:
            teams = my_team.my_teams(int(ev["event_id"]), max_age=3600.0)
        except Exception:  # noqa: BLE001
            teams = []
        if teams:
            out.append({**live_state.summary(ev), "team_name": teams[0]["team_name"], "league_name": teams[0]["league_name"]})
    return {"events": out}


@router.get("/{event_id}")
def get_my_team(event_id: int, refresh: bool = False) -> dict:
    report = my_team.team_report(int(event_id), max_age=0.0 if refresh else my_team.REFRESH)
    if not report:
        raise HTTPException(status_code=404, detail="No team of yours in this event")
    return report
