"""The three kinds of fantasy points a roster's player carries.

Every roster the Top 5 pages show is scored three ways, and each kind is
split the same way (rating / win / role / booster / total):

  average   expected points over every outcome of the event: what the
            Average Player Value ranking sorts by.
  outcome   the player's points in ONE specific outcome. For Best Single
            Outcome that is the roster's own best outcome, so the five
            players' numbers add up to the roster's ceiling and a star can
            score little there when the roster peaks on an upset. For the
            completed groups / completed bracket views it is the outcome the
            operator picked.
  winning   expected points over the outcomes in which this roster is the
            top roster: what Most Likely Winner shows.

A player entry carries each kind under its own key (`average`, `outcome`,
`winning`), or None when that kind was not computed. There is no catch-all
score field: every page picks the kind that matches its ranking mode
(renderer: rosterPoints). The flat `rating_ev` / `win_ev` / `role_ev` /
`booster_ev` / `total_ev` fields remain the optimiser's inputs and always
describe the average.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

KINDS = ("average", "outcome", "winning")
PARTS = ("rating", "win", "role", "booster")
# Stored roster sets written before this module carried `mode_score`,
# `ceiling_score`, `win_score`, `ceiling_parts` and `win_parts`.
LEGACY_FIELDS = ("mode_score", "ceiling_score", "win_score", "ceiling_parts", "win_parts")
SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)


def points(rating=None, win=None, role=None, booster=None, total=None) -> Dict[str, Optional[float]]:
    """One kind of points. The total is the sum of the parts when all four
    are known, else the total given (parts stay None)."""
    parts = [rating, win, role, booster]
    known = [None if v is None else float(v) for v in parts]
    if total is None and all(v is not None for v in known):
        total = sum(known)  # type: ignore[arg-type]
    return {
        "rating": known[0],
        "win": known[1],
        "role": known[2],
        "booster": known[3],
        "total": float(total or 0.0),
    }


def from_parts(value: Any, total: Optional[float] = None) -> Optional[Dict[str, Optional[float]]]:
    """`points` from a {rating, win, role, booster[, total]} mapping (the
    per-outcome component dicts the simulators emit); None for nothing."""
    if value is None and total is None:
        return None
    d = value if isinstance(value, dict) else {}
    return points(d.get("rating"), d.get("win"), d.get("role"), d.get("booster"), d.get("total") if total is None else total)


def parts_from_flat(player: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """The points a serialised player's flat `*_ev` fields describe."""
    return points(
        float(player.get("rating_ev") or 0.0),
        float(player.get("win_ev") or 0.0),
        float(player.get("role_ev") or 0.0),
        float(player.get("booster_ev") or 0.0),
        float(player.get("total_ev") or 0.0),
    )


def refresh_average(player: Dict[str, Any]) -> Dict[str, Any]:
    """Set `average` from the flat fields (after any role / booster patch)."""
    player["average"] = parts_from_flat(player)
    return player


def set_kind(player: Dict[str, Any], kind: str, value: Any, total: Optional[float] = None) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown points kind {kind!r}")
    player[kind] = from_parts(value, total)


def outcome_of(components: Any, total: float) -> Dict[str, Optional[float]]:
    """`outcome` points for a player in one specific outcome: its component
    split when the outcome stored one, else the total alone."""
    return from_parts(components if components else None, float(total)) or points(total=float(total))


def migrate_player(player: Dict[str, Any]) -> bool:
    """Legacy entry → kinds. Returns whether anything changed."""
    changed = False
    if "average" not in player:
        refresh_average(player)
        changed = True
    if "outcome" not in player:
        if player.get("ceiling_parts") is not None or player.get("ceiling_score") is not None:
            player["outcome"] = from_parts(player.get("ceiling_parts"), player.get("ceiling_score"))
        else:
            player["outcome"] = None
        changed = True
    if "winning" not in player:
        if player.get("win_parts") is not None or player.get("win_score") is not None:
            player["winning"] = from_parts(player.get("win_parts"), player.get("win_score"))
        else:
            player["winning"] = None
        changed = True
    for field in LEGACY_FIELDS:
        if field in player:
            player.pop(field, None)
            changed = True
    return changed


def migrate_roster_set(result: Dict[str, Any]) -> bool:
    changed = False
    for list_key in ("all_teams", "top_teams"):
        for team in result.get(list_key) or []:
            for player in team.get("players") or []:
                changed = migrate_player(player) or changed
    return changed


# A stored result bigger than this is not migrated: the migration adds fields
# to every player of every roster, and SQLite refuses a string over 1 GB
# ("string or blob too big"), so such a blob could never be saved back — and
# parsing it costs seconds and gigabytes at every start. (A single-row-era
# bounty set of 791 MB did exactly that on 2026-10-27; it was deleted.)
MIGRATE_MAX_BYTES = 400_000_000


def migrate_stored_roster_sets() -> Dict[str, int]:
    """Bring every stored roster set (groups, playoff, bounty; every event) up
    to this schema, once: a set whose payload is already marked is not even
    parsed (the playoff blobs are large). A set that cannot be migrated is
    logged and skipped; it never stops the others."""
    from backend.data.db import connect
    from backend.routes import groups, playoff

    stores = [groups._GROUPS_BEST_STATE] + [variant["best"] for variant in playoff._STATE_SETS.values()]
    migrated = 0
    skipped = 0
    for store in stores:
        conn = connect()
        try:
            rows = conn.execute(f"SELECT singleton_id, payload_json, length({store.result_column}) AS size FROM {store.table}").fetchall()
        except Exception:  # noqa: BLE001 - table not created yet
            rows = []
        finally:
            conn.close()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}") or {}
            except Exception:  # noqa: BLE001
                payload = {}
            if int(payload.get("scores_schema") or 0) >= SCHEMA_VERSION:
                continue
            key = int(row["singleton_id"])
            size = int(row["size"] or 0)
            if size > MIGRATE_MAX_BYTES:
                logger.warning("Roster scores: %s key %s is %.0f MB, too big to migrate; left as stored", store.table, key, size / 1e6)
                skipped += 1
                continue
            try:
                latest = store.load(key=key)
                if not latest:
                    continue
                result = latest.get("result") or {}
                migrate_roster_set(result)
                new_payload = dict(latest.get("payload") or {})
                new_payload["scores_schema"] = SCHEMA_VERSION
                store.save(new_payload, result, key=key)
            except Exception:  # noqa: BLE001 - one set must not stop the rest
                logger.warning("Roster scores: could not migrate %s key %s (%.0f MB)", store.table, key, size / 1e6, exc_info=True)
                skipped += 1
                continue
            migrated += 1
            logger.info("Roster scores: migrated %s key %s", store.table, key)
    return {"migrated": migrated, "skipped": skipped}
