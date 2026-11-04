"""One engine for every stage format: outcomes as PATHS.

Every format the Tournament page values — the Swiss stage (sampled), a
single-elimination bracket of 2–16 teams, the 8-team double-elimination
bracket (both enumerated exactly) — is expressed the same way: a set of
outcomes, each the path of every team through the stage's schedule of
STAGES (opponent, result, series type and match number where it plays, the
padded matches it is credited at a pad stage, the −3-per-round penalty when
it goes out early), with a weight per outcome. Points are a function of the
path: every distinct (team, opponent, result, match number, series) pairing
is scored once by the shared match scorer, and numpy over the paths gives

  * the outcome model the Top 5 modes rank rosters over (vectors + the
    rating/win, rating, matches-played and role-matches decomposition the
    roster planner needs),
  * expected points, reach and advance odds per team,
  * the Point Sources contract (per-stage cells, opponents, boosters, pads,
    elimination credits) for all outcomes or any subset of them, under a
    roster's plan,
  * the team's runs through the stage over a subset of outcomes.

A format supplies: the stage list (labels and the modal's layout hints),
the paths, the weights, and two rules — how a padded match is valued
("constant": the player's base rating, +6 and their role per missing match,
the Swiss rule; "average": the per-match average of the real games played
so far, the bracket bye rule) and which later stages an elimination at a
stage is credited to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_WIN_PENALTY = -3.0
_PAD_WIN = 6.0
PARTS = ("rating", "win", "role", "booster")


@dataclass
class Stage:
    key: str
    label: str
    full: str
    section: str = "playoff"
    section_title: str = "Playoffs"
    bracket: str = "playoff"  # "upper" / "lower" lay the section out as a bracket grid; "playoff" is one row
    opener: bool = False
    pad: bool = False  # a pad stage: no match, the team is credited padded matches here
    series: int = 3  # best-of of the matches played at this stage (Swiss paths carry their own per match)
    # a pad stage with fixed (rating, win, role) points per padded match,
    # overriding the paths' pad rule (a bracket bye: no earlier game to average)
    pad_points: Optional[tuple] = None

    def descriptor(self) -> Dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "full": self.full, "phase": "playoff", "section": self.section,
            "section_title": self.section_title, "bracket": self.bracket, "opener": bool(self.opener), "pad": bool(self.pad),
        }


@dataclass
class Paths:
    """Every outcome as arrays (n, T, S): outcome, team position, stage."""

    team_ids: List[int]
    stages: List[Stage]
    opp: Any  # int16: opponent's team position, -1 when the team has no match at that stage
    won: Any  # int8: 1 won, 0 lost, -1 no match
    bo: Any  # int8: series of the match (1 / 3 / 5), 0 no match
    match_no: Any  # int16: the team's match number at that stage, 0 no match
    pad: Any  # int8: padded matches credited at that (pad) stage
    pen: Any  # float64 (n, T): penalty points, <= 0
    elim_stage: Any  # int8 (n, T): stage index of the loss that ended the run, -1 none
    advance: Any  # bool (n, T): the team got through (won the stage / qualified)
    idle_done: Any  # bool (n, T, S): no match at the stage because the team was already through
    weight: Any  # float64 (n): outcome weights for expectations, sum 1
    outcome_prob: Any  # float64 (n): the exact probability of the outcome
    missed_after: Dict[int, List[int]]  # stage index -> later stage indexes an elimination there is credited to, in order
    pad_rule: str = "constant"
    kind: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return int(self.opp.shape[0])

    @property
    def n_stages(self) -> int:
        return len(self.stages)


@dataclass
class Scored:
    """The paths with their pairing index and every player's per-pairing points."""

    paths: Paths
    pair_id: Any  # int32 (n, T, S): pairing table index, -1 none
    keys: List[tuple]  # pairing index -> (team pos, opponent pos, won, match number, series)
    pids: List[int]
    pid_pos: Dict[int, int]
    team_pos_of: Dict[int, int]
    tbl: Any  # float64 (K, P, 4)
    tbl_bid: Any  # int32 (K, P), -1 none
    tbl_rate: Any  # float64 (K, P)
    booster_names: Dict[int, str]
    pad_rating: Any  # float64 (P,): one padded match's rating points (base rating), constant rule
    pad_role: Any  # float64 (P,): one padded match's role points (own role)
    role_of: Dict[int, dict]
    base_states: Dict[int, Any]

    @property
    def team_ids(self) -> List[int]:
        return self.paths.team_ids

    @property
    def stages(self) -> List[Stage]:
        return self.paths.stages


