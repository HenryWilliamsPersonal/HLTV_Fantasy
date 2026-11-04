"""The stage formats the path engine values, each as paths + a stage list.

  swiss   16 teams, top 8, three wins / three losses: sampled stages
          (services/swiss_engine), five rounds plus a padding stage.
  single  a 2/4/8/16-team single-elimination bracket, optional third-place
          decider, a longer final: every outcome enumerated.
  single6 the 6-team byes bracket (two group winners straight into the semis;
          slots [bye1, qf1a, qf1b, qf2a, qf2b, bye2]), the bye padded +6.
  double  the 8-team double-elimination bracket (HLTV's routing, the two
          round byes padded with the real games' average): every outcome
          enumerated.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional

from backend.services.paths_engine import Paths, Stage, bracket_paths

SINGLE_SIZES = (2, 4, 8, 16)
_ROUND_LABEL = {"round_of_32": "R32", "round_of_16": "R16", "quarters": "QF", "semis": "SF", "final": "Final", "third_place": "3rd"}
_ROUND_FULL = {
    "round_of_32": "Round of 32", "round_of_16": "Round of 16", "quarters": "Quarter-final", "semis": "Semi-final",
    "final": "Final", "third_place": "Third-place decider",
}
_DOUBLE_LABEL = {
    "ub_r1": "UB R1", "ub_sf": "UB SF", "lb_r1": "LB R1", "bye_r3": "Bye", "lb_sf": "LB SF", "ub_final": "UB Final",
    "lb_final": "LB Final", "bye_r5": "Bye", "cons_final": "Cons. Final", "grand_final": "Grand Final",
}


def win_matrices(team_ids: List[int], series: Iterable[int], prob_cache: Dict) -> Dict[str, List[List[float]]]:
    """P(row beats column) per series type, through the shared canonical-
    direction cache (the match scorer reads the same odds)."""
    from backend.routes.playoff import cached_win_prob

    return {
        f"bo{s}": [[cached_win_prob(prob_cache, a, b, f"bo{s}") if a != b else 0.5 for b in team_ids] for a in team_ids]
        for s in sorted(set(int(x) for x in series))
    }


def matrices_by_series(win_probs: Dict[str, List[List[float]]]) -> Dict[int, Any]:
    import numpy as np

    return {int(k[2:]): np.asarray(v, dtype=np.float64) for k, v in (win_probs or {}).items()}


# ------------------------------------------------------------------ swiss


def swiss_paths(samples, team_ids: List[int], probs_by_series: Dict[int, Any]) -> Paths:
    """A sampled Swiss stage as paths: five rounds and a padding stage; a
    team out at three losses is charged −3 per match it misses, a team
    through at three wins is credited its missing matches (the constant
    rule: base rating, +6, its role)."""
    import numpy as np

    from backend.services.swiss_engine import ROUNDS, TARGET

    n, T = samples.played.shape
    S = ROUNDS + 1
    stages = [Stage(key=f"r{r + 1}", label=f"R{r + 1}", full=f"Round {r + 1}", section="playoff", section_title="Swiss Stage") for r in range(ROUNDS)]
    stages.append(Stage(key="padding", label="Padding", full="Padding", section="playoff", section_title="Swiss Stage", pad=True))
    opp = np.full((n, T, S), -1, dtype=np.int16)
    won = np.full((n, T, S), -1, dtype=np.int8)
    bo = np.zeros((n, T, S), dtype=np.int8)
    opp[:, :, :ROUNDS] = samples.opp
    won[:, :, :ROUNDS] = samples.won
    bo[:, :, :ROUNDS] = samples.bo
    mask = opp >= 0
    match_no = (np.cumsum(mask, axis=2) * mask).astype(np.int16)
    played = samples.played.astype(np.int64)
    wins = samples.wins.astype(np.int64)
    missing = np.clip(ROUNDS - played, 0, None)
    qualified = wins >= TARGET
    eliminated = (played - wins) >= TARGET
    pad = np.zeros((n, T, S), dtype=np.int8)
    pad[:, :, ROUNDS] = (missing * qualified).astype(np.int8)
    pen = (-3.0 * missing * eliminated).astype(np.float64)
    elim_stage = np.where(eliminated & (missing > 0), played - 1, -1).astype(np.int8)
    idle_done = np.zeros((n, T, S), dtype=bool)
    for r in range(ROUNDS):
        idle_done[:, :, r] = (played <= r) & qualified
    # the exact probability of each sampled stage: every match once
    prob = np.ones(n, dtype=np.float64)
    for t in range(T):
        for r in range(ROUNDS):
            o = samples.opp[:, t, r].astype(np.int64)
            hit = (o > t)
            if not hit.any():
                continue
            series = samples.bo[hit, t, r].astype(np.int64)
            p = np.empty(hit.sum(), dtype=np.float64)
            for s_val in np.unique(series):
                m_s = series == s_val
                p[m_s] = probs_by_series[int(s_val)][t, o[hit][m_s]]
            w_t = samples.won[hit, t, r] == 1
            prob[hit] *= np.where(w_t, p, 1.0 - p)
    missed = {r: list(range(r + 1, ROUNDS)) for r in range(ROUNDS)}
    return Paths(
        team_ids=list(team_ids), stages=stages, opp=opp, won=won, bo=bo, match_no=match_no, pad=pad, pen=pen,
        elim_stage=elim_stage, advance=qualified, idle_done=idle_done, weight=np.full(n, 1.0 / n, dtype=np.float64),
        outcome_prob=prob, missed_after=missed, pad_rule="constant", kind="swiss",
    )


# ---------------------------------------------------------------- brackets


def single_elim_spec(team_slots: List[int], third_place: bool, final_bo: int) -> Dict[str, Any]:
    """Matches (key, stage, feeders), stages, rules of a single-elimination
    bracket in the stored playoff codes' play order: round by round, slot
    pairs (0 v 1, 2 v 3, …), the decider last between the semi-final losers."""
    from backend.routes.playoff import _ROUND_NAME_BY_TEAMS

    n = len(team_slots)
    rounds_total = int(round(math.log2(n)))
    if n not in SINGLE_SIZES or 2 ** rounds_total != n:
        raise ValueError(f"single-elimination needs 2, 4, 8 or 16 teams, got {n}")
    final_bo = int(final_bo) if int(final_bo or 3) in (1, 3, 5) else 3
    matches: List[tuple] = []
    stages: List[Stage] = []
    rem_by_stage: Dict[str, int] = {}
    prev_keys: List[str] = []
    alive = n
    r = 0
    while alive > 1:
        name = _ROUND_NAME_BY_TEAMS.get(alive, f"round_of_{alive}")
        keys = []
        for i in range(alive // 2):
            key = f"{name}_{i + 1}"
            fa = ("seed", 2 * i) if r == 0 else ("win", prev_keys[2 * i])
            fb = ("seed", 2 * i + 1) if r == 0 else ("win", prev_keys[2 * i + 1])
            matches.append((key, name, fa, fb))
            keys.append(key)
        stages.append(Stage(key=name, label=_ROUND_LABEL.get(name, name), full=_ROUND_FULL.get(name, name), series=final_bo if alive == 2 else 3))
        rem_by_stage[name] = rounds_total - r - 1
        prev_keys = keys
        alive //= 2
        r += 1
    advance_match = prev_keys[0]
    if third_place and n >= 4:
        semis = [k for k, st, _a, _b in matches if st == "semis"]
        matches.append(("third_place_1", "third_place", ("lose", semis[0]), ("lose", semis[1])))
        stages.append(Stage(key="third_place", label="3rd", full="Third-place decider", series=3))
        rem_by_stage["third_place"] = 0
        rem_by_stage["semis"] = 0  # a semi-final loser plays the decider: no match missed
    return {
        "kind": "single", "matches": matches, "stages": stages, "rem_by_stage": rem_by_stage, "pad_after": {},
        "advance_match": advance_match, "missed_after": None, "pad_rule": "average", "rounds": rounds_total,
    }


# A bye: HLTV credits the skipped quarter-final as a padded match; with no
# earlier game in the stage to average it carries the win points only (the
# rule the 6-team playoff engine has always used).
BYE_PAD_POINTS = (0.0, 6.0, 0.0)


def single6_spec(team_slots: List[int], third_place: bool, final_bo: int) -> Dict[str, Any]:
    """The 6-team byes bracket: QF1 = slots 1 v 2, QF2 = 3 v 4, SF1 = bye 1
    (slot 0) v QF1 winner, SF2 = bye 2 (slot 5) v QF2 winner, the final, the
    decider between the semi losers; the byes padded at the quarter-finals."""
    if len(team_slots) != 6:
        raise ValueError(f"the byes bracket needs 6 teams, got {len(team_slots)}")
    final_bo = int(final_bo) if int(final_bo or 3) in (1, 3, 5) else 3
    matches = [
        ("quarters_1", "quarters", ("seed", 1), ("seed", 2)),
        ("quarters_2", "quarters", ("seed", 3), ("seed", 4)),
        ("semis_1", "semis", ("seed", 0), ("win", "quarters_1")),
        ("semis_2", "semis", ("seed", 5), ("win", "quarters_2")),
        ("final_1", "final", ("win", "semis_1"), ("win", "semis_2")),
    ]
    stages = [
        Stage(key="quarters", label="QF", full="Quarter-final", series=3),
        Stage(key="bye", label="Bye", full="Quarter-final bye", pad=True, pad_points=BYE_PAD_POINTS),
        Stage(key="semis", label="SF", full="Semi-final", series=3),
        Stage(key="final", label="Final", full="Final", series=final_bo),
    ]
    rem_by_stage = {"quarters": 2, "semis": 1, "final": 0}
    if third_place:
        matches.append(("third_place_1", "third_place", ("lose", "semis_1"), ("lose", "semis_2")))
        stages.append(Stage(key="third_place", label="3rd", full="Third-place decider", series=3))
        rem_by_stage["third_place"] = 0
        rem_by_stage["semis"] = 0  # a semi-final loser plays the decider: no match missed
    return {
        "kind": "single6", "matches": matches, "stages": stages, "rem_by_stage": rem_by_stage, "pad_after": {},
        "advance_match": "final_1", "missed_after": None, "pad_rule": "average", "rounds": 3,
        "bye_pads": [(0, "bye"), (5, "bye")],
    }


def double_elim_spec(team_slots: List[int], grand_final_bo: int) -> Dict[str, Any]:
    from backend.routes import playoff as P

    if len(team_slots) != 8:
        raise ValueError(f"the double-elimination bracket needs 8 teams, got {len(team_slots)}")
    grand_final_bo = int(grand_final_bo) if int(grand_final_bo or 3) in (1, 3, 5) else 3
    layout = {x["key"]: x for x in P._DOUBLE8_LAYOUT}
    stages = []
    for key in P._DOUBLE8_STAGES:
        lo = layout.get(key, {})
        stages.append(Stage(
            key=key, label=_DOUBLE_LABEL.get(key, key), full=str(lo.get("full") or key), section=str(lo.get("section") or "playoff"),
            section_title=str(lo.get("section_title") or "Playoffs"), bracket=str(lo.get("bracket") or "playoff"),
            opener=bool(lo.get("opener")), pad=bool(lo.get("pad")), series=grand_final_bo if key == "grand_final" else 3,
        ))
    return {
        "kind": "double", "matches": list(P._DOUBLE8_MATCHES), "stages": stages, "rem_by_stage": dict(P._DOUBLE8_REM),
        "pad_after": {k: v[0] for k, v in P._DOUBLE8_PAD_AFTER.items()}, "advance_match": "grand_final",
        "missed_after": {k: list(v) for k, v in P._DOUBLE8_MISSED_AFTER.items()}, "pad_rule": "average", "rounds": P._DOUBLE8_ROUNDS_TOTAL,
    }


def bracket_spec(kind: str, team_slots: List[int], third_place: bool, final_bo: int) -> Dict[str, Any]:
    if str(kind) == "double":
        return double_elim_spec(team_slots, final_bo)
    if str(kind) == "single6":
        return single6_spec(team_slots, third_place, final_bo)
    return single_elim_spec(team_slots, third_place, final_bo)


def bracket_series(spec: Dict[str, Any]) -> List[int]:
    return sorted({int(st.series) for st in spec["stages"] if not st.pad})


def bracket_paths_for(spec: Dict[str, Any], team_slots: List[int], probs_by_series: Dict[int, Any]) -> Paths:
    return bracket_paths(
        [int(t) for t in team_slots], spec["matches"], spec["stages"], spec["rem_by_stage"], spec["pad_after"],
        spec["advance_match"], probs_by_series, pad_rule=spec["pad_rule"], missed_after=spec["missed_after"], kind=spec["kind"],
        bye_pads=spec.get("bye_pads"),
    )


def picks_to_code(spec: Dict[str, Any], picks: Dict[str, int], team_slots: List[int]) -> Optional[int]:
    """The outcome code of a complete set of winner picks (match key ->
    winning team id), None while any match is unresolved."""
    winners: Dict[str, int] = {}
    losers: Dict[str, int] = {}
    code = 0
    for key, _stage, fa, fb in spec["matches"]:
        a = int(team_slots[int(fa[1])]) if fa[0] == "seed" else (winners if fa[0] == "win" else losers).get(str(fa[1]))
        b = int(team_slots[int(fb[1])]) if fb[0] == "seed" else (winners if fb[0] == "win" else losers).get(str(fb[1]))
        if a is None or b is None:
            return None
        w = picks.get(str(key))
        if w is None or int(w) not in (a, b):
            return None
        bit = 1 if int(w) == b else 0
        code = (code << 1) | bit
        winners[str(key)] = int(w)
        losers[str(key)] = b if int(w) == a else a
    return code
