"""Season Maximum: rank rosters by the HLTV fantasy SEASON points they are
expected to earn, not by their expected fantasy points.

Season points come from the roster's placement among the league
(seasonTierData: top 1% 25, top 5% 18, ... bottom 20% 0), so a roster's worth
in an outcome is where it lands against the field in THAT outcome. The field
is the event's own user teams (services/fantasy_field, readable before roster
lock): each is scored on every engine outcome with its chosen players and
roles and the best booster plan for its roster, which gives per outcome the
score each tier needs. A candidate roster scored under its own plan then
earns the points of the tier its score reaches, averaged over the outcomes
with their probabilities. The payoff is convex at the top, so a roster that
differs from the field where it matters can beat a higher-average one.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fall season 2026 (seasonTierData); the stored season feed's table wins when present
DEFAULT_TIERS: List[Tuple[float, int]] = [
    (0.01, 25), (0.05, 18), (0.10, 15), (0.20, 12), (0.30, 10), (0.40, 8), (0.50, 6), (0.60, 4), (0.70, 2), (0.80, 1), (1.00, 0),
]
FIELD_SAMPLE = 5000  # field teams scored (seeded sample of the league; quantiles need no more)
OUTCOME_BLOCK = 2000  # outcomes per numpy block


# ---------------------------------------------------------------- frozen at roster lock
# Lineups cannot change once the event starts, so the first complete field
# read that STARTED after the start is the field Season Maximum ranks against
# for the rest of the event: frozen then (every 5-player roster, in stored
# order, plus the season tier table of that moment). Later re-reads still
# update the leaderboard and match points; Season Maximum no longer moves.
_FIELD_LOCK = None


def _lock_state():
    global _FIELD_LOCK
    if _FIELD_LOCK is None:
        from backend.data.singleton_state import SingletonState

        _FIELD_LOCK = SingletonState("season_max_field_lock", keyed=True)
        _FIELD_LOCK.ensure_table()
    return _FIELD_LOCK


def frozen_field(event_id: int) -> Optional[Dict[str, Any]]:
    """{rosters: [[pids, {pid: role}]], tiers, frozen_at, read_started_at} once
    frozen, else None (freezing it now when it is due)."""
    st = _lock_state()
    row = st.load(key=int(event_id))
    if row:
        return row["result"]
    return _freeze_if_due(int(event_id))


def _freeze_if_due(event_id: int) -> Optional[Dict[str, Any]]:
    import time

    from backend.data.db import connect
    from backend.routes.groups import event_start_at
    from backend.services import fantasy_field

    start = event_start_at(event_id)
    if not start or time.time() < float(start):
        return None
    conn = connect()
    try:
        run = conn.execute(
            "SELECT started_at FROM fantasy_field_runs WHERE event_id = ? AND status = 'done' AND started_at >= ? ORDER BY started_at LIMIT 1",
            (int(event_id), float(start)),
        ).fetchone()
    except Exception:  # noqa: BLE001 - no field table yet
        run = None
    finally:
        conn.close()
    if not run:
        return None
    rosters = []
    for t in fantasy_field.teams(int(event_id)):
        players = [p for p in t.get("players") or [] if p.get("player_id") is not None]
        if len(players) != 5:
            continue
        rosters.append([sorted(int(p["player_id"]) for p in players), {str(int(p["player_id"])): p.get("role_name") for p in players}])
    if len(rosters) < 50:
        return None
    frozen = {"rosters": rosters, "tiers": [list(t) for t in _live_tiers()], "frozen_at": time.time(), "read_started_at": float(run["started_at"])}
    _lock_state().save({"event_id": int(event_id)}, frozen, key=int(event_id))
    logger.info("Season Maximum: field of event %s frozen (%s rosters, read started %s)", event_id, len(rosters), run["started_at"])
    return frozen


def tiers(event_id: Optional[int] = None) -> List[Tuple[float, int]]:
    """[(top share, season points)] best tier first: the event's frozen table
    once its field is frozen, else the last season feed read's."""
    if event_id is not None:
        fz = frozen_field(int(event_id))
        if fz and fz.get("tiers"):
            return [(float(a), int(b)) for a, b in fz["tiers"]]
    return _live_tiers()