# ------------------------------------------------------------------ scoring


def _pairing_index(paths: Paths) -> tuple:
    import numpy as np

    T = len(paths.team_ids)
    mask = paths.opp >= 0
    t_idx = np.arange(T, dtype=np.int64)[None, :, None]
    mno = paths.match_no.astype(np.int64)
    M = int(mno.max()) + 1 if mno.size else 1
    packed = (((t_idx * T + paths.opp.astype(np.int64)) * 2 + paths.won.astype(np.int64).clip(min=0)) * M + mno) * 8 + paths.bo.astype(np.int64)
    flat = packed[mask]
    uniq, inv = np.unique(flat, return_inverse=True)
    pair_id = np.full(paths.opp.shape, -1, dtype=np.int32)
    pair_id[mask] = inv.astype(np.int32)
    keys = []
    for u in uniq.tolist():
        series = u % 8
        u //= 8
        m = u % M
        u //= M
        w = u % 2
        u //= 2
        o = u % T
        t = u // T
        keys.append((int(t), int(o), int(w), int(m), int(series)))
    return pair_id, keys


def score(paths: Paths, base_states: Dict[int, Any], player_rows_by_id: Dict[int, dict], team_rank_by_id: Dict[int, int], prob_cache: Dict) -> Scored:
    """Score every distinct pairing the paths contain for the players of the
    team involved (the shared match scorer on fresh states at that match
    number; penalties are the paths' own), plus the constant-rule padding
    values and each player's role."""
    import numpy as np

    from backend.routes.playoff import _fresh_bracket_state, _play_match_deterministic
    from backend.swiss_stage.fantasy_scoring import compute_padding_components, compute_role_points

    team_ids = paths.team_ids
    pair_id, keys = _pairing_index(paths)
    pids: List[int] = []
    team_pos_of: Dict[int, int] = {}
    for t, tid in enumerate(team_ids):
        for pid in base_states[tid].players:
            pids.append(int(pid))
            team_pos_of[int(pid)] = t
    pid_pos = {pid: i for i, pid in enumerate(pids)}
    K, P = len(keys), len(pids)
    tbl = np.zeros((K, P, 4), dtype=np.float64)
    tbl_bid = np.full((K, P), -1, dtype=np.int32)
    tbl_rate = np.zeros((K, P), dtype=np.float64)
    booster_names: Dict[int, str] = {}
    for k, (t, o, w, m, series) in enumerate(keys):
        a, b = team_ids[t], team_ids[o]
        states = {a: _fresh_bracket_state(base_states, a, m - 1), b: _fresh_bracket_state(base_states, b, 0)}
        _play_match_deterministic(
            states, a, b, a if w else b, remaining_rounds_after=0, prob_cache=prob_cache,
            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id, match_type=f"bo{series}",
        )
        for pid, p in states[a].players.items():
            real = next((r for r in p.point_breakdown if r.get("match_number") is not None), None)
            if real is None:
                continue
            j = pid_pos[int(pid)]
            tbl[k, j] = (
                float(real.get("rating_points") or 0.0), float(real.get("win_points") or 0.0),
                float(real.get("role_points") or 0.0), float(real.get("booster_points") or 0.0),
            )
            bid = real.get("booster_id")
            if bid is not None:
                tbl_bid[k, j] = int(bid)
                tbl_rate[k, j] = float(real.get("booster_trigger_rate") or 0.0)
                booster_names.setdefault(int(bid), str(real.get("booster_name") or f"Booster {bid}"))
    pad_rating = np.zeros(P, dtype=np.float64)
    pad_role = np.zeros(P, dtype=np.float64)
    role_of: Dict[int, dict] = {}
    for tid in team_ids:
        for pid, p in base_states[tid].players.items():
            j = pid_pos[int(pid)]
            comps = compute_padding_components(p)
            pad_rating[j] = float(comps["rating"])
            pad_role[j] = float(comps["role"])
            role_of[int(pid)] = {
                "role_id": p.role_id, "major": float(p.major_pct), "minor": float(p.minor_pct),
                "points": float(compute_role_points(p)),
            }
    return Scored(
        paths=paths, pair_id=pair_id, keys=keys, pids=pids, pid_pos=pid_pos, team_pos_of=team_pos_of, tbl=tbl,
        tbl_bid=tbl_bid, tbl_rate=tbl_rate, booster_names=booster_names, pad_rating=pad_rating, pad_role=pad_role,
        role_of=role_of, base_states=base_states,
    )


