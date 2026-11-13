"""A roster's plan and how it scores.

Two layers shared by every Top 5 (groups, playoff, bounty, swiss):

* choosing the plan for a five-player roster — a clash-free role assignment
  (each player a distinct role) and a roster-wide booster assignment (each of
  the 18 boosters used at most once, one per player per match slot), both
  maximising expected points (`plan_for_roster`); and
* scoring the roster in a given outcome under that fixed plan
  (`plan_outcome_scores`): rating and win points as scored in the outcome,
  the assigned role's per-match points for every match the player is scored
  for, and the assigned boosters' points for the match slots actually played.

The functions above `plan_for_roster` were moved here from groups.py unchanged.
"""
from __future__ import annotations
from backend.services import roster_scores

from typing import Any, Dict, List

from backend.services.swiss_booster_assignment import (
    BOOSTER_NAMES,
    BOOSTER_POINT_VALUE,
    _max_weight_assignment,
    match_rate,
)
from backend.services.team_optimizer import serialize_roster


class SlotReach(dict):
    """{team: {match number: P(team plays that match)}}, the booster slot
    odds, plus `bo` {team: {match number: {series: P(plays it as that
    best-of)}}} when the series varies (Swiss Bo1 rounds, Bo5 finals).
    Without it every match counts as a best-of-three."""

    def __init__(self, data=None, bo=None):
        super().__init__(data or {})
        self.bo = bo or {}


def slot_trigger(reach_by_team, team_id: int, n: int, rate: float) -> float:
    """P(team plays its nth match AND the booster triggers in it), from
    HLTV's best-of-three rate and the series that match is played in (the
    part of the slot's odds the series split does not cover is a Bo3)."""
    p = float((reach_by_team.get(int(team_id)) or {}).get(int(n), 0.0))
    mix = (getattr(reach_by_team, "bo", None) or {}).get(int(team_id), {}).get(int(n))
    if not mix:
        return p * float(rate)
    split = sum(float(q) for q in mix.values())
    return float(sum(float(q) * match_rate(rate, int(bo)) for bo, q in mix.items())) + max(0.0, p - split) * float(rate)


def _player_booster_ub(team_id: int, reach_by_team: Dict[int, Dict[int, float]], rates: Dict[int, float]) -> float:
    """Upper bound on a single player's booster EV: his own best booster
    assignment over his match slots (a player holds at most one booster per
    slot, each booster once). Ignores that boosters are shared across the
    roster, so summing this over 5 players over-counts — exactly what makes
    it a safe admissible bound for pruning / ranking before the exact
    assignment."""
    slots = [n for n, p in sorted((reach_by_team.get(int(team_id)) or {}).items()) if p > 0]
    if not slots:
        return 0.0
    weights = [[slot_trigger(reach_by_team, team_id, n, float((rates or {}).get(b, 0.0))) * BOOSTER_POINT_VALUE for n in slots] for b in range(18)]
    return float(sum(w for _b, _c, w in _max_weight_assignment(weights) if w > 0))


def _booster_slot_weights(pid: int, tid: int, reach_by_team, rates_by_pid) -> tuple:
    """(per match slot in order, {booster_id: P(plays it and the booster
    triggers)}) for one player."""
    rates = rates_by_pid.get(int(pid)) or {}
    slots = [
        {b: slot_trigger(reach_by_team, tid, n, float(rates.get(b, 0.0))) for b in range(18)}
        for n, p in sorted((reach_by_team.get(int(tid)) or {}).items()) if p > 0
    ]
    return slots, rates


def _player_booster_sub(slots: List[Dict[int, float]], rates: Dict[int, float], lam: List[float]) -> tuple:
    """One player's Lagrangian sub-problem: assign boosters to their match
    slots (one per slot, each booster once) maximising Σ (P(play and
    trigger) × value − λ_booster), negative reduced weights clipped to 0
    (booster unused). Returns (value, {booster_id used})."""
    if not slots:
        return 0.0, set()
    weights = [
        [max(0.0, sp[b] * BOOSTER_POINT_VALUE - lam[b]) for sp in slots]
        for b in range(18)
    ]
    value = 0.0
    used = set()
    for b, _c, w in _max_weight_assignment(weights):
        if w > 1e-9:
            value += float(w)
            used.add(int(b))
    return value, used


