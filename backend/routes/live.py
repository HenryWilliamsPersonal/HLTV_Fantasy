"""The Live page: current fantasy events as they stand (services/live_state),
and the live stream that tells open apps when an event's pages changed."""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.services import live_pages, live_state

router = APIRouter()
_PING_EVERY = 20  # seconds between keep-alive comments (the tunnel cuts silent responses)


@router.get("/events")
def get_live_events() -> dict:
    return {"events": [live_state.summary(e) for e in live_state.live_events()]}


@router.get("/stream")
async def live_stream(request: Request) -> StreamingResponse:
    """Server-sent events: `hello` with the current version, then a `change`
    per event whose matches were re-read and differ (event_id, version,
    read_at, live_count); a comment every _PING_EVERY seconds keeps the
    connection alive. The app re-reads the event on a change."""

    async def events():
        seen = live_pages.version()
        yield f"event: hello\ndata: {json.dumps({'version': seen})}\n\n"
        quiet = 0
        while not await request.is_disconnected():
            await asyncio.sleep(1.0)
            current = live_pages.version()
            if current != seen:
                for change in live_pages.changes_since(seen):
                    yield f"event: change\ndata: {json.dumps(change)}\n\n"
                seen = current
                quiet = 0
                continue
            quiet += 1
            if quiet >= _PING_EVERY:
                quiet = 0
                yield ": ping\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/events/{event_id}/best-team")
def get_live_best_team(event_id: int) -> dict:
    """The roster that would have scored most with hindsight, with its
    roles and the booster for every match (services/fantasy_field.best_team);
    needs the field to be stored."""
    from backend.services import fantasy_field

    if not fantasy_field.status(int(event_id)).get("teams_stored"):
        raise HTTPException(status_code=404, detail="The field of this event has not been read")
    out = dict(fantasy_field.best_team(int(event_id)))
    out.pop("_key", None)
    return out


@router.get("/events/{event_id}/teams")
def get_live_teams(event_id: int, request: Request, q: str = "", sort: str = "expected_final", desc: int = 1, page: int = 0, size: int = 50) -> dict:
    """Every user team of the event's fantasy league: points so far, the
    expected points still to come and at the end (services/field_projection),
    searchable by team name, sorted and paged. The caller's own team (its
    connected HLTV account's) comes back marked and in `mine`."""
    from backend.data import my_team_cache
    from backend.services import fantasy_field, field_projection, public_access

    if not fantasy_field.status(int(event_id)).get("teams_stored"):
        raise HTTPException(status_code=404, detail="The field of this event has not been read")
    mine = []
    uid = public_access.session_user_id(request)
    key = f"user:{int(uid)}" if uid else None
    if key is None and public_access.is_operator(request):
        from backend.services import my_team

        key = my_team.OPERATOR_KEY
    if key:
        cached = my_team_cache.get(key, int(event_id)) or {}
        tid = (cached.get("team") or {}).get("team_id")
        if tid:
            mine.append(int(tid))
    return field_projection.page(int(event_id), q=q, sort=sort, desc=bool(desc), page_no=page, size=size, mine=mine)


@router.get("/events/{event_id}/leaderboard")
def get_live_leaderboard(event_id: int) -> dict:
    """Every player in the event scored from the matches played so far
    (rating and win points; services/leaderboard)."""
    from backend.services import leaderboard

    if not live_state.live_event(int(event_id), fetch_if_missing=False):
        raise HTTPException(status_code=404, detail="No valuation stored for this event")
    return leaderboard.event_leaderboard(int(event_id))


@router.get("/events/{event_id}")
def get_live_event(event_id: int) -> dict:
    entry = live_state.live_event(int(event_id))
    if not entry:
        raise HTTPException(status_code=404, detail="No valuation stored for this event")
    return {"event": entry}