def _player_matrix(sc: Scored, pid: int, sel=None, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Per-outcome arrays for one player over the selected outcomes, under the
    plan when given: per-stage components (m, S, 4) of the matches played,
    the per-stage pad components (m, S, 3: rating, win, role), the penalty,
    and the masks."""
    import numpy as np

    p = sc.paths
    j = sc.pid_pos[pid]
    t = sc.team_pos_of[pid]
    pick = (slice(None), t, slice(None)) if sel is None else (sel, t, slice(None))
    ids = sc.pair_id[pick]
    mask = ids >= 0
    safe = np.where(mask, ids, 0)
    comps = sc.tbl[safe, j, :] * mask[:, :, None]
    match_no = p.match_no[pick].astype(np.int64)
    role_pm = (plan or {}).get("role_pm")
    by_slot = (plan or {}).get("by_slot")
    if role_pm is not None:
        comps[:, :, 2] = mask * float(role_pm)
    if by_slot is not None:
        from backend.services.swiss_booster_assignment import BOOSTER_POINT_VALUE, match_rate

        # the plan's booster per slot, triggering at the rate of the series each match is played in
        slot_rate = np.zeros(int(match_no.max()) + 2 if match_no.size else 2, dtype=np.float64)
        for slot, b in by_slot.items():
            if 0 <= int(slot) < len(slot_rate):
                slot_rate[int(slot)] = float(b[2])
        bos = p.bo[pick].astype(np.int64)
        rates = slot_rate[match_no]
        pts = np.zeros_like(rates)
        for bo in np.unique(bos[mask]).tolist():
            at = bos == bo
            pts[at] = BOOSTER_POINT_VALUE * np.vectorize(lambda r, _bo=int(bo): match_rate(r, _bo))(rates[at]) if at.any() else 0.0
        comps[:, :, 3] = pts * mask
    pads = p.pad[pick].astype(np.float64)  # (m, S)
    pad_comps = np.zeros(comps.shape[:2] + (3,), dtype=np.float64)
    if pads.any():
        if p.pad_rule == "average":
            # the per-match average of the real games played before the pad stage
            cum = np.cumsum(comps[:, :, :3], axis=1)
            games = np.cumsum(mask, axis=1).astype(np.float64)
            prev_cum = np.concatenate([np.zeros_like(cum[:, :1]), cum[:, :-1]], axis=1)
            prev_games = np.concatenate([np.zeros_like(games[:, :1]), games[:, :-1]], axis=1)
            avg = prev_cum / np.maximum(prev_games, 1.0)[:, :, None]
            pad_comps = avg * pads[:, :, None]
        else:
            const = np.array([sc.pad_rating[j], _PAD_WIN, float(role_pm) if role_pm is not None else sc.pad_role[j]])
            pad_comps = pads[:, :, None] * const[None, None, :]
        for s_i, st in enumerate(p.stages):
            if st.pad and st.pad_points is not None:
                pad_comps[:, s_i, :] = pads[:, s_i, None] * np.asarray(st.pad_points, dtype=np.float64)[None, :]
    pen = p.pen[:, t] if sel is None else p.pen[sel, t]
    return {
        "ids": ids, "safe": safe, "mask": mask, "comps": comps, "match_no": match_no, "pads": pads, "pad_comps": pad_comps,
        "pen": pen.astype(np.float64),
        "opp": p.opp[pick].astype(np.int64), "won": p.won[pick],
        "elim_stage": p.elim_stage[:, t] if sel is None else p.elim_stage[sel, t],
        "advance": p.advance[:, t] if sel is None else p.advance[sel, t],
        "idle_done": p.idle_done[pick],
    }


def player_totals(sc: Scored, pid: int) -> Dict[str, Any]:
    """The player's per-outcome points with their own role and boosters, and
    the decomposition the roster planner needs."""
    import numpy as np

    m = _player_matrix(sc, pid)
    comps = m["comps"].sum(axis=1)
    pad_total = m["pad_comps"].sum(axis=1)  # (n, 3)
    comps[:, 0] += pad_total[:, 0]
    comps[:, 1] += pad_total[:, 1] + m["pen"]
    comps[:, 2] += pad_total[:, 2]
    played = m["mask"].sum(axis=1)
    pads = m["pads"].sum(axis=1)
    return {
        "comps": comps,
        "total": comps.sum(axis=1),
        "rw": comps[:, 0] + comps[:, 1],
        "rt": comps[:, 0].copy(),
        "mp": played.astype(np.uint8),
        "mr": played.astype(np.float64) + pads,
    }


def build_model(sc: Scored) -> tuple:
    """(vectors, probs, group_of_player, decomposition): the single-axis
    outcome model of the Top 5, with the scored paths attached."""
    import numpy as np

    w = sc.paths.weight
    vec: Dict[int, Any] = {}
    rw: Dict[int, Any] = {}
    rt: Dict[int, Any] = {}
    mp: Dict[int, Any] = {}
    mr: Dict[int, Any] = {}
    means: Dict[int, Dict[str, float]] = {}
    for pid in sc.pids:
        tot = player_totals(sc, pid)
        vec[pid] = tot["total"]
        rw[pid] = tot["rw"]
        rt[pid] = tot["rt"]
        mp[pid] = tot["mp"]
        mr[pid] = tot["mr"]
        c = w @ tot["comps"]
        means[pid] = {"rating": float(c[0]), "win": float(c[1]), "role": float(c[2]), "booster": float(c[3]), "total": float(c.sum())}
    decomp = {"rw": rw, "rt": rt, "mp": mp, "mr": mr, "paths": sc, "means": means}
    return {0: vec}, {0: np.asarray(w, dtype=np.float64)}, {pid: 0 for pid in vec}, decomp


def team_summary(sc: Scored) -> Dict[str, Any]:
    """Per team over every outcome: the chance of playing each stage, of
    advancing, and of playing its Nth match (the booster slot odds), split by
    the series that match is played in (match_bo: {n: {bo: P}}, summing to
    the match's odds; a booster's trigger chance depends on it)."""
    import numpy as np

    p = sc.paths
    w = p.weight
    stage_reach: Dict[str, Dict[str, float]] = {}
    advance: Dict[str, float] = {}
    match_reach: Dict[str, Dict[str, float]] = {}
    match_bo: Dict[str, Dict[str, Dict[str, float]]] = {}
    for t, tid in enumerate(p.team_ids):
        played = (p.opp[:, t, :] >= 0)
        stage_reach[str(tid)] = {st.key: float(w @ played[:, s]) for s, st in enumerate(p.stages)}
        advance[str(tid)] = float(w @ p.advance[:, t].astype(np.float64))
        n_played = played.sum(axis=1)
        match_reach[str(tid)] = {str(k): float(w @ (n_played >= k)) for k in range(1, int(n_played.max()) + 1)} if n_played.size else {}
        mno, bos = p.match_no[:, t, :], p.bo[:, t, :]
        per_k: Dict[str, Dict[str, float]] = {}
        for k in range(1, int(mno.max()) + 1 if mno.size else 1):
            at_k = mno == k
            for bo in np.unique(bos[at_k]).tolist():
                if int(bo) <= 0:
                    continue
                q = float(w @ (at_k & (bos == bo)).any(axis=1))
                if q > 0:
                    per_k.setdefault(str(k), {})[str(int(bo))] = q
        match_bo[str(tid)] = per_k
    return {"stage_reach": stage_reach, "advance": advance, "match_reach": match_reach, "match_bo": match_bo}


# ------------------------------------------------------------ the modal view


def _booster_rows(bucket: Dict[int, list]) -> List[Dict[str, Any]]:
    from backend.routes.groups import _booster_list

    return _booster_list(bucket)


def stage_view(sc: Scored, sel, w, pids: List[int], plans: Optional[Dict[int, Dict[str, Any]]] = None,
               team_rank_by_id: Optional[Dict[int, int]] = None) -> Dict[str, Any]:
    """The Point Sources contract over the selected outcomes (weights w, sum
    1): the stage list, every team's rounds (play / win / opponents /
    eliminated-earlier credit / bye) and the given players' per-stage cells
    (components, boosters, opponents) under their plans, with the penalty
    credited to the stages an early exit misses (in the win component, as
    the groups engine does) and the pads on their pad stages."""
    import numpy as np

    p = sc.paths
    T = len(p.team_ids)
    S = p.n_stages
    w = np.asarray(w, dtype=np.float64)
    sel_idx = np.arange(p.n) if sel is None else np.asarray(sel, dtype=np.int64)
    stages = [st.descriptor() for st in p.stages]
    ranks = team_rank_by_id or {}
    teams: Dict[str, Dict[str, Any]] = {}
    credit_by_team: Dict[int, Dict[int, List[float]]] = {}  # team pos -> stage -> [prob, points]
    for t, tid in enumerate(p.team_ids):
        opp = p.opp[sel_idx, t, :].astype(np.int64)
        won = p.won[sel_idx, t, :]
        mask = opp >= 0
        pads = p.pad[sel_idx, t, :]
        idle = p.idle_done[sel_idx, t, :]
        pen = p.pen[sel_idx, t]
        elim = p.elim_stage[sel_idx, t].astype(np.int64)
        credit: Dict[int, List[float]] = {}
        k_missed = np.rint(-pen / -_WIN_PENALTY).astype(np.int64)  # rounds charged
        for s in range(S):
            picks = (elim == s) & (k_missed > 0)
            if not picks.any():
                continue
            for pos, later in enumerate(p.missed_after.get(s, [])):
                hit = picks & (k_missed > pos)
                if hit.any():
                    cell = credit.setdefault(later, [0.0, 0.0])
                    cell[0] += float(w[hit].sum())
                    cell[1] += float(w[hit].sum()) * _WIN_PENALTY
        credit_by_team[t] = credit
        rounds: Dict[str, Dict[str, Any]] = {}
        for s, st in enumerate(p.stages):
            mk = mask[:, s]
            play = float(w[mk].sum())
            win_mass = float(w[mk & (won[:, s] == 1)].sum())
            opponents: Dict[str, Dict[str, float]] = {}
            if mk.any():
                o = opp[mk, s]
                pl = np.bincount(o, weights=w[mk], minlength=T)
                wn = np.bincount(o, weights=w[mk] * (won[mk, s] == 1), minlength=T)
                opponents = {str(p.team_ids[k]): {"play": float(pl[k]), "win": float(wn[k] / pl[k]) if pl[k] > 0 else 0.0} for k in np.nonzero(pl > 0)[0]}
            bye = float(w[pads[:, s] > 0].sum()) if st.pad else float(w[idle[:, s]].sum())
            eb = credit.get(s, [0.0, 0.0])
            rounds[st.key] = {
                "play": play, "win": (win_mass / play) if play > 0 else 0.0, "opponents": opponents,
                "elim_before": {"prob": eb[0], "points": eb[1]}, "bye": bye,
            }
        teams[str(tid)] = {
            "rounds": rounds, "rank": ranks.get(int(tid)), "padding_prob": 0.0,
            "playoff_padding_prob": float(w[(pads > 0).any(axis=1)].sum()),
            "champion": float(w[p.advance[sel_idx, t]].sum()),
        }
    players: Dict[str, Dict[str, Any]] = {}
    for pid in pids:
        plan = (plans or {}).get(pid)
        m = _player_matrix(sc, pid, sel_idx, plan)
        t = sc.team_pos_of[pid]
        j = sc.pid_pos[pid]
        cells: Dict[str, Dict[str, Any]] = {}
        wm = w[:, None] * m["mask"]
        ev = (m["comps"] * wm[:, :, None]).sum(axis=0)  # (S, 4)
        pad_ev = (m["pad_comps"] * w[:, None, None]).sum(axis=0)  # (S, 3)
        by_slot = (plan or {}).get("by_slot")
        for s, st in enumerate(p.stages):
            if st.pad:
                if m["pads"][:, s].any():
                    cells[st.key] = {
                        "rating": float(pad_ev[s, 0]), "win": float(pad_ev[s, 1]), "role": float(pad_ev[s, 2]), "booster": 0.0,
                        "total": float(pad_ev[s].sum()), "boosters": [], "opponents": {},
                    }
                continue
            mk = m["mask"][:, s]
            if not mk.any() and s not in credit_by_team.get(t, {}):
                continue
            cell = {"rating": float(ev[s, 0]), "win": float(ev[s, 1]), "role": float(ev[s, 2]), "booster": float(ev[s, 3]), "total": float(ev[s].sum())}
            bucket: Dict[int, list] = {}
            opp_pts: Dict[str, Dict[str, float]] = {}
            if mk.any():
                o = m["opp"][mk, s]
                wr = w[mk]
                per = [np.bincount(o, weights=wr * m["comps"][mk, s, c], minlength=T) for c in range(4)]
                pl = np.bincount(o, weights=wr, minlength=T)
                opp_pts = {
                    str(p.team_ids[k]): {"rating": float(per[0][k]), "win": float(per[1][k]), "role": float(per[2][k]), "booster": float(per[3][k]), "total": float(per[0][k] + per[1][k] + per[2][k] + per[3][k])}
                    for k in np.nonzero(pl > 0)[0]
                }
                slots = m["match_no"][mk, s]
                if by_slot is not None:
                    for slot in np.unique(slots):
                        b = by_slot.get(int(slot))
                        if b is None:
                            continue
                        weight = float(wr[slots == slot].sum())
                        cb = bucket.get(b[0])
                        if cb is None:
                            bucket[b[0]] = [weight, b[1], b[2], int(slot), 0.0]
                        else:
                            cb[0] += weight
                else:
                    bids = sc.tbl_bid[m["safe"][mk, s], j]
                    rates = sc.tbl_rate[m["safe"][mk, s], j]
                    for bid in np.unique(bids[bids >= 0]):
                        hit = bids == bid
                        bucket[int(bid)] = [float(wr[hit].sum()), sc.booster_names.get(int(bid), f"Booster {bid}"), float(rates[hit][0]), int(slots[hit][0]), 0.0]
            cell["boosters"] = _booster_rows(bucket)
            cell["opponents"] = opp_pts
            cells[st.key] = cell
        for s, eb in credit_by_team.get(t, {}).items():
            key = p.stages[s].key
            cell = cells.setdefault(key, {"rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0, "total": 0.0, "boosters": [], "opponents": {}})
            cell["win"] += eb[1]
            cell["total"] += eb[1]
        role_pm = (plan or {}).get("role_pm")
        role_info = (
            {"role_id": plan.get("role_id"), "major": float(plan.get("major") or 0.0), "minor": float(plan.get("minor") or 0.0), "points": float(role_pm)}
            if plan and role_pm is not None
            else dict(sc.role_of.get(pid) or {})
        )
        players[str(pid)] = {
            "stages": cells, "role": role_info, "padding": None, "penalty": float(w @ m["pen"]),
            "playoff_padding": None, "playoff_penalty": 0.0,
        }
    return {"stages": stages, "teams": teams, "players": players}


def components_of(view_row: Dict[str, Any]) -> Dict[str, float]:
    comps = {k: 0.0 for k in PARTS}
    for cell in (view_row.get("stages") or {}).values():
        for k in PARTS:
            comps[k] += float(cell.get(k) or 0.0)
    comps["total"] = sum(comps[k] for k in PARTS)
    return comps


# ------------------------------------------------------------------- runs


def runs(sc: Scored, pid: int, sel, w, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The team's runs through the stage over the selected outcomes, grouped
    by results path, in player_runs' output."""
    import numpy as np

    from backend.services import player_runs

    p = sc.paths
    T = len(p.team_ids)
    S = p.n_stages
    j = sc.pid_pos[pid]
    sel_idx = np.arange(p.n) if sel is None else np.asarray(sel, dtype=np.int64)
    m = _player_matrix(sc, pid, sel_idx, plan)
    w = np.asarray(w, dtype=np.float64)
    by_slot = (plan or {}).get("by_slot")
    code = ((m["won"].astype(np.int64) + 1) * (3 ** np.arange(S, dtype=np.int64))[None, :]).sum(axis=1)
    run_map: Dict[tuple, Dict[str, Any]] = {}
    for c in np.unique(code):
        g = code == c
        wg = w[g]
        weight = float(wg.sum())
        if weight <= 0:
            continue
        matches = []
        pads = []
        for s, st in enumerate(p.stages):
            if st.pad:
                if m["pads"][g, s].any():
                    sums = (m["pad_comps"][g, s, :] * wg[:, None]).sum(axis=0)
                    pads.append({"stage": st.key, "label": st.full, "sums": {"rating": float(sums[0]), "win": float(sums[1]), "role": float(sums[2]), "booster": 0.0, "total": float(sums.sum())}})
                continue
            if not m["mask"][g, s].any():
                continue
            o = m["opp"][g, s]
            opps = np.bincount(o, weights=wg, minlength=T)
            sums = (m["comps"][g, s, :] * wg[:, None]).sum(axis=0)
            boosters: Dict[str, float] = {}
            slot = int(m["match_no"][g, s][0])
            if by_slot is not None:
                b = by_slot.get(slot)
                if b is not None:
                    boosters[b[1]] = weight
            else:
                bids = sc.tbl_bid[m["safe"][g, s], j]
                for bid in np.unique(bids[bids >= 0]):
                    boosters[sc.booster_names.get(int(bid), f"Booster {bid}")] = float(wg[bids == bid].sum())
            matches.append({
                "stage": st.key, "label": st.label, "full": st.full, "slot": slot, "won": bool(m["won"][g, s][0] == 1),
                "opps": {p.team_ids[k]: float(opps[k]) for k in np.nonzero(opps > 0)[0]},
                "boosters": boosters,
                "sums": {"rating": float(sums[0]), "win": float(sums[1]), "role": float(sums[2]), "booster": float(sums[3]), "total": float(sums.sum())},
            })
        penalty_sum = float((wg * m["pen"][g]).sum())
        totals = {k: sum(mm["sums"][k] for mm in matches) + sum(pp["sums"][k] for pp in pads) for k in PARTS}
        totals["win"] += penalty_sum
        totals["total"] = sum(totals.values())
        sig = (tuple((mm["stage"], mm["won"]) for mm in matches), tuple(pp["stage"] for pp in pads))
        run_map[sig] = {"weight": weight, "matches": matches, "pads": pads, "penalty_sum": penalty_sum, "sums": totals}
    return player_runs.finish_runs(run_map, float(w.sum()) or 1.0)


