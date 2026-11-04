from typing import Any, Dict, List, Optional
from datetime import date
import re
import json
from urllib.parse import urlsplit, urlunsplit

from backend.data.db import connect


def ensure_event_schema() -> None:
    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id    INTEGER PRIMARY KEY,
                hltv_event_id INTEGER,
                hltv_event_url TEXT,
                imported_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        event_cols = conn.execute("PRAGMA table_info(events)").fetchall()
        event_col_names = {row["name"] for row in event_cols}
        if "hltv_event_id" not in event_col_names:
            conn.execute("ALTER TABLE events ADD COLUMN hltv_event_id INTEGER")
        if "hltv_event_url" not in event_col_names:
            conn.execute("ALTER TABLE events ADD COLUMN hltv_event_url TEXT")
        # Detected group format ("gsl4" | "de8") and the auto-parsed group seeds
        # (as this app's team ids), captured at import so the Groups tab can
        # prefill without a manual "Autofill from HLTV event" click.
        if "group_format" not in event_col_names:
            conn.execute("ALTER TABLE events ADD COLUMN group_format TEXT")
        if "groups_autofill_json" not in event_col_names:
            conn.execute("ALTER TABLE events ADD COLUMN groups_autofill_json TEXT")
        # Manual override of the auto-detected tournament kind for this fantasy
        # event ("swiss" | "groups" | "playoff" | "bounty"); NULL = auto-detect.
        if "tournament_kind" not in event_col_names:
            conn.execute("ALTER TABLE events ADD COLUMN tournament_kind TEXT")
        # Human-readable event name derived from the linked HLTV event URL.
        if "name" not in event_col_names:
            conn.execute("ALTER TABLE events ADD COLUMN name TEXT")
        # Semi-final layout of a 2-group GSL → 4-team combined playoff
        # ("cross" | "same_group"); NULL = the default (routes/groups.py).
        if "playoff_layout" not in event_col_names:
            conn.execute("ALTER TABLE events ADD COLUMN playoff_layout TEXT")
        # Fantasy events removed by the operator: the nightly discovery must not
        # import them again while HLTV still lists them.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_events (
                event_id   INTEGER PRIMARY KEY,
                name       TEXT,
                deleted_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_teams (
                event_id    INTEGER NOT NULL,
                team_name   TEXT NOT NULL,
                player1_id  INTEGER,
                player2_id  INTEGER,
                player3_id  INTEGER,
                player4_id  INTEGER,
                player5_id  INTEGER,
                PRIMARY KEY (event_id, team_name),
                FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_player_prices (
                event_id   INTEGER NOT NULL,
                player_id  INTEGER NOT NULL,
                team_name  TEXT,
                price      INTEGER NOT NULL,
                PRIMARY KEY (event_id, player_id),
                FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                singleton_id    INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                active_event_id INTEGER
            );
            """
        )
        conn.execute("INSERT OR IGNORE INTO app_state (singleton_id, active_event_id) VALUES (1, NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_player_prices_event ON event_player_prices(event_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_player_prices_player ON event_player_prices(player_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_teams_event ON event_teams(event_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hltv_results (
                match_url    TEXT PRIMARY KEY,
                match_id     INTEGER,
                match_date   TEXT,
                team1        TEXT NOT NULL,
                team2        TEXT NOT NULL,
                score1       INTEGER,
                score2       INTEGER,
                winner       TEXT,
                event_name   TEXT,
                source_offset INTEGER DEFAULT 0,
                hltv_points_1 INTEGER,
                hltv_points_2 INTEGER,
                hltv_rank_1   INTEGER,
                hltv_rank_2   INTEGER,
                vrs_points_1  INTEGER,
                vrs_points_2  INTEGER,
                vrs_rank_1    INTEGER,
                vrs_rank_2    INTEGER,
                maps_json     TEXT,
                hltv_effective_date TEXT,
                vrs_effective_date  TEXT,
                points_enriched_at  TEXT,
                imported_at  TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        hltv_cols = conn.execute("PRAGMA table_info(hltv_results)").fetchall()
        hltv_col_names = {row["name"] for row in hltv_cols}
        if "match_date" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN match_date TEXT")
        if "hltv_points_1" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN hltv_points_1 INTEGER")
        if "hltv_points_2" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN hltv_points_2 INTEGER")
        if "hltv_rank_1" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN hltv_rank_1 INTEGER")
        if "hltv_rank_2" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN hltv_rank_2 INTEGER")
        if "vrs_points_1" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN vrs_points_1 INTEGER")
        if "vrs_points_2" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN vrs_points_2 INTEGER")
        if "vrs_rank_1" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN vrs_rank_1 INTEGER")
        if "vrs_rank_2" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN vrs_rank_2 INTEGER")
        if "maps_json" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN maps_json TEXT")
        if "hltv_effective_date" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN hltv_effective_date TEXT")
        if "vrs_effective_date" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN vrs_effective_date TEXT")
        if "points_enriched_at" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN points_enriched_at TEXT")
        if "veto_json" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN veto_json TEXT")
        if "player_stats_json" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN player_stats_json TEXT")
        if "map_player_stats_json" not in hltv_col_names:
            conn.execute("ALTER TABLE hltv_results ADD COLUMN map_player_stats_json TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hltv_results_match_id ON hltv_results(match_id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hltv_results_imported ON hltv_results(imported_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hltv_results_match_date ON hltv_results(match_date DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hltv_rankings_history (
                snapshot_date   TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                team_name       TEXT NOT NULL,
                hltv_rank       INTEGER,
                points          INTEGER,
                source_url      TEXT,
                imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (snapshot_date, normalized_name)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vrs_rankings_history (
                snapshot_date   TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                team_name       TEXT NOT NULL,
                vrs_rank        INTEGER,
                points          INTEGER,
                source_url      TEXT,
                imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (snapshot_date, normalized_name)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hltv_rank_hist_date ON hltv_rankings_history(snapshot_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vrs_rank_hist_date ON vrs_rankings_history(snapshot_date DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS historical_team_map_stats (
                normalized_name TEXT NOT NULL,
                team_name       TEXT NOT NULL,
                hltv_team_id    INTEGER NOT NULL,
                start_date      TEXT NOT NULL,
                end_date        TEXT NOT NULL,
                maps_json       TEXT NOT NULL,
                source_url      TEXT,
                imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (normalized_name, start_date, end_date)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_team_map_stats_team ON historical_team_map_stats(normalized_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_team_map_stats_window ON historical_team_map_stats(start_date, end_date)")
        conn.commit()
    finally:
        conn.close()


def _normalize_team_snapshot(team: Dict[str, Any]) -> Dict[str, Any]:
    team_name = str(team.get("team_name", "")).strip()
    player_ids_raw = team.get("player_ids") or []
    player_ids = [int(x) for x in player_ids_raw if str(x).strip()]
    player_ids = player_ids[:5]
    while len(player_ids) < 5:
        player_ids.append(0)

    raw_prices = team.get("prices_by_player") or {}
    prices_by_player = {}
    for pid, price in raw_prices.items():
        try:
            prices_by_player[int(pid)] = int(price)
        except Exception:
            continue

    return {
        "team_name": team_name,
        "player_ids": player_ids,
        "prices_by_player": prices_by_player,
    }


def get_historical_team_map_stats_keys() -> set[tuple[str, str, str]]:
    """All (normalized_name, start_date, end_date) windows already stored."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT normalized_name, start_date, end_date FROM historical_team_map_stats"
        ).fetchall()
        return {(str(r["normalized_name"]), str(r["start_date"]), str(r["end_date"])) for r in rows}
    finally:
        conn.close()


