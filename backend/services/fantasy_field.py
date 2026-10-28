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
            INSERT INTO fantasy_field_runs (event_id, league_id, started_at, finished_at, status, scanned, found, errors, lowest_id, highest_id, league_size, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, started_at) DO UPDATE SET
                finished_at = excluded.finished_at, status = excluded.status, scanned = excluded.scanned, found = excluded.found, errors = excluded.errors,
                lowest_id = excluded.lowest_id, highest_id = excluded.highest_id, league_size = excluded.league_size, message = excluded.message
            """,
            (int(run["event_id"]), int(run["league_id"]), float(run["started_at"]), run.get("finished_at"), str(run["status"]), int(run["scanned"]),
             int(run["found"]), int(run["errors"]), run.get("lowest_id"), run.get("highest_id"), run.get("league_size"), str(run.get("message") or "")),
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
    for k in ("league_id", "status", "scanned", "found", "errors", "lowest_id", "highest_id", "league_size", "started_at", "finished_at", "message", "statuses"):
        if k in run:
            out[k] = run[k]
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


def _read(event_id: int, league_id: int, seed_team_id: int, max_ids: int) -> None:
    from backend.services.hltv_browser import run_hltv_browser_session
    from backend.services.my_team import ENTRY_URL

    eid, lid = int(event_id), int(league_id)
    run = _RUNS[eid]
    base = f"https://www.hltv.org/fantasy/{eid}/league/{lid}/overview/"
    lo, hi = int(seed_team_id), int(seed_team_id) + 1  # next ids to try downward (lo-1...) and upward (hi...)
    down_misses = up_misses = 0
    down_open = up_open = True
    first = True
    unresolved: List[int] = []  # ids HLTV never answered about (kept for a last pass)

    def chunk_ids() -> tuple:
        nonlocal lo, hi, first
        if first:
            first = False
            ids = list(range(int(seed_team_id), int(seed_team_id) + CHUNK_IDS))
            hi = int(seed_team_id) + CHUNK_IDS
            return "up", ids
        if up_open and (not down_open or (hi - int(seed_team_id)) <= (int(seed_team_id) - lo)):
            ids = list(range(hi, hi + CHUNK_IDS))
            hi += CHUNK_IDS
            return "up", ids
        ids = list(range(lo - CHUNK_IDS, lo))
        lo -= CHUNK_IDS
        return "down", ids

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
            time.sleep(5.0)
            return len(ids)
        nonlocal up_misses, down_misses, up_open, down_open
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
                if tid not in unresolved:
                    unresolved.append(int(tid))
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
            up_misses = (up_misses + miss_run) if not found_rows else miss_run
            if up_misses >= STOP_AFTER_MISSES:
                up_open = False
        elif direction == "down":
            down_misses = (down_misses + miss_run) if not found_rows else miss_run
            if down_misses >= STOP_AFTER_MISSES:
                down_open = False
        _save_teams(eid, lid, found_rows)
        return left

    try:
        while (down_open or up_open) and run["scanned"] < max_ids and not _STOP.get(eid):
            direction, ids = chunk_ids()
            scan(ids, direction)
            if run.get("league_size") and run["found"] >= int(run["league_size"]):
                run["message"] = "every team of the league found"
                break
            _save_run(run)
            time.sleep(PAUSE_BETWEEN_CHUNKS)
        # a last pass over what HLTV never answered about
        passes = 0
        while unresolved and passes < 3 and not _STOP.get(eid):
            passes += 1
            retry, unresolved = unresolved[:CHUNK_IDS], unresolved[CHUNK_IDS:]
            run["scanned"] -= len(retry)  # counted again by scan()
            time.sleep(10.0)
            scan(retry, None)
        run["status"] = "stopped" if _STOP.get(eid) else "done"
        if not run.get("message"):
            run["message"] = f"scan ended: ids {run.get('lowest_id')}–{run.get('highest_id')}, {run['found']} teams"
        if unresolved:
            run["message"] += f" | {len(unresolved)} ids unanswered"
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
        }
        _RUNS[eid] = run
        _STOP.pop(eid, None)
    _save_run(run)
    threading.Thread(target=_read, args=(eid, int(league_id), int(seed_team_id), int(max_ids)), name=f"fantasy-field-{eid}", daemon=True).start()
    return {"status": "started", "event_id": eid, "league_id": int(league_id), "seed_team_id": int(seed_team_id), "max_ids": int(max_ids)}


def stop_read(event_id: int) -> bool:
    with _LOCK:
        if int(event_id) in _RUNS:
            _STOP[int(event_id)] = True
            return True
    return False


# ---------------------------------------------------------------- per-player summary
_SUMMARY_CACHE: Dict[int, Dict[str, Any]] = {}
_FACTS_CACHE: Dict[int, Dict[str, Any]] = {}


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
# Checked on 29,000 padding items of PGL Masters Bucharest 2026.
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
                        "end_time": m.get("end_time"), "rating": 0, "team": 0, "_at": -1.0, "roles": {}, "boosters": {},
                    }
                if read_at >= e["_at"]:  # the latest read's figures: scoring catches up in the hour after a match
                    e["_at"], e["rating"], e["team"] = read_at, int(m.get("rating") or 0), int(m.get("team") or 0)
                if role:
                    e["roles"][role] = max(e["roles"].get(role, -99), int(m.get("role") or 0))
                b = m.get("booster_name")
                if b:
                    e["boosters"][b] = max(e["boosters"].get(b, 0), int(m.get("booster") or 0))
            s["padding"] = max(int(s["padding"]), len(p.get("padding") or []))

    out: Dict[int, Dict[str, Any]] = {}
    for pid, s in players.items():
        ms = sorted(s["matches"].values(), key=lambda e: (e.get("end_time") or 0.0, e.get("match_url") or ""))
        n, pad = len(ms), int(s["padding"])
        ratings = [int(e["rating"]) for e in ms]
        wins = [int(e["team"]) for e in ms]
        pad_rating = _trunc_mean(ratings) if pad else 0
        roles = []
        for r in sorted({r for e in ms for r in e["roles"]}, key=lambda r: (ROLE_INDEX_BY_NAME.get(str(r).lower(), 99), r)):
            vals = [e["roles"].get(r) for e in ms]
            seen = [int(v) for v in vals if v is not None]
            pad_role = _trunc_mean(seen) if (pad and seen) else 0
            roles.append({
                "role_name": r, "role_index": ROLE_INDEX_BY_NAME.get(str(r).lower()), "per_match": vals, "padding": pad_role,
                "total": sum(seen) + pad * pad_role, "complete": len(seen) == n, "seen": len(seen),
            })
        complete = [r for r in roles if r["complete"]]
        best_role = max(complete, key=lambda r: (r["total"], -(r["role_index"] if r["role_index"] is not None else 99))) if complete else None
        booster_names = sorted({b for e in ms for b in e["boosters"]})
        triggered = {b: {i for i, e in enumerate(ms) if int(e["boosters"].get(b) or 0) > 0} for b in booster_names}
        plan = _max_matching({b: v for b, v in triggered.items() if v})
        boosters = [{"booster_name": b, "per_match": [e["boosters"].get(b) for e in ms], "triggers": len(triggered[b])} for b in booster_names]
        rating_total = sum(ratings) + pad * pad_rating
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
