from datetime import date, datetime, timezone
import json
import logging
import sqlite3
import threading
import time

from fastapi import APIRouter, HTTPException

from backend.routes.players import _INTERRUPTION_BOILERPLATE
from backend.data.player_db import add_or_update_player
from backend.services.hltv_rankings import (
    get_hltv_team_lineup,
    HLTVRankingError,
    RankingPageParseError,
    TeamNotRankedError,
    get_hltv_team_map_stats,
    get_latest_hltv_rankings,
    get_latest_vrs_rankings,
    get_team_hltv_rating_and_points_on_date,
    get_team_vrs_rank_and_points_on_date,
)
from backend.data.team_db import (
    update_team_roster,
    connect as connect_team_db,
    get_all_teams,
    get_team_by_id,
    get_team_by_name,
    add_or_update_team,
    delete_team,
    update_team_map_stats,
)


router = APIRouter()
logger = logging.getLogger(__name__)
MAP_STATS_IMPORT_JOBS: dict[str, dict] = {}
MAP_STATS_IMPORT_WORKERS: dict[str, threading.Thread] = {}
MAP_STATS_IMPORT_JOBS_LOCK = threading.Lock()


def ensure_map_stats_import_schema() -> None:
    conn = connect_team_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS team_map_stats_import_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                items_json TEXT NOT NULL,
                results_json TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                processed_teams INTEGER NOT NULL DEFAULT 0,
                total_teams INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_team_map_stats_jobs_updated ON team_map_stats_import_jobs(updated_at DESC)")
        conn.commit()
    finally:
        conn.close()


def _map_stats_job_from_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["items"] = json.loads(out.pop("items_json") or "[]")
    out["results"] = json.loads(out.pop("results_json") or "[]")
    out["pause_requested"] = bool(out.get("pause_requested"))
    out["cancel_requested"] = bool(out.get("cancel_requested"))
    out["progress"] = float(out.get("progress") or 0.0)
    return out