def get_all_historical_team_map_stats_json() -> Dict[tuple, str]:
    """All stored windows as {(normalized_name, start_date, end_date): maps_json}.

    One query instead of thousands of per-window lookups when the Model Lab
    resolves coverage for a large training slice.
    """
    ensure_event_schema()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT normalized_name, start_date, end_date, maps_json FROM historical_team_map_stats"
        ).fetchall()
        return {
            (str(row["normalized_name"]), str(row["start_date"]), str(row["end_date"])): row["maps_json"]
            for row in rows
        }
    finally:
        conn.close()


def upsert_historical_team_map_stats(
    *,
    normalized_name: str,
    team_name: str,
    hltv_team_id: int,
    start_date: str,
    end_date: str,
    maps_json: str,
    source_url: str,
) -> None:
    ensure_event_schema()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO historical_team_map_stats (
                normalized_name, team_name, hltv_team_id, start_date, end_date, maps_json, source_url, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(normalized_name, start_date, end_date) DO UPDATE SET
                team_name = excluded.team_name,
                hltv_team_id = excluded.hltv_team_id,
                maps_json = excluded.maps_json,
                source_url = excluded.source_url,
                imported_at = excluded.imported_at
            """,
            (
                str(normalized_name),
                str(team_name),
                int(hltv_team_id),
                str(start_date),
                str(end_date),
                str(maps_json or "[]"),
                str(source_url or ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_event_snapshot(
    event_id: int,
    teams: List[Dict[str, Any]],
    hltv_event_id: Optional[int] = None,
    hltv_event_url: Optional[str] = None,
) -> None:
    eid = int(event_id)
    normalized_teams = [_normalize_team_snapshot(t) for t in (teams or [])]
    real_event_id = int(hltv_event_id) if hltv_event_id is not None else None
    real_event_url = str(hltv_event_url or "").strip() or None

    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO events (event_id, hltv_event_id, hltv_event_url, imported_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(event_id) DO UPDATE SET
                hltv_event_id = COALESCE(excluded.hltv_event_id, events.hltv_event_id),
                hltv_event_url = COALESCE(excluded.hltv_event_url, events.hltv_event_url),
                imported_at = excluded.imported_at
            """,
            (eid, real_event_id, real_event_url),
        )
        conn.execute("DELETE FROM event_teams WHERE event_id = ?", (eid,))
        conn.execute("DELETE FROM event_player_prices WHERE event_id = ?", (eid,))

        for team in normalized_teams:
            if not team["team_name"]:
                continue
            pids = team["player_ids"]
            conn.execute(
                """
                INSERT INTO event_teams (
                    event_id, team_name, player1_id, player2_id, player3_id, player4_id, player5_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (eid, team["team_name"], pids[0], pids[1], pids[2], pids[3], pids[4]),
            )
            for pid, price in team["prices_by_player"].items():
                conn.execute(
                    """
                    INSERT INTO event_player_prices (event_id, player_id, team_name, price)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_id, player_id) DO UPDATE SET
                        team_name = excluded.team_name,
                        price = excluded.price
                    """,
                    (eid, int(pid), team["team_name"], int(price)),
                )
        conn.commit()
    finally:
        conn.close()


def set_event_groups_autofill(
    event_id: int, group_format: Optional[str], groups: Optional[List[Dict[str, Any]]]
) -> None:
    """Persist the detected group format and auto-parsed group seeds for an event."""
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE events
            SET group_format = ?,
                groups_autofill_json = ?
            WHERE event_id = ?
            """,
            (
                str(group_format).strip().lower() if group_format else None,
                json.dumps(groups) if groups is not None else None,
                int(event_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_event_groups_autofill(event_id: int) -> Optional[Dict[str, Any]]:
    """The stored {group_format, groups} for an event, or None if not captured."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT group_format, groups_autofill_json FROM events WHERE event_id = ?",
            (int(event_id),),
        ).fetchone()
        if not row:
            return None
        fmt = row["group_format"]
        raw = row["groups_autofill_json"]
        if not fmt and not raw:
            return None
        try:
            groups = json.loads(raw) if raw else []
        except Exception:
            groups = []
        return {"group_format": fmt or "gsl4", "groups": groups}
    finally:
        conn.close()


def get_player_latest_teams() -> Dict[int, Dict[str, Any]]:
    """Each player's CURRENT team, defined by the most recent event they were
    rostered in (event_teams, highest event_id). Team creation order can't be
    used — a player can move to a team whose row is older than their previous
    team's. Returns {player_id: {team_name, event_id}}."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT event_id, team_name,
                   player1_id, player2_id, player3_id, player4_id, player5_id
            FROM event_teams
            ORDER BY event_id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    latest: Dict[int, Dict[str, Any]] = {}
    # Ascending event order: later events simply overwrite earlier entries.
    for r in rows:
        for col in ("player1_id", "player2_id", "player3_id", "player4_id", "player5_id"):
            pid = r[col]
            if pid:
                latest[int(pid)] = {"team_name": str(r["team_name"]), "event_id": int(r["event_id"])}
    return latest


def set_event_tournament_kind(event_id: int, kind: Optional[str]) -> None:
    """Store (or clear with None) the manual tournament-kind override."""
    value = str(kind).strip().lower() if kind else None
    if value not in (None, "swiss", "groups", "playoff", "bounty"):
        value = None
    conn = connect()
    try:
        conn.execute(
            "UPDATE events SET tournament_kind = ? WHERE event_id = ?",
            (value, int(event_id)),
        )
        conn.commit()
    finally:
        conn.close()


def get_event_playoff_layout(event_id: int) -> Optional[str]:
    conn = connect()
    try:
        row = conn.execute("SELECT playoff_layout FROM events WHERE event_id = ?", (int(event_id),)).fetchone()
        return (row["playoff_layout"] or None) if row else None
    finally:
        conn.close()


def set_event_playoff_layout(event_id: int, layout: Optional[str]) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE events SET playoff_layout = ? WHERE event_id = ?", (layout or None, int(event_id)))
        conn.commit()
    finally:
        conn.close()


def delete_event(event_id: int) -> Dict[str, Any]:
    """Remove a fantasy event, its rosters and prices, and remember the id so
    the nightly discovery leaves it alone. Match results and team data are
    shared across events and stay."""
    eid = int(event_id)
    conn = connect()
    try:
        row = conn.execute("SELECT name FROM events WHERE event_id = ?", (eid,)).fetchone()
        name = row["name"] if row else None
        counts = {
            "event_teams": conn.execute("DELETE FROM event_teams WHERE event_id = ?", (eid,)).rowcount,
            "event_player_prices": conn.execute("DELETE FROM event_player_prices WHERE event_id = ?", (eid,)).rowcount,
            "events": conn.execute("DELETE FROM events WHERE event_id = ?", (eid,)).rowcount,
        }
        conn.execute(
            "INSERT OR REPLACE INTO deleted_events (event_id, name, deleted_at) VALUES (?, ?, datetime('now'))",
            (eid, name),
        )
        conn.commit()
        return {"event_id": eid, "name": name, "removed": counts}
    finally:
        conn.close()


def list_deleted_event_ids() -> List[int]:
    conn = connect()
    try:
        return [int(r["event_id"]) for r in conn.execute("SELECT event_id FROM deleted_events").fetchall()]
    finally:
        conn.close()


def get_event_tournament_kind(event_id: int) -> Optional[str]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tournament_kind FROM events WHERE event_id = ?", (int(event_id),)
        ).fetchone()
        return (row["tournament_kind"] or None) if row else None
    finally:
        conn.close()


def set_active_event(event_id: Optional[int]) -> None:
    conn = connect()
    try:
        if event_id is None:
            conn.execute("UPDATE app_state SET active_event_id = NULL WHERE singleton_id = 1")
        else:
            conn.execute(
                "UPDATE app_state SET active_event_id = ? WHERE singleton_id = 1",
                (int(event_id),),
            )
        conn.commit()
    finally:
        conn.close()


def get_active_event_id() -> Optional[int]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT active_event_id FROM app_state WHERE singleton_id = 1"
        ).fetchone()
        if not row:
            return None
        val = row["active_event_id"]
        return int(val) if val is not None else None
    finally:
        conn.close()


