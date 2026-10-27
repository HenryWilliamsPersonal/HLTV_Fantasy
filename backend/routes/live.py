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
