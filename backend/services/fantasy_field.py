"""The field: every team in an event's fantasy league, read from HLTV.

HLTV's standings page lists ten teams a page with no team ids, but any
team's overview feed answers for the scraper account:
  /fantasy/{eid}/league/{lid}/overview/{tid}/json
with the lineup (player, role, total points), the player tracker (per
finished match: rating / team / role / booster points and the booster
used) and the team's placement. Team ids are dense — a league's teams sit
in a near-consecutive id range — so the field is read by walking the ids
outward from a known team (the user's) until a long run of ids belongs to
nothing, or the league's team count is reached.

The reads happen as fetches INSIDE one browser session (same-origin,
several at a time: ~0.2 s each instead of a 2 s page load), in chunks of
CHUNK_IDS ids per session so the shared browser is handed back between
chunks for live-page and personal reads. Teams are stored as they are
found (fantasy_field_teams), so the leaderboard fills in while a read runs.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from backend.data.db import connect

logger = logging.getLogger(__name__)

CHUNK_IDS = 80  # ids per browser session (the browser is handed back between chunks)
CONCURRENT = 3  # fetches in flight inside the session: HLTV answers 429 to bursts (6 at once → half refused)
BATCH_DELAY = 0.25  # seconds between batches inside a session
RETRY_PASSES = 4  # 429s are fetched again after a back-off, this many times per chunk
BACKOFF = 8.0  # seconds, doubled per pass
STOP_AFTER_MISSES = 400  # consecutive ids outside the league end a direction
MAX_IDS = 60000  # hard cap per run
PAUSE_BETWEEN_CHUNKS = 1.0

_FETCH_JS = """
const [urls, done] = arguments;
Promise.all(urls.map((u) => fetch(u, {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
  .then((r) => r.text().then((t) => ({status: r.status, text: t})))
  .catch((e) => ({status: 0, text: String(e)}))))
  .then(done);
"""

_READY = False
_LOCK = threading.Lock()
_RUNS: Dict[int, Dict[str, Any]] = {}  # event_id -> live run state
_STOP: Dict[int, bool] = {}


def _ensure() -> None:
    global _READY
    if _READY:
        return
    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fantasy_field_teams (
                event_id       INTEGER NOT NULL,
                league_id      INTEGER NOT NULL,
                team_id        INTEGER NOT NULL,
                team_name      TEXT,
                placement      INTEGER,
                total_points   INTEGER,
                role_points    INTEGER,
                booster_points INTEGER,
                lineup_json    TEXT NOT NULL,
                read_at        REAL NOT NULL,
                PRIMARY KEY (event_id, team_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fantasy_field_teams_event ON fantasy_field_teams(event_id, league_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fantasy_field_runs (
                event_id    INTEGER NOT NULL,
                league_id   INTEGER NOT NULL,
                started_at  REAL NOT NULL,
                finished_at REAL,
                status      TEXT NOT NULL,
                scanned     INTEGER NOT NULL DEFAULT 0,
                found       INTEGER NOT NULL DEFAULT 0,
                errors      INTEGER NOT NULL DEFAULT 0,
                lowest_id   INTEGER,
                highest_id  INTEGER,
                league_size INTEGER,
                message     TEXT,
                PRIMARY KEY (event_id, started_at)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fantasy_field_match_runs (
                event_id   INTEGER NOT NULL,
                match_id   INTEGER NOT NULL,
                keys_json  TEXT NOT NULL,
                state_json TEXT,
                status     TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (event_id, match_id)
            )
            """
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fantasy_field_runs)").fetchall()}
        if "state_json" not in cols:
            conn.execute("ALTER TABLE fantasy_field_runs ADD COLUMN state_json TEXT")  # the walk's position, for a resume
        conn.commit()
    finally:
        conn.close()
    _READY = True


# ---------------------------------------------------------------- parsing
def _int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip()) if v is not None and str(v).strip() not in ("", "-") else None
    except (TypeError, ValueError):
        return None


def parse_team_feed(data: Dict[str, Any], event_id: int, league_id: int) -> Optional[Dict[str, Any]]:
    """A stored team row from a team overview feed, or None when the feed is
    not a team of this event's league."""
    if not isinstance(data, dict):
        return None
    league = (data.get("leagueId") or ((data.get("leagueGeneralData") or {}).get("leagueId")) or {})
    if _int((data.get("draftId") or {}).get("id")) != int(event_id) or _int(league.get("id") if isinstance(league, dict) else league) != int(league_id):
        return None
    players_in = data.get("teamOverviewPlayers") or []
    if not players_in:
        return None
    kn = data.get("keyNumbers") or {}
    by_pid: Dict[int, Dict[str, Any]] = {}
    for p in players_in:
        pl = p.get("player") or {}
        pid = _int((pl.get("fantasyPlayerId") or {}).get("playerId"))
        if pid is None:
            continue
        role = p.get("role") or {}
        by_pid[pid] = {
            "player_id": pid, "name": str(pl.get("name") or ""), "team": str((pl.get("team") or {}).get("name") or ""),
            "role_id": _int(role.get("id")), "role_name": str(role.get("name") or "") or None,
            "total": _int((p.get("totalPoints") or {}).get("value")) if isinstance(p.get("totalPoints"), dict) else _int(p.get("totalPoints")),
            "matches": [], "padding": [],
        }
    for it in data.get("playerTrackerItems") or []:
        kind = str(it.get("type") or "").rsplit(".", 1)[-1]
        pid = _int(((it.get("player") or {}).get("fantasyPlayerId") or {}).get("playerId"))
        row = by_pid.get(pid) if pid is not None else None
        if row is None:
            continue
        pts = {"total": _int(it.get("totalPoints")), "rating": _int(it.get("playerPoints")), "team": _int(it.get("teamPoints")),
               "role": _int(it.get("rolePoints")), "booster": _int(it.get("boosterPoints"))}
        if kind == "FinishedMatch":
            m = it.get("match") or {}
            b = it.get("booster") or {}
            row["matches"].append({
                **pts, "match_url": str(m.get("matchUrl") or ""), "opponent": str(m.get("opponent") or ""), "result": str(m.get("result") or ""),
                "end_time": (float(m["endTime"]) / 1000.0) if m.get("endTime") else None,
                "booster_id": _int(b.get("id")), "booster_name": str(b.get("name") or "") or None,
            })
        elif kind == "Padding":
            row["padding"].append(pts)
    for row in by_pid.values():
        row["matches"].sort(key=lambda m: (m.get("end_time") or 0.0, m.get("match_url") or ""))
    return {
        "team_name": str(data.get("teamName") or ""),
        "placement": _int(kn.get("leaguePlacement")),
        "total_points": _int(kn.get("totalPoints")),
        "role_points": _int(kn.get("rolePoints")),
        "booster_points": _int(kn.get("boostPoints")),
        "league_size": _int(data.get("numberofTeamsInLeague")),
        "players": list(by_pid.values()),
    }