def list_events() -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT
                e.event_id,
                e.name,
                e.hltv_event_id,
                e.hltv_event_url,
                e.imported_at,
                (SELECT COUNT(*) FROM event_teams et WHERE et.event_id = e.event_id) AS team_count,
                (SELECT COUNT(*) FROM event_player_prices ep WHERE ep.event_id = e.event_id) AS player_count,
                (SELECT MIN(price) FROM event_player_prices ep WHERE ep.event_id = e.event_id) AS min_price,
                (SELECT MAX(price) FROM event_player_prices ep WHERE ep.event_id = e.event_id) AS max_price
            FROM events e
            ORDER BY e.imported_at DESC, e.event_id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_event_name(
    event_id: int,
    name: Optional[str] = None,
    hltv_event_id: Optional[int] = None,
    hltv_event_url: Optional[str] = None,
    overwrite_name: bool = False,
) -> None:
    """Fill in the event name (and any newly recovered HLTV event ref).
    Refs never overwrite existing values; the name does when overwrite_name is
    set (the fantasy page's own nav name is more authoritative than a name
    derived from an event-URL slug)."""
    clean_name = (str(name).strip() or None) if name else None
    conn = connect()
    try:
        name_sql = "?" if overwrite_name and clean_name else "COALESCE(?, name)"
        conn.execute(
            f"""
            UPDATE events SET
                name = {name_sql},
                hltv_event_id = COALESCE(?, hltv_event_id),
                hltv_event_url = COALESCE(?, hltv_event_url)
            WHERE event_id = ?
            """,
            (clean_name, hltv_event_id, str(hltv_event_url or "").strip() or None, int(event_id)),
        )
        conn.commit()
    finally:
        conn.close()


