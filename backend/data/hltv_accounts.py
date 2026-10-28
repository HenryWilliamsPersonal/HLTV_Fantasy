"""Each app user's own HLTV login for the My Team page: the session cookies
HLTV handed their browser (autologin, PHPSESSID), the account name HLTV
showed for them, and when the autologin cookie expires. Keyed by the app
user ("user:<id>") or "operator" for the server operator's own account."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

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
            CREATE TABLE IF NOT EXISTS user_hltv_sessions (
                user_key              TEXT PRIMARY KEY,
                cookies_json          TEXT NOT NULL,
                username              TEXT,
                saved_at              REAL NOT NULL,
                autologin_expires_at  REAL
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
        row = conn.execute("SELECT * FROM user_hltv_sessions WHERE user_key = ?", (str(user_key),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        cookies = json.loads(row["cookies_json"] or "[]")
    except Exception:  # noqa: BLE001
        cookies = []
    return {
        "user_key": row["user_key"],
        "cookies": cookies if isinstance(cookies, list) else [],
        "username": row["username"] or "",
        "saved_at": float(row["saved_at"] or 0.0),
        "autologin_expires_at": float(row["autologin_expires_at"]) if row["autologin_expires_at"] else None,
    }


def save(user_key: str, cookies: List[Dict[str, Any]], username: str) -> Dict[str, Any]:
    _ensure()
    expiry = None
    for c in cookies:
        if c.get("name") == "autologin" and c.get("expiry"):
            try:
                expiry = float(c["expiry"])
            except (TypeError, ValueError):
                expiry = None
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO user_hltv_sessions (user_key, cookies_json, username, saved_at, autologin_expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_key) DO UPDATE SET
                cookies_json = excluded.cookies_json, username = excluded.username,
                saved_at = excluded.saved_at, autologin_expires_at = excluded.autologin_expires_at
            """,
            (str(user_key), json.dumps(cookies), str(username or ""), time.time(), expiry),
        )
        conn.commit()
    finally:
        conn.close()
    return get(user_key) or {}


def list_accounts() -> List[Dict[str, Any]]:
    """Every stored session's metadata (never the cookies)."""
    _ensure()
    conn = connect()
    try:
        rows = conn.execute("SELECT user_key, username, saved_at, autologin_expires_at FROM user_hltv_sessions").fetchall()
    finally:
        conn.close()
    return [{"user_key": r["user_key"], "username": r["username"] or "", "saved_at": float(r["saved_at"] or 0.0),
             "autologin_expires_at": float(r["autologin_expires_at"]) if r["autologin_expires_at"] else None} for r in rows]


def delete(user_key: str) -> bool:
    _ensure()
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM user_hltv_sessions WHERE user_key = ?", (str(user_key),))
        conn.commit()
        return int(cur.rowcount or 0) > 0
    finally:
        conn.close()


def status(user_key: str) -> Dict[str, Any]:
    row = get(user_key)
    if not row:
        return {"connected": False}
    exp = row.get("autologin_expires_at")
    return {
        "connected": True,
        "username": row.get("username") or "",
        "saved_at": row.get("saved_at"),
        "autologin_expires_at": exp,
        "days_left": round((float(exp) - time.time()) / 86400, 1) if exp else None,
    }
