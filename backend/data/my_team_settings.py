"""Per user and event: whether the app sets the suggested boosters on HLTV
by itself (auto mode), and the log of boosters it set."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

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
            CREATE TABLE IF NOT EXISTS my_team_settings (
                user_key      TEXT NOT NULL,
                event_id      INTEGER NOT NULL,
                auto_boosters INTEGER NOT NULL DEFAULT 0,
                updated_at    REAL,
                PRIMARY KEY (user_key, event_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS booster_actions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key      TEXT NOT NULL,
                event_id      INTEGER NOT NULL,
                at            REAL NOT NULL,
                trigger       TEXT,
                player_id     INTEGER,
                player_name   TEXT,
                match_id      INTEGER,
                opponent      TEXT,
                booster_index INTEGER,
                booster_name  TEXT,
                ok            INTEGER NOT NULL,
                message       TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    _READY = True


def auto_enabled(user_key: str, event_id: int) -> bool:
    _ensure()
    conn = connect()
    try:
        row = conn.execute("SELECT auto_boosters FROM my_team_settings WHERE user_key = ? AND event_id = ?", (str(user_key), int(event_id))).fetchone()
    finally:
        conn.close()
    return bool(row and int(row["auto_boosters"] or 0))


def set_auto(user_key: str, event_id: int, enabled: bool) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO my_team_settings (user_key, event_id, auto_boosters, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_key, event_id) DO UPDATE SET auto_boosters = excluded.auto_boosters, updated_at = excluded.updated_at
            """,
            (str(user_key), int(event_id), 1 if enabled else 0, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def auto_pairs() -> List[Tuple[str, int]]:
    """Every (user_key, event_id) with auto mode on."""
    _ensure()
    conn = connect()
    try:
        rows = conn.execute("SELECT user_key, event_id FROM my_team_settings WHERE auto_boosters = 1").fetchall()
    finally:
        conn.close()
    return [(str(r["user_key"]), int(r["event_id"])) for r in rows]


def add_action(user_key: str, event_id: int, **fields: Any) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO booster_actions (user_key, event_id, at, trigger, player_id, player_name, match_id, opponent, booster_index, booster_name, ok, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(user_key), int(event_id), float(fields.get("at") or time.time()), str(fields.get("trigger") or "manual"),
                fields.get("player_id"), fields.get("player_name"), fields.get("match_id"), fields.get("opponent"),
                fields.get("booster_index"), fields.get("booster_name"), 1 if fields.get("ok") else 0, str(fields.get("message") or ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def actions(user_key: str, event_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    _ensure()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM booster_actions WHERE user_key = ? AND event_id = ? ORDER BY at DESC, id DESC LIMIT ?",
            (str(user_key), int(event_id), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_user(user_key: str) -> None:
    _ensure()
    conn = connect()
    try:
        conn.execute("DELETE FROM my_team_settings WHERE user_key = ?", (str(user_key),))
        conn.execute("DELETE FROM booster_actions WHERE user_key = ?", (str(user_key),))
        conn.commit()
    finally:
        conn.close()