def _booster_bound_lagrangian(
    players_info: List[Dict[str, Any]],
    reach_by_team: Dict[int, Dict[int, float]],
    rates_by_pid: Dict[int, Dict[int, float]],
    reference_rosters: List[List[Dict[str, Any]]],
    iters: int = 40,
) -> tuple:
    """Additive booster bound that stays close to the exact roster solve.

    With booster prices λ ≥ 0 shared by every roster, for ANY roster
        exact booster ≤ Σ_players sub_p(λ) + Σ_b λ_b
    (each booster is used at most once, so the prices paid are at most Σλ;
    each player's part of any feasible assignment is feasible for sub_p). The
    plain per-player ceiling is the λ = 0 case and over-counts by ~40 points on
    a strong roster because all five players claim the same best boosters; the
    prices are tuned by subgradient descent to minimise the bound over the
    reference rosters (the strongest by the plain bound), where it becomes
    nearly tight. Returns ({pid: sub_p(λ)}, Σλ)."""
    slot_info = {
        int(p["player_id"]): _booster_slot_weights(int(p["player_id"]), int(p.get("team_id", 0)), reach_by_team, rates_by_pid)
        for p in players_info
    }
    lam = [0.0] * 18
    refs = [[int(p["player_id"]) for p in roster] for roster in reference_rosters if roster]
    if refs:
        step = 1.0
        for it in range(int(iters)):
            counts = [0.0] * 18
            for pids in refs:
                for pid in pids:
                    slots, rates = slot_info[pid]
                    _v, used = _player_booster_sub(slots, rates, lam)
                    for b in used:
                        counts[b] += 1.0 / len(refs)
            # d bound / d λ_b = 1 − (players using b): raise over-used prices, lower unused ones
            lam = [max(0.0, lam[b] - step * (1.0 - counts[b])) for b in range(18)]
            step *= 0.93
    sub = {}
    for pid, (slots, rates) in slot_info.items():
        sub[pid], _used = _player_booster_sub(slots, rates, lam)
    return sub, float(sum(lam))


# Each player's booster weights (18 boosters x his match slots) depend only on
# his team's slot odds and his trigger rates, not on the roster around him:
# kept per (reach, rates) pair, so the thousands of rosters a search scores
# share the few hundred players' columns instead of rebuilding them.
_PLAYER_COLS: Dict[str, Any] = {"refs": None, "map": {}}


def _player_columns(pid: int, tid: int, reach_by_team, rates_by_pid):
    refs = _PLAYER_COLS["refs"]
    if refs is None or refs[0] is not reach_by_team or refs[1] is not rates_by_pid:
        _PLAYER_COLS["refs"] = (reach_by_team, rates_by_pid)
        _PLAYER_COLS["map"] = {}
    key = (int(pid), int(tid))
    hit = _PLAYER_COLS["map"].get(key)
    if hit is None:
        slots = [(int(n), float(prob)) for n, prob in sorted((reach_by_team.get(int(tid)) or {}).items()) if prob > 0]
        rates = rates_by_pid.get(int(pid)) or {}
        weights = [
            [slot_trigger(reach_by_team, tid, n, float(rates.get(booster_id, 0.0))) * BOOSTER_POINT_VALUE for n, _p in slots]
            for booster_id in range(18)
        ]
        hit = _PLAYER_COLS["map"][key] = (slots, weights)
    return hit


