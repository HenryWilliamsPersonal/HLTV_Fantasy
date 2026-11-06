"""The finish step: when a fantasy game ends, bring everything it shows up
to date once, and mark it done.

Run from the live watch (services/live_watch) for every event that reads as
finished (dates, every match played, or HLTV's fantasy page saying the game
is over — services/fantasy_games). One event at a time, in a background
thread, each step retried on later ticks until it holds:

1. pages    one last read of the event's HLTV pages (the final's result)
2. boards   every finished match's scoreboard stored (the leaderboard's
            reader; the results survive restarts from here on)
3. field    the stored field shows every finished match (the per-match
            re-read for any the live watch missed)
4. season   the season standings read (season points land when a game ends)
5. my_team  every connected account's report re-read

An event that finished more than MAX_AGE ago is never picked up (the
previous events in the list are not re-done after an upgrade), and one that
keeps failing is given up after GIVE_UP_AFTER."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from backend.data.db import connect

logger = logging.getLogger(__name__)

MAX_AGE = 3 * 86400.0  # finished longer ago than this: left alone
GIVE_UP_AFTER = 2 * 86400.0  # a finish still incomplete this long after it began is marked done anyway
RETRY_EVERY = 600.0  # seconds between attempts at an incomplete finish
FRONT_PAGE_EVERY = 1800.0  # re-read HLTV's fantasy front page this often while an event is live
STEPS = ("pages", "boards", "field", "season", "my_team")

_LOCK = threading.Lock()
_BUSY: Dict[str, Any] = {"event_id": None}
_LAST_TRY: Dict[int, float] = {}
_front_page_read = 0.0


def _ensure() -> None:
    conn = connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS event_finish (event_id INTEGER PRIMARY KEY, started_at REAL, finished_at REAL, steps_json TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


def _load(event_id: int) -> Optional[Dict[str, Any]]:
    _ensure()
    conn = connect()
    try:
        r = conn.execute("SELECT started_at, finished_at, steps_json FROM event_finish WHERE event_id = ?", (int(event_id),)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {"started_at": r["started_at"], "finished_at": r["finished_at"], "steps": json.loads(r["steps_json"] or "{}")}


def _save(event_id: int, rec: Dict[str, Any]) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO event_finish (event_id, started_at, finished_at, steps_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(event_id) DO UPDATE SET started_at = excluded.started_at, finished_at = excluded.finished_at, steps_json = excluded.steps_json",
            (int(event_id), rec.get("started_at"), rec.get("finished_at"), json.dumps(rec.get("steps") or {})),
        )
        conn.commit()
    finally:
        conn.close()


def status(event_id: int) -> Dict[str, Any]:
    rec = _load(int(event_id)) or {}
    return {"event_id": int(event_id), "running": _BUSY.get("event_id") == int(event_id), **rec}


# ------------------------------------------------------------------ steps


def _step_pages(eid: int) -> bool:
    from backend.services import live_state

    entry = live_state.live_event(eid, fetch_if_missing=False, force_pages=True)
    return entry is not None


def _step_boards(eid: int) -> bool:
    from backend.services import leaderboard

    board = leaderboard.event_leaderboard(eid, fetch_missing=True)
    if int(board.get("matches_pending") or 0) == 0:
        return True
    # the reader runs in its own thread: wait for it, then look again
    deadline = time.time() + 600.0
    while time.time() < deadline and eid in leaderboard._INFLIGHT:
        time.sleep(5.0)
    board = leaderboard.event_leaderboard(eid, fetch_missing=False)
    return int(board.get("matches_pending") or 0) == 0


def _step_field(eid: int) -> Optional[bool]:
    """True once the stored field shows every finished match (the per-match
    re-read the live watch runs, done here for any it missed); None while a
    re-read is running or another read holds the browser."""
    from backend.data import team_aliases
    from backend.services import fantasy_field, live_state

    if not fantasy_field.status(eid).get("teams_stored"):
        return True  # the field was never read: nothing to bring up to date
    entry = live_state.live_event(eid, fetch_if_missing=False) or {}
    names = {int(t): str(v.get("name") or "") for t, v in (entry.get("teams") or {}).items()}
    ended = [m for m in (entry.get("matches") or []) if m.get("match_id") and m.get("winner") is not None]
    missing = [m for m in ended if not fantasy_field._match_covered(eid, int(m["match_id"]))]
    if not missing:
        return True
    with fantasy_field._LOCK:
        busy = any(r.get("status") == "running" for r in fantasy_field._RUNS.values())
    if busy:
        return None
    m = missing[0]
    keys = [team_aliases.key(names.get(int(m["a"]), "")), team_aliases.key(names.get(int(m["b"]), ""))]
    fantasy_field._start_match_refresh(eid, int(m["match_id"]), keys)
    return None


def _step_season(eid: int) -> Optional[bool]:
    from backend.services import season_standings

    st = season_standings.status()
    rec = _load(eid) or {}
    began = float(rec.get("started_at") or time.time())
    if st.get("running"):
        return None
    if float(st.get("finished_at") or 0) >= began and st.get("status") == "done":
        return True
    season_standings.start()
    return None


def _step_my_team(eid: int) -> bool:
    from backend.data import hltv_accounts
    from backend.services import my_team

    for acc in hltv_accounts.list_accounts():
        key = str(acc.get("user_key") or "")
        if not key or key == "scraper":
            continue  # the scraper account has no team of its own
        try:
            my_team.report_for(eid, key, refresh=True)
        except Exception:  # noqa: BLE001 - one account must not stop the rest
            logger.warning("Finish: My Team refresh failed for %s / %s", eid, key, exc_info=True)
    return True


_RUNNERS = {"pages": _step_pages, "boards": _step_boards, "field": _step_field, "season": _step_season, "my_team": _step_my_team}


def _run(eid: int) -> None:
    try:
        rec = _load(eid) or {"started_at": time.time(), "finished_at": None, "steps": {}}
        rec.setdefault("steps", {})
        for step in STEPS:
            if rec["steps"].get(step) == "done":
                continue
            try:
                ok = _RUNNERS[step](eid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Finish step %s failed for event %s", step, eid, exc_info=True)
                rec["steps"][step] = f"error: {str(exc)[:120]}"
                ok = False
            if ok is True:
                rec["steps"][step] = "done"
            elif ok is None:
                rec["steps"][step] = "waiting"
                _save(eid, rec)
                return  # a read in progress: carry on next attempt
            else:
                rec["steps"].setdefault(step, "incomplete")
                if rec["steps"][step] == "waiting":
                    rec["steps"][step] = "incomplete"
            _save(eid, rec)
        complete = all(rec["steps"].get(s) == "done" for s in STEPS)
        expired = time.time() - float(rec.get("started_at") or time.time()) > GIVE_UP_AFTER
        if complete or expired:
            rec["finished_at"] = time.time()
            logger.info("Event %s finished and brought up to date: %s", eid, rec["steps"])
        _save(eid, rec)
        if complete:
            from backend.services import live_pages

            with live_pages._LOCK:  # open apps re-read the event
                live_pages._VERSION += 1
                live_pages._CHANGED[eid] = {"version": live_pages._VERSION, "read_at": time.time(), "live_count": 0}
    finally:
        with _LOCK:
            _BUSY["event_id"] = None


def _refresh_front_page(entries: List[Dict[str, Any]]) -> None:
    """While an event is live, re-read HLTV's fantasy front page now and then:
    it is what says a stage's game is over (services/fantasy_games)."""
    global _front_page_read
    if not any(e.get("status") == "live" for e in entries):
        return
    if time.time() - _front_page_read < FRONT_PAGE_EVERY:
        return
    _front_page_read = time.time()
    try:
        from backend.services import fantasy_games
        from backend.services.hltv_browser import fetch_hltv_html

        fetch_hltv_html(fantasy_games.FRONT_PAGE)  # archived as a page snapshot
        with fantasy_games._LOCK:
            fantasy_games._CACHE["at"] = 0.0
    except Exception:  # noqa: BLE001
        logger.warning("Finish: fantasy front page read failed", exc_info=True)


def tick(entries: List[Dict[str, Any]], now: Optional[float] = None) -> Optional[str]:
    """Start the finish step of one finished event that needs it."""
    now = float(now or time.time())
    _refresh_front_page(entries)
    with _LOCK:
        if _BUSY.get("event_id") is not None:
            return None
    for e in entries or []:
        if e.get("status") != "finished":
            continue
        eid = int(e["event_id"])
        if now - float(e.get("end_at") or now) > MAX_AGE:
            continue
        rec = _load(eid)
        if rec and rec.get("finished_at"):
            continue
        if now - _LAST_TRY.get(eid, 0.0) < RETRY_EVERY:
            continue
        _LAST_TRY[eid] = now
        with _LOCK:
            if _BUSY.get("event_id") is not None:
                return None
            _BUSY["event_id"] = eid
        if rec is None:
            _save(eid, {"started_at": now, "finished_at": None, "steps": {}})
        threading.Thread(target=_run, args=(eid,), name=f"event-finish-{eid}", daemon=True).start()
        return f"finish step started for {eid}"
    return None