# ------------------------------------------------------------ bracket paths


def bracket_paths(
    team_ids: List[int], matches: List[tuple], stages: List[Stage], rem_by_stage: Dict[str, int],
    pad_after: Dict[str, str], advance_match: str, probs_by_series: Dict[int, Any], pad_rule: str = "average",
    missed_after: Optional[Dict[str, List[str]]] = None, kind: str = "single",
    bye_pads: Optional[List[tuple]] = None,
) -> Paths:
    """Every outcome of a bracket, enumerated exactly: one bit per match in
    play order (0 = the first-listed team wins, as the stored playoff codes),
    `matches` = (key, stage key, feeder a, feeder b) with feeders ("seed",
    slot) / ("win", key) / ("lose", key). The loser of a match at a stage with
    rem_by_stage > 0 is charged −3 per round it misses; the winner of a match
    at a stage in pad_after is credited one padded match at that pad stage.
    bye_pads = (seed slot, pad stage key): a team that skips a round, credited
    one padded match there in every outcome.
    The outcome's probability is the product of its match odds."""
    import numpy as np

    T = len(team_ids)
    S = len(stages)
    stage_pos = {st.key: i for i, st in enumerate(stages)}
    M = len(matches)
    n = 1 << M
    codes = np.arange(n, dtype=np.int64)
    rows = np.arange(n, dtype=np.int64)
    opp = np.full((n, T, S), -1, dtype=np.int16)
    won = np.full((n, T, S), -1, dtype=np.int8)
    bo = np.zeros((n, T, S), dtype=np.int8)
    match_no = np.zeros((n, T, S), dtype=np.int16)
    pad = np.zeros((n, T, S), dtype=np.int8)
    pen = np.zeros((n, T), dtype=np.float64)
    elim_stage = np.full((n, T), -1, dtype=np.int8)
    advance = np.zeros((n, T), dtype=bool)
    played = np.zeros((n, T), dtype=np.int64)
    prob = np.ones(n, dtype=np.float64)
    winner_of: Dict[str, Any] = {}
    loser_of: Dict[str, Any] = {}
    match_a = np.zeros((n, M), dtype=np.int16)
    match_b = np.zeros((n, M), dtype=np.int16)
    match_w = np.zeros((n, M), dtype=np.int16)

    def feed(f) -> Any:
        kind_, ref = f[0], f[1]
        if kind_ == "seed":
            return np.full(n, int(ref), dtype=np.int64)
        return winner_of[str(ref)] if kind_ == "win" else loser_of[str(ref)]

    for m_i, (key, stage_key, fa, fb) in enumerate(matches):
        s = stage_pos[stage_key]
        series = int(stages[s].series)
        a = feed(fa)
        b = feed(fb)
        bit = (codes >> (M - 1 - m_i)) & 1
        wnr = np.where(bit == 1, b, a)
        lsr = np.where(bit == 1, a, b)
        p_a = probs_by_series[series][a, b]
        prob *= np.where(bit == 1, 1.0 - p_a, p_a)
        played[rows, a] += 1
        played[rows, b] += 1
        opp[rows, a, s] = b
        opp[rows, b, s] = a
        won[rows, wnr, s] = 1
        won[rows, lsr, s] = 0
        bo[rows, a, s] = series
        bo[rows, b, s] = series
        match_no[rows, a, s] = played[rows, a]
        match_no[rows, b, s] = played[rows, b]
        rem = int(rem_by_stage.get(stage_key, 0))
        if rem > 0:
            pen[rows, lsr] += _WIN_PENALTY * rem
            elim_stage[rows, lsr] = s
        pad_stage = pad_after.get(stage_key)
        if pad_stage is not None:
            pad[rows, wnr, stage_pos[pad_stage]] = 1
        winner_of[key] = wnr
        loser_of[key] = lsr
        match_a[:, m_i] = a
        match_b[:, m_i] = b
        match_w[:, m_i] = wnr
        if key == advance_match:
            advance[rows, wnr] = True
    for slot, pad_stage in bye_pads or []:
        pad[:, int(slot), stage_pos[pad_stage]] = 1
    missed: Dict[int, List[int]] = {}
    if missed_after:
        for k, later in missed_after.items():
            missed[stage_pos[k]] = [stage_pos[x] for x in later]
    else:
        match_stages = [i for i, st in enumerate(stages) if not st.pad]
        for i in match_stages:
            missed[i] = [x for x in match_stages if x > i and rem_by_stage.get(stages[x].key, 0) >= 0 and stages[x].key != "third_place"]
    return Paths(
        team_ids=list(team_ids), stages=list(stages), opp=opp, won=won, bo=bo, match_no=match_no, pad=pad, pen=pen,
        elim_stage=elim_stage, advance=advance, idle_done=np.zeros((n, T, S), dtype=bool), weight=prob / prob.sum(),
        outcome_prob=prob, missed_after=missed, pad_rule=pad_rule, kind=kind,
        extra={"matches": [list(x) for x in matches], "n_matches": M, "match_a": match_a, "match_b": match_b, "match_w": match_w},
    )


def outcome_bracket(paths: Paths, i: int) -> Dict[str, List[dict]]:
    """One enumerated outcome as the bracket dict the page draws: stage key
    -> matches with teams, winner, loser and key, in play order."""
    out: Dict[str, List[dict]] = {}
    matches = paths.extra.get("matches") or []
    ma, mb, mw = paths.extra.get("match_a"), paths.extra.get("match_b"), paths.extra.get("match_w")
    if ma is None:
        return out
    for m_i, (key, stage_key, _fa, _fb) in enumerate(matches):
        a, b, w = paths.team_ids[int(ma[i, m_i])], paths.team_ids[int(mb[i, m_i])], paths.team_ids[int(mw[i, m_i])]
        out.setdefault(stage_key, []).append({"key": key, "teams": [a, b], "winner": w, "loser": b if w == a else a})
    return out
