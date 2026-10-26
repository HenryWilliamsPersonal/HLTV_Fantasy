"""Each app user's My Team state per event, kept across server restarts so
an opened event page shows the last report at once while a fresh read runs
in the background: which team is theirs (it never changes, so the draft
page is read once per event) and the last report with its read time."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from backend.data.db import connect

_READY = False


def _ensure() -> None:
    global _READY
    if _READY:
        return
    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS my_team_cache (
                user_key     TEXT NOT NULL,
                event_id     INTEGER NOT NULL,
                team_json    TEXT,
                report_json  TEXT,
                read_at      REAL,
                PRIMARY KEY (user_key, event_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    _READY = True


def _loads(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        value = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, dict) else None


def get(user_key: str, event_id: int) -> Optional[Dict[str, Any]]:
    """{"team", "report", "read_at"} or None."""
    _ensure()
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM my_team_cache WHERE user_key = ? AND event_id = ?", (str(user_key), int(event_id))).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"team": _loads(row["team_json"]), "report": _loads(row["report_json"]), "read_at": float(row["read_at"] or 0.0)}


def save_team(user_key: str, event_id: int, team: Dict[str, Any]) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO my_team_cache (user_key, event_id, team_json) VALUES (?, ?, ?)
            ON CONFLICT(user_key, event_id) DO UPDATE SET team_json = excluded.team_json
            """,
            (str(user_key), int(event_id), json.dumps(team)),
        )
        conn.commit()
    finally:
        conn.close()


def save_report(user_key: str, event_id: int, team: Dict[str, Any], report: Dict[str, Any], read_at: Optional[float] = None) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO my_team_cache (user_key, event_id, team_json, report_json, read_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_key, event_id) DO UPDATE SET
                team_json = excluded.team_json, report_json = excluded.report_json, read_at = excluded.read_at
            """,
            (str(user_key), int(event_id), json.dumps(team), json.dumps(report), float(read_at or time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user(user_key: str) -> int:
    _ensure()
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM my_team_cache WHERE user_key = ?", (str(user_key),))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()
