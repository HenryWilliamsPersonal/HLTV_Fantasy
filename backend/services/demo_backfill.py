"""Back-fill of per-player per-map demo stats for the trigger model.

Every match in the last year with at least one top-30 team, newest first:
download its demo archive through the app's browser (demo_fetch), unpack it,
rebuild each map's role/booster stats (demo_triggers), match the demo's player
names to HLTV player ids from the stored scoreboard, store one row per player
per map, and delete the files. A match is done once (demo_matches); a failed
one is retried on a later night. Runs in the background, a few hundred
matches a night (the nightly tail starts it, see scheduler).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.data.db import connect

logger = logging.getLogger(__name__)

TOP_RANK = 30
DAYS_BACK = 365
NIGHT_LIMIT = 300
PAUSE_S = 10.0
RETRY_AFTER_S = 3 * 86400.0
TAR = r"C:\Windows\System32\tar.exe"

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"running": False}
_STOP = threading.Event()


def _ensure() -> None:
    conn = connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS demo_matches (
                match_id   INTEGER PRIMARY KEY,
                status     TEXT NOT NULL,
                demo_id    INTEGER,
                bytes      INTEGER,
                maps       INTEGER,
                error      TEXT,
                updated_at REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS demo_map_stats (
                match_id   INTEGER NOT NULL,
                map_index  INTEGER NOT NULL,
                map_name   TEXT,
                player_id  INTEGER,
                steam_id   TEXT NOT NULL,
                demo_name  TEXT,
                rounds     INTEGER,
                stats_json TEXT NOT NULL,
                PRIMARY KEY (match_id, map_index, steam_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS demo_match_ratings (
                match_id  INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                total     REAL,
                ct        REAL,
                t         REAL,
                PRIMARY KEY (match_id, player_id)
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def _rank(v) -> int:
    try:
        r = int(v)
    except (TypeError, ValueError):
        return 999
    return r if r > 0 else 999


def candidates(limit: int, match_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """Matches still to do, newest first (or just `match_ids`, done or not)."""
    _ensure()
    since = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    conn = connect()
    try:
        done = {
            int(r["match_id"]): (r["status"], float(r["updated_at"]))
            for r in conn.execute("SELECT match_id, status, updated_at FROM demo_matches")
        }
        rows = conn.execute(
            """SELECT match_id, match_date, hltv_rank_1, hltv_rank_2, maps_json, map_player_stats_json
               FROM hltv_results WHERE match_date >= ? AND map_player_stats_json IS NOT NULL
               ORDER BY match_date DESC, match_id DESC""",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    now = time.time()
    for r in rows:
        mid = int(r["match_id"])
        if match_ids is not None:
            if mid in match_ids:
                out.append(dict(r))
            continue
        if min(_rank(r["hltv_rank_1"]), _rank(r["hltv_rank_2"])) > TOP_RANK:
            continue
        st = done.get(mid)
        if st and (st[0] in ("done", "no_demo") or now - st[1] < RETRY_AFTER_S):
            continue
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _player_ids(scoreboard: List[Dict[str, Any]], demo_names: Dict[str, str]) -> Dict[str, Optional[int]]:
    """steamid -> HLTV player id, by name: exact after normalising, else a
    prefix either way (demo names carry clan tags / suffixes, e.g. 'Techno4K')."""
    by = {_norm(p.get("player")): int(p["player_id"]) for p in scoreboard if p.get("player_id")}
    out: Dict[str, Optional[int]] = {}
    for sid, name in demo_names.items():
        n = _norm(name)
        pid = by.get(n)
        if pid is None:
            hits = [v for k, v in by.items() if k and n and (n.startswith(k) or k.startswith(n))]
            pid = hits[0] if len(hits) == 1 else None
        out[sid] = pid
    return out


_MAP_FILE = re.compile(r"-m(\d+)-([a-z0-9_]+)\.dem$", re.I)


_STATS_LINK = re.compile(r"/stats/matches/mapstatsid/(\d+)/([a-z0-9-]+)")


def _page(url: str, fresh: bool = False) -> str:
    """A page's HTML: the stored snapshot, else (or when `fresh`) a browser read."""
    from backend.data.page_snapshots import get_page_snapshot
    from backend.services.hltv_browser import fetch_hltv_html

    if not fresh:
        snap = get_page_snapshot(url)
        if snap and snap.get("html"):
            return snap["html"]
    return fetch_hltv_html(url) or ""


def map_pages(match_id: int, n_maps: int) -> Dict[str, Dict[str, Any]]:
    """{normalised map name: parsed map stats page} for the match's maps. The
    match page also links older meetings of the same teams, so per map name
    the newest stats page wins; a stored match page from before the last map
    finished is read again."""
    from backend.services import demo_fetch, hltv_mapstats

    url = demo_fetch.match_url(int(match_id))
    if not url:
        return {}
    pages: Dict[str, Dict[str, Any]] = {}
    for fresh in (False, True):
        links = sorted(set(_STATS_LINK.findall(_page(url, fresh=fresh))), key=lambda x: int(x[0]))
        pages = {}
        for sid_, slug in links:
            pg = hltv_mapstats.parse(_page(f"https://www.hltv.org/stats/matches/mapstatsid/{sid_}/{slug}"))
            if pg.get("total") and pg.get("map"):
                pages[_norm(pg["map"])] = pg
        if len(pages) >= n_maps:
            break
    return pages