def optimize_group_boosters_for_roster(
    players: List[Dict[str, Any]],
    reach_by_team: Dict[int, Dict[int, float]],
    rates_by_pid: Dict[int, Dict[int, float]],
) -> Dict[str, Any]:
    """Assign the 18 booster types across a roster's (player, match-slot) columns
    to maximise total expected booster points (max-weight bipartite matching via
    min-cost flow). Group matches are all scored as BO3 (matching the outcome
    enumeration); where the series varies (SlotReach.bo) each slot's weight
    is the trigger chance in the series it is played in.
    """
    columns: List[Dict[str, Any]] = []
    weights: List[List[float]] = [[] for _ in range(18)]
    for player in players:
        pid = int(player["player_id"])
        tid = int(player.get("team_id", 0))
        slots, pw = _player_columns(pid, tid, reach_by_team, rates_by_pid)
        for n, prob in slots:
            columns.append(
                {
                    "player_id": pid,
                    "player_name": player.get("name", f"Player {pid}"),
                    "match_number": n,
                    "team_id": tid,
                    "slot_probability": prob,
                }
            )
        for booster_id in range(18):
            weights[booster_id].extend(pw[booster_id])
    if not columns:
        return {"assignments": [], "total_expected_booster_points": 0.0}

    assignments: List[Dict[str, Any]] = []
    total = 0.0
    for booster_id, col_idx, ev in _max_weight_assignment(weights):
        if ev <= 1e-9:
            continue
        col = columns[col_idx]
        assignments.append(
            {
                "booster_id": int(booster_id),
                "booster": BOOSTER_NAMES.get(int(booster_id), f"Booster {booster_id}"),
                "player_id": int(col["player_id"]),
                "player": col["player_name"],
                "match_number": int(col["match_number"]),
                "slot_probability": float(col["slot_probability"]),
                "expected_points": float(ev),
            }
        )
        total += float(ev)
    return {"assignments": assignments, "total_expected_booster_points": total}


_NUM_ROLES = 12
_ROLE_UNAVAILABLE = -1e9  # weight for a role the player has no trigger data for


def _exact_role_assignment(
    players: List[Dict[str, Any]], role_scores_by_pid: Dict[int, Dict[int, float]]
) -> tuple:
    """Optimal clash-free role assignment: each player takes a DISTINCT role,
    maximising total expected role points (max-weight matching via min-cost
    flow). Per-role EV = the player's role_ev (which is best-role points, i.e.
    best_per_match_score × padding-inclusive match count) scaled by that role's
    per-match score relative to their best — role points scale linearly with the
    per-match score over the same match count. Returns
    (total_role_ev, {pid: role_index}, {pid: assigned_role_ev}).

    This replaces the old 'everyone on their best role' sum (which ignored
    clashes and overstated role); when two players share a best role the optimum
    moves one to their next-best free role, reducing the total.
    """
    weights: List[List[float]] = []
    for p in players:
        pid = int(p["player_id"])
        rs = role_scores_by_pid.get(pid) or {}
        role_ev = float(p.get("role_ev") or 0.0)
        best = max(rs.values()) if rs else 0.0
        if abs(best) <= 1e-9:
            # No usable role data — neutral everywhere so it never blocks others.
            weights.append([0.0] * _NUM_ROLES)
            continue
        row = [_ROLE_UNAVAILABLE] * _NUM_ROLES
        for r in range(_NUM_ROLES):
            if r in rs:
                row[r] = role_ev * (float(rs[r]) / best)
        weights.append(row)

    total = 0.0
    role_of: Dict[int, int] = {}
    role_ev_of: Dict[int, float] = {}
    for row_idx, col_idx, w in _max_weight_assignment(weights):
        pid = int(players[row_idx]["player_id"])
        role_of[pid] = int(col_idx)
        contrib = float(w) if w > _ROLE_UNAVAILABLE / 2 else 0.0
        role_ev_of[pid] = contrib
        total += contrib
    return total, role_of, role_ev_of


