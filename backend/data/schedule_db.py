"""Persistence for the nightly data-ingestion scheduler: one config row plus a
run-history log. Kept in the main SQLite DB alongside the rest of the app state.
"""

import threading
import time
from typing import Any, Dict, List, Optional

from backend.data.db import connect as _connect

_schema_lock = threading.Lock()
_schema_ready = False

# Task keys the scheduler knows how to run, in the order it runs them (they all
# serialize on the shared HLTV browser anyway). Events first: a new fantasy
# event's teams get rankings/matches the same night.
TASK_KEYS = ("events", "rankings", "team_data", "matches", "map_model")

_DEFAULT_CONFIG = {
    "enabled": 1,
    "run_time": "00:00",
    "do_events": 1,
    "do_rankings": 1,
    "do_matches": 1,
    "do_map_model": 1,
    # Team rosters + six-month map stats for the top-N ranked teams, nightly.
    "do_team_data": 1,
    "team_data_top_n": 200,
    "matches_lookback_days": 3,
    # Nightly time budget for the map-model backfills (historical map stats,
    # vetoes, per-map scoreboards); whatever is left continues the next night.
    "map_model_minutes": 120,
}


def ensure_schedule_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_config (
                    singleton_id          INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    enabled               INTEGER NOT NULL DEFAULT 1,
                    run_time              TEXT    NOT NULL DEFAULT '00:00',
                    do_rankings           INTEGER NOT NULL DEFAULT 1,
                    do_matches            INTEGER NOT NULL DEFAULT 1,
                    do_ratings            INTEGER NOT NULL DEFAULT 1,
                    matches_lookback_days INTEGER NOT NULL DEFAULT 3,
                    updated_at            REAL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task        TEXT NOT NULL,
                    trigger     TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    message     TEXT,
                    started_at  REAL,
                    finished_at REAL
                );
                """
            )
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(schedule_config)").fetchall()}
            if "do_events" not in cols:
                conn.execute("ALTER TABLE schedule_config ADD COLUMN do_events INTEGER NOT NULL DEFAULT 1")
            if "do_map_model" not in cols:
                conn.execute("ALTER TABLE schedule_config ADD COLUMN do_map_model INTEGER NOT NULL DEFAULT 1")
            if "map_model_minutes" not in cols:
                conn.execute("ALTER TABLE schedule_config ADD COLUMN map_model_minutes INTEGER NOT NULL DEFAULT 120")
            if "do_team_data" not in cols:
                conn.execute("ALTER TABLE schedule_config ADD COLUMN do_team_data INTEGER NOT NULL DEFAULT 1")
            if "team_data_top_n" not in cols:
                conn.execute("ALTER TABLE schedule_config ADD COLUMN team_data_top_n INTEGER NOT NULL DEFAULT 200")
            conn.execute(
                "INSERT OR IGNORE INTO schedule_config (singleton_id) VALUES (1)"
            )
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True


def get_schedule_config() -> Dict[str, Any]:
    ensure_schedule_schema()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM schedule_config WHERE singleton_id = 1").fetchone()
    finally:
        conn.close()
    if not row:
        return dict(_DEFAULT_CONFIG)
    cfg = {k: row[k] for k in row.keys()}
    cfg.pop("do_ratings", None)  # the Top-X ratings task is gone; the column stays in old databases
    for flag in ("enabled", "do_events", "do_rankings", "do_matches", "do_map_model", "do_team_data"):
        cfg[flag] = bool(cfg.get(flag))
    cfg["matches_lookback_days"] = int(cfg.get("matches_lookback_days") or 3)
    cfg["map_model_minutes"] = int(cfg.get("map_model_minutes") or 120)
    cfg["team_data_top_n"] = int(cfg.get("team_data_top_n") or 200)
    return cfg


def update_schedule_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    ensure_schedule_schema()
    allowed = {
        "enabled": lambda v: 1 if v else 0,
        "do_events": lambda v: 1 if v else 0,
        "do_rankings": lambda v: 1 if v else 0,
        "do_matches": lambda v: 1 if v else 0,
        "do_map_model": lambda v: 1 if v else 0,
        "do_team_data": lambda v: 1 if v else 0,
        "team_data_top_n": lambda v: max(10, min(500, int(v))),
        "run_time": lambda v: _normalize_time(str(v)),
        "matches_lookback_days": lambda v: max(1, min(30, int(v))),
        "map_model_minutes": lambda v: max(1, min(720, int(v))),
    }
    sets = []
    vals: List[Any] = []
    for key, coerce in allowed.items():
        if key in patch and patch[key] is not None:
            try:
                sets.append(f"{key} = ?")
                vals.append(coerce(patch[key]))
            except (TypeError, ValueError):
                continue
    if sets:
        sets.append("updated_at = ?")
        vals.append(time.time())
        conn = _connect()
        try:
            conn.execute(
                f"UPDATE schedule_config SET {', '.join(sets)} WHERE singleton_id = 1",
                tuple(vals),
            )
            conn.commit()
        finally:
            conn.close()
    return get_schedule_config()


def _normalize_time(value: str) -> str:
    parts = str(value or "").strip().split(":")
    hh = max(0, min(23, int(parts[0]))) if parts and parts[0] != "" else 0
    mm = max(0, min(59, int(parts[1]))) if len(parts) > 1 and parts[1] != "" else 0
    return f"{hh:02d}:{mm:02d}"


def start_run(task: str, trigger: str) -> int:
    ensure_schedule_schema()
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO schedule_runs (task, trigger, status, started_at) VALUES (?, ?, 'running', ?)",
            (str(task), str(trigger), time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def finish_run(run_id: int, status: str, message: str = "") -> None:
    ensure_schedule_schema()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE schedule_runs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
            (str(status), str(message)[:2000], time.time(), int(run_id)),
        )
        conn.commit()
    finally:
        conn.close()


def fail_orphan_runs() -> list:
    """Close out run rows stuck as 'running' — they belong to a previous
    process that died mid-task and would otherwise show as running forever.
    Returns the rows closed ({task, trigger, started_at}) so the scheduler
    can resume the batch they belonged to."""
    ensure_schedule_schema()
    conn = _connect()
    try:
        rows = conn.execute("SELECT task, trigger, started_at FROM schedule_runs WHERE status = 'running'").fetchall()
        orphans = [{"task": str(r["task"]), "trigger": str(r["trigger"] or ""), "started_at": float(r["started_at"] or 0.0)} for r in rows]
        conn.execute(
            "UPDATE schedule_runs SET status = 'error', message = 'Interrupted by backend restart', finished_at = ? "
            "WHERE status = 'running'",
            (time.time(),),
        )
        conn.commit()
        return orphans
    finally:
        conn.close()


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    ensure_schedule_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM schedule_runs ORDER BY id DESC LIMIT ?", (int(max(1, min(500, limit))),)
        ).fetchall()
    finally:
        conn.close()
    return [{k: r[k] for k in r.keys()} for r in rows]


def last_scheduled_run_ts() -> float:
    """Start time of the most recent *scheduled* (not manual) run, so the loop
    knows whether today's slot has already fired (survives process restarts)."""
    ensure_schedule_schema()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(started_at) AS ts FROM schedule_runs WHERE trigger IN ('scheduled', 'catch-up')"
        ).fetchone()
    finally:
        conn.close()
    return float((row["ts"] if row else 0) or 0.0)


def last_success_by_task() -> Dict[str, float]:
    """Most recent successful finish time per task, for the UI 'last run' column."""
    ensure_schedule_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT task, MAX(finished_at) AS ts FROM schedule_runs WHERE status = 'success' GROUP BY task"
        ).fetchall()
    finally:
        conn.close()
    return {str(r["task"]): float(r["ts"] or 0.0) for r in rows}