def get_event_price(player_id: int, event_id: Optional[int] = None) -> Optional[int]:
    eid = get_active_event_id() if event_id is None else int(event_id)
    if eid is None:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT price
            FROM event_player_prices
            WHERE event_id = ? AND player_id = ?
            """,
            (eid, int(player_id)),
        ).fetchone()
        if not row:
            return None
        return int(row["price"])
    finally:
        conn.close()


def get_event_price_map(event_id: Optional[int] = None) -> Dict[int, int]:
    eid = get_active_event_id() if event_id is None else int(event_id)
    if eid is None:
        return {}
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT player_id, price
            FROM event_player_prices
            WHERE event_id = ?
            """,
            (eid,),
        ).fetchall()
        return {int(r["player_id"]): int(r["price"]) for r in rows}
    finally:
        conn.close()


def get_event_detail(event_id: int) -> Optional[Dict[str, Any]]:
    eid = int(event_id)
    conn = connect()
    try:
        event_row = conn.execute(
            "SELECT event_id, hltv_event_id, hltv_event_url, imported_at FROM events WHERE event_id = ?",
            (eid,),
        ).fetchone()
        if not event_row:
            return None

        team_rows = conn.execute(
            """
            SELECT team_name, player1_id, player2_id, player3_id, player4_id, player5_id
            FROM event_teams
            WHERE event_id = ?
            ORDER BY lower(team_name)
            """,
            (eid,),
        ).fetchall()

        player_rows = conn.execute(
            """
            SELECT ep.player_id, p.name AS player_name, ep.team_name, ep.price
            FROM event_player_prices ep
            LEFT JOIN players p ON p.player_id = ep.player_id
            WHERE ep.event_id = ?
            ORDER BY lower(ep.team_name), lower(COALESCE(p.name, '')), ep.player_id
            """,
            (eid,),
        ).fetchall()

        out_teams = []
        for tr in team_rows:
            out_teams.append(
                {
                    "team_name": tr["team_name"],
                    "player_ids": [tr["player1_id"], tr["player2_id"], tr["player3_id"], tr["player4_id"], tr["player5_id"]],
                }
            )

        out_players = [dict(r) for r in player_rows]
        return {
            "event_id": int(event_row["event_id"]),
            "hltv_event_id": int(event_row["hltv_event_id"]) if event_row["hltv_event_id"] is not None else None,
            "hltv_event_url": event_row["hltv_event_url"],
            "imported_at": event_row["imported_at"],
            "teams": out_teams,
            "players": out_players,
        }
    finally:
        conn.close()


