"""The last HLTV fantasy season read per app user (services/fantasy_season),
kept across restarts so the Season tab opens with the stored copy at once."""
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
            CREATE TABLE IF NOT EXISTS season_cache (
                user_key    TEXT PRIMARY KEY,
                season_json TEXT NOT NULL,
                read_at     REAL NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    _READY = True


def get(user_key: str) -> Optional[Dict[str, Any]]:
    _ensure()
    conn = connect()
    try:
        row = conn.execute("SELECT season_json, read_at FROM season_cache WHERE user_key = ?", (str(user_key),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        season = json.loads(row["season_json"] or "{}")
    except Exception:  # noqa: BLE001
        return None
    return {"season": season if isinstance(season, dict) else None, "read_at": float(row["read_at"] or 0.0)}


def save(user_key: str, season: Dict[str, Any]) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO season_cache (user_key, season_json, read_at) VALUES (?, ?, ?)
            ON CONFLICT(user_key) DO UPDATE SET season_json = excluded.season_json, read_at = excluded.read_at
            """,
            (str(user_key), json.dumps(season), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
