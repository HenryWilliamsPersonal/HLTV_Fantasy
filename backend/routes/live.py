"""The Live page: current fantasy events as they stand (services/live_state)."""
from fastapi import APIRouter, HTTPException

from backend.services import live_state

router = APIRouter()


@router.get("/events")
def get_live_events() -> dict:
    return {"events": [live_state.summary(e) for e in live_state.live_events()]}


@router.get("/events/{event_id}")
def get_live_event(event_id: int) -> dict:
    entry = live_state.live_event(int(event_id))
    if not entry:
        raise HTTPException(status_code=404, detail="No valuation stored for this event")
    return {"event": entry}