def _score_roster_exact(
    pids: List[int],
    players_meta: Dict[str, Dict[str, Any]],
    reach_by_team: Dict[int, Dict[int, float]],
    rates_by_pid: Dict[int, Dict[int, float]],
    role_scores_by_pid: Dict[int, Dict[int, float]],
) -> Dict[str, Any]:
    """Serialised roster with the exact clash-free role assignment and the
    exact roster-wide booster assignment (each booster type once across the
    roster, one per player per match slot): average_ev = rating+win + exact
    role + exact booster, with per-player role_ev / booster_ev / total_ev
    patched to the assigned role and boosters so the player rows sum to it."""
    roster_players = [players_meta[str(pid)] for pid in pids]
    booster_result = optimize_group_boosters_for_roster(roster_players, reach_by_team, rates_by_pid)
    role_total, role_of, role_ev_of = _exact_role_assignment(roster_players, role_scores_by_pid)
    rating_win = sum(float(p.get("rating_ev", 0.0)) + float(p.get("win_ev", 0.0)) for p in roster_players)
    avg_ev = rating_win + role_total + float(booster_result["total_expected_booster_points"])
    cost = sum(int(p.get("price") or 0) for p in roster_players)
    serialized = serialize_roster(
        players_meta, pids, [str(role_of.get(pid, "-")) for pid in pids], avg_ev, cost,
        booster_assignments=booster_result["assignments"],
    )
    for player in serialized.get("players") or []:
        pid = int(player["player_id"])
        new_role_ev = role_ev_of.get(pid)
        if new_role_ev is not None:
            old_role_ev = float(player.get("role_ev") or 0.0)
            player["role_ev"] = float(new_role_ev)
            player["total_ev"] = float(player.get("total_ev") or 0.0) - old_role_ev + float(new_role_ev)
        roster_scores.refresh_average(player)
    serialized["average_ev"] = float(avg_ev)
    serialized["mode_metric"] = float(avg_ev)
    return serialized


def plan_for_roster(
    players: List[Dict[str, Any]],
    reach_by_team: Dict[int, Dict[int, float]],
    rates_by_pid: Dict[int, Dict[int, float]],
    role_scores_by_pid: Dict[int, Dict[int, float]],
) -> Dict[str, Any]:
    """The roster's plan: exact clash-free roles and the roster-wide booster
    assignment, both chosen to maximise expected points. Returns role_of
    {pid: role index}, role_pm {pid: assigned role's per-match points},
    role_ev_of {pid: expected role points}, role_total, boosters (the
    assignment rows), slot_rates {pid: {match number: trigger rate}} and
    booster_total."""
    booster = optimize_group_boosters_for_roster(players, reach_by_team, rates_by_pid)
    role_total, role_of, role_ev_of = _exact_role_assignment(players, role_scores_by_pid)
    role_pm: Dict[int, float] = {}
    for p in players:
        pid = int(p["player_id"])
        rs = role_scores_by_pid.get(pid) or {}
        r = role_of.get(pid)
        role_pm[pid] = float(rs.get(r, 0.0)) if r is not None else 0.0
    slot_rates: Dict[int, Dict[int, float]] = {}
    team_of = {int(p["player_id"]): int(p.get("team_id", 0)) for p in players}
    for a in booster.get("assignments") or []:
        pid = int(a["player_id"])
        n = int(a["match_number"])
        rate = float((rates_by_pid.get(pid) or {}).get(int(a["booster_id"]), 0.0))
        # the trigger chance GIVEN the match is played, in its series mix
        reach = float((reach_by_team.get(team_of[pid]) or {}).get(n, 0.0))
        slot_rates.setdefault(pid, {})[n] = (slot_trigger(reach_by_team, team_of[pid], n, rate) / reach) if reach > 0 else rate
    return {
        "role_of": role_of,
        "role_ev_of": role_ev_of,
        "role_pm": role_pm,
        "role_total": float(role_total),
        "boosters": booster.get("assignments") or [],
        "booster_total": float(booster.get("total_expected_booster_points") or 0.0),
        "slot_rates": slot_rates,
    }