def process(match: Dict[str, Any]) -> Dict[str, Any]:
    from backend.services import demo_fetch
    from backend.services.demo_triggers import compute_map, load_map, with_page

    mid = int(match["match_id"])
    try:
        got = demo_fetch.download(mid)
    except Exception:
        shutil.rmtree(demo_fetch.DEMO_DIR / str(mid), ignore_errors=True)  # a partial download
        raise
    folder = Path(got["path"]).parent
    try:
        subprocess.run([TAR, "-xf", got["path"], "-C", str(folder)], check=True, capture_output=True, timeout=900)
        Path(got["path"]).unlink(missing_ok=True)
        maps = json.loads(match.get("maps_json") or "[]")
        boards = json.loads(match.get("map_player_stats_json") or "{}")
        rows = []
        dems = sorted(folder.glob("*.dem"))
        pages = map_pages(mid, len(dems))
        for dem in dems:
            m = _MAP_FILE.search(dem.name)
            idx = int(m.group(1)) - 1 if m else dems.index(dem)
            # the map by its name in the file (m2-dust2), else by position
            by_name = [i for i, mp in enumerate(maps) if m and _norm(mp.get("map")) == _norm(m.group(2))]
            if by_name:
                idx = by_name[0]
            map_name = maps[idx]["map"] if idx < len(maps) else (m.group(2).title() if m else None)
            raw = load_map(str(dem))
            page = pages.get(_norm(map_name)) or pages.get(_norm(m.group(2)) if m else "")
            if not page:
                raise RuntimeError(f"No HLTV stats page for map {map_name} of match {mid}")
            parsed = with_page(raw, page)
            if parsed["trade_error"]:
                logger.warning("Demo back-fill: match %s %s: traded deaths off by %s vs HLTV at the best phase", mid, map_name, parsed["trade_error"])
            ids = _player_ids(boards.get(map_name) or [], parsed["names"])
            for sid, stats in parsed["players"].items():
                pid = stats.get("hltv_player_id") or ids.get(sid)
                rows.append((mid, idx, map_name, pid, sid, parsed["names"].get(sid), parsed["rounds"], json.dumps(stats)))
            dem.unlink(missing_ok=True)
        # all-maps Rating 3.0 per player (Stathunter / Noob total, Attacker T,
        # Defender CT), from the match page
        from backend.services import hltv_mapstats

        ratings = hltv_mapstats.match_ratings(_page(demo_fetch.match_url(mid) or ""))
        conn = connect()
        try:
            conn.execute("DELETE FROM demo_map_stats WHERE match_id = ?", (mid,))
            conn.executemany("INSERT INTO demo_map_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.execute("DELETE FROM demo_match_ratings WHERE match_id = ?", (mid,))
            conn.executemany(
                "INSERT INTO demo_match_ratings VALUES (?, ?, ?, ?, ?)",
                [(mid, pid, r.get("total"), r.get("ct"), r.get("t")) for pid, r in ratings.items()],
            )
            conn.execute(
                "INSERT OR REPLACE INTO demo_matches VALUES (?, 'done', ?, ?, ?, NULL, ?)",
                (mid, got["demo_id"], got["bytes"], len(dems), time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        unmatched = sum(1 for r in rows if r[3] is None)
        return {"maps": len(dems), "players": len(rows), "unmatched": unmatched}
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _mark(mid: int, status: str, error: str) -> None:
    conn = connect()
    try:
        conn.execute("INSERT OR REPLACE INTO demo_matches VALUES (?, ?, NULL, NULL, NULL, ?, ?)", (mid, status, error[:500], time.time()))
        conn.commit()
    finally:
        conn.close()


def _run(limit: int, match_ids: Optional[List[int]] = None) -> None:
    todo = candidates(limit, match_ids)
    _STATE.update(total=len(todo), done=0, failed=0, current=None)
    logger.info("Demo back-fill: %s matches to do", len(todo))
    for match in todo:
        if _STOP.is_set():
            break
        mid = int(match["match_id"])
        _STATE["current"] = mid
        try:
            out = process(match)
            _STATE["done"] += 1
            logger.info("Demo back-fill: match %s stored (%s)", mid, out)
        except Exception as exc:  # noqa: BLE001 - recorded, retried on a later night
            _STATE["failed"] += 1
            msg = str(exc)
            _mark(mid, "no_demo" if "No demo link" in msg else "error", msg)
            logger.warning("Demo back-fill: match %s failed: %s", mid, msg)
        _STOP.wait(PAUSE_S)


def start(limit: int = NIGHT_LIMIT, match_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    with _LOCK:
        if _STATE.get("running"):
            return status()
        _STOP.clear()
        _STATE.clear()
        _STATE.update(running=True, started_at=time.time(), limit=int(limit))

    def work():
        try:
            _run(int(limit), [int(m) for m in match_ids] if match_ids else None)
        except Exception:  # noqa: BLE001
            logger.exception("Demo back-fill failed")
        finally:
            _STATE.update(running=False, finished_at=time.time(), current=None)

    threading.Thread(target=work, name="demo-backfill", daemon=True).start()
    return status()


def stop() -> Dict[str, Any]:
    _STOP.set()
    return status()


def status() -> Dict[str, Any]:
    _ensure()
    conn = connect()
    try:
        counts = {r["status"]: int(r["n"]) for r in conn.execute("SELECT status, COUNT(*) AS n FROM demo_matches GROUP BY status")}
        maps = conn.execute("SELECT COUNT(DISTINCT match_id || ':' || map_index) FROM demo_map_stats").fetchone()[0]
    finally:
        conn.close()
    return {**_STATE, "stored": counts, "maps_stored": int(maps)}
