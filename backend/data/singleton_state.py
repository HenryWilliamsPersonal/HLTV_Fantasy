"""Small tables that persist the latest (payload, result) JSON pair for a feature.

Historically one row per table (`singleton_id = 1`). Tables created with
`keyed=True` hold one row PER KEY instead — the groups simulator keys its
stored simulation and best-team combos by fantasy event, so switching the
active event never shows another tournament's stored run. Un-keyed tables keep
the single-row behaviour (key 1) unchanged.
"""

import json
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from backend.data.db import connect


class SingletonState:
    """A saved payload/result pair kept in a one-row-per-key table.

    `iso_timestamps` keeps the legacy per-table updated_at format: ISO-8601 TEXT
    for the swiss simulation table, epoch REAL for the playoff tables.

    Loads are cached in-process (per key) because parsing these blobs per
    request stalls the backend. Caching is size-capped: blobs above
    _CACHE_MAX_RAW_BYTES parse per request instead of staying resident (a
    755 MB combos blob expands to several GB of Python objects and causes
    MemoryError if retained). This process is the only writer, so
    save()/delete()/invalidate() keep the cache coherent. Callers must treat
    loaded values as read-only.
    """

    _CACHE_MAX_RAW_BYTES = 100 * 1024 * 1024
    # Parsing an oversized blob transiently needs several GB; two of them at
    # once (e.g. the playoff and bounty combo stores) can OOM the process, so
    # oversized parses are serialized across ALL instances, not just per table.
    _BIG_PARSE_LOCK = threading.Lock()

    def __init__(
        self,
        table: str,
        *,
        result_column: str = "result_json",
        result_key: str = "result",
        iso_timestamps: bool = False,
        keyed: bool = False,
    ):
        self.table = table
        self.result_column = result_column
        self.result_key = result_key
        self.iso_timestamps = iso_timestamps
        self.keyed = keyed
        self._cache_lock = threading.Lock()
        # key -> cached value; a cached None means "known to be empty".
        self._cache: Dict[int, Optional[dict]] = {}
        # Serializes oversized (uncached) loads: parsing one of these blobs can
        # transiently need several GB, and two concurrent parses OOM the backend.
        self._load_lock = threading.Lock()

    # ---- helpers -------------------------------------------------------------
    def _key(self, key: Optional[int]) -> int:
        if key is None:
            return 1
        if not self.keyed and int(key) != 1:
            raise ValueError(f"{self.table} is a single-row table; key {key} is not allowed")
        return int(key)

    def invalidate(self, key: Optional[int] = None) -> None:
        with self._cache_lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(int(key), None)

    def _now(self):
        if self.iso_timestamps:
            return datetime.now(timezone.utc).isoformat()
        return float(time.time())

    def _ddl(self, name: str) -> str:
        check = "" if self.keyed else " CHECK(singleton_id = 1)"
        return f"""
            CREATE TABLE IF NOT EXISTS {name} (
                singleton_id INTEGER PRIMARY KEY{check},
                payload_json TEXT NOT NULL,
                {self.result_column} TEXT NOT NULL,
                updated_at {"TEXT" if self.iso_timestamps else "REAL"} NOT NULL
            );
        """

    def ensure_table(self) -> None:
        conn = connect()
        try:
            conn.execute(self._ddl(self.table))
            if self.keyed:
                # A table created by the single-row version still carries the
                # CHECK(singleton_id = 1) constraint, which SQLite cannot drop
                # in place: rebuild it once, keeping the legacy row as key 1.
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (self.table,)
                ).fetchone()
                if row and "CHECK" in str(row[0] or "").upper():
                    tmp = f"{self.table}__keyed"
                    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
                    conn.execute(self._ddl(tmp))
                    conn.execute(
                        f"INSERT INTO {tmp} (singleton_id, payload_json, {self.result_column}, updated_at) "
                        f"SELECT singleton_id, payload_json, {self.result_column}, updated_at FROM {self.table}"
                    )
                    conn.execute(f"DROP TABLE {self.table}")
                    conn.execute(f"ALTER TABLE {tmp} RENAME TO {self.table}")
            conn.commit()
        finally:
            conn.close()

    def exists(self, key: Optional[int] = None) -> bool:
        """Whether a row is stored for the key, without parsing the blob."""
        k = self._key(key)
        with self._cache_lock:
            if k in self._cache:
                return self._cache[k] is not None
        conn = connect()
        try:
            return conn.execute(f"SELECT 1 FROM {self.table} WHERE singleton_id = ?", (k,)).fetchone() is not None
        finally:
            conn.close()

    def migrate_legacy_row(self, to_key: int) -> bool:
        """Re-key the single-row era row (key 1) as `to_key` when no row for
        that key exists yet: when a table becomes keyed, the run it holds
        keeps showing for the event it belonged to (the active one)."""
        if not self.keyed or int(to_key) == 1:
            return False
        conn = connect()
        try:
            has_legacy = conn.execute(f"SELECT 1 FROM {self.table} WHERE singleton_id = 1").fetchone()
            has_target = conn.execute(
                f"SELECT 1 FROM {self.table} WHERE singleton_id = ?", (int(to_key),)
            ).fetchone()
            if not has_legacy or has_target:
                return False
            conn.execute(f"UPDATE {self.table} SET singleton_id = ? WHERE singleton_id = 1", (int(to_key),))
            conn.commit()
        finally:
            conn.close()
        self.invalidate()
        return True

    # ---- persistence ---------------------------------------------------------
    def save(self, payload: dict, result: dict, key: Optional[int] = None) -> None:
        k = self._key(key)
        stamp = self._now()
        payload_text = json.dumps(payload)
        result_text = json.dumps(result)
        raw_size = len(payload_text) + len(result_text)
        conn = connect()
        try:
            conn.execute(
                f"""
                INSERT INTO {self.table} (singleton_id, payload_json, {self.result_column}, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    {self.result_column} = excluded.{self.result_column},
                    updated_at = excluded.updated_at
                """,
                (k, payload_text, result_text, stamp),
            )
            conn.commit()
        finally:
            conn.close()
        with self._cache_lock:
            if raw_size <= self._CACHE_MAX_RAW_BYTES:
                self._cache[k] = {
                    "payload": payload,
                    self.result_key: result,
                    "updated_at": stamp if self.iso_timestamps else float(stamp),
                }
            else:
                self._cache.pop(k, None)

    def load_payload(self, key: Optional[int] = None) -> Optional[dict]:
        """The stored payload alone (small), without parsing the result blob."""
        k = self._key(key)
        with self._cache_lock:
            hit = self._cache.get(k)
            if hit is not None:
                return hit.get("payload")
        conn = connect()
        try:
            row = conn.execute(f"SELECT payload_json FROM {self.table} WHERE singleton_id = ?", (k,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row and row[0] else None

    def keys(self) -> list:
        """Every stored key (one row per key when keyed)."""
        conn = connect()
        try:
            return [int(r["singleton_id"]) for r in conn.execute(f"SELECT singleton_id FROM {self.table}").fetchall()]
        finally:
            conn.close()

    def delete(self, key: Optional[int] = None) -> None:
        k = self._key(key)
        conn = connect()
        try:
            conn.execute(f"DELETE FROM {self.table} WHERE singleton_id = ?", (k,))
            conn.commit()
        finally:
            conn.close()
        self.invalidate(k)

    def load(self, key: Optional[int] = None) -> Optional[dict]:
        k = self._key(key)
        with self._cache_lock:
            if k in self._cache:
                return self._cache[k]
        with self._load_lock:
            # Another request may have populated the cache while we waited.
            with self._cache_lock:
                if k in self._cache:
                    return self._cache[k]
            conn = connect()
            try:
                row = conn.execute(
                    f"SELECT payload_json, {self.result_column}, updated_at FROM {self.table} WHERE singleton_id = ?",
                    (k,),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                with self._cache_lock:
                    self._cache[k] = None
                return None
            payload_text = row["payload_json"]
            result_text = row[self.result_column]
            raw_size = len(payload_text or "") + len(result_text or "")
            updated_at = row["updated_at"] if self.iso_timestamps else float(row["updated_at"])
            del row
            if raw_size > self._CACHE_MAX_RAW_BYTES:
                with SingletonState._BIG_PARSE_LOCK:
                    payload = json.loads(payload_text)
                    del payload_text
                    result = json.loads(result_text)
                    del result_text
            else:
                payload = json.loads(payload_text)
                del payload_text
                result = json.loads(result_text)
                del result_text
            value = {
                "payload": payload,
                self.result_key: result,
                "updated_at": updated_at,
            }
            if raw_size <= self._CACHE_MAX_RAW_BYTES:
                with self._cache_lock:
                    self._cache[k] = value
            return value