def _live_tiers() -> List[Tuple[float, int]]:
    """[(top share, season points)] best tier first, from the last season
    feed read (any account), else DEFAULT_TIERS."""
    from backend.data.db import connect

    import json

    try:
        conn = connect()
        try:
            row = conn.execute("SELECT season_json FROM season_cache ORDER BY read_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        raw = (json.loads(row["season_json"]) or {}).get("tiers") if row else None
        out = []
        for t in raw or []:
            pct = float(str(t.get("top_pct") or "").rstrip("%")) / 100.0
            out.append((pct, int(t.get("points") or 0)))
        if out and out[-1][0] >= 1.0:
            return sorted(out)
    except Exception:  # noqa: BLE001
        logger.debug("season tiers not readable, using the default table", exc_info=True)
    return list(DEFAULT_TIERS)


def field_rosters(event_id: int, pid_idx: Dict[int, int], sample: int = FIELD_SAMPLE) -> List[Tuple[tuple, Dict[int, Optional[str]]]]:
    """The league's complete rosters inside the pool as (pids, {pid: role
    name}), a seeded sample of at most `sample`."""
    import random

    from backend.services import fantasy_field

    out = []
    fz = frozen_field(int(event_id))
    if fz:
        for pids, roles in fz["rosters"]:
            if any(int(pid) not in pid_idx for pid in pids):
                continue
            out.append((tuple(int(pid) for pid in pids), {int(k): v for k, v in roles.items()}))
    else:
        for t in fantasy_field.teams(int(event_id)):
            players = [p for p in t.get("players") or [] if p.get("player_id") is not None]
            if len(players) != 5 or any(int(p["player_id"]) not in pid_idx for p in players):
                continue
            out.append((tuple(sorted(int(p["player_id"]) for p in players)), {int(p["player_id"]): p.get("role_name") for p in players}))
    if len(out) > sample:
        out = random.Random(int(event_id)).sample(out, sample)
    return out


def _field_plan(key: tuple, roles: Dict[int, Optional[str]], players_meta, reach_by_team, rates_by_pid, role_scores_by_pid, cache: dict):
    """A field roster's (role per-match points as chosen, booster slot
    rates of the roster's best booster plan); plans cached per roster."""
    from backend.services.my_team import ROLE_INDEX_BY_NAME
    from backend.services.roster_plan import plan_for_roster

    plan = cache.get(key)
    if plan is None:
        plan = plan_for_roster([players_meta[str(pid)] for pid in key], reach_by_team, rates_by_pid, role_scores_by_pid)
        cache[key] = plan
    role_pm = {}
    for pid in key:
        idx = ROLE_INDEX_BY_NAME.get(str(roles.get(pid) or "").strip().lower())
        role_pm[pid] = float((role_scores_by_pid.get(pid) or {}).get(idx, 0.0)) if idx is not None else 0.0
    return role_pm, plan["slot_rates"]


def _score_block(rows, role_pm, tables, RW, MP, MR, sl):
    """Scores (rosters × outcomes in the slice) under fixed plans."""
    import numpy as np

    S = RW[rows, sl].sum(axis=1)
    S += (role_pm[:, :, None] * MR[rows, sl]).sum(axis=1)
    S += np.take_along_axis(tables, MP[rows, sl].astype(np.int64), axis=2).sum(axis=1)
    return S


def _plan_arrays(plans: List[Tuple[Dict[int, float], Dict[int, Dict[int, float]]]], keys: List[tuple], pid_idx, max_slot: int):
    import numpy as np

    from backend.services.roster_plan import booster_prefix_table

    C = len(keys)
    rows = np.asarray([[pid_idx[int(pid)] for pid in key] for key in keys], dtype=np.int64).reshape(C, 5)
    role_pm = np.zeros((C, 5))
    tables = np.zeros((C, 5, max_slot + 1))
    for i, (key, (rpm, slots)) in enumerate(zip(keys, plans)):
        for p, pid in enumerate(key):
            role_pm[i, p] = float(rpm.get(int(pid), 0.0))
            sr = slots.get(int(pid))
            if sr:
                tables[i, p] = booster_prefix_table(sr, max_slot)
    return rows, role_pm, tables


def season_points(
    event_id: int,
    cand_keys: List[tuple],
    pid_idx: Dict[int, int],
    RW, MP, MR, probs,
    players_meta, reach_by_team, rates_by_pid, role_scores_by_pid,
) -> Optional[Dict[str, Any]]:
    """Expected season points of every candidate roster (under its own
    plan) against the event's field; None without a stored field. Returns
    {expected: [per candidate], tier_probs: [[P(tier) per tier] per
    candidate], tiers: [(share, points)], field_teams: n}."""
    import numpy as np

    from backend.services.roster_plan import plan_for_roster

    field = field_rosters(event_id, pid_idx)
    if len(field) < 50 or not cand_keys:
        return None
    table = tiers(event_id)
    shares = np.asarray([s for s, _ in table[:-1]])  # the last tier is everyone left
    points = np.asarray([p for _, p in table], dtype=np.float64)
    max_slot = int(MP.max()) if MP.size else 0
    cache: dict = {}
    f_plans = [_field_plan(k, roles, players_meta, reach_by_team, rates_by_pid, role_scores_by_pid, cache) for k, roles in field]
    f_rows, f_rpm, f_tab = _plan_arrays(f_plans, [k for k, _ in field], pid_idx, max_slot)
    c_plans = []
    for key in cand_keys:
        plan = cache.get(key) or plan_for_roster([players_meta[str(pid)] for pid in key], reach_by_team, rates_by_pid, role_scores_by_pid)
        c_plans.append((plan["role_pm"], plan["slot_rates"]))
    c_rows, c_rpm, c_tab = _plan_arrays(c_plans, list(cand_keys), pid_idx, max_slot)
    n_field = len(field)
    # position (0-based, best first) of each tier's last team
    cut_pos = np.minimum(n_field - 1, np.maximum(0, np.ceil(shares * n_field).astype(np.int64) - 1))
    N = RW.shape[1]
    w = np.asarray(probs, dtype=np.float64)
    w = w / (w.sum() or 1.0)
    from backend.services import roster_kernels as rk

    tier_mass = rk.season_tier_mass(RW, MP, MR, w, f_rows, f_rpm, f_tab, cut_pos, c_rows, c_rpm, c_tab)
    if tier_mass is None:
        tier_mass = np.zeros((len(cand_keys), len(points)))
        for st in range(0, N, OUTCOME_BLOCK):
            sl = slice(st, min(N, st + OUTCOME_BLOCK))
            F = -np.sort(-_score_block(f_rows, f_rpm, f_tab, RW, MP, MR, sl), axis=0)  # best first
            cuts = F[cut_pos]  # (tiers-1, outcomes): the score each tier needs
            S = _score_block(c_rows, c_rpm, c_tab, RW, MP, MR, sl)
            tier = (S[:, None, :] < cuts[None, :, :] - 1e-9).sum(axis=1)  # cuts the score falls short of
            for t in range(len(points)):
                tier_mass[:, t] += ((tier == t) * w[sl]).sum(axis=1)
    return {
        "expected": (tier_mass @ points).tolist(),
        "tier_probs": tier_mass.tolist(),
        "tiers": table,
        "field_teams": n_field,
        "plans": c_plans,
    }
