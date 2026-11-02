"""The whole HLTV fantasy season leaderboard, every user, not just the top
ten and the rows around one account.

The season feed (GET /fantasy/json, services/fantasy_season) pages the
standings with ?offset=N, OFFSET_STEP rows a page. The standings are the same
for every account, so one read under the scraper's login serves every app
user: every page fetched inside a browser session (the field reader's
in-session fetch, services/fantasy_field._fetch_feeds: a few at a time,
429s retried), PAGES_PER_SESSION per session so the shared browser is handed
back between chunks, rows stored as they come (table season_standings, one
row per season and user). ~3,300 pages for 33k users: about 40 minutes.
Started by the nightly batch after the season read (scheduler tail step
`season`) and by POST /admin/season/standings/read.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from backend.data.db import connect

logger = logging.getLogger(__name__)

OFFSET_STEP = 10  # rows per page of the feed
PAGES_PER_SESSION = 80
PAUSE_BETWEEN_SESSIONS = 1.0
_LOCK = threading.Lock()
_RUN: Dict[str, Any] = {}
_READY = False


def _ensure() -> None:
    global _READY
    if _READY:
        return
    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS season_standings (
                season        TEXT NOT NULL,
                user_id       INTEGER NOT NULL,
                placement     INTEGER,
                username      TEXT,
                leagues       INTEGER,
                live_points   INTEGER,
                season_points INTEGER,
                prize         TEXT,
                read_at       REAL NOT NULL,
                PRIMARY KEY (season, user_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_season_standings_place ON season_standings (season, placement)")
        conn.commit()
    finally:
        conn.close()
    _READY = True


def _url(offset: int) -> str:
    from backend.services.fantasy_season import SEASON_URL

    return SEASON_URL if not offset else f"{SEASON_URL}?offset={int(offset)}"


def _save(season: str, rows: List[Dict[str, Any]], at: float) -> None:
    if not rows:
        return
    _ensure()
    conn = connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO season_standings (season, user_id, placement, username, leagues, live_points, season_points, prize, read_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(season, int(r["user_id"]), r.get("placement"), r.get("username"), r.get("leagues"), r.get("live_points"), r.get("season_points"), r.get("prize"), at)
             for r in rows if r.get("user_id") is not None],
        )
        conn.commit()
    finally:
        conn.close()


def _parse_page(text: str) -> Optional[Dict[str, Any]]:
    from backend.services import fantasy_season

    data = fantasy_season._json_from_page(text)
    if not data or not isinstance(data.get("seasonLeaderboardData"), dict):
        return None
    sl = data["seasonLeaderboardData"]
    return {"season": str(data.get("seasonName") or ""), "total": fantasy_season._int(sl.get("totalTeamsInSeason")),
            "rows": [fantasy_season._row(p) for p in sl.get("seasonStanding") or []]}


def _fetch(offsets: List[int]) -> List[Optional[Dict[str, Any]]]:
    from backend.services.fantasy_field import _fetch_feeds
    from backend.services.hltv_browser import run_hltv_browser_session
    from backend.services.my_team import ENTRY_URL

    urls = [_url(o) for o in offsets]
    answers = run_hltv_browser_session(ENTRY_URL, lambda d: _fetch_feeds(d, urls), wait_text=None, timeout_ms=60000) or []
    out: List[Optional[Dict[str, Any]]] = []
    for ans in list(answers) + [None] * (len(urls) - len(answers)):
        ok = ans and int(ans.get("status") or 0) == 200
        out.append(_parse_page(str(ans.get("text") or "")) if ok else None)
    return out


def _read() -> None:
    run = _RUN
    try:
        first = _fetch([0])[0]
        if not first or not first["total"]:
            raise RuntimeError("the season feed did not answer")
        season, total = first["season"], int(first["total"])
        run.update(season=season, total=total)
        at = time.time()
        _save(season, first["rows"], at)
        run["rows"] = len(first["rows"])
        pending = list(range(OFFSET_STEP, total, OFFSET_STEP))
        failed: List[int] = []
        for attempt in range(2):
            queue, pending = pending, []
            for i in range(0, len(queue), PAGES_PER_SESSION):
                if run.get("stop"):
                    raise RuntimeError("stopped")
                chunk = queue[i:i + PAGES_PER_SESSION]
                for off, page in zip(chunk, _fetch(chunk)):
                    if page and page["rows"]:
                        _save(season, page["rows"], at)
                        run["rows"] = int(run.get("rows") or 0) + len(page["rows"])
                    else:
                        pending.append(off)
                run["pages_done"] = int(run.get("pages_done") or 0) + len(chunk)
                time.sleep(PAUSE_BETWEEN_SESSIONS)
            if not pending:
                break
        failed = pending
        # rows of users who dropped out since the last read stay out of this read's season copy
        conn = connect()
        try:
            conn.execute("DELETE FROM season_standings WHERE season = ? AND read_at < ?", (season, at))
            conn.commit()
        finally:
            conn.close()
        run.update(status="done", finished_at=time.time(), failed_pages=len(failed))
        logger.info("Season standings read: %s rows of %s (%s pages failed) in %.0f s", run.get("rows"), total, len(failed), time.time() - run["started_at"])
    except Exception as exc:  # noqa: BLE001
        run.update(status="error", message=str(exc)[:200], finished_at=time.time())
        logger.warning("Season standings read failed", exc_info=True)


def start() -> str:
    """Start the full read in the background unless one is running."""
    with _LOCK:
        if _RUN.get("status") == "running":
            return "season standings read already running"
        _RUN.clear()
        _RUN.update(status="running", started_at=time.time(), rows=0, pages_done=0)
    threading.Thread(target=_read, name="season-standings", daemon=True).start()
    return "season standings read started"


def stop() -> bool:
    if _RUN.get("status") != "running":
        return False
    _RUN["stop"] = True
    return True


def latest_season() -> Optional[str]:
    _ensure()
    conn = connect()
    try:
        row = conn.execute("SELECT season FROM season_standings GROUP BY season ORDER BY MAX(read_at) DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return str(row["season"]) if row else None


def status() -> Dict[str, Any]:
    season = latest_season()
    stored = 0
    read_at = None
    if season:
        conn = connect()
        try:
            r = conn.execute("SELECT COUNT(*) AS n, MAX(read_at) AS at FROM season_standings WHERE season = ?", (season,)).fetchone()
            stored, read_at = int(r["n"] or 0), r["at"]
        finally:
            conn.close()
    return {**{k: v for k, v in _RUN.items() if k != "stop"}, "running": _RUN.get("status") == "running", "season": season, "stored": stored, "read_at": read_at}


def page(season: Optional[str], q: str = "", page_no: int = 0, size: int = 50, me_user_id: Optional[int] = None) -> Dict[str, Any]:
    """One page of the stored standings by placement, filtered by username;
    `me_page` is the page holding the given user (unfiltered)."""
    season = season or latest_season()
    if not season:
        return {"rows": [], "total": 0, "page": 0, "size": size, "season": None}
    size = max(1, min(200, int(size)))
    where, args = "season = ?", [season]
    if q.strip():
        where += " AND username LIKE ? ESCAPE '\\'"
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{needle}%")
    conn = connect()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) FROM season_standings WHERE {where}", args).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM season_standings WHERE {where} ORDER BY placement, username COLLATE NOCASE LIMIT ? OFFSET ?",
            args + [size, max(0, int(page_no)) * size],
        ).fetchall()
        me_page = None
        if me_user_id is not None:
            r = conn.execute("SELECT placement, username FROM season_standings WHERE season = ? AND user_id = ?", (season, int(me_user_id))).fetchone()
            if r:
                before = int(conn.execute(
                    "SELECT COUNT(*) FROM season_standings WHERE season = ? AND (placement < ? OR (placement = ? AND username < ? COLLATE NOCASE))",
                    (season, r["placement"], r["placement"], r["username"]),
                ).fetchone()[0])
                me_page = before // size
        read_at = conn.execute("SELECT MAX(read_at) FROM season_standings WHERE season = ?", (season,)).fetchone()[0]
    finally:
        conn.close()
    out = []
    for r in rows:
        d = {k: r[k] for k in ("placement", "user_id", "username", "leagues", "live_points", "season_points", "prize")}
        d["is_user"] = me_user_id is not None and int(r["user_id"]) == int(me_user_id)
        out.append(d)
    return {"rows": out, "total": total, "page": int(page_no), "size": size, "season": season, "me_page": me_page, "read_at": read_at, "running": _RUN.get("status") == "running"}