def _get_stored_map_stats_job(job_id: str) -> dict | None:
    conn = connect_team_db()
    try:
        row = conn.execute("SELECT * FROM team_map_stats_import_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _map_stats_job_from_row(row) if row else None
    finally:
        conn.close()


def _save_map_stats_job(job: dict) -> None:
    now = time.time()
    job["updated_at"] = now
    conn = connect_team_db()
    try:
        conn.execute(
            """
            INSERT INTO team_map_stats_import_jobs (
                job_id, status, items_json, results_json, error, last_error,
                progress, processed_teams, total_teams, ok, failed,
                pause_requested, cancel_requested, created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                items_json = excluded.items_json,
                results_json = excluded.results_json,
                error = excluded.error,
                last_error = excluded.last_error,
                progress = excluded.progress,
                processed_teams = excluded.processed_teams,
                total_teams = excluded.total_teams,
                ok = excluded.ok,
                failed = excluded.failed,
                pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested,
                updated_at = excluded.updated_at,
                started_at = COALESCE(excluded.started_at, team_map_stats_import_jobs.started_at),
                finished_at = excluded.finished_at
            """,
            (
                str(job["job_id"]),
                str(job.get("status") or "queued"),
                json.dumps(job.get("items") or []),
                json.dumps(job.get("results") or []),
                str(job.get("error") or ""),
                str(job.get("last_error") or ""),
                float(job.get("progress") or 0.0),
                int(job.get("processed_teams") or 0),
                int(job.get("total_teams") or 0),
                int(job.get("ok") or 0),
                int(job.get("failed") or 0),
                1 if job.get("pause_requested") else 0,
                1 if job.get("cancel_requested") else 0,
                float(job.get("created_at") or now),
                now,
                job.get("started_at"),
                job.get("finished_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _publish_map_stats_job(job: dict) -> None:
    with MAP_STATS_IMPORT_JOBS_LOCK:
        MAP_STATS_IMPORT_JOBS[str(job["job_id"])] = dict(job)
    _save_map_stats_job(job)


def _map_stats_worker_is_active(job_id: str) -> bool:
    with MAP_STATS_IMPORT_JOBS_LOCK:
        worker = MAP_STATS_IMPORT_WORKERS.get(job_id)
        return bool(worker and worker.is_alive())


def _map_stats_job_response(job: dict) -> dict:
    return {
        "job_id": str(job["job_id"]),
        "status": job.get("status", "queued"),
        "error": job.get("error", ""),
        "last_error": job.get("last_error", ""),
        "progress": job.get("progress", 0.0),
        "processed_teams": job.get("processed_teams", 0),
        "total_teams": job.get("total_teams", 0),
        "ok": job.get("ok", 0),
        "failed": job.get("failed", 0),
        "pause_requested": bool(job.get("pause_requested")),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "results": (job.get("results") or [])[-50:],
    }


def _get_map_stats_job_for_response(job_id: str) -> dict:
    # Memory first: while the worker is alive it publishes every update here,
    # so polls avoid touching SQLite alongside the job's own writes.
    with MAP_STATS_IMPORT_JOBS_LOCK:
        cached = MAP_STATS_IMPORT_JOBS.get(job_id)
        job = dict(cached) if cached else None
    if not job:
        job = _get_stored_map_stats_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.get("status") in {"queued", "running", "pausing", "canceling"} and not _map_stats_worker_is_active(job_id):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["last_error"] = job.get("last_error") or "Job was interrupted before completion. Resume to continue."
        job["error"] = ""
        job["pause_requested"] = False
        job["cancel_requested"] = False
        _publish_map_stats_job(job)
    return job


def _import_team_map_stats_item(item: dict) -> dict:
    team_id = int(item.get("team_id") or 0)
    hltv_team_id = int(item.get("hltv_team_id") or 0)
    team_name = str(item.get("team_name") or "").strip()
    if team_id <= 0:
        raise ValueError("team_id is required")
    if hltv_team_id <= 0:
        raise ValueError("hltv_team_id is required")
    stats = get_hltv_team_map_stats(hltv_team_id, team_name)
    update_team_map_stats(
        team_id,
        map_stats_json=json.dumps(stats.get("maps") or []),
        source_url=str(stats.get("url") or ""),
        hltv_team_id=hltv_team_id,
    )
    return {
        "team_id": team_id,
        "team_name": team_name,
        "hltv_team_id": hltv_team_id,
        "status": "ok",
        "source_url": stats.get("url"),
        "maps": len(stats.get("maps") or []),
    }


def _run_map_stats_import_job(job_id: str) -> None:
    job = _get_stored_map_stats_job(job_id)
    if not job:
        return
    if job.get("cancel_requested") or job.get("pause_requested"):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["pause_requested"] = False
        job["cancel_requested"] = False
        job["error"] = ""
        job["last_error"] = job.get("last_error") or ("Canceled" if job["status"] == "canceled" else "Paused")
        job["finished_at"] = time.time()
        _publish_map_stats_job(job)
        with MAP_STATS_IMPORT_JOBS_LOCK:
            MAP_STATS_IMPORT_WORKERS.pop(job_id, None)
        return

    job["status"] = "running"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["started_at"] = job.get("started_at") or time.time()
    job["finished_at"] = None
    _publish_map_stats_job(job)
    try:
        items = _resolve_missing_hltv_ids(job.get("items") or [])
        job["items"] = items
        _publish_map_stats_job(job)
        results = list(job.get("results") or [])
        processed_ids = {int(row.get("team_id") or 0) for row in results if int(row.get("team_id") or 0) > 0}
        ok = len([r for r in results if r.get("status") == "ok"])
        failed = len([r for r in results if r.get("status") == "error"])

        for item in items:
            team_id = int(item.get("team_id") or 0)
            if team_id in processed_ids:
                continue
            latest = _get_stored_map_stats_job(job_id) or job
            if latest.get("cancel_requested") or latest.get("pause_requested"):
                job.update(latest)
                job["status"] = "canceled" if latest.get("cancel_requested") else "paused"
                job["pause_requested"] = False
                job["cancel_requested"] = False
                job["error"] = ""
                job["last_error"] = "Canceled" if job["status"] == "canceled" else "Paused"
                job["finished_at"] = time.time()
                _publish_map_stats_job(job)
                return
            try:
                row = _import_team_map_stats_item(item)
                ok += 1
            except Exception as exc:
                failed += 1
                row = {
                    "team_id": team_id,
                    "team_name": item.get("team_name") or "",
                    "hltv_team_id": item.get("hltv_team_id"),
                    "status": "error",
                    "detail": str(exc),
                }
                job["last_error"] = f"{row['team_name'] or team_id}: {exc}"
            results.append(row)
            processed_ids.add(team_id)
            processed = ok + failed
            total = int(job.get("total_teams") or len(items))
            job["results"] = results
            job["ok"] = ok
            job["failed"] = failed
            job["processed_teams"] = min(processed, total)
            job["progress"] = 0.0 if total <= 0 else min(1.0, float(processed) / float(total))
            _publish_map_stats_job(job)

        job["status"] = "completed"
        job["progress"] = 1.0
        job["finished_at"] = time.time()
        job["error"] = ""
        _publish_map_stats_job(job)
    except Exception as exc:
        logger.exception("Map stats import job failed: job_id=%s", job_id)
        job["status"] = "failed"
        job["error"] = str(exc)
        job["last_error"] = str(exc)
        job["finished_at"] = time.time()
        _publish_map_stats_job(job)
    finally:
        with MAP_STATS_IMPORT_JOBS_LOCK:
            MAP_STATS_IMPORT_WORKERS.pop(job_id, None)


def _start_map_stats_worker(job_id: str) -> None:
    with MAP_STATS_IMPORT_JOBS_LOCK:
        worker = MAP_STATS_IMPORT_WORKERS.get(job_id)
        if worker and worker.is_alive():
            return
        worker = threading.Thread(target=_run_map_stats_import_job, args=(job_id,), daemon=True)
        MAP_STATS_IMPORT_WORKERS[job_id] = worker
        worker.start()


def _get_latest_map_stats_job(*, include_completed: bool = True) -> dict | None:
    statuses = "" if include_completed else "WHERE status IN ('queued', 'running', 'pausing', 'canceling', 'paused', 'failed')"
    conn = connect_team_db()
    try:
        row = conn.execute(f"SELECT * FROM team_map_stats_import_jobs {statuses} ORDER BY updated_at DESC LIMIT 1").fetchone()
        return _get_map_stats_job_for_response(str(row["job_id"])) if row else None
    finally:
        conn.close()


def _build_map_stats_import_items(payload: dict | None = None) -> list[dict]:
    body = payload or {}
    requested_ids = set()
    for raw_id in body.get("team_ids") or []:
        try:
            team_id = int(raw_id)
        except Exception:
            continue
        if team_id > 0:
            requested_ids.add(team_id)
    missing_only = bool(body.get("missing_only"))
    teams = get_all_teams()
    selected = [t for t in teams if not requested_ids or int(t.get("team_id") or 0) in requested_ids]
    items = []
    for team in selected:
        if missing_only and team.get("map_stats_json"):
            continue
        team_id = int(team.get("team_id") or 0)
        if team_id <= 0:
            continue
        # hltv_team_id may be 0 here; the worker resolves missing ids via a
        # rankings refresh so this builder (called from the start endpoint)
        # never blocks on the global HLTV fetch lock.
        items.append(
            {
                "team_id": team_id,
                "team_name": str(team.get("name") or ""),
                "hltv_team_id": int(team.get("hltv_team_id") or 0),
            }
        )
    return items


def _resolve_missing_hltv_ids(items: list[dict]) -> list[dict]:
    """Fill in hltv_team_id for items that lack one, scraping today's rankings once if needed."""
    if not any(int(item.get("hltv_team_id") or 0) <= 0 for item in items):
        return items
    try:
        refresh_hltv_for_all_teams_today()
    except Exception as exc:
        logger.warning("HLTV rankings refresh for map-stats id resolution failed: %s", exc)
    by_id = {int(t.get("team_id") or 0): t for t in get_all_teams()}
    resolved = []
    for item in items:
        if int(item.get("hltv_team_id") or 0) <= 0:
            team = by_id.get(int(item.get("team_id") or 0)) or {}
            item = {**item, "hltv_team_id": int(team.get("hltv_team_id") or 0)}
        resolved.append(item)
    return resolved


def _team_rows_by_key(teams: list) -> dict:
    """Team rows by canonical name key; a renamed team's old-name row never
    shadows its current row."""
    from backend.data import team_aliases

    out: dict = {}
    for t in teams:
        raw = team_aliases.normalize(t.get("name") or "")
        if not raw:
            continue
        k = team_aliases.canonical(raw)
        if raw == k or k not in out:
            out[k] = t
    return out


def _ranking_target(name: str, teams_by_name: dict, teams_by_key: dict) -> dict:
    """The stored team a ranking entry updates: after a rename the old name's
    entry lands on the current team's row."""
    from backend.data import team_aliases

    raw = team_aliases.normalize(name)
    if team_aliases.is_alias_source(raw):
        return teams_by_key.get(team_aliases.canonical(raw)) or teams_by_name.get(name.lower()) or {}
    return teams_by_name.get(name.lower()) or {}


def _with_aliases(rows: list) -> list:
    """Attach each team's earlier names (`formerly`) and, for an old-name row,
    the team it became (`renamed_to`)."""
    from backend.data import team_aliases

    aliases = team_aliases.list_aliases()
    by_new: dict = {}
    by_old: dict = {}
    for a in aliases:
        by_new.setdefault(int(a.get("new_team_id") or 0), []).append(a)
        by_old[int(a.get("old_team_id") or 0)] = a
    for t in rows:
        tid = int(t.get("team_id") or 0)
        t["formerly"] = [
            {"old_key": a["old_key"], "old_team_id": a.get("old_team_id"), "old_name": a.get("old_name")}
            for a in by_new.get(tid, [])
        ]
        r = by_old.get(tid)
        t["renamed_to"] = {"new_team_id": r.get("new_team_id"), "new_name": r.get("new_name")} if r else None
    return rows


@router.get("/")
def list_teams():
    return _with_aliases(get_all_teams())


@router.get("/hltv-ranking/by-date")
def fetch_hltv_ranking_by_date(team_name: str, on_date: str):
    """
    Fetch HLTV ranking position ("hltv_rating") and points for a team on a specific date.
    `on_date` must be YYYY-MM-DD.
    """
    if not team_name or not team_name.strip():
        raise HTTPException(status_code=400, detail="team_name is required")

    try:
        parsed_date = date.fromisoformat(on_date)
    except Exception:
        raise HTTPException(status_code=400, detail="on_date must be in YYYY-MM-DD format")

    try:
        return get_team_hltv_rating_and_points_on_date(team_name, parsed_date)
    except TeamNotRankedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RankingPageParseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except HLTVRankingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/refresh-hltv-today")
def refresh_hltv_for_all_teams_today():
    """
    Refresh all teams' HLTV rank and HLTV points for today's date.
    """
    today = date.today()
    teams = get_all_teams()
    teams_by_name = {(t.get("name") or "").strip().lower(): t for t in teams if (t.get("name") or "").strip()}
    teams_by_key = _team_rows_by_key(teams)
    logger.info("HLTV refresh requested: date=%s existing_teams=%d", today.isoformat(), len(teams))

    results = []
    updated = 0
    failed = 0
    snapshot = None

    try:
        snapshot = get_latest_hltv_rankings()
        logger.info(
            "HLTV latest snapshot selected: requested=%s effective=%s",
            snapshot.get("requested_date"),
            snapshot.get("effective_date"),
        )
    except HLTVRankingError as exc:
        logger.error("HLTV refresh snapshot fetch failed: %s", exc)
        return {
            "status": "ok",
            "date": today.isoformat(),
            "updated": 0,
            "failed": len([t for t in teams if (t.get("name") or "").strip()]),
            "results": [
                {
                    "team_id": t.get("team_id"),
                    "team_name": (t.get("name") or "").strip(),
                    "status": "error",
                    "detail": str(exc),
                }
                for t in teams
                if (t.get("name") or "").strip()
            ],
        }

    rankings = snapshot.get("rankings_by_team") or {}
    effective_date = snapshot.get("effective_date")
    requested_date = snapshot.get("requested_date")
    days_back = int(snapshot.get("days_back") or 0)

    if not rankings:
        logger.warning("HLTV refresh snapshot contains no teams")
        return {
            "status": "ok",
            "date": today.isoformat(),
            "updated": 0,
            "failed": 0,
            "inserted": 0,
            "results": [],
        }

    inserted = 0
    for _, data in rankings.items():
        name = (data.get("team_name") or "").strip()
        if not name:
            continue
        try:
            existing = _ranking_target(name, teams_by_name, teams_by_key)
            before_exists = bool(existing)
            routed = bool(existing) and (existing.get("name") or "").strip().lower() != name.lower()
            hltv_team_id = (
                (existing.get("hltv_team_id") or data.get("hltv_team_id"))
                if routed
                else (data.get("hltv_team_id") or existing.get("hltv_team_id"))
            )
            add_or_update_team(
                name=existing.get("name") or name,
                hltv_rank=int(data.get("hltv_rating") or 999),
                hltv_points=int(data.get("points") or 0),
                vrs_rank=int(existing.get("vrs_rank") or 999),
                vrs_points=int(existing.get("vrs_points") or 0),
                win_rate=float(existing.get("win_rate") or 0.5),
                player_ids=[
                    int(existing.get("player1_id") or 0),
                    int(existing.get("player2_id") or 0),
                    int(existing.get("player3_id") or 0),
                    int(existing.get("player4_id") or 0),
                    int(existing.get("player5_id") or 0),
                ],
                hltv_team_id=hltv_team_id,
            )
            if hltv_team_id:
                from backend.data.team_db import sync_twin_team_rows

                sync_twin_team_rows(int(hltv_team_id), hltv_rank=int(data.get("hltv_rating") or 999), hltv_points=int(data.get("points") or 0))
            updated += 1
            if not before_exists:
                inserted += 1
            results.append(
                {
                    "team_id": existing.get("team_id"),
                    "team_name": name,
                    "status": "ok",
                    "hltv_rank": int(data.get("hltv_rating") or 999),
                    "hltv_points": int(data.get("points") or 0),
                    "hltv_team_id": hltv_team_id,
                    "requested_date": requested_date,
                    "effective_date": effective_date,
                    "days_back": days_back,
                    "inserted": not before_exists,
                }
            )
        except Exception as exc:
            logger.error("HLTV refresh error: team='%s' detail=%s", name, exc)
            results.append({"team_name": name, "status": "error", "detail": str(exc)})
            failed += 1

    # Teams the snapshot no longer lists are unranked now, whatever rank they
    # last held; otherwise renamed and dead rosters stay in every top-N scope.
    unranked = 0
    if updated > 0:
        from backend.data.team_db import clear_stale_hltv_ranks

        unranked = clear_stale_hltv_ranks([int(r.get("team_id") or 0) for r in results if r.get("status") == "ok"])
    logger.info("HLTV refresh complete: updated=%d inserted=%d failed=%d unranked=%d", updated, inserted, failed, unranked)
    return {
        "status": "ok",
        "date": today.isoformat(),
        "updated": updated,
        "inserted": inserted,
        "failed": failed,
        "unranked": unranked,
        "results": results,
    }


@router.get("/vrs-ranking/by-date")
def fetch_vrs_ranking_by_date(team_name: str, on_date: str):
    if not team_name or not team_name.strip():
        raise HTTPException(status_code=400, detail="team_name is required")

    try:
        parsed_date = date.fromisoformat(on_date)
    except Exception:
        raise HTTPException(status_code=400, detail="on_date must be in YYYY-MM-DD format")

    try:
        return get_team_vrs_rank_and_points_on_date(team_name, parsed_date)
    except TeamNotRankedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RankingPageParseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except HLTVRankingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/refresh-vrs-today")
def refresh_vrs_for_all_teams_today():
    today = date.today()
    teams = get_all_teams()
    teams_by_name = {(t.get("name") or "").strip().lower(): t for t in teams if (t.get("name") or "").strip()}
    teams_by_key = _team_rows_by_key(teams)
    logger.info("VRS refresh requested: date=%s existing_teams=%d", today.isoformat(), len(teams))

    results = []
    updated = 0
    failed = 0

    try:
        snapshot = get_latest_vrs_rankings()
        logger.info(
            "VRS latest snapshot selected: requested=%s effective=%s",
            snapshot.get("requested_date"),
            snapshot.get("effective_date"),
        )
    except HLTVRankingError as exc:
        logger.error("VRS refresh snapshot fetch failed: %s", exc)
        return {
            "status": "ok",
            "date": today.isoformat(),
            "updated": 0,
            "failed": len([t for t in teams if (t.get("name") or "").strip()]),
            "results": [
                {
                    "team_id": t.get("team_id"),
                    "team_name": (t.get("name") or "").strip(),
                    "status": "error",
                    "detail": str(exc),
                }
                for t in teams
                if (t.get("name") or "").strip()
            ],
        }

    rankings = snapshot.get("rankings_by_team") or {}
    effective_date = snapshot.get("effective_date")
    requested_date = snapshot.get("requested_date")
    days_back = int(snapshot.get("days_back") or 0)

    if not rankings:
        logger.warning("VRS refresh snapshot contains no teams")
        return {
            "status": "ok",
            "date": today.isoformat(),
            "updated": 0,
            "failed": 0,
            "inserted": 0,
            "results": [],
        }

    inserted = 0
    for _, data in rankings.items():
        name = (data.get("team_name") or "").strip()
        if not name:
            continue
        try:
            existing = _ranking_target(name, teams_by_name, teams_by_key)
            before_exists = bool(existing)
            add_or_update_team(
                name=existing.get("name") or name,
                hltv_rank=int(existing.get("hltv_rank") or 999),
                hltv_points=int(existing.get("hltv_points") or 0),
                vrs_rank=int(data.get("vrs_rank") or 999),
                vrs_points=int(data.get("points") or 0),
                win_rate=float(existing.get("win_rate") or 0.5),
                player_ids=[
                    int(existing.get("player1_id") or 0),
                    int(existing.get("player2_id") or 0),
                    int(existing.get("player3_id") or 0),
                    int(existing.get("player4_id") or 0),
                    int(existing.get("player5_id") or 0),
                ],
                hltv_team_id=existing.get("hltv_team_id"),
            )
            if existing.get("hltv_team_id"):
                from backend.data.team_db import sync_twin_team_rows

                sync_twin_team_rows(int(existing["hltv_team_id"]), vrs_rank=int(data.get("vrs_rank") or 999), vrs_points=int(data.get("points") or 0))
            updated += 1
            if not before_exists:
                inserted += 1
            results.append(
                {
                    "team_id": existing.get("team_id"),
                    "team_name": name,
                    "status": "ok",
                    "vrs_rank": int(data.get("vrs_rank") or 999),
                    "vrs_points": int(data.get("points") or 0),
                    "requested_date": requested_date,
                    "effective_date": effective_date,
                    "days_back": days_back,
                    "inserted": not before_exists,
                }
            )
        except Exception as exc:
            logger.error("VRS refresh error: team='%s' detail=%s", name, exc)
            results.append({"team_name": name, "status": "error", "detail": str(exc)})
            failed += 1

    logger.info("VRS refresh complete: updated=%d inserted=%d failed=%d", updated, inserted, failed)
    return {
        "status": "ok",
        "date": today.isoformat(),
        "updated": updated,
        "inserted": inserted,
        "failed": failed,
        "results": results,
    }


@router.post("/refresh-rankings-today")
def refresh_all_rankings_for_all_teams_today():
    """
    Run HLTV + VRS refresh in one call.
    """
    hltv = refresh_hltv_for_all_teams_today()
    vrs = refresh_vrs_for_all_teams_today()
    return {
        "status": "ok",
        "date": date.today().isoformat(),
        "hltv": hltv,
        "vrs": vrs,
        "updated_total": int(hltv.get("updated", 0)) + int(vrs.get("updated", 0)),
        "inserted_total": int(hltv.get("inserted", 0)) + int(vrs.get("inserted", 0)),
        "failed_total": int(hltv.get("failed", 0)) + int(vrs.get("failed", 0)),
    }


# --- Rankings refresh as a pausable/resumable background job -----------------
# Two phases (HLTV ranking page, VRS ranking page); each phase is one scrape
# plus a fast DB apply, so pause/cancel take effect between phases.

RANKINGS_REFRESH_JOBS: dict[str, dict] = {}
RANKINGS_REFRESH_WORKERS: dict[str, threading.Thread] = {}
RANKINGS_REFRESH_JOBS_LOCK = threading.Lock()
_RANKINGS_PHASES = ("hltv", "vrs")


def ensure_rankings_refresh_schema() -> None:
    conn = connect_team_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rankings_refresh_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                results_json TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                processed_phases INTEGER NOT NULL DEFAULT 0,
                total_phases INTEGER NOT NULL DEFAULT 2,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rankings_refresh_jobs_updated ON rankings_refresh_jobs(updated_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def _rankings_job_from_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["results"] = json.loads(out.pop("results_json") or "[]")
    out["pause_requested"] = bool(out.get("pause_requested"))
    out["cancel_requested"] = bool(out.get("cancel_requested"))
    out["progress"] = float(out.get("progress") or 0.0)
    return out


def _get_stored_rankings_job(job_id: str) -> dict | None:
    conn = connect_team_db()
    try:
        row = conn.execute("SELECT * FROM rankings_refresh_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _rankings_job_from_row(row) if row else None
    finally:
        conn.close()


def _save_rankings_job(job: dict) -> None:
    now = time.time()
    job["updated_at"] = now
    conn = connect_team_db()
    try:
        conn.execute(
            """
            INSERT INTO rankings_refresh_jobs (
                job_id, status, results_json, error, last_error, progress,
                processed_phases, total_phases, pause_requested, cancel_requested,
                created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                results_json = excluded.results_json,
                error = excluded.error,
                last_error = excluded.last_error,
                progress = excluded.progress,
                processed_phases = excluded.processed_phases,
                total_phases = excluded.total_phases,
                pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested,
                updated_at = excluded.updated_at,
                started_at = COALESCE(excluded.started_at, rankings_refresh_jobs.started_at),
                finished_at = excluded.finished_at
            """,
            (
                str(job["job_id"]),
                str(job.get("status") or "queued"),
                json.dumps(job.get("results") or []),
                str(job.get("error") or ""),
                str(job.get("last_error") or ""),
                float(job.get("progress") or 0.0),
                int(job.get("processed_phases") or 0),
                int(job.get("total_phases") or len(_RANKINGS_PHASES)),
                1 if job.get("pause_requested") else 0,
                1 if job.get("cancel_requested") else 0,
                float(job.get("created_at") or now),
                now,
                job.get("started_at"),
                job.get("finished_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _publish_rankings_job(job: dict) -> None:
    with RANKINGS_REFRESH_JOBS_LOCK:
        RANKINGS_REFRESH_JOBS[str(job["job_id"])] = dict(job)
    _save_rankings_job(job)


def _rankings_worker_is_active(job_id: str) -> bool:
    with RANKINGS_REFRESH_JOBS_LOCK:
        worker = RANKINGS_REFRESH_WORKERS.get(job_id)
        return bool(worker and worker.is_alive())


def _rankings_job_response(job: dict) -> dict:
    return {
        "job_id": str(job["job_id"]),
        "status": job.get("status", "queued"),
        "error": job.get("error", ""),
        "last_error": job.get("last_error", ""),
        "progress": job.get("progress", 0.0),
        "processed_phases": job.get("processed_phases", 0),
        "total_phases": job.get("total_phases", len(_RANKINGS_PHASES)),
        "pause_requested": bool(job.get("pause_requested")),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "results": job.get("results", []),
    }


def _get_rankings_job_for_response(job_id: str) -> dict:
    # Memory first: while the worker is alive it publishes every update here,
    # so polls avoid touching SQLite alongside the job's own writes.
    with RANKINGS_REFRESH_JOBS_LOCK:
        cached = RANKINGS_REFRESH_JOBS.get(job_id)
        job = dict(cached) if cached else None
    if not job:
        job = _get_stored_rankings_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.get("status") in {"queued", "running", "pausing", "canceling"} and not _rankings_worker_is_active(job_id):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["last_error"] = job.get("last_error") or "Job was interrupted before completion. Resume to continue."
        job["error"] = ""
        job["pause_requested"] = False
        job["cancel_requested"] = False
        _publish_rankings_job(job)
    return job


def _run_rankings_refresh_job(job_id: str) -> None:
    job = _get_stored_rankings_job(job_id)
    if not job:
        return
    if job.get("cancel_requested") or job.get("pause_requested"):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["pause_requested"] = False
        job["cancel_requested"] = False
        job["error"] = ""
        job["finished_at"] = time.time()
        _publish_rankings_job(job)
        with RANKINGS_REFRESH_JOBS_LOCK:
            RANKINGS_REFRESH_WORKERS.pop(job_id, None)
        return

    job["status"] = "running"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["started_at"] = job.get("started_at") or time.time()
    job["finished_at"] = None
    _publish_rankings_job(job)

    phase_runners = {
        "hltv": refresh_hltv_for_all_teams_today,
        "vrs": refresh_vrs_for_all_teams_today,
    }
    try:
        results = list(job.get("results") or [])
        done_phases = {str(row.get("phase")) for row in results}
        for phase in _RANKINGS_PHASES:
            if phase in done_phases:
                continue
            latest = _get_stored_rankings_job(job_id) or job
            if latest.get("cancel_requested") or latest.get("pause_requested"):
                job.update(latest)
                job["status"] = "canceled" if latest.get("cancel_requested") else "paused"
                job["pause_requested"] = False
                job["cancel_requested"] = False
                job["error"] = ""
                job["last_error"] = "Canceled" if job["status"] == "canceled" else "Paused"
                job["finished_at"] = time.time()
                _publish_rankings_job(job)
                return
            try:
                summary = phase_runners[phase]()
                results.append(
                    {
                        "phase": phase,
                        "status": "ok",
                        "updated": int(summary.get("updated") or 0),
                        "inserted": int(summary.get("inserted") or 0),
                        "failed": int(summary.get("failed") or 0),
                    }
                )
            except Exception as exc:
                results.append({"phase": phase, "status": "error", "detail": str(exc)})
                job["last_error"] = f"{phase.upper()} refresh failed: {exc}"
            job["results"] = results
            job["processed_phases"] = len(results)
            job["total_phases"] = len(_RANKINGS_PHASES)
            job["progress"] = len(results) / float(len(_RANKINGS_PHASES))
            _publish_rankings_job(job)

        job["status"] = "completed"
        job["finished_at"] = time.time()
        _publish_rankings_job(job)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = time.time()
        _publish_rankings_job(job)
    finally:
        with RANKINGS_REFRESH_JOBS_LOCK:
            RANKINGS_REFRESH_WORKERS.pop(job_id, None)


def _start_rankings_worker(job_id: str) -> None:
    with RANKINGS_REFRESH_JOBS_LOCK:
        worker = RANKINGS_REFRESH_WORKERS.get(job_id)
        if worker and worker.is_alive():
            return
        worker = threading.Thread(target=_run_rankings_refresh_job, args=(job_id,), daemon=True)
        RANKINGS_REFRESH_WORKERS[job_id] = worker
        worker.start()


def _get_latest_rankings_job() -> dict | None:
    conn = connect_team_db()
    try:
        row = conn.execute("SELECT job_id FROM rankings_refresh_jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return _get_rankings_job_for_response(str(row["job_id"])) if row else None


@router.post("/rankings-refresh/start")
def start_rankings_refresh_job():
    latest = _get_latest_rankings_job()
    if latest and latest.get("status") in {"queued", "running", "pausing", "canceling"}:
        return {"job_id": latest["job_id"], "status": latest.get("status"), "reused": True}
    now = time.time()
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    job = {
        "job_id": job_id,
        "status": "queued",
        "error": "",
        "last_error": "",
        "progress": 0.0,
        "processed_phases": 0,
        "total_phases": len(_RANKINGS_PHASES),
        "pause_requested": False,
        "cancel_requested": False,
        "results": [],
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    _publish_rankings_job(job)
    _start_rankings_worker(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/rankings-refresh/latest")
def get_latest_rankings_refresh_job():
    job = _get_latest_rankings_job()
    if not job:
        return {"exists": False}
    return {"exists": True, **_rankings_job_response(job)}


@router.get("/rankings-refresh/job/{job_id}")
def get_rankings_refresh_job(job_id: str):
    return _rankings_job_response(_get_rankings_job_for_response(job_id))


@router.post("/rankings-refresh/job/{job_id}/pause")
def pause_rankings_refresh_job(job_id: str):
    job = _get_rankings_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed", "paused"}:
        return _rankings_job_response(job)
    job["pause_requested"] = True
    job["status"] = "pausing" if _rankings_worker_is_active(job_id) else "paused"
    _publish_rankings_job(job)
    return _rankings_job_response(job)


@router.post("/rankings-refresh/job/{job_id}/cancel")
def cancel_rankings_refresh_job(job_id: str):
    job = _get_rankings_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed"}:
        return _rankings_job_response(job)
    job["cancel_requested"] = True
    job["status"] = "canceling" if _rankings_worker_is_active(job_id) else "canceled"
    _publish_rankings_job(job)
    return _rankings_job_response(job)


@router.post("/rankings-refresh/job/{job_id}/resume")
def resume_rankings_refresh_job(job_id: str):
    job = _get_rankings_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled"}:
        return _rankings_job_response(job)
    if job.get("status") == "running" and _rankings_worker_is_active(job_id):
        return _rankings_job_response(job)
    job["status"] = "queued"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["finished_at"] = None
    _publish_rankings_job(job)
    _start_rankings_worker(job_id)
    return _rankings_job_response(_get_rankings_job_for_response(job_id))


@router.post("/map-stats-import/start")
def start_map_stats_import_job(payload: dict | None = None):
    latest = _get_latest_map_stats_job(include_completed=False)
    if latest and latest.get("status") in {"queued", "running", "pausing", "canceling"}:
        return {"job_id": latest["job_id"], "status": latest.get("status"), "reused": True}
    items = _build_map_stats_import_items(payload)
    if not items:
        raise HTTPException(status_code=400, detail="No teams were found for map-stats import.")
    now = time.time()
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    job = {
        "job_id": job_id,
        "status": "queued",
        "error": "",
        "last_error": "",
        "progress": 0.0,
        "processed_teams": 0,
        "total_teams": len(items),
        "ok": 0,
        "failed": 0,
        "pause_requested": False,
        "cancel_requested": False,
        "items": items,
        "results": [],
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    _publish_map_stats_job(job)
    _start_map_stats_worker(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/map-stats-import/latest")
def get_latest_map_stats_import_job():
    job = _get_latest_map_stats_job()
    if not job:
        return {"exists": False}
    return {"exists": True, **_map_stats_job_response(job)}


@router.get("/map-stats-import/job/{job_id}")
def get_map_stats_import_job(job_id: str):
    return _map_stats_job_response(_get_map_stats_job_for_response(job_id))


@router.post("/map-stats-import/job/{job_id}/pause")
def pause_map_stats_import_job(job_id: str):
    job = _get_map_stats_job_for_response(job_id)
    if job.get("status") in {"completed", "failed", "canceled"}:
        return _map_stats_job_response(job)
    if job.get("status") == "canceling" or job.get("cancel_requested"):
        return _map_stats_job_response(job)
    job["pause_requested"] = True
    job["status"] = "pausing" if job.get("status") == "running" or _map_stats_worker_is_active(job_id) else "paused"
    _publish_map_stats_job(job)
    return _map_stats_job_response(job)


@router.post("/map-stats-import/job/{job_id}/cancel")
def cancel_map_stats_import_job(job_id: str):
    job = _get_map_stats_job_for_response(job_id)
    if job.get("status") in {"completed", "failed", "canceled"}:
        return _map_stats_job_response(job)
    job["cancel_requested"] = True
    job["pause_requested"] = False
    job["last_error"] = "Cancel requested"
    job["status"] = "canceling" if job.get("status") == "running" or _map_stats_worker_is_active(job_id) else "canceled"
    if job["status"] == "canceled":
        job["cancel_requested"] = False
        job["finished_at"] = time.time()
    _publish_map_stats_job(job)
    return _map_stats_job_response(job)


@router.post("/map-stats-import/job/{job_id}/resume")
def resume_map_stats_import_job(job_id: str):
    job = _get_map_stats_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled"}:
        return _map_stats_job_response(job)
    if job.get("status") == "running" and _map_stats_worker_is_active(job_id):
        return _map_stats_job_response(job)
    job["status"] = "queued"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["finished_at"] = None
    _publish_map_stats_job(job)
    _start_map_stats_worker(job_id)
    return _map_stats_job_response(_get_map_stats_job_for_response(job_id))


@router.get("/{team_id}/veto-profile")
def team_veto_profile(team_id: int, months: int = 3):
    """Pick/ban rates per map computed from OUR stored match vetoes. Covers
    maps the team never plays — HLTV's team-maps page omits those entirely,
    yet a permaban (played 0, banned every match) is exactly what a fantasy
    picker needs to see."""
    import json as _json
    from datetime import date, timedelta

    from backend.data.db import connect as _connect

    team = get_team_by_id(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    name = str(team.get("name") or "").strip()
    months = max(1, min(24, int(months)))
    cutoff = (date.today() - timedelta(days=months * 30)).isoformat()

    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT veto_json FROM hltv_results
            WHERE (LOWER(TRIM(team1)) = ? OR LOWER(TRIM(team2)) = ?)
              AND match_date >= ?
              AND veto_json IS NOT NULL AND veto_json != ''
            """,
            (name.lower(), name.lower(), cutoff),
        ).fetchall()
    finally:
        conn.close()

    counts: dict = {}
    matches = 0
    for r in rows:
        try:
            veto = _json.loads(r["veto_json"])
        except Exception:
            continue
        if not isinstance(veto, list) or not veto:
            continue
        matches += 1
        for step in veto:
            if str(step.get("team") or "").strip().lower() != name.lower():
                continue
            map_name = str(step.get("map") or "").strip()
            action = str(step.get("action") or "").strip().lower()
            if not map_name or action not in ("picked", "removed"):
                continue
            slot = counts.setdefault(map_name, {"picks": 0, "bans": 0})
            slot["picks" if action == "picked" else "bans"] += 1

    maps = {
        m: {
            "picks": c["picks"],
            "bans": c["bans"],
            "pick_rate": c["picks"] / matches if matches else 0.0,
            "ban_rate": c["bans"] / matches if matches else 0.0,
        }
        for m, c in counts.items()
    }
    return {"team_id": int(team_id), "team_name": name, "months": months, "matches": matches, "maps": maps}


@router.get("/{team_id}/map-profile")
def team_map_profile(team_id: int, months: int = 3):
    """Per-map record computed from OUR stored matches over an adjustable
    window: wins/losses/rounds from the stored map scores, pick/ban rates from
    the stored vetoes. Powers the Maps page team panel (the old fixed 3-month
    HLTV team-page scrape couldn't change timeframe)."""
    import json as _json
    from datetime import date, timedelta

    from backend.data.db import connect as _connect

    team = get_team_by_id(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    name = str(team.get("name") or "").strip()
    lname = name.lower()
    months = max(1, min(24, int(months)))
    cutoff = (date.today() - timedelta(days=months * 30)).isoformat()

    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT team1, team2, maps_json, veto_json FROM hltv_results
            WHERE (LOWER(TRIM(team1)) = ? OR LOWER(TRIM(team2)) = ?)
              AND match_date >= ?
            """,
            (lname, lname, cutoff),
        ).fetchall()
    finally:
        conn.close()

    def _slot(stats, map_name):
        return stats.setdefault(
            map_name,
            {"played": 0, "wins": 0, "losses": 0, "rounds": 0, "picks": 0, "bans": 0},
        )

    stats: dict = {}
    matches = 0
    veto_matches = 0
    for r in rows:
        is_team1 = str(r["team1"] or "").strip().lower() == lname
        try:
            maps = _json.loads(r["maps_json"] or "[]")
        except Exception:
            maps = []
        counted = False
        for m in maps if isinstance(maps, list) else []:
            map_name = str((m or {}).get("map") or "").strip()
            if not map_name:
                continue
            try:
                s1 = int(m.get("score1"))
                s2 = int(m.get("score2"))
            except (TypeError, ValueError):
                continue
            own, opp = (s1, s2) if is_team1 else (s2, s1)
            slot = _slot(stats, map_name)
            slot["played"] += 1
            if own > opp:
                slot["wins"] += 1
            elif opp > own:
                slot["losses"] += 1
            slot["rounds"] += own + opp
            counted = True
        if counted:
            matches += 1
        try:
            veto = _json.loads(r["veto_json"] or "[]")
        except Exception:
            veto = []
        if isinstance(veto, list) and veto:
            veto_matches += 1
            for step in veto:
                if str(step.get("team") or "").strip().lower() != lname:
                    continue
                map_name = str(step.get("map") or "").strip()
                action = str(step.get("action") or "").strip().lower()
                if not map_name or action not in ("picked", "removed"):
                    continue
                slot = _slot(stats, map_name)
                slot["picks" if action == "picked" else "bans"] += 1

    out = []
    for m, s in stats.items():
        out.append(
            {
                "map": m,
                "played": s["played"],
                "wins": s["wins"],
                "losses": s["losses"],
                "win_rate": (s["wins"] / s["played"]) if s["played"] else None,
                "total_rounds": s["rounds"],
                "picks": s["picks"],
                "bans": s["bans"],
                "pick_rate": (s["picks"] / veto_matches) if veto_matches else None,
                "ban_rate": (s["bans"] / veto_matches) if veto_matches else None,
            }
        )
    out.sort(key=lambda r: (-r["played"], r["map"]))
    return {
        "status": "ok",
        "team_id": int(team_id),
        "team_name": name,
        "months": months,
        "matches": matches,
        "veto_matches": veto_matches,
        "maps": out,
    }


@router.get("/{team_id}")
def fetch_team(team_id: int):
    team = get_team_by_id(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return _with_aliases([team])[0]


@router.post("/{team_id}/refresh-map-stats")
def refresh_team_map_stats(team_id: int, payload: dict | None = None):
    body = payload or {}
    team = get_team_by_id(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    hltv_team_id = body.get("hltv_team_id") or team.get("hltv_team_id")
    if hltv_team_id is None or str(hltv_team_id).strip() == "":
        raise HTTPException(status_code=400, detail="Set this team's HLTV team id before importing map stats.")
    try:
        hltv_team_id = int(hltv_team_id)
    except Exception:
        raise HTTPException(status_code=400, detail="A valid HLTV team id is required.")
    slug = str(body.get("slug") or team.get("name") or "").strip()
    try:
        stats = get_hltv_team_map_stats(hltv_team_id, slug)
    except RankingPageParseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except HLTVRankingError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    update_team_map_stats(
        int(team_id),
        map_stats_json=json.dumps(stats.get("maps") or []),
        source_url=str(stats.get("url") or ""),
        hltv_team_id=hltv_team_id,
    )
    return {
        "status": "ok",
        "team_id": int(team_id),
        "hltv_team_id": hltv_team_id,
        "source_url": stats.get("url"),
        "maps": stats.get("maps") or [],
    }


@router.get("/by-name/{name}")
def fetch_team_by_name(name: str):
    team = get_team_by_name(name)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


@router.post("/")
def upsert_team(payload: dict):
    """
    Requires: name, hltv_rank, vrs_rank, win_rate, player_ids (len==5).
    """
    required = ["name", "hltv_rank", "vrs_rank", "win_rate", "player_ids"]
    missing = [k for k in required if k not in payload]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {', '.join(missing)}")
    add_or_update_team(**payload)
    return {"status": "ok"}


@router.delete("/{team_id}")
def remove_team(team_id: int):
    delete_team(team_id)
    return {"status": "ok"}


# --- Team rosters as a pausable/resumable background job -----------------------
# One HLTV team page per team: the current five-player lineup goes into the
# team's roster slots and each player is upserted by HLTV id (name only, so
# fantasy prices/roles of event players are never touched). Scope: the top N
# ranked teams that have an HLTV id. Same job shape as the other backfills so
# the scheduler and the Scheduling tab drive it the same way.
ROSTER_JOBS: dict[str, dict] = {}
ROSTER_WORKERS: dict[str, threading.Thread] = {}
ROSTER_JOBS_LOCK = threading.Lock()
_ROSTER_SCHEMA_READY = False
DEFAULT_ROSTER_TOP_N = 200


def ensure_roster_job_schema() -> None:
    global _ROSTER_SCHEMA_READY
    if _ROSTER_SCHEMA_READY:
        return
    conn = connect_team_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS roster_import_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                processed_items INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                current_item TEXT NOT NULL DEFAULT '',
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                top_n INTEGER NOT NULL DEFAULT 200,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_roster_jobs_updated ON roster_import_jobs(updated_at DESC)")
        conn.commit()
    finally:
        conn.close()
    _ROSTER_SCHEMA_READY = True


def _roster_job_from_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["pause_requested"] = bool(out.get("pause_requested"))
    out["cancel_requested"] = bool(out.get("cancel_requested"))
    return out


def _save_roster_job(job: dict) -> None:
    ensure_roster_job_schema()
    job["updated_at"] = time.time()
    conn = connect_team_db()
    try:
        conn.execute(
            """
            INSERT INTO roster_import_jobs (
                job_id, status, error, last_error, progress, processed_items, total_items,
                ok, failed, current_item, pause_requested, cancel_requested, top_n,
                created_at, updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status, error = excluded.error, last_error = excluded.last_error,
                progress = excluded.progress, processed_items = excluded.processed_items,
                total_items = excluded.total_items, ok = excluded.ok, failed = excluded.failed,
                current_item = excluded.current_item, pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested, top_n = excluded.top_n,
                updated_at = excluded.updated_at, started_at = excluded.started_at,
                finished_at = excluded.finished_at
            """,
            (
                str(job["job_id"]), str(job.get("status") or "queued"), str(job.get("error") or ""),
                str(job.get("last_error") or ""), float(job.get("progress") or 0.0),
                int(job.get("processed_items") or 0), int(job.get("total_items") or 0),
                int(job.get("ok") or 0), int(job.get("failed") or 0), str(job.get("current_item") or ""),
                1 if job.get("pause_requested") else 0, 1 if job.get("cancel_requested") else 0,
                int(job.get("top_n") or DEFAULT_ROSTER_TOP_N),
                float(job.get("created_at") or time.time()), float(job["updated_at"]),
                job.get("started_at"), job.get("finished_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_stored_roster_job(job_id: str) -> dict | None:
    ensure_roster_job_schema()
    conn = connect_team_db()
    try:
        row = conn.execute("SELECT * FROM roster_import_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _roster_job_from_row(row) if row else None
    finally:
        conn.close()


def _publish_roster_job(job: dict) -> None:
    with ROSTER_JOBS_LOCK:
        ROSTER_JOBS[str(job["job_id"])] = dict(job)
    _save_roster_job(job)


def _roster_worker_is_active(job_id: str) -> bool:
    with ROSTER_JOBS_LOCK:
        worker = ROSTER_WORKERS.get(job_id)
        return bool(worker and worker.is_alive())


def _roster_job_response(job: dict) -> dict:
    return {
        "job_id": str(job["job_id"]),
        "status": job.get("status", "queued"),
        "error": job.get("error", ""),
        "last_error": job.get("last_error", ""),
        "progress": job.get("progress", 0.0),
        "processed_items": job.get("processed_items", 0),
        "total_items": job.get("total_items", 0),
        "ok": job.get("ok", 0),
        "failed": job.get("failed", 0),
        "current_item": job.get("current_item", ""),
        "pause_requested": bool(job.get("pause_requested")),
        "cancel_requested": bool(job.get("cancel_requested")),
        "top_n": int(job.get("top_n") or DEFAULT_ROSTER_TOP_N),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _get_roster_job_for_response(job_id: str) -> dict:
    with ROSTER_JOBS_LOCK:
        cached = ROSTER_JOBS.get(job_id)
        job = dict(cached) if cached else None
    if not job:
        job = _get_stored_roster_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.get("status") in {"queued", "running", "pausing", "canceling"} and not _roster_worker_is_active(job_id):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["last_error"] = job.get("last_error") or "Job was interrupted before completion. Resume to continue."
        job["error"] = ""
        job["pause_requested"] = False
        job["cancel_requested"] = False
        _publish_roster_job(job)
    return job


def _get_latest_roster_job() -> dict | None:
    ensure_roster_job_schema()
    conn = connect_team_db()
    try:
        row = conn.execute("SELECT job_id FROM roster_import_jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return _get_roster_job_for_response(str(row["job_id"])) if row else None


def _roster_scope_teams(top_n: int) -> list[dict]:
    """The top-N ranked teams that have an HLTV id, best rank first."""
    teams = []
    for t in get_all_teams():
        rank = int(t.get("hltv_rank") or 0)
        if rank <= 0 or rank > int(top_n) or not t.get("hltv_team_id"):
            continue
        teams.append(t)
    teams.sort(key=lambda t: int(t.get("hltv_rank") or 0))
    return teams


ROSTER_CHECK_FRESH_DAYS = 7


def _roster_lineup_size(t: dict) -> int:
    return sum(1 for i in range(1, 6) if int(t.get(f"player{i}_id") or 0) > 0)


def _roster_coverage(top_n: int) -> dict:
    """A team is covered once its HLTV page was read recently, whatever that
    page listed: a lineup of fewer than five is HLTV's answer, not a gap."""
    scope = _roster_scope_teams(top_n)
    fresh_after = time.time() - ROSTER_CHECK_FRESH_DAYS * 86400.0
    checked = [t for t in scope if float(t.get("roster_checked_at") or 0.0) >= fresh_after]
    short = sum(1 for t in checked if _roster_lineup_size(t) < 5)
    return {
        "top_n": int(top_n),
        "teams_in_scope": len(scope),
        "checked": len(checked),
        "unchecked": len(scope) - len(checked),
        "short_lineups": short,
        "full_lineups": len(checked) - short,
    }


def _run_roster_job(job_id: str) -> None:
    job = _get_stored_roster_job(job_id)
    if not job:
        return

    def stop_requested() -> str:
        with ROSTER_JOBS_LOCK:
            live = ROSTER_JOBS.get(job_id) or {}
        if live.get("cancel_requested"):
            return "canceled"
        if live.get("pause_requested"):
            return "paused"
        return ""

    job["status"] = "running"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["started_at"] = job.get("started_at") or time.time()
    job["finished_at"] = None
    job["current_item"] = "Listing teams"
    _publish_roster_job(job)
    try:
        # Every (re)start re-lists the scope so a resumed job carries on with
        # whatever is still unrefreshed tonight: teams whose roster was stored
        # earlier in this same job are recognised by the processed counter.
        teams = _roster_scope_teams(int(job.get("top_n") or DEFAULT_ROSTER_TOP_N))
        total = len(teams)
        processed = int(job.get("processed_items") or 0)
        ok = int(job.get("ok") or 0)
        failed = int(job.get("failed") or 0)
        job["total_items"] = total
        job["progress"] = (processed / total) if total else 1.0
        _publish_roster_job(job)
        for team in teams[processed:]:
            stopped = stop_requested()
            if stopped:
                job["status"] = stopped
                job["pause_requested"] = False
                job["cancel_requested"] = False
                job["last_error"] = "Canceled" if stopped == "canceled" else "Paused"
                job["finished_at"] = time.time()
                _publish_roster_job(job)
                return
            name = str(team.get("name") or "")
            job["current_item"] = f"{name} (#{team.get('hltv_rank')})"
            try:
                info = get_hltv_team_lineup(int(team["hltv_team_id"]), name)
                lineup = list(info.get("lineup") or [])
                for p in lineup:
                    add_or_update_player(player_id=int(p["player_id"]), name=str(p.get("name") or "") or None)
                # No confirmed lineup on HLTV: clear the stale slots; the team
                # then shows as "missing" in coverage, which is the truth.
                update_team_roster(int(team["team_id"]), [int(p["player_id"]) for p in lineup])
                if info.get("no_lineup"):
                    job["last_error"] = f"{name}: no current lineup listed on HLTV"
                ok += 1
            except Exception as exc:  # noqa: BLE001 - record and move on
                failed += 1
                job["last_error"] = f"{name}: {exc}"
            processed += 1
            job["processed_items"] = processed
            job["ok"] = ok
            job["failed"] = failed
            job["progress"] = processed / float(total) if total else 1.0
            with ROSTER_JOBS_LOCK:
                live = ROSTER_JOBS.get(job_id) or {}
            job["pause_requested"] = bool(live.get("pause_requested"))
            job["cancel_requested"] = bool(live.get("cancel_requested"))
            job["status"] = "canceling" if job["cancel_requested"] else "pausing" if job["pause_requested"] else "running"
            _publish_roster_job(job)
        job["status"] = "completed"
        job["current_item"] = ""
        job["finished_at"] = time.time()
        job["progress"] = 1.0
        _publish_roster_job(job)
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(exc)
        job["last_error"] = str(exc)
        job["finished_at"] = time.time()
        _publish_roster_job(job)


def _start_roster_worker(job_id: str) -> None:
    with ROSTER_JOBS_LOCK:
        worker = ROSTER_WORKERS.get(job_id)
        if worker and worker.is_alive():
            return
        worker = threading.Thread(target=_run_roster_job, args=(job_id,), daemon=True)
        ROSTER_WORKERS[job_id] = worker
        worker.start()


def _configured_roster_top_n() -> int:
    """The Scheduling tab's top-N setting; the manual start and the coverage
    panel use it so everything agrees on the scope."""
    try:
        from backend.data import schedule_db

        return max(10, min(500, int(schedule_db.get_schedule_config().get("team_data_top_n") or DEFAULT_ROSTER_TOP_N)))
    except Exception:  # noqa: BLE001
        return DEFAULT_ROSTER_TOP_N


@router.get("/rosters/coverage")
def get_roster_coverage(top_n: int = 0):
    n = max(1, min(500, int(top_n))) if int(top_n or 0) > 0 else _configured_roster_top_n()
    return {"status": "ok", **_roster_coverage(n)}


@router.post("/rosters/start")
def start_roster_import_job(payload: dict | None = None):
    body = payload or {}
    top_n = max(1, min(500, int(body.get("top_n") or 0))) if int(body.get("top_n") or 0) > 0 else _configured_roster_top_n()
    latest = _get_latest_roster_job()
    if latest and latest.get("status") in {"queued", "running", "pausing", "canceling"}:
        return {"job_id": latest["job_id"], "status": latest.get("status"), "reused": True}
    now = time.time()
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    job = {
        "job_id": job_id, "status": "queued", "error": "", "last_error": "", "progress": 0.0,
        "processed_items": 0, "total_items": 0, "ok": 0, "failed": 0, "current_item": "",
        "pause_requested": False, "cancel_requested": False, "top_n": top_n,
        "created_at": now, "updated_at": now, "started_at": None, "finished_at": None,
    }
    _publish_roster_job(job)
    _start_roster_worker(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/rosters/latest")
def get_latest_roster_import_job():
    job = _get_latest_roster_job()
    if not job:
        return {"exists": False}
    return {"exists": True, **_roster_job_response(job)}


@router.get("/rosters/job/{job_id}")
def get_roster_import_job(job_id: str):
    return _roster_job_response(_get_roster_job_for_response(job_id))


@router.post("/rosters/job/{job_id}/pause")
def pause_roster_import_job(job_id: str):
    job = _get_roster_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed", "paused"}:
        return _roster_job_response(job)
    job["pause_requested"] = True
    job["status"] = "pausing" if _roster_worker_is_active(job_id) else "paused"
    _publish_roster_job(job)
    return _roster_job_response(job)


@router.post("/rosters/job/{job_id}/cancel")
def cancel_roster_import_job(job_id: str):
    job = _get_roster_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed"}:
        return _roster_job_response(job)
    job["cancel_requested"] = True
    job["pause_requested"] = False
    job["status"] = "canceling" if _roster_worker_is_active(job_id) else "canceled"
    if job["status"] == "canceled":
        job["cancel_requested"] = False
        job["last_error"] = "Canceled"
        job["finished_at"] = time.time()
    _publish_roster_job(job)
    return _roster_job_response(job)


@router.post("/rosters/job/{job_id}/resume")
def resume_roster_import_job(job_id: str):
    job = _get_roster_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled"}:
        return _roster_job_response(job)
    if job.get("status") == "running" and _roster_worker_is_active(job_id):
        return _roster_job_response(job)
    job["status"] = "queued"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["finished_at"] = None
    _publish_roster_job(job)
    _start_roster_worker(job_id)
    return _roster_job_response(job)
