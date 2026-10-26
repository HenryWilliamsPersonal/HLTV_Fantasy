import time
from typing import List, Dict, Optional

from backend.data.db import connect


def ensure_team_schema() -> None:
    """Creates the teams table if it does not already exist."""
    conn = connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teams (
            team_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            hltv_team_id  INTEGER,
            name          TEXT NOT NULL UNIQUE,
            hltv_rank     INTEGER,
            hltv_points   INTEGER,
            vrs_rank      INTEGER,
            vrs_points    INTEGER,
            win_rate      REAL,
            map_stats_json TEXT,
            map_stats_imported_at TEXT,
            map_stats_source_url TEXT,

            player1_id    INTEGER,
            player2_id    INTEGER,
            player3_id    INTEGER,
            player4_id    INTEGER,
            player5_id    INTEGER,

            FOREIGN KEY(player1_id) REFERENCES players(player_id),
            FOREIGN KEY(player2_id) REFERENCES players(player_id),
            FOREIGN KEY(player3_id) REFERENCES players(player_id),
            FOREIGN KEY(player4_id) REFERENCES players(player_id),
            FOREIGN KEY(player5_id) REFERENCES players(player_id)
        );
        """
    )
    # Backward-compatible migration for existing databases.
    cols = conn.execute("PRAGMA table_info(teams)").fetchall()
    col_names = {row["name"] for row in cols}
    if "hltv_team_id" not in col_names:
        conn.execute("ALTER TABLE teams ADD COLUMN hltv_team_id INTEGER")
    if "hltv_points" not in col_names:
        conn.execute("ALTER TABLE teams ADD COLUMN hltv_points INTEGER")
    if "vrs_points" not in col_names:
        conn.execute("ALTER TABLE teams ADD COLUMN vrs_points INTEGER")
    if "map_stats_json" not in col_names:
        conn.execute("ALTER TABLE teams ADD COLUMN map_stats_json TEXT")
    if "map_stats_imported_at" not in col_names:
        conn.execute("ALTER TABLE teams ADD COLUMN map_stats_imported_at TEXT")
    if "map_stats_source_url" not in col_names:
        conn.execute("ALTER TABLE teams ADD COLUMN map_stats_source_url TEXT")
    if "roster_checked_at" not in col_names:
        conn.execute("ALTER TABLE teams ADD COLUMN roster_checked_at REAL")
    conn.commit()
    conn.close()


def add_or_update_team(
    *,
    name: str,
    hltv_rank: int,
    hltv_points: Optional[int] = None,
    vrs_rank: int,
    vrs_points: Optional[int] = None,
    win_rate: float,
    player_ids: List[int],  # exactly 5 HLTV player IDs
    hltv_team_id: Optional[int] = None,
) -> None:
    """Insert a new team or update existing team based on name."""
    if len(player_ids) != 5:
        raise ValueError("player_ids must contain exactly 5 player IDs.")

    conn = connect()
    conn.execute(
        """
        INSERT INTO teams (
            hltv_team_id, name, hltv_rank, hltv_points, vrs_rank,
            vrs_points, win_rate,
            player1_id, player2_id, player3_id, player4_id, player5_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            hltv_team_id = COALESCE(excluded.hltv_team_id, teams.hltv_team_id),
            hltv_rank = excluded.hltv_rank,
            hltv_points = excluded.hltv_points,
            vrs_rank = excluded.vrs_rank,
            vrs_points = excluded.vrs_points,
            win_rate = excluded.win_rate,
            player1_id = excluded.player1_id,
            player2_id = excluded.player2_id,
            player3_id = excluded.player3_id,
            player4_id = excluded.player4_id,
            player5_id = excluded.player5_id;
        """,
        (
            hltv_team_id,
            name,
            hltv_rank,
            hltv_points,
            vrs_rank,
            vrs_points,
            win_rate,
            player_ids[0],
            player_ids[1],
            player_ids[2],
            player_ids[3],
            player_ids[4],
        ),
    )
    conn.commit()
    conn.close()


def update_team_roster(team_id: int, player_ids: List[int], checked_at: Optional[float] = None) -> None:
    """Store a team's current lineup in its five roster slots (missing slots
    become 0) and stamp when HLTV was read for it. Only the slots and the
    stamp change; ranks and stats are left alone."""
    ids = [int(p) for p in list(player_ids)[:5] if p] + [0] * 5
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE teams
            SET player1_id = ?, player2_id = ?, player3_id = ?, player4_id = ?, player5_id = ?, roster_checked_at = ?
            WHERE team_id = ?
            """,
            (ids[0], ids[1], ids[2], ids[3], ids[4], float(checked_at if checked_at is not None else time.time()), int(team_id)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_stale_hltv_ranks(keep_team_ids: List[int]) -> int:
    """Teams the latest HLTV ranking no longer lists drop to the unranked
    placeholder (999) so nothing scopes by a rank they held months ago.

    A kept row's twins — other rows with the same HLTV team id, i.e. the same
    team under another spelling (BetBoom / BETBOOM) — are the same team: they
    are never unranked (the refresh loops sync their ranking)."""
    keep = sorted({int(t) for t in keep_team_ids if t})
    if not keep:
        return 0
    conn = connect()
    try:
        marks = ",".join("?" * len(keep))
        kept_ids = f"SELECT hltv_team_id FROM teams WHERE team_id IN ({marks}) AND hltv_team_id IS NOT NULL"
        cur = conn.execute(
            f"""UPDATE teams SET hltv_rank = 999
                WHERE hltv_rank IS NOT NULL AND hltv_rank < 999
                  AND team_id NOT IN ({marks})
                  AND (hltv_team_id IS NULL OR hltv_team_id NOT IN ({kept_ids}))""",
            keep + keep,
        )
        unranked = int(cur.rowcount or 0)
        conn.commit()
        return unranked
    finally:
        conn.close()


def sync_twin_team_rows(
    hltv_team_id: int,
    *,
    hltv_rank: Optional[int] = None,
    hltv_points: Optional[int] = None,
    vrs_rank: Optional[int] = None,
    vrs_points: Optional[int] = None,
) -> int:
    """Every row with this HLTV team id — the same team under another
    spelling (BetBoom / BETBOOM) — carries the same ranking, so whichever
    row an event or a ranking entry lands on, the rank is the current one."""
    cols = {k: v for k, v in {"hltv_rank": hltv_rank, "hltv_points": hltv_points, "vrs_rank": vrs_rank, "vrs_points": vrs_points}.items() if v is not None}
    if not cols or not hltv_team_id:
        return 0
    conn = connect()
    try:
        cur = conn.execute(
            f"UPDATE teams SET {', '.join(f'{k} = ?' for k in cols)} WHERE hltv_team_id = ?",
            [*cols.values(), int(hltv_team_id)],
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def update_team_map_stats(
    team_id: int,
    *,
    map_stats_json: str,
    source_url: str,
    hltv_team_id: Optional[int] = None,
) -> None:
    conn = connect()
    conn.execute(
        """
        UPDATE teams
        SET hltv_team_id = COALESCE(?, hltv_team_id),
            map_stats_json = ?,
            map_stats_source_url = ?,
            map_stats_imported_at = datetime('now')
        WHERE team_id = ?
        """,
        (hltv_team_id, map_stats_json, source_url, int(team_id)),
    )
    conn.commit()
    conn.close()


def get_team_by_id(team_id: int) -> Optional[Dict]:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM teams WHERE team_id = ?", (team_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_team_by_name(name: str) -> Optional[Dict]:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM teams WHERE name = ?", (name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_teams() -> List[Dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM teams ORDER BY hltv_rank ASC NULLS LAST"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_team(team_id: int) -> None:
    conn = connect()
    conn.execute("DELETE FROM teams WHERE team_id = ?", (team_id,))
    conn.commit()
    conn.close()