def booster_prefix_table(slot_rates: Dict[int, float], max_slot: int):
    """R[m] = booster points of the plan's slots 1..m (5 × trigger rate each),
    so a player who plays m matches in an outcome scores R[m]."""
    import numpy as np

    table = np.zeros(max(int(max_slot), 0) + 1, dtype=np.float64)
    for k in range(1, len(table)):
        table[k] = table[k - 1] + BOOSTER_POINT_VALUE * float(slot_rates.get(k, 0.0))
    return table


def plan_outcome_scores(plan: Dict[str, Any], pids: List[int], rows: List[int], RW, MP, MR):
    """The roster's score in every outcome under its plan (vector over
    outcomes): RW rows (rating + win as scored, penalties and paddings
    included) + assigned role per-match points × MR (matches the role is
    scored for) + the assigned boosters' points for the MP matches played."""
    import numpy as np

    S = RW[rows].sum(axis=0).astype(np.float64)
    max_slot = int(MP.max()) if MP.size else 0
    for pid, j in zip(pids, rows):
        rpm = float(plan["role_pm"].get(int(pid), 0.0))
        if rpm:
            S += rpm * MR[j]
        sr = plan["slot_rates"].get(int(pid))
        if sr:
            S += booster_prefix_table(sr, max_slot)[MP[j]]
    return S


def match_reach_from_played(pids: List[int], team_of: Dict[int, int], MP, probs) -> Dict[int, Dict[int, float]]:
    """{team: {match number: P(team plays that match)}} from the per-outcome
    matches-played table (any one player of the team stands for the team)."""

    reach: Dict[int, Dict[int, float]] = {}
    seen: set = set()
    for j, pid in enumerate(pids):
        tid = int(team_of.get(int(pid), 0))
        if tid in seen:
            continue
        seen.add(tid)
        row = MP[j]
        d: Dict[int, float] = {}
        for n in range(1, int(row.max()) + 1 if row.size else 1):
            p = float(probs[row >= n].sum())
            if p > 0:
                d[n] = p
        reach[tid] = d
    return reach


