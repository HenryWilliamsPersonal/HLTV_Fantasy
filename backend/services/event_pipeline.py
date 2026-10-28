"""One lifecycle for every tournament format: the event behaviour contract.

Every fantasy event, whatever its format, is handled the same way:

1. Imported: baked straight away. Inputs come from the event page (group draw
   or playoff bracket), every outcome is enumerated, the roster combinations
   are stored and the Top 5 caches warmed. If the draw or bracket is not
   published yet the event is "pending" and the nightly run retries it.
2. Until its first match: refreshed by the nightly run with that night's
   ratings, rankings, roles and boosters.
3. From its first match: frozen. Nothing automatic touches it again.
   An event is ACTIVE until it has finished (its last day, plus a day's
   grace); every active event gets this treatment, finished ones are archive.
4. Stored per event. Another event's run never replaces it.
5. The operator's Run buttons are manual overrides, allowed at any time.
6. The public app reads exactly the stored run and can view any imported event.

Formats plug in through FORMATS below. A format without an automatic baker is
reported as "manual" (never silently skipped) until it is brought in.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple

from backend.services import event_rosters


def _groups_hooks() -> Dict[str, Any]:
    from backend.routes import groups

    return {"bake": groups.bake_event_valuations, "exists": groups.has_baked_run}


def _bounty_hooks() -> Dict[str, Any]:
    from backend.routes import playoff

    return {"bake": None, "exists": lambda eid: playoff._STATE_SETS["bounty"]["playoff"].exists(eid)}


# kind (as detected by events._detect_event_tournament_kind_cached, or the
# operator's override) -> hooks factory. Factories import lazily: the routers
# import this module's neighbours at load time.
FORMATS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "groups": _groups_hooks,
    "playoff": _groups_hooks,  # brackets are a format of the groups engine too (services/paths_engine)
    "double_elim": _groups_hooks,
    "swiss": _groups_hooks,  # the Swiss stage is a group format of the groups engine (services/swiss_engine)
    "bounty": _bounty_hooks,
}


def detect_kind(event_id: int) -> Tuple[Optional[str], Dict[str, Any]]:
    """(kind, detection dict) for an event; kind honours the operator override."""
    from backend.data.event_db import get_event_detail, get_event_tournament_kind
    from backend.routes import events as events_routes

    event = get_event_detail(int(event_id))
    if not event:
        return None, {}
    try:
        detected = events_routes._detect_event_tournament_kind_cached(event) or {}
    except Exception:  # noqa: BLE001
        detected = {}
    return (get_event_tournament_kind(int(event_id)) or detected.get("kind")), detected


def _hooks_for(kind: Optional[str]) -> Optional[Dict[str, Any]]:
    factory = FORMATS.get(str(kind or ""))
    return factory() if factory else None


def has_stored_run(event_id: int) -> bool:
    kind, _ = detect_kind(event_id)
    hooks = _hooks_for(kind)
    return bool(hooks and hooks["exists"](int(event_id)))


def has_started(event_id: int) -> Optional[bool]:
    """True/False from the event page's start stamp; None when unknown."""
    _, detected = detect_kind(event_id)
    start = detected.get("start_at")
    if not start:
        return None
    return time.time() >= float(start)


# The page's end stamp is the last day's start; matches run into that night.
FINISH_GRACE_SECONDS = 24 * 3600
# Without an end stamp, an event that started this long ago is treated as over.
ASSUMED_LENGTH_SECONDS = 14 * 24 * 3600


def _finished(detected: Dict[str, Any], now: Optional[float] = None) -> Optional[bool]:
    now = now or time.time()
    end = detected.get("end_at")
    if end:
        return now > float(end) + FINISH_GRACE_SECONDS
    start = detected.get("start_at")
    if start:
        return now > float(start) + ASSUMED_LENGTH_SECONDS
    return None


def live_events() -> list:
    """Ids of every event that has not finished (unknown dates count as
    live), soonest start first. These are the events the nightly run tends."""
    from backend.data.event_db import list_events

    live = []
    for row in list_events():
        eid = int(row.get("event_id") or 0)
        if not eid:
            continue
        _, detected = detect_kind(eid)
        if _finished(detected) is True:
            continue
        live.append((float(detected.get("start_at") or 0), eid))
    return [eid for _, eid in sorted(live)]


def refresh_live_events(trigger: str) -> list:
    """The contract applied to every live event: bake what is missing (retry),
    refresh with tonight's inputs what has not started, leave started events
    alone. Returns one outcome dict per event."""
    outcomes = []
    for eid in live_events():
        baked = has_stored_run(eid)
        started = has_started(eid)
        if baked and started is not False:
            outcomes.append({"status": "frozen", "event_id": eid, "reason": "started; left alone"})
            continue
        if baked:
            outcomes.append(bake_event(eid, trigger=trigger, only_if_missing=False, refresh_inputs=True))
        else:
            outcomes.append(bake_event(eid, trigger=trigger, only_if_missing=True))
    return outcomes


def bake_event(event_id: int, trigger: str = "import", only_if_missing: bool = False, refresh_inputs: bool = False) -> Dict[str, Any]:
    """Run the event's format baker. Never raises; returns a status dict the
    scheduler records ("ok", "exists", "skipped", "pending", "manual",
    "unsupported", "error")."""
    event_id = int(event_id)
    kind, _ = detect_kind(event_id)
    hooks = _hooks_for(kind)
    if not hooks:
        return {"status": "unsupported", "event_id": event_id, "kind": kind, "reason": f"no pipeline for format {kind!r}"}
    if hooks["bake"] is None:
        return {
            "status": "manual",
            "event_id": event_id,
            "kind": kind,
            "reason": f"{kind} events are not automated yet; run them from the operator app",
        }
    try:
        with event_rosters.for_event(event_id):
            return hooks["bake"](event_id, trigger=trigger, only_if_missing=only_if_missing, refresh_inputs=refresh_inputs)
    except Exception as exc:  # noqa: BLE001 - the scheduler must always get a status
        return {"status": "error", "event_id": event_id, "kind": kind, "reason": str(exc)[:300]}


def event_status(event_id: int) -> Dict[str, Any]:
    """Where an event sits in the lifecycle, for the Events tab and tooling."""
    event_id = int(event_id)
    kind, detected = detect_kind(event_id)
    hooks = _hooks_for(kind)
    baked = bool(hooks and hooks["exists"](event_id))
    start_at = detected.get("start_at")
    end_at = detected.get("end_at")
    started: Optional[bool] = (time.time() >= float(start_at)) if start_at else None
    finished = _finished(detected)
    automated = bool(hooks and hooks["bake"])
    if not hooks:
        status, label = "unsupported", "Format not supported"
    elif finished:
        status = "finished"
        label = "Finished, published" if baked else "Finished, not published"
    elif not automated:
        status = "manual"
        label = "Published (run manually)" if baked else "Not automated: run from the operator app"
    elif baked and started:
        status, label = "frozen", "Published, frozen (event started)"
    elif baked:
        status, label = "baked", "Published, refreshes nightly until the first match"
    elif started:
        status, label = "missing", "Not published (event already started)"
    else:
        status, label = "pending", "Not published yet, retried nightly"
    return {
        "kind": kind,
        "automated": automated,
        "baked": baked,
        "started": started,
        "finished": bool(finished),
        "active": finished is not True,
        "start_at": float(start_at) if start_at else None,
        "end_at": float(end_at) if end_at else None,
        "status": status,
        "label": label,
        "published": bool(baked),
    }