def upsert_hltv_results(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    def _canonical_match_url(url: Any) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        try:
            parts = urlsplit(raw)
            path = (parts.path or "").rstrip("/")
            if not path:
                path = "/"
            return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        except Exception:
            return raw.rstrip("/")

    def _to_int(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except Exception:
            return None

    # Deduplicate the incoming payload first.
    # Prefer dedupe by match_id when available, otherwise by canonical match_url.
    deduped_rows: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for src in rows or []:
        row = dict(src or {})
        row["match_url"] = _canonical_match_url(row.get("match_url"))
        mid = _to_int(row.get("match_id"))
        row["match_id"] = mid
        if not row["match_url"]:
            continue
        key = f"id:{mid}" if mid is not None else f"url:{row['match_url']}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped_rows.append(row)

    conn = connect()
    inserted = 0
    updated = 0
    try:
        for r in deduped_rows:
            match_url = str(r.get("match_url") or "").strip()
            if not match_url:
                continue

            # If the same match_id already exists under a different URL, update that row
            # instead of creating a duplicate entry.
            match_id = _to_int(r.get("match_id"))
            if match_id is not None:
                same_match = conn.execute(
                    "SELECT match_url FROM hltv_results WHERE match_id = ? LIMIT 1",
                    (match_id,),
                ).fetchone()
                if same_match and str(same_match["match_url"] or "").strip():
                    match_url = str(same_match["match_url"]).strip()

            existing = conn.execute(
                "SELECT 1 FROM hltv_results WHERE match_url = ?",
                (match_url,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO hltv_results (
                    match_url, match_id, match_date, team1, team2, score1, score2, winner, event_name, source_offset,
                    maps_json, veto_json, player_stats_json, map_player_stats_json, imported_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(match_url) DO UPDATE SET
                    match_id = excluded.match_id,
                    match_date = COALESCE(excluded.match_date, hltv_results.match_date),
                    team1 = excluded.team1,
                    team2 = excluded.team2,
                    score1 = excluded.score1,
                    score2 = excluded.score2,
                    winner = excluded.winner,
                    event_name = excluded.event_name,
                    source_offset = excluded.source_offset,
                    maps_json = COALESCE(excluded.maps_json, hltv_results.maps_json),
                    veto_json = COALESCE(excluded.veto_json, hltv_results.veto_json),
                    player_stats_json = COALESCE(excluded.player_stats_json, hltv_results.player_stats_json),
                    map_player_stats_json = COALESCE(excluded.map_player_stats_json, hltv_results.map_player_stats_json),
                    last_seen_at = datetime('now')
                """,
                (
                    match_url,
                    match_id,
                    str(r.get("match_date")) if r.get("match_date") else None,
                    str(r.get("team1") or ""),
                    str(r.get("team2") or ""),
                    _to_int(r.get("score1")),
                    _to_int(r.get("score2")),
                    str(r.get("winner")) if r.get("winner") is not None else None,
                    str(r.get("event")) if r.get("event") is not None else None,
                    _to_int(r.get("source_offset")) or 0,
                    str(r.get("maps_json")) if r.get("maps_json") is not None else None,
                    str(r.get("veto_json")) if r.get("veto_json") is not None else None,
                    str(r.get("player_stats_json")) if r.get("player_stats_json") is not None else None,
                    str(r.get("map_player_stats_json")) if r.get("map_player_stats_json") is not None else None,
                ),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
        conn.commit()
        return {"inserted": inserted, "updated": updated}
    finally:
        conn.close()


def dedupe_hltv_results_by_match_id() -> int:
    conn = connect()
    removed = 0
    try:
        dup_ids = conn.execute(
            """
            SELECT match_id
            FROM hltv_results
            WHERE match_id IS NOT NULL
            GROUP BY match_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for row in dup_ids:
            mid = int(row["match_id"])
            rows = conn.execute(
                """
                SELECT rowid
                FROM hltv_results
                WHERE match_id = ?
                ORDER BY datetime(last_seen_at) DESC, datetime(imported_at) DESC, rowid DESC
                """,
                (mid,),
            ).fetchall()
            if len(rows) <= 1:
                continue
            keep_rowid = int(rows[0]["rowid"])
            for r in rows[1:]:
                rid = int(r["rowid"])
                if rid == keep_rowid:
                    continue
                conn.execute("DELETE FROM hltv_results WHERE rowid = ?", (rid,))
                removed += 1
        conn.commit()
        return removed
    finally:
        conn.close()


def get_imported_match_ids() -> set[int]:
    """Match ids already stored with map details — used to skip re-importing them."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT match_id FROM hltv_results
            WHERE match_id IS NOT NULL AND maps_json IS NOT NULL AND maps_json != '' AND maps_json != '[]'
            """
        ).fetchall()
        return {int(row["match_id"]) for row in rows}
    finally:
        conn.close()


def update_hltv_results_points(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    conn = connect()
    updated = 0
    try:
        for r in rows:
            match_url = str(r.get("match_url") or "").strip()
            if not match_url:
                continue
            cur = conn.execute(
                """
                UPDATE hltv_results
                SET
                    hltv_points_1 = ?,
                    hltv_points_2 = ?,
                    hltv_rank_1 = ?,
                    hltv_rank_2 = ?,
                    vrs_points_1 = ?,
                    vrs_points_2 = ?,
                    vrs_rank_1 = ?,
                    vrs_rank_2 = ?,
                    hltv_effective_date = ?,
                    vrs_effective_date = ?,
                    points_enriched_at = datetime('now')
                WHERE match_url = ?
                """,
                (
                    int(r["hltv_points_1"]) if r.get("hltv_points_1") is not None else None,
                    int(r["hltv_points_2"]) if r.get("hltv_points_2") is not None else None,
                    int(r["hltv_rank_1"]) if r.get("hltv_rank_1") is not None else None,
                    int(r["hltv_rank_2"]) if r.get("hltv_rank_2") is not None else None,
                    int(r["vrs_points_1"]) if r.get("vrs_points_1") is not None else None,
                    int(r["vrs_points_2"]) if r.get("vrs_points_2") is not None else None,
                    int(r["vrs_rank_1"]) if r.get("vrs_rank_1") is not None else None,
                    int(r["vrs_rank_2"]) if r.get("vrs_rank_2") is not None else None,
                    str(r.get("hltv_effective_date")) if r.get("hltv_effective_date") else None,
                    str(r.get("vrs_effective_date")) if r.get("vrs_effective_date") else None,
                    match_url,
                ),
            )
            if int(cur.rowcount or 0) > 0:
                updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


def list_hltv_results_for_team(team_name: str, limit: int = 30) -> List[Dict[str, Any]]:
    """A team's stored matches, newest first, however old — the recent-form
    endpoint needs a team's history even when they haven't played in months."""
    name = str(team_name or "").strip().lower()
    if not name:
        return []
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT match_url, match_date, team1, team2, score1, score2, winner,
                   player_stats_json
            FROM hltv_results
            WHERE LOWER(TRIM(team1)) = ? OR LOWER(TRIM(team2)) = ?
            ORDER BY match_date DESC, imported_at DESC
            LIMIT ?
            """,
            (name, name, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# Sort keys the stored-matches browser can request; anything else falls back
# to the historical default (match_id desc = newest import first).
_HLTV_RESULTS_SORTS = {
    "date_desc": "match_date DESC, match_id DESC",
    "date_asc": "match_date ASC, match_id ASC",
    "team1_asc": "team1 COLLATE NOCASE ASC, match_date DESC",
    "team1_desc": "team1 COLLATE NOCASE DESC, match_date DESC",
    "team2_asc": "team2 COLLATE NOCASE ASC, match_date DESC",
    "team2_desc": "team2 COLLATE NOCASE DESC, match_date DESC",
    "winner_asc": "winner COLLATE NOCASE ASC, match_date DESC",
    "winner_desc": "winner COLLATE NOCASE DESC, match_date DESC",
}


def list_hltv_results(
    limit: int = 100,
    offset: int = 0,
    search: str = "",
    sort: str = "",
    vrs_rank: int = 0,
    vrs_scope: str = "",
    vrs_dir: str = "",
) -> List[Dict[str, Any]]:
    lim = max(1, min(int(limit), 100000))
    off = max(0, int(offset))
    q = str(search or "").strip()
    # The UI shows day/month/year, so date searches arrive that way too;
    # translate to the stored ISO format (24/08/2026 → 2026-08-24,
    # 08/2026 → 2026-08, 24/08 → -08-24).
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", q)
    if m:
        q = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    else:
        m = re.fullmatch(r"(\d{1,2})/(\d{4})", q)
        if m:
            q = f"{m.group(2)}-{int(m.group(1)):02d}"
        else:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", q)
            if m:
                q = f"-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    conds: list = []
    params: list = []
    if q:
        conds.append("(team1 LIKE ? OR team2 LIKE ? OR match_date LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    # VRS rank filter: matches where both/at least one team is inside (<= N)
    # or outside (> N) the top N. Teams with no VRS rank count as outside.
    rank_n = int(vrs_rank or 0)
    scope = str(vrs_scope or "").strip().lower()
    direction = str(vrs_dir or "").strip().lower()
    if rank_n > 0 and scope in {"both", "either"} and direction in {"within", "outside"}:
        if direction == "within":
            c1 = "(vrs_rank_1 IS NOT NULL AND vrs_rank_1 <= ?)"
            c2 = "(vrs_rank_2 IS NOT NULL AND vrs_rank_2 <= ?)"
        else:
            c1 = "(vrs_rank_1 IS NULL OR vrs_rank_1 > ?)"
            c2 = "(vrs_rank_2 IS NULL OR vrs_rank_2 > ?)"
        joiner = " AND " if scope == "both" else " OR "
        conds.append(f"({c1}{joiner}{c2})")
        params += [rank_n, rank_n]
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    order = _HLTV_RESULTS_SORTS.get(
        str(sort or "").strip().lower(),
        "CASE WHEN match_id IS NULL THEN 1 ELSE 0 END ASC, match_id DESC, imported_at DESC",
    )
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT
                match_url,
                match_id,
                match_date,
                team1,
                team2,
                score1,
                score2,
                winner,
                event_name,
                source_offset,
                hltv_points_1,
                hltv_points_2,
                hltv_rank_1,
                hltv_rank_2,
                vrs_points_1,
                vrs_points_2,
                vrs_rank_1,
                vrs_rank_2,
                maps_json,
                veto_json,
                player_stats_json,
                map_player_stats_json,
                hltv_effective_date,
                vrs_effective_date,
                points_enriched_at,
                imported_at,
                last_seen_at
            FROM hltv_results
            {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            (*params, lim, off),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["event"] = d.pop("event_name", None)
            out.append(d)
        return out
    finally:
        conn.close()


def update_hltv_result_details(
    match_url: str,
    *,
    veto_json: Optional[str] = None,
    player_stats_json: Optional[str] = None,
    maps_json: Optional[str] = None,
    map_player_stats_json: Optional[str] = None,
) -> None:
    """Backfill match-page details on an already stored result row."""
    sets = []
    params: list = []
    for col, val in (
        ("veto_json", veto_json),
        ("player_stats_json", player_stats_json),
        ("maps_json", maps_json),
        ("map_player_stats_json", map_player_stats_json),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    conn = connect()
    try:
        conn.execute(f"UPDATE hltv_results SET {', '.join(sets)} WHERE match_url = ?", (*params, str(match_url)))
        conn.commit()
    finally:
        conn.close()


def get_hltv_result_by_url(match_url: str) -> Optional[Dict[str, Any]]:
    mu = str(match_url or "").strip()
    if not mu:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT
                match_url,
                match_id,
                match_date,
                team1,
                team2,
                score1,
                score2,
                winner,
                event_name,
                source_offset,
                hltv_points_1,
                hltv_points_2,
                hltv_rank_1,
                hltv_rank_2,
                vrs_points_1,
                vrs_points_2,
                vrs_rank_1,
                vrs_rank_2,
                maps_json,
                veto_json,
                player_stats_json,
                map_player_stats_json,
                hltv_effective_date,
                vrs_effective_date,
                points_enriched_at,
                imported_at,
                last_seen_at
            FROM hltv_results
            WHERE match_url = ?
            LIMIT 1
            """,
            (mu,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["event"] = d.pop("event_name", None)
        return d
    finally:
        conn.close()


def set_hltv_result_maps(match_url: str, maps: List[Dict[str, Any]]) -> bool:
    mu = str(match_url or "").strip()
    if not mu:
        return False
    payload = json.dumps(maps or [])
    conn = connect()
    try:
        cur = conn.execute(
            """
            UPDATE hltv_results
            SET maps_json = ?,
                last_seen_at = datetime('now')
            WHERE match_url = ?
            """,
            (payload, mu),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0
    finally:
        conn.close()


def count_hltv_results() -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM hltv_results").fetchone()
        return int(row["c"] or 0) if row else 0
    finally:
        conn.close()


def clear_hltv_results() -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM hltv_results").fetchone()
        count_before = int(row["c"] or 0) if row else 0
        conn.execute("DELETE FROM hltv_results")
        conn.commit()
        return count_before
    finally:
        conn.close()


def _norm_team_name(name: str) -> str:
    """Normalised team name with renames resolved (see team_aliases.py)."""
    from backend.data import team_aliases

    return team_aliases.key(name)


def upsert_hltv_rankings_snapshot(
    snapshot_date: str,
    rankings_by_team: Dict[str, Dict[str, Any]],
    source_url: Optional[str] = None,
) -> int:
    sd = str(snapshot_date or "").strip()
    if not sd or not rankings_by_team:
        return 0
    conn = connect()
    written = 0
    try:
        for key, data in rankings_by_team.items():
            team_name = str((data or {}).get("team_name") or "").strip()
            if not team_name:
                continue
            norm = _norm_team_name(key or team_name)
            if not norm:
                continue
            conn.execute(
                """
                INSERT INTO hltv_rankings_history (
                    snapshot_date, normalized_name, team_name, hltv_rank, points, source_url, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(snapshot_date, normalized_name) DO UPDATE SET
                    team_name = excluded.team_name,
                    hltv_rank = excluded.hltv_rank,
                    points = excluded.points,
                    source_url = excluded.source_url,
                    imported_at = datetime('now')
                """,
                (
                    sd,
                    norm,
                    team_name,
                    int((data or {}).get("hltv_rating")) if (data or {}).get("hltv_rating") is not None else None,
                    int((data or {}).get("points")) if (data or {}).get("points") is not None else None,
                    str(source_url) if source_url else None,
                ),
            )
            written += 1
        conn.commit()
        return written
    finally:
        conn.close()


def upsert_vrs_rankings_snapshot(
    snapshot_date: str,
    rankings_by_team: Dict[str, Dict[str, Any]],
    source_url: Optional[str] = None,
) -> int:
    sd = str(snapshot_date or "").strip()
    if not sd or not rankings_by_team:
        return 0
    conn = connect()
    written = 0
    try:
        for key, data in rankings_by_team.items():
            team_name = str((data or {}).get("team_name") or "").strip()
            if not team_name:
                continue
            norm = _norm_team_name(key or team_name)
            if not norm:
                continue
            conn.execute(
                """
                INSERT INTO vrs_rankings_history (
                    snapshot_date, normalized_name, team_name, vrs_rank, points, source_url, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(snapshot_date, normalized_name) DO UPDATE SET
                    team_name = excluded.team_name,
                    vrs_rank = excluded.vrs_rank,
                    points = excluded.points,
                    source_url = excluded.source_url,
                    imported_at = datetime('now')
                """,
                (
                    sd,
                    norm,
                    team_name,
                    int((data or {}).get("vrs_rank")) if (data or {}).get("vrs_rank") is not None else None,
                    int((data or {}).get("points")) if (data or {}).get("points") is not None else None,
                    str(source_url) if source_url else None,
                ),
            )
            written += 1
        conn.commit()
        return written
    finally:
        conn.close()


def _get_cached_snapshot_date(table: str, requested_date: str, max_days_back: int) -> Optional[str]:
    rd = str(requested_date or "").strip()
    if not rd:
        return None
    req = date.fromisoformat(rd)
    min_date = req.fromordinal(req.toordinal() - max(0, int(max_days_back))).isoformat()
    conn = connect()
    try:
        row = conn.execute(
            f"""
            SELECT snapshot_date
            FROM {table}
            WHERE snapshot_date <= ? AND snapshot_date >= ?
            GROUP BY snapshot_date
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            (rd, min_date),
        ).fetchone()
        return str(row["snapshot_date"]) if row and row["snapshot_date"] else None
    finally:
        conn.close()


def get_cached_hltv_rankings_on_or_before_date(requested_date: str, max_days_back: int = 7) -> Optional[Dict[str, Any]]:
    eff = _get_cached_snapshot_date("hltv_rankings_history", requested_date, max_days_back)
    if not eff:
        return None
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT normalized_name, team_name, hltv_rank, points, source_url
            FROM hltv_rankings_history
            WHERE snapshot_date = ?
            """,
            (eff,),
        ).fetchall()
        if not rows:
            return None
        rankings: Dict[str, Dict[str, Any]] = {}
        source_url = None
        for r in rows:
            key = str(r["normalized_name"] or "").strip()
            if not key:
                continue
            rankings[key] = {
                "team_name": str(r["team_name"] or ""),
                "hltv_rating": int(r["hltv_rank"]) if r["hltv_rank"] is not None else None,
                "points": int(r["points"]) if r["points"] is not None else None,
            }
            if not source_url and r["source_url"]:
                source_url = str(r["source_url"])
        days_back = max(0, date.fromisoformat(requested_date).toordinal() - date.fromisoformat(eff).toordinal())
        return {
            "requested_date": str(requested_date),
            "effective_date": eff,
            "days_back": int(days_back),
            "url": source_url,
            "rankings_by_team": rankings,
        }
    finally:
        conn.close()


def get_cached_vrs_rankings_on_or_before_date(requested_date: str, max_days_back: int = 7) -> Optional[Dict[str, Any]]:
    eff = _get_cached_snapshot_date("vrs_rankings_history", requested_date, max_days_back)
    if not eff:
        return None
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT normalized_name, team_name, vrs_rank, points, source_url
            FROM vrs_rankings_history
            WHERE snapshot_date = ?
            """,
            (eff,),
        ).fetchall()
        if not rows:
            return None
        rankings: Dict[str, Dict[str, Any]] = {}
        source_url = None
        for r in rows:
            key = str(r["normalized_name"] or "").strip()
            if not key:
                continue
            rankings[key] = {
                "team_name": str(r["team_name"] or ""),
                "vrs_rank": int(r["vrs_rank"]) if r["vrs_rank"] is not None else None,
                "points": int(r["points"]) if r["points"] is not None else None,
            }
            if not source_url and r["source_url"]:
                source_url = str(r["source_url"])
        days_back = max(0, date.fromisoformat(requested_date).toordinal() - date.fromisoformat(eff).toordinal())
        return {
            "requested_date": str(requested_date),
            "effective_date": eff,
            "days_back": int(days_back),
            "url": source_url,
            "rankings_by_team": rankings,
        }
    finally:
        conn.close()