# ---------------------------------------------------------------- storage
def _save_teams(event_id: int, league_id: int, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    _ensure()
    conn = connect()
    try:
        conn.executemany(
            """
            INSERT INTO fantasy_field_teams (event_id, league_id, team_id, team_name, placement, total_points, role_points, booster_points, lineup_json, read_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, team_id) DO UPDATE SET
                team_name = excluded.team_name, placement = excluded.placement, total_points = excluded.total_points,
                role_points = excluded.role_points, booster_points = excluded.booster_points, lineup_json = excluded.lineup_json, read_at = excluded.read_at
            """,
            [
                (int(event_id), int(league_id), int(r["team_id"]), r["team_name"], r["placement"], r["total_points"], r["role_points"], r["booster_points"],
                 json.dumps(r["players"]), float(r["read_at"]))
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _save_run(run: Dict[str, Any]) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO fantasy_field_runs (event_id, league_id, started_at, finished_at, status, scanned, found, errors, lowest_id, highest_id, league_size, message, state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, started_at) DO UPDATE SET
                finished_at = excluded.finished_at, status = excluded.status, scanned = excluded.scanned, found = excluded.found, errors = excluded.errors,
                lowest_id = excluded.lowest_id, highest_id = excluded.highest_id, league_size = excluded.league_size, message = excluded.message,
                state_json = excluded.state_json
            """,
            (int(run["event_id"]), int(run["league_id"]), float(run["started_at"]), run.get("finished_at"), str(run["status"]), int(run["scanned"]),
             int(run["found"]), int(run["errors"]), run.get("lowest_id"), run.get("highest_id"), run.get("league_size"), str(run.get("message") or ""),
             json.dumps({**(run.get("walk") or {}), "statuses": run.get("statuses") or {}}) if run.get("walk") is not None else None),
        )
        conn.commit()
    finally:
        conn.close()


def teams(event_id: int) -> List[Dict[str, Any]]:
    """Every stored team of the event: {team_id, team_name, placement, total_points, players: [...], read_at}."""
    _ensure()
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM fantasy_field_teams WHERE event_id = ?", (int(event_id),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            players = json.loads(r["lineup_json"] or "[]")
        except Exception:  # noqa: BLE001
            players = []
        out.append({"team_id": int(r["team_id"]), "league_id": int(r["league_id"]), "team_name": r["team_name"], "placement": r["placement"],
                    "total_points": r["total_points"], "role_points": r["role_points"], "booster_points": r["booster_points"], "players": players, "read_at": float(r["read_at"] or 0.0)})
    return out


def stored_events() -> List[int]:
    """The events with a stored field, most recently read first."""
    _ensure()
    conn = connect()
    try:
        rows = conn.execute("SELECT event_id, MAX(read_at) AS at FROM fantasy_field_teams GROUP BY event_id ORDER BY at DESC").fetchall()
    finally:
        conn.close()
    return [int(r["event_id"]) for r in rows]


def status(event_id: int) -> Dict[str, Any]:
    """The live run if one is going, else the last stored run, plus the
    stored team count."""
    _ensure()
    eid = int(event_id)
    with _LOCK:
        live = dict(_RUNS.get(eid) or {})
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS c, MAX(read_at) AS t FROM fantasy_field_teams WHERE event_id = ?", (eid,)).fetchone()
        last = conn.execute("SELECT * FROM fantasy_field_runs WHERE event_id = ? ORDER BY started_at DESC LIMIT 1", (eid,)).fetchone()
    finally:
        conn.close()
    out = {"event_id": eid, "teams_stored": int(n["c"] or 0), "stored_at": float(n["t"]) if n["t"] else None, "running": bool(live) and live.get("status") == "running"}
    run = live or (dict(last) if last else {})
    for k in ("kind", "match_id", "league_id", "status", "scanned", "found", "errors", "lowest_id", "highest_id", "league_size", "started_at", "finished_at", "message", "statuses", "teams_read", "teams_target"):
        if k in run:
            out[k] = run[k]
    if not out.get("league_size") and last and last["league_size"]:
        out["league_size"] = int(last["league_size"])  # a match re-read does not carry it
    return out


# ---------------------------------------------------------------- the read
def _fetch_feeds(driver, urls: List[str]) -> List[Dict[str, Any]]:
    """The fetches of one session, CONCURRENT at a time with BATCH_DELAY
    between batches; a 429 is retried after a back-off (RETRY_PASSES)."""
    results: Dict[str, Dict[str, Any]] = {}
    pending = list(urls)
    backoff = BACKOFF
    for attempt in range(RETRY_PASSES + 1):
        if not pending:
            break
        if attempt:
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
        again: List[str] = []
        for i in range(0, len(pending), CONCURRENT):
            batch = pending[i:i + CONCURRENT]
            try:
                driver.set_script_timeout(60)
                res = driver.execute_async_script(_FETCH_JS, batch) or []
            except Exception as exc:  # noqa: BLE001
                res = [{"status": 0, "text": str(exc)} for _ in batch]
            if not isinstance(res, list) or len(res) != len(batch):
                res = [{"status": 0, "text": "no answer"}] * len(batch)
            for u, r in zip(batch, res):
                if int((r or {}).get("status") or 0) == 429:
                    again.append(u)
                results[u] = r or {"status": 0, "text": ""}
            if i + CONCURRENT < len(pending):
                time.sleep(BATCH_DELAY)
        pending = again
    return [results.get(u) or {"status": 0, "text": ""} for u in urls]


# passes over the ids HLTV refused (403 / 429) before giving up on them
UNANSWERED_PASSES = 8  # passes over the ids HLTV refused (403 / 429) before giving up on them
UNANSWERED_PAUSE = 30.0  # seconds, times the pass number


def _new_walk(seed_team_id: int) -> Dict[str, Any]:
    seed = int(seed_team_id)
    return {"seed": seed, "lo": seed, "hi": seed + 1, "first": True, "down_misses": 0, "up_misses": 0,
            "down_open": True, "up_open": True, "unresolved": [], "phase": "walk", "passes": 0}


def _read(event_id: int, league_id: int, seed_team_id: int, max_ids: int) -> None:
    """The id walk, its position kept in run["walk"] and saved with the run
    after every chunk, so a read cut off by a restart resumes where it was
    (resume_interrupted) instead of starting over."""
    from backend.services.hltv_browser import run_hltv_browser_session
    from backend.services.my_team import ENTRY_URL

    eid, lid = int(event_id), int(league_id)
    run = _RUNS[eid]
    base = f"https://www.hltv.org/fantasy/{eid}/league/{lid}/overview/"
    w = run.setdefault("walk", _new_walk(seed_team_id))
    seed = int(w["seed"])

    def chunk_ids() -> tuple:
        if w["first"]:
            w["first"] = False
            w["hi"] = seed + CHUNK_IDS
            return "up", list(range(seed, seed + CHUNK_IDS))
        if w["up_open"] and (not w["down_open"] or (w["hi"] - seed) <= (seed - w["lo"])):
            ids = list(range(w["hi"], w["hi"] + CHUNK_IDS))
            w["hi"] += CHUNK_IDS
            return "up", ids
        ids = list(range(w["lo"] - CHUNK_IDS, w["lo"]))
        w["lo"] -= CHUNK_IDS
        return "down", ids

    def unanswered(tid: int) -> None:
        if int(tid) not in w["unresolved"]:
            w["unresolved"].append(int(tid))

    def scan(ids: List[int], direction: Optional[str]) -> int:
        """Fetch the ids in one session, store the teams found, return the
        number of ids left unresolved (throttled or failed)."""
        urls = [f"{base}{tid}/json" for tid in ids]

        def session(driver, _urls=urls):
            return _fetch_feeds(driver, _urls)

        try:
            answers = run_hltv_browser_session(ENTRY_URL, session, wait_text=None, timeout_ms=60000)
        except Exception:  # noqa: BLE001
            logger.warning("Field read: browser session failed for event %s", eid, exc_info=True)
            run["errors"] += len(ids)
            for tid in ids:
                unanswered(tid)
            time.sleep(5.0)
            return len(ids)
        found_rows: List[Dict[str, Any]] = []
        left = 0
        miss_run = 0
        ordered = list(zip(ids, answers)) if direction != "down" else list(zip(ids, answers))[::-1]
        for tid, ans in ordered:
            run["scanned"] += 1
            status_ = int((ans or {}).get("status") or 0)
            text = str((ans or {}).get("text") or "")
            stats = run.setdefault("statuses", {})
            stats[str(status_)] = int(stats.get(str(status_), 0)) + 1
            parsed = None
            if status_ == 200 and text:
                try:
                    parsed = parse_team_feed(json.loads(text), eid, lid)
                except Exception:  # noqa: BLE001
                    parsed = None
            if status_ in (0, 403, 429) or status_ >= 500:
                run["errors"] += 1  # no answer about the id: throttled, challenged or down
                left += 1
                unanswered(tid)
                continue
            if parsed:
                parsed["team_id"] = int(tid)
                parsed["read_at"] = time.time()
                found_rows.append(parsed)
                run["found"] += 1
                run["lowest_id"] = min(run["lowest_id"] or tid, tid)
                run["highest_id"] = max(run["highest_id"] or tid, tid)
                if parsed.get("league_size"):
                    run["league_size"] = parsed["league_size"]
                miss_run = 0
            else:
                miss_run += 1
        if direction == "up":
            w["up_misses"] = (w["up_misses"] + miss_run) if not found_rows else miss_run
            if w["up_misses"] >= STOP_AFTER_MISSES:
                w["up_open"] = False
        elif direction == "down":
            w["down_misses"] = (w["down_misses"] + miss_run) if not found_rows else miss_run
            if w["down_misses"] >= STOP_AFTER_MISSES:
                w["down_open"] = False
        _save_teams(eid, lid, found_rows)
        return left

    try:
        if w["phase"] == "walk":
            while (w["down_open"] or w["up_open"]) and run["scanned"] < max_ids and not _STOP.get(eid):
                direction, ids = chunk_ids()
                scan(ids, direction)
                if run.get("league_size") and run["found"] >= int(run["league_size"]):
                    run["message"] = "every team of the league found"
                    break
                _save_run(run)
                time.sleep(PAUSE_BETWEEN_CHUNKS)
            if not _STOP.get(eid):
                w["phase"] = "retry"
                _save_run(run)
        # retry what HLTV never answered about (403 / 429 bursts): every id,
        # pass after pass with a growing pause, until a pass recovers nothing
        while w["phase"] == "retry" and w["unresolved"] and w["passes"] < UNANSWERED_PASSES and not _STOP.get(eid):
            w["passes"] += 1
            time.sleep(UNANSWERED_PAUSE * w["passes"])
            todo, w["unresolved"] = list(w["unresolved"]), []
            before = len(todo)
            for i in range(0, len(todo), CHUNK_IDS):
                if _STOP.get(eid):
                    w["unresolved"].extend(todo[i:])
                    break
                retry = todo[i:i + CHUNK_IDS]
                run["scanned"] -= len(retry)  # counted again by scan()
                scan(retry, None)
                # the ids this pass has not reached yet stay listed in the saved state
                rest = [t for t in todo[i + CHUNK_IDS:] if t not in w["unresolved"]]
                _save_run({**run, "walk": {**w, "unresolved": w["unresolved"] + rest}})
                time.sleep(PAUSE_BETWEEN_CHUNKS)
            if len(w["unresolved"]) >= before:
                break  # HLTV still refuses every one of them
        run["status"] = "stopped" if _STOP.get(eid) else "done"
        if not run.get("message"):
            run["message"] = f"scan ended: ids {run.get('lowest_id')}–{run.get('highest_id')}, {run['found']} teams"
        if w["unresolved"]:
            run["message"] += f" | {len(w['unresolved'])} ids unanswered"
        run["message"] += f" | statuses {json.dumps(run.get('statuses') or {}, sort_keys=True)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Field read failed for event %s", eid)
        run["status"] = "error"
        run["message"] = str(exc)[:300]
    finally:
        run["finished_at"] = time.time()
        _save_run(run)
        logger.info("Field read for event %s: %s - %s found in %s ids, %s errors", eid, run["status"], run["found"], run["scanned"], run["errors"])
        with _LOCK:
            _STOP.pop(eid, None)
            _RUNS.pop(eid, None)


def start_read(event_id: int, league_id: int, seed_team_id: int, max_ids: int = MAX_IDS) -> Dict[str, Any]:
    """Start reading the field in a background thread (one per event)."""
    _ensure()
    eid = int(event_id)
    with _LOCK:
        live = _RUNS.get(eid)
        if live and live.get("status") == "running":
            return {"status": "running", "reused": True, **{k: live.get(k) for k in ("scanned", "found", "errors")}}
        run = {
            "event_id": eid, "league_id": int(league_id), "started_at": time.time(), "finished_at": None, "status": "running",
            "scanned": 0, "found": 0, "errors": 0, "lowest_id": None, "highest_id": None, "league_size": None, "message": "", "statuses": {},
            "walk": _new_walk(seed_team_id),
        }
        _RUNS[eid] = run
        _STOP.pop(eid, None)
    _save_run(run)
    threading.Thread(target=_read, args=(eid, int(league_id), int(seed_team_id), int(max_ids)), name=f"fantasy-field-{eid}", daemon=True).start()
    return {"status": "started", "event_id": eid, "league_id": int(league_id), "seed_team_id": int(seed_team_id), "max_ids": int(max_ids)}


def resume_interrupted() -> Optional[str]:
    """Pick up a full read a restart cut off: the newest run of an event
    still marked running with no thread behind it, continued from its saved
    walk under the same run row. A cut-off run saved before walks were kept
    is closed as interrupted (auto_reads then starts a fresh one when the
    event still needs it). One read at a time; None when nothing waited."""
    _ensure()
    with _LOCK:
        if any(r.get("status") == "running" for r in _RUNS.values()):
            return None
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM fantasy_field_runs WHERE status = 'running' ORDER BY started_at DESC").fetchall()
    finally:
        conn.close()
    seen: set = set()
    keys = ("event_id", "league_id", "started_at", "finished_at", "status", "scanned", "found", "errors", "lowest_id", "highest_id", "league_size", "message")
    for r in rows:
        eid = int(r["event_id"])
        with _LOCK:
            if eid in _RUNS:
                continue
        run = {k: r[k] for k in keys}
        run["message"] = run.get("message") or ""
        if eid in seen:  # an older stuck row of an event already handled
            _save_run({**run, "status": "interrupted", "finished_at": time.time()})
            continue
        seen.add(eid)
        state = json.loads(r["state_json"]) if r["state_json"] else None
        if not state or "seed" not in state:
            _save_run({**run, "status": "interrupted", "finished_at": time.time(), "message": (run["message"] + " | cut off by a restart").strip(" |")})
            continue
        run["statuses"] = state.pop("statuses", {}) or {}
        run["walk"] = state
        run["message"] = ""  # the finished run writes its own summary
        with _LOCK:
            _RUNS[eid] = run
            _STOP.pop(eid, None)
        threading.Thread(target=_read, args=(eid, int(run["league_id"]), int(state["seed"]), MAX_IDS), name=f"fantasy-field-{eid}", daemon=True).start()
        logger.info("Field read for event %s resumed after a restart (%s ids scanned, %s found)", eid, run["scanned"], run["found"])
        return f"resumed {eid}"
    return _resume_match_refresh()


def _resume_match_refresh() -> Optional[str]:
    """A per-match re-read a restart cut off (still marked running, no thread
    behind it): carried on from its saved progress. Rows older than a day are
    closed instead (the event has moved on)."""
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM fantasy_field_match_runs WHERE status = 'running' ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()
    for r in rows:
        eid, mid = int(r["event_id"]), int(r["match_id"])
        with _LOCK:
            if eid in _RUNS:
                continue
        keys = json.loads(r["keys_json"] or "[]")
        state = json.loads(r["state_json"]) if r["state_json"] else {}
        if time.time() - float(r["updated_at"]) > 86400.0:
            _save_match_run(eid, mid, keys, state, "interrupted", float(r["started_at"]))
            continue
        if _start_match_refresh(eid, mid, keys, resume=state):
            st = _MATCH_DONE.setdefault((eid, mid), {"seen": float(r["started_at"]), "passes": 1, "last": time.time()})
            st["last"] = time.time()
            logger.info("Field: match %s of event %s re-read resumed after a restart (%s teams already read)", mid, eid, state.get("read", 0))
            return f"resumed match {mid} of {eid}"
        return None
    return None


# ---------------------------------------------------------------- after a match
# Every team that owns a player sees the same rating and team points for a
# match; its role and booster points depend only on the role and booster it
# chose. So once the field is stored, a finished match needs only enough
# teams re-read to observe each (player, role) and (player, booster) of the
# players who played it — a few hundred teams, not the whole league. Roles
# are known from the stored lineups, so teams are picked greedily by the
# (player, role) pairs they would add; boosters are only known once read, so
# reading stops when every pair is seen and the booster pairs stop growing.
MATCH_SETTLE = 20 * 60.0  # HLTV's scoring catches up in the minutes after a match
MATCH_SECOND_PASS = 90 * 60.0  # one more pass this long after, for late-scored boosters
MATCH_MAX_TEAMS = 2500  # cap per match
MATCH_CHUNK = 60
BOOSTER_STALL_CHUNKS = 2  # chunks without a new (player, booster) pair end the read
_MATCH_DONE: Dict[tuple, Dict[str, float]] = {}  # (event_id, match_id) -> {"first", "last"}
_CHECKED: Dict[int, float] = {}
CHECK_EVERY = 120.0


def seed_for(event_id: int) -> Optional[tuple]:
    """(league_id, team_id) of a team the app knows in the event (any
    connected account's), the starting point of the id walk; None without."""
    from backend.data import my_team_cache

    conn = connect()
    try:
        rows = conn.execute("SELECT team_json FROM my_team_cache WHERE event_id = ? AND team_json IS NOT NULL", (int(event_id),)).fetchall()
    finally:
        conn.close()
    for r in rows:
        t = my_team_cache._loads(r["team_json"]) or {}
        if t.get("team_id") and t.get("league_id"):
            return int(t["league_id"]), int(t["team_id"])
    return None


def _read_team_ids(event_id: int, league_id: int, team_ids: List[int]) -> List[Dict[str, Any]]:
    """Read these teams' feeds in one browser session, store them, return the parsed rows."""
    from backend.services.hltv_browser import run_hltv_browser_session
    from backend.services.my_team import ENTRY_URL

    base = f"https://www.hltv.org/fantasy/{int(event_id)}/league/{int(league_id)}/overview/"
    urls = [f"{base}{int(t)}/json" for t in team_ids]
    answers = run_hltv_browser_session(ENTRY_URL, lambda d: _fetch_feeds(d, urls), wait_text=None, timeout_ms=60000) or []
    rows = []
    for tid, ans in zip(team_ids, answers):
        if int((ans or {}).get("status") or 0) != 200:
            continue
        try:
            parsed = parse_team_feed(json.loads(str(ans.get("text") or "")), int(event_id), int(league_id))
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed:
            parsed["team_id"], parsed["read_at"] = int(tid), time.time()
            rows.append(parsed)
    _save_teams(int(event_id), int(league_id), rows)
    return rows


def _save_match_run(event_id: int, match_id: int, keys: List[str], state: Optional[Dict[str, Any]], status_: str, started_at: float) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO fantasy_field_match_runs (event_id, match_id, keys_json, state_json, status, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(event_id), int(match_id), json.dumps(list(keys)), json.dumps(state) if state is not None else None, status_, float(started_at), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def refresh_match(event_id: int, match_id: int, team_keys: List[str], resume: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Re-read the teams needed for one finished match (see above): the
    players of the two teams (by team name key), every role each was given
    somewhere in the field, read greedily until covered. Its progress (teams
    read, pairs seen) is saved after every chunk, so a restart or a stop
    carries on from there (`resume`, see resume_interrupted) instead of
    dropping the rest of the pass."""
    from backend.data import team_aliases

    eid, mid = int(event_id), int(match_id)
    started_at = float((resume or {}).get("started_at") or time.time())
    stored = teams(eid)
    if not stored:
        return {"status": "no field"}
    league_id = int(stored[0]["league_id"])
    keys = {str(k) for k in team_keys}
    pids = set()
    roles_needed: set = set()
    by_team: Dict[int, List[tuple]] = {}
    for t in stored:
        pairs = []
        for p in t.get("players") or []:
            if team_aliases.key(str(p.get("team") or "")) in keys and p.get("player_id") is not None:
                pid = int(p["player_id"])
                pids.add(pid)
                pair = (pid, p.get("role_name"))
                roles_needed.add(pair)
                pairs.append(pair)
        if pairs:
            by_team[int(t["team_id"])] = pairs
    seen_roles: set = {(int(a), b) for a, b in (resume or {}).get("seen_roles") or []}
    seen_boosters: set = {(int(a), b) for a, b in (resume or {}).get("seen_boosters") or []}
    done_ids: set = {int(t) for t in (resume or {}).get("read_ids") or []}
    read = int((resume or {}).get("read") or 0)
    stall = int((resume or {}).get("stall") or 0)
    order = sorted(by_team, key=lambda tid: -len(set(by_team[tid])))
    pending = [tid for tid in order if tid not in done_ids]
    run = _RUNS.get(eid) if (_RUNS.get(eid) or {}).get("kind") == "match" else {}
    run.update(teams_read=read, teams_target=min(len(order), MATCH_MAX_TEAMS))

    def save(status_: str) -> None:
        _save_match_run(eid, mid, sorted(keys), {
            "started_at": started_at, "read": read, "stall": stall, "read_ids": sorted(done_ids),
            "seen_roles": sorted([list(x) for x in seen_roles], key=str), "seen_boosters": sorted([list(x) for x in seen_boosters], key=str),
        }, status_, started_at)

    save("running")
    stopped = False
    while pending and read < MATCH_MAX_TEAMS:
        if _STOP.get(eid):
            stopped = True
            break
        # the teams adding the most unseen (player, role) pairs first; once all
        # are seen, the rest for booster variety (two players per team at most)
        pending.sort(key=lambda tid: -len(set(by_team[tid]) - seen_roles))
        chunk, pending = pending[:MATCH_CHUNK], pending[MATCH_CHUNK:]
        rows = _read_team_ids(eid, league_id, chunk)
        read += len(chunk)
        done_ids.update(int(t) for t in chunk)
        run["teams_read"] = read
        new_boosters = 0
        for row in rows:
            for p in row.get("players") or []:
                pid = p.get("player_id")
                if pid is None or int(pid) not in pids:
                    continue
                for m in p.get("matches") or []:
                    if f"/matches/{mid}/" not in str(m.get("match_url") or ""):
                        continue
                    seen_roles.add((int(pid), p.get("role_name")))
                    if m.get("booster_name") and (int(pid), m["booster_name"]) not in seen_boosters:
                        seen_boosters.add((int(pid), m["booster_name"]))
                        new_boosters += 1
        # every (player, role) seen — or none of the teams left can add one
        # (pairs from lineups that no longer show the match: stale stored rows)
        roles_done = roles_needed <= seen_roles or not any(set(by_team[tid]) - seen_roles for tid in pending)
        if roles_done:
            stall = stall + 1 if new_boosters == 0 else 0
        save("running")
        if roles_done and stall >= BOOSTER_STALL_CHUNKS:
            break
        time.sleep(PAUSE_BETWEEN_CHUNKS)
    save("stopped" if stopped else "done")
    logger.info("Field: match %s of event %s re-read from %s of %s teams (%s of %s player-role pairs, %s player-booster pairs)",
                mid, eid, read, len(by_team), len(seen_roles & roles_needed), len(roles_needed), len(seen_boosters))
    return {"status": "stopped" if stopped else "done", "teams_read": read, "teams_with_players": len(by_team), "roles": [len(seen_roles & roles_needed), len(roles_needed)], "boosters": len(seen_boosters)}


def after_matches(entries: List[Dict[str, Any]], now: Optional[float] = None) -> Optional[str]:
    """For the live events whose field is stored: start the re-read of a
    finished match not yet covered (after MATCH_SETTLE, once more after
    MATCH_SECOND_PASS), one at a time in a background thread."""
    from backend.data import team_aliases

    now = float(now or time.time())
    with _LOCK:
        if any(r.get("status") == "running" for r in _RUNS.values()):
            return None
    for e in entries or []:
        if e.get("status") != "live":
            continue
        eid = int(e["event_id"])
        if now - _CHECKED.get(eid, 0.0) < CHECK_EVERY:
            continue
        _CHECKED[eid] = now
        if not status(eid).get("teams_stored"):
            continue
        names = {int(t): str(v.get("name") or "") for t, v in (e.get("teams") or {}).items()}
        ended = {int(m["match_id"]): m for m in (e.get("matches") or []) if m.get("match_id") and m.get("winner") is not None}
        for mid, m in sorted(ended.items()):
            st = _MATCH_DONE.get((eid, mid))
            if st is None:
                ended_at = _match_end(eid, mid)
                if ended_at is not None:
                    # already in the stored field: its first pass is the read
                    # that stored it; only the late pass is left, if still due
                    late = now - ended_at <= MATCH_SECOND_PASS * 2
                    _MATCH_DONE[(eid, mid)] = {"seen": ended_at, "passes": 1 if late else 2, "last": ended_at}
                else:
                    _MATCH_DONE[(eid, mid)] = {"seen": now, "passes": 0, "last": 0.0}
                continue
            if st["passes"] >= 2:
                continue
            if st["passes"] == 0:
                if now - st["seen"] < MATCH_SETTLE:
                    continue
                if _match_covered(eid, mid) and now - st["seen"] > MATCH_SECOND_PASS:
                    st["passes"] = 2  # in the stored field already (an older match)
                    continue
            elif now - st["last"] < MATCH_SECOND_PASS:
                continue
            keys = [team_aliases.key(names.get(int(m["a"]), "")), team_aliases.key(names.get(int(m["b"]), ""))]
            st["passes"] += 1
            st["last"] = now
            _start_match_refresh(eid, mid, keys)
            return f"{eid}: match {mid}"
    return None


def _match_end(event_id: int, match_id: int) -> Optional[float]:
    """When the match ended, from the stored field (None when no stored
    team shows it yet)."""
    facts = match_facts(int(event_id)).get("players") or {}
    ends = [float(m["end_time"]) for f in facts.values() for m in f.get("matches") or []
            if int(m.get("match_id") or 0) == int(match_id) and m.get("end_time")]
    return max(ends) if ends else None


def _match_covered(event_id: int, match_id: int) -> bool:
    facts = match_facts(int(event_id)).get("players") or {}
    return any(int(m.get("match_id") or 0) == int(match_id) for f in facts.values() for m in f.get("matches") or [])


def _start_match_refresh(event_id: int, match_id: int, keys: List[str], resume: Optional[Dict[str, Any]] = None) -> bool:
    eid = int(event_id)
    with _LOCK:
        if any(r.get("status") == "running" for r in _RUNS.values()):
            return False
        _RUNS[eid] = {"event_id": eid, "status": "running", "kind": "match", "match_id": int(match_id), "started_at": time.time()}
        _STOP.pop(eid, None)

    def work():
        try:
            refresh_match(eid, int(match_id), keys, resume=resume)
        except Exception:  # noqa: BLE001
            logger.warning("Field: match refresh failed for %s / %s", eid, match_id, exc_info=True)
        finally:
            with _LOCK:
                if (_RUNS.get(eid) or {}).get("kind") == "match":
                    _RUNS.pop(eid, None)
                _STOP.pop(eid, None)

    threading.Thread(target=work, name=f"fantasy-field-match-{eid}", daemon=True).start()
    return True


FIELD_LEAD = 2 * 86400.0  # an upcoming event's field is read this long before it starts (lineups are open)


def auto_reads(now: Optional[float] = None) -> str:
    """Start the full field read an event needs (one at a time; reads share
    the browser): an upcoming event within FIELD_LEAD of its start, for
    Season Maximum's field; a live event never read, cut off by a restart, or
    last read before it started (lineups change until roster lock). A field
    read after the start is kept current match by match (after_matches)."""
    from backend.services import live_state

    now = float(now or time.time())
    with _LOCK:
        running = [eid for eid, r in _RUNS.items() if r.get("status") == "running"]
    if running:
        return f"field read already running for {', '.join(str(e) for e in running)}"
    for e in live_state.live_events(now):
        live = e.get("status") == "live"
        if not live and not (e.get("status") == "upcoming" and float(e.get("start_at") or 0) - now <= FIELD_LEAD):
            continue
        eid = int(e["event_id"])
        seed = seed_for(eid)
        if not seed:
            continue
        st = status(eid)
        stale = live and st.get("stored_at") and float(st["stored_at"]) < float(e.get("start_at") or 0)
        if not st.get("running") and st.get("status") == "running":
            continue  # cut off by a restart: resume_interrupted carries it on
        if not st.get("teams_stored") or stale or st.get("status") == "interrupted":
            start_read(eid, seed[0], seed[1])
            return f"field read started for {eid}"
    return "every field is stored"


def stop_read(event_id: int) -> bool:
    with _LOCK:
        if int(event_id) in _RUNS:
            _STOP[int(event_id)] = True
            return True
    return False


# ---------------------------------------------------------------- per-player summary
_SUMMARY_CACHE: Dict[int, Dict[str, Any]] = {}
_FACTS_CACHE: Dict[int, Dict[str, Any]] = {}
_BEST_CACHE: Dict[int, Dict[str, Any]] = {}


def player_summary(event_id: int) -> Dict[str, Any]:
    """{teams, league_size, read_at, players: {player_id: {owned, hltv_rating_points,
    hltv_win_points, matches, best: {...}}}} from the stored field: HLTV's
    actual rating and team points per player (the same in every team that
    has him), how many teams own him, and the team where he scored most —
    its role, boosters used (match order) and their points."""
    eid = int(event_id)
    st = status(eid)
    key = (st.get("teams_stored"), st.get("stored_at"))
    with _LOCK:
        hit = _SUMMARY_CACHE.get(eid)
        if hit and hit.get("_key") == key:
            return hit
    rows = teams(eid)
    players: Dict[int, Dict[str, Any]] = {}
    seen_matches: Dict[int, set] = {}
    for t in rows:
        for p in t.get("players") or []:
            pid = p.get("player_id")
            if pid is None:
                continue
            s = players.setdefault(int(pid), {"owned": 0, "hltv_rating_points": 0, "hltv_win_points": 0, "matches": 0, "best": None, "name": p.get("name")})
            s["owned"] += 1
            seen = seen_matches.setdefault(int(pid), set())
            for m in p.get("matches") or []:
                mk = m.get("match_url") or (m.get("opponent"), m.get("end_time"))
                if mk in seen:
                    continue
                seen.add(mk)
                s["hltv_rating_points"] += int(m.get("rating") or 0)
                s["hltv_win_points"] += int(m.get("team") or 0)
                s["matches"] += 1
            total = p.get("total")
            if total is not None and (s["best"] is None or int(total) > int(s["best"]["total"])):
                s["best"] = {
                    "total": int(total), "team_id": t["team_id"], "team_name": t.get("team_name"), "placement": t.get("placement"),
                    "role_id": p.get("role_id"), "role_name": p.get("role_name"),
                    "role_points": sum(int(m.get("role") or 0) for m in (p.get("matches") or [])) + sum(int(x.get("role") or 0) for x in (p.get("padding") or [])),
                    "boosters": [m.get("booster_name") for m in (p.get("matches") or [])],
                    "booster_points": sum(int(m.get("booster") or 0) for m in (p.get("matches") or [])),
                }
    out = {"_key": key, "teams": len(rows), "league_size": st.get("league_size"), "read_at": st.get("stored_at"), "running": st.get("running"), "players": players}
    with _LOCK:
        _SUMMARY_CACHE[eid] = out
    return out


# ---------------------------------------------------------------- per-match facts
# What the field tells about a player match by match. Every team that owns
# him sees the same rating and team points; the role points differ by the
# role the team gave him and the booster points by the booster it set — so
# across 14,000 teams most roles and boosters are observed for every match.
#
# Padding: a team that finishes the Swiss early (3-0, 3-1) plays fewer
# matches than a 3-2 team, and HLTV pads the missing rounds with the
# player's average so far — rating and role points truncated toward zero
# (int(mean): -0.67 -> 0, 1.5 -> 1), team points a flat 6, no booster.
# Checked on 29,000 padding items of PGL Masters Bucharest 2026. "So far"
# means the matches BEFORE the padded round: a group bracket pads a round
# the team skips mid-run (IEM Beijing 2026: MOUZ's LB Round 2 is the mean
# of its first two matches, not of all three), so each padding item is
# matched to the number of matches it averages.
PADDING_TEAM_POINTS = 6
BOOSTER_VALUE = 5


def _trunc_mean(values: List[int]) -> int:
    return int(sum(values) / len(values)) if values else 0


def _max_matching(triggered: Dict[str, set]) -> Dict[int, str]:
    """booster -> the match indexes it triggered in; returns match index ->
    booster with every booster used at most once (augmenting paths)."""
    owner: Dict[int, str] = {}

    def place(b: str, seen: set) -> bool:
        for mi in sorted(triggered[b]):
            if mi in seen:
                continue
            seen.add(mi)
            if mi not in owner or place(owner[mi], seen):
                owner[mi] = b
                return True
        return False

    for b in sorted(triggered, key=lambda k: (len(triggered[k]), k)):  # the boosters with the fewest options first
        place(b, set())
    return owner


def _match_id(url: Optional[str]) -> Optional[int]:
    import re

    m = re.search(r"/matches/(\d+)/", str(url or ""))
    return int(m.group(1)) if m else None


def match_facts(event_id: int) -> Dict[str, Any]:
    """{players: {player_id: {name, team, matches: [...], padding: {count,
    rating, team}, roles: [...], boosters: [...], rating_points, win_points,
    max: {...}}}} from the stored field: per match HLTV's rating and team
    points, the points of every role seen and whether each booster seen
    triggered (0 or 5); the theoretical maximum = rating + team + the best
    complete role + a different booster for every match one triggered in."""
    from backend.services.my_team import ROLE_INDEX_BY_NAME

    eid = int(event_id)
    st = status(eid)
    key = (st.get("teams_stored"), st.get("stored_at"))
    with _LOCK:
        hit = _FACTS_CACHE.get(eid)
        if hit and hit.get("_key") == key:
            return hit
    rows = teams(eid)
    players: Dict[int, Dict[str, Any]] = {}
    for t in rows:
        read_at = float(t.get("read_at") or 0.0)
        for p in t.get("players") or []:
            pid = p.get("player_id")
            if pid is None:
                continue
            s = players.setdefault(int(pid), {"player_id": int(pid), "name": p.get("name"), "team": p.get("team"), "matches": {}, "padding": 0})
            role = p.get("role_name")
            for m in p.get("matches") or []:
                mk = m.get("match_url") or f"{m.get('opponent')}@{m.get('end_time')}"
                e = s["matches"].get(mk)
                if e is None:
                    e = s["matches"][mk] = {
                        "match_url": m.get("match_url") or None, "match_id": _match_id(m.get("match_url")), "opponent": m.get("opponent"), "result": m.get("result"),
                        "end_time": m.get("end_time"), "rating": 0, "team": 0, "_at": -1.0, "roles": {}, "boosters": {}, "_role_at": {}, "_boost_at": {},
                    }
                if read_at >= e["_at"]:  # the latest read's figures: scoring catches up in the hour after a match
                    e["_at"], e["rating"], e["team"] = read_at, int(m.get("rating") or 0), int(m.get("team") or 0)
                # role and booster points as of the latest read (HLTV revises them
                # after a match; the highest ever seen would keep a stale figure)
                if role:
                    at, _v = e["_role_at"].get(role, (-1.0, None))
                    if read_at >= at:
                        e["_role_at"][role] = (read_at, int(m.get("role") or 0))
                        e["roles"][role] = int(m.get("role") or 0) if read_at > at else max(int(e["roles"].get(role, -99)), int(m.get("role") or 0))
                b = m.get("booster_name")
                if b:
                    at = e["_boost_at"].get(b, -1.0)
                    if read_at >= at:
                        e["_boost_at"][b] = read_at
                        e["boosters"][b] = int(m.get("booster") or 0) if read_at > at else max(int(e["boosters"].get(b, 0)), int(m.get("booster") or 0))
            s["padding"] = max(int(s["padding"]), len(p.get("padding") or []))
            if p.get("padding") and read_at >= float(s.get("_pad_at", -1.0)):
                s["_pad_at"], s["pad_items"] = read_at, (role, list(p["padding"]))

    out: Dict[int, Dict[str, Any]] = {}
    for pid, s in players.items():
        ms = sorted(s["matches"].values(), key=lambda e: (e.get("end_time") or 0.0, e.get("match_url") or ""))
        n, pad = len(ms), int(s["padding"])
        ratings = [int(e["rating"]) for e in ms]
        wins = [int(e["team"]) for e in ms]
        # how many matches each padding round averages: the latest k whose
        # truncated means give HLTV's own item (rating, and role when known)
        pad_role_name, pad_items = s.get("pad_items") or (None, [])
        ks: List[int] = []
        for item in pad_items[:pad]:
            fit = [
                k for k in range(1, n + 1)
                if _trunc_mean(ratings[:k]) == int(item.get("rating") or 0)
                and (item.get("role") is None or not pad_role_name or _trunc_mean([int(v) for v in (e["roles"].get(pad_role_name) for e in ms[:k]) if v is not None]) == int(item.get("role") or 0))
            ]
            ks.append(fit[-1] if fit else n)
        ks += [n] * (pad - len(ks))
        pad_rating = _trunc_mean(ratings[:ks[0]]) if pad else 0
        roles = []
        for r in sorted({r for e in ms for r in e["roles"]}, key=lambda r: (ROLE_INDEX_BY_NAME.get(str(r).lower(), 99), r)):
            vals = [e["roles"].get(r) for e in ms]
            seen = [int(v) for v in vals if v is not None]
            pads = [_trunc_mean([int(v) for v in vals[:k] if v is not None]) for k in ks]
            pad_role = pads[0] if pads else 0
            roles.append({
                "role_name": r, "role_index": ROLE_INDEX_BY_NAME.get(str(r).lower()), "per_match": vals, "padding": pad_role,
                "total": sum(seen) + sum(pads), "complete": len(seen) == n, "seen": len(seen),
            })
        complete = [r for r in roles if r["complete"]]
        best_role = max(complete, key=lambda r: (r["total"], -(r["role_index"] if r["role_index"] is not None else 99))) if complete else None
        booster_names = sorted({b for e in ms for b in e["boosters"]})
        triggered = {b: {i for i, e in enumerate(ms) if int(e["boosters"].get(b) or 0) > 0} for b in booster_names}
        plan = _max_matching({b: v for b, v in triggered.items() if v})
        boosters = [{"booster_name": b, "per_match": [e["boosters"].get(b) for e in ms], "triggers": len(triggered[b])} for b in booster_names]
        rating_total = sum(ratings) + sum(_trunc_mean(ratings[:k]) for k in ks)
        win_total = sum(wins) + pad * PADDING_TEAM_POINTS
        booster_total = BOOSTER_VALUE * len(plan)
        matches = []
        for i, e in enumerate(ms):
            matches.append({
                "match_url": e["match_url"], "match_id": e["match_id"], "opponent": e["opponent"], "result": e["result"], "end_time": e["end_time"],
                "rating": int(e["rating"]), "team": int(e["team"]),
                "role": e["roles"].get(best_role["role_name"]) if best_role else None, "booster": plan.get(i),
                "triggered": sorted(b for b in booster_names if i in triggered[b]), "roles_seen": len(e["roles"]), "boosters_seen": len(e["boosters"]),
            })
        out[int(pid)] = {
            "player_id": int(pid), "name": s.get("name"), "team": s.get("team"), "matches": matches,
            "padding": {"count": pad, "rating": pad_rating, "team": PADDING_TEAM_POINTS, "role": best_role["padding"] if best_role else 0},
            "roles": roles, "boosters": boosters, "rating_points": rating_total, "win_points": win_total,
            "max": {
                "total": rating_total + win_total + (best_role["total"] if best_role else 0) + booster_total,
                "role_name": best_role["role_name"] if best_role else None, "role_index": best_role["role_index"] if best_role else None,
                "role_points": best_role["total"] if best_role else 0, "booster_points": booster_total, "boosters": [plan.get(i) for i in range(n)],
            },
        }
    res = {"_key": key, "teams": len(rows), "players": out}
    with _LOCK:
        _FACTS_CACHE[eid] = res
    return res


# ---------------------------------------------------------------- best team in hindsight
# HLTV's roster rules, checked on the field itself (PGL Masters Bucharest
# 2026, 14,582 teams): five players, a 1,000,000 budget (the dearest lineup
# costs exactly that), at most two players of one team (no lineup carries
# three), one player per role, each booster once per event.
BUDGET = 1_000_000
MAX_PER_TEAM = 2
_CANDIDATES = 2000  # rosters taken from the kernel per pass, by upper bound
_MAX_CANDIDATES = 250_000


def best_team(event_id: int, budget: int = BUDGET, max_per_team: int = MAX_PER_TEAM) -> Dict[str, Any]:
    """The roster that would have scored most with hindsight: rating and win
    points as scored, the best assignment of distinct roles, and a different
    booster on every (player, match) slot it triggered in. Exact: the roster
    kernel lists legal rosters by an upper bound (each player's own maximum)
    and they are valued exactly, best first, until the next bound cannot beat
    the best value found."""
    import numpy as np

    from backend.data.player_db import get_player
    from backend.services import roster_kernels as rk
    from backend.services.swiss_booster_assignment import _max_weight_assignment

    eid = int(event_id)
    facts = match_facts(eid)
    key = (facts.get("_key"), int(budget), int(max_per_team))
    with _LOCK:
        hit = _BEST_CACHE.get(eid)
        if hit and hit.get("_key") == key:
            return hit
    from backend.data.event_db import get_event_price_map

    event_prices = get_event_price_map(eid)  # this event's prices, not the last import's
    rows: List[Dict[str, Any]] = []
    role_names: List[str] = []
    for pid, f in (facts.get("players") or {}).items():
        p = get_player(int(pid)) or {}
        price = event_prices.get(int(pid))
        if not price:
            continue
        roles = {r["role_name"]: r for r in f.get("roles") or [] if r.get("complete")}
        for r in roles:
            if r not in role_names:
                role_names.append(r)
        slots = {i: set(m.get("triggered") or []) for i, m in enumerate(f.get("matches") or []) if m.get("triggered")}
        base = int(f.get("rating_points") or 0) + int(f.get("win_points") or 0)
        rows.append({
            "player_id": int(pid), "name": f.get("name") or p.get("name"), "team": f.get("team") or "", "price": int(price), "base": base,
            "roles": roles, "slots": slots, "facts": f,
            "bound": base + max([int(r["total"]) for r in roles.values()] + [0]) + BOOSTER_VALUE * len(slots),
        })
    from backend.services.my_team import ROLE_INDEX_BY_NAME

    role_names.sort(key=lambda r: (ROLE_INDEX_BY_NAME.get(str(r).lower(), 99), r))
    empty = {"_key": key, "players": [], "total": 0, "cost": 0, "budget": int(budget), "max_per_team": int(max_per_team)}
    if len(rows) < 5:
        with _LOCK:
            _BEST_CACHE[eid] = empty
        return empty
    team_ids: Dict[str, int] = {}
    team_of = np.array([team_ids.setdefault(r["team"], len(team_ids)) for r in rows], dtype=np.int64)
    scores = np.array([float(r["bound"]) for r in rows], dtype=np.float64)
    prices = np.array([r["price"] for r in rows], dtype=np.int64)
    NO = -1e9

    def value(idx: List[int]) -> Dict[str, Any]:
        chosen = [rows[i] for i in idx]
        n_roles = len(role_names)
        weights = []
        for a, r in enumerate(chosen):  # 5 rows x (roles + one 'no role' column per player)
            w = [float(r["roles"][name]["total"]) if name in r["roles"] else NO for name in role_names]
            w += [0.0 if b == a else NO for b in range(len(chosen))]
            weights.append(w)
        role_of: Dict[int, Optional[str]] = {}
        role_pts = 0
        for a, c, w in _max_weight_assignment(weights):
            role_of[a] = role_names[c] if c < n_roles and w > NO / 2 else None
            role_pts += int(w) if w > NO / 2 else 0
        triggered: Dict[str, set] = {}
        for a, r in enumerate(chosen):
            for mi, names in r["slots"].items():
                for b in names:
                    triggered.setdefault(b, set()).add((a, mi))
        plan = _max_matching(triggered)
        total = sum(r["base"] for r in chosen) + role_pts + BOOSTER_VALUE * len(plan)
        return {"total": int(total), "role_pts": int(role_pts), "booster_pts": BOOSTER_VALUE * len(plan), "role_of": role_of, "plan": plan, "idx": list(idx)}

    best: Optional[Dict[str, Any]] = None
    k = _CANDIDATES
    tried = 0
    while True:
        cands = rk.top_rosters_one(scores, prices, team_of, int(budget), int(max_per_team), int(k))
        settled = False
        for bound_sum, roster in cands:
            if best is not None and bound_sum <= best["total"]:
                settled = True
                break
            tried += 1
            v = value([int(i) for i in roster])
            if best is None or v["total"] > best["total"]:
                best = v
        if settled or len(cands) < k or k >= _MAX_CANDIDATES:
            break
        k = min(k * 5, _MAX_CANDIDATES)
    if best is None:
        with _LOCK:
            _BEST_CACHE[eid] = empty
        return empty

    from backend.services import live_state

    entry = live_state.live_event(eid, fetch_if_missing=False) or {}
    hltv_ids = {str(v.get("name") or ""): v.get("hltv_team_id") for v in (entry.get("teams") or {}).values()}
    players_out = []
    for a, i in enumerate(best["idx"]):
        r = rows[i]
        f = r["facts"]
        role = best["role_of"].get(a)
        role_row = r["roles"].get(role) if role else None
        matches = []
        for mi, m in enumerate(f.get("matches") or []):
            b = best["plan"].get((a, mi))
            matches.append({
                "opponent": m.get("opponent"), "result": m.get("result"), "match_url": m.get("match_url"), "rating": m.get("rating"), "team": m.get("team"),
                "role": (role_row["per_match"][mi] if role_row else None), "booster": b, "booster_points": BOOSTER_VALUE if b else 0,
            })
        booster_pts = sum(m["booster_points"] for m in matches)
        role_pts = int(role_row["total"]) if role_row else 0
        players_out.append({
            "player_id": r["player_id"], "name": r["name"], "team": r["team"], "hltv_team_id": hltv_ids.get(r["team"]), "price": r["price"],
            "role_name": role, "role_index": role_row["role_index"] if role_row else None, "role_points": role_pts,
            "rating_points": int(f.get("rating_points") or 0), "win_points": int(f.get("win_points") or 0), "booster_points": booster_pts,
            "total": r["base"] + role_pts + booster_pts, "matches": matches, "padding": f.get("padding"),
        })
    players_out.sort(key=lambda p: -p["total"])
    out = {
        "_key": key, "players": players_out, "total": int(best["total"]), "cost": int(sum(p["price"] for p in players_out)),
        "budget": int(budget), "max_per_team": int(max_per_team),
        "rating_points": sum(p["rating_points"] for p in players_out), "win_points": sum(p["win_points"] for p in players_out),
        "role_points": int(best["role_pts"]), "booster_points": int(best["booster_pts"]), "rosters_valued": tried,
    }
    with _LOCK:
        _BEST_CACHE[eid] = out
    return out