def plan_metrics(
    keys: List[tuple],
    players_meta: Dict[str, Dict[str, Any]],
    pid_idx: Dict[int, int],
    RW,
    MP,
    MR,
    probs,
    reach_by_team: Dict[int, Dict[int, float]],
    rates_by_pid: Dict[int, Dict[int, float]],
    role_scores_by_pid: Dict[int, Dict[int, float]],
    chunk: int = 16,
    progress_callback=None,
    RT=None,
) -> List[Dict[str, Any]]:
    """For each candidate roster (a tuple of pids): its plan, expected points
    under the plan, its ceiling (best outcome), that outcome's probability and
    index, each player's score in it, and the share of outcomes the roster
    tops among the candidates. A fused compiled kernel scores every candidate
    in every outcome (numpy chunks over the outcome tables without numba)."""
    import numpy as np

    from backend.services import roster_kernels as rk

    C = len(keys)
    N = RW.shape[1]
    max_slot = int(MP.max()) if MP.size else 0
    rows = np.asarray([[pid_idx[int(pid)] for pid in key] for key in keys], dtype=np.int64).reshape(C, 5)
    plans: List[Dict[str, Any]] = []
    role_pm = np.zeros((C, 5))
    tables = np.zeros((C, 5, max_slot + 1))
    for i, key in enumerate(keys):
        plan = plan_for_roster([players_meta[str(pid)] for pid in key], reach_by_team, rates_by_pid, role_scores_by_pid)
        plans.append(plan)
        for p, pid in enumerate(key):
            role_pm[i, p] = float(plan["role_pm"].get(int(pid), 0.0))
            sr = plan["slot_rates"].get(int(pid))
            if sr:
                tables[i, p] = booster_prefix_table(sr, max_slot)
    stats = rk.plan_block_stats(RW, MP, MR, probs, rows, role_pm, tables) if C else None
    if stats is not None:
        avg, ceil, argmax, ceil_p, best_i = stats
        if progress_callback:
            progress_callback(C, C)
    else:
        best_val = np.full(N, -np.inf)
        best_i = np.full(N, -1, dtype=np.int64)
        avg = np.empty(C)
        ceil = np.empty(C)
        ceil_p = np.empty(C)
        argmax = np.empty(C, dtype=np.int64)
        for st in range(0, C, chunk):
            R = rows[st:st + chunk]
            S = RW[R].sum(axis=1)
            S += (role_pm[st:st + chunk, :, None] * MR[R]).sum(axis=1)
            S += np.take_along_axis(tables[st:st + chunk], MP[R].astype(np.int64), axis=2).sum(axis=1)
            avg[st:st + chunk] = S @ probs
            mx = S.max(axis=1)
            ceil[st:st + chunk] = mx
            argmax[st:st + chunk] = S.argmax(axis=1)
            ceil_p[st:st + chunk] = ((S >= mx[:, None] - 1e-9) * probs).sum(axis=1)
            cb = S.max(axis=0)
            ca = S.argmax(axis=0)
            better = cb > best_val + 1e-9
            best_val[better] = cb[better]
            best_i[better] = st + ca[better]
            if progress_callback:
                progress_callback(min(C, st + chunk), C)
    won = best_i >= 0
    wins_prob = np.bincount(best_i[won], weights=probs[won], minlength=C)[:C].astype(np.float64) if C else np.zeros(0)
    wins_count = np.bincount(best_i[won], minlength=C)[:C].astype(np.float64) if C else np.zeros(0)
    # the outcomes each candidate tops, most probable first
    wins_idx: List[List[int]] = [[] for _ in range(C)]
    won_cols = np.nonzero(won)[0]
    for c in won_cols[np.argsort(-probs[won_cols], kind="stable")]:
        wins_idx[int(best_i[c])].append(int(c))
    # expected components given the roster wins (over the outcomes it tops)
    win_parts: List[Dict[int, Dict[str, Any]]] = [dict() for _ in range(C)]
    for i in range(C):
        cols = wins_idx[i]
        if not cols:
            continue
        cols_arr = np.asarray(cols, dtype=np.int64)
        w = probs[cols_arr]
        tot = float(w.sum())
        if tot <= 0:
            continue
        w = w / tot
        for p, pid in enumerate(keys[i]):
            j = rows[i, p]
            rw_v = float(RW[j, cols_arr] @ w)
            rating_v = float(RT[j, cols_arr] @ w) if RT is not None else None
            role_v = float(role_pm[i, p] * (MR[j, cols_arr] @ w))
            boost_v = float(tables[i, p][MP[j, cols_arr].astype(np.int64)] @ w)
            win_parts[i][int(pid)] = {
                "rating": rating_v,
                "win": (rw_v - rating_v) if rating_v is not None else None,
                "rating_win": rw_v,
                "role": role_v,
                "booster": boost_v,
                "total": rw_v + role_v + boost_v,
            }
    out = []
    for i, key in enumerate(keys):
        o = int(argmax[i])
        peak = {}
        peak_parts = {}
        for p, pid in enumerate(key):
            j = rows[i, p]
            rw_v = float(RW[j, o])
            role_v = role_pm[i, p] * float(MR[j, o])
            boost_v = float(tables[i, p, int(MP[j, o])])
            peak[int(pid)] = rw_v + role_v + boost_v
            rating_v = float(RT[j, o]) if RT is not None else None
            peak_parts[int(pid)] = {
                "rating": rating_v,
                "win": (rw_v - rating_v) if rating_v is not None else None,
                "rating_win": rw_v,
                "role": role_v,
                "booster": boost_v,
                "total": rw_v + role_v + boost_v,
            }
        out.append({
            "plan": plans[i],
            "avg": float(avg[i]),
            "ceiling": float(ceil[i]),
            "ceiling_p": float(ceil_p[i]),
            "argmax": o,
            "peak": peak,
            "peak_parts": peak_parts,
            "win_parts": win_parts[i],
            "wins_prob": float(wins_prob[i]),
            "wins_count": float(wins_count[i]),
            "wins_idx": wins_idx[i],
        })
    return out
