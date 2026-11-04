import base64
import json
import logging
import math
import os
import random
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from backend.services import event_rosters
from backend.services import roster_scores
from backend.data.db import connect as _connect
from backend.data.player_db import get_player
from backend.data.singleton_state import SingletonState
from backend.data.team_db import get_team_by_id
from backend.services.team_optimizer import optimize_rosters, parse_optimizer_payload, serialize_roster
from backend.swiss_stage.fantasy_scoring import compute_elimination_penalty_components, compute_role_points
from backend.swiss_stage.team_initialization import initialize_teams
from backend.swiss_stage.swiss_models import TeamState, PlayerState
from backend.services.match_engine import simulate_match_outcome, apply_fantasy_points_for_team, calculate_win_probability, BOOSTER_NAMES
from backend.services.swiss_booster_assignment import BOOSTER_POINT_VALUE

router = APIRouter()
PLAYOFF_JOBS = {}
PLAYOFF_JOBS_LOCK = threading.Lock()
PLAYOFF_BEST_TEAM_JOBS = {}
PLAYOFF_BEST_TEAM_JOBS_LOCK = threading.Lock()
PLAYOFF_COMPLETED_BRACKET_JOBS = {}
PLAYOFF_COMPLETED_BRACKET_JOBS_LOCK = threading.Lock()


# Two independent copies of the playoff pipeline state: the regular playoff
# bracket and the Bounty Event playoffs share every endpoint, distinguished by
# a `variant` field ("main"/"bounty") in payloads or query params.
_STATE_SETS = {
    "main": {
        "playoff": SingletonState("playoff_simulation_state", result_column="results_json", result_key="results", keyed=True),
        "completed": SingletonState("playoff_completed_bracket_state", keyed=True),
        "best": SingletonState("playoff_best_team_state", keyed=True),
        # Small summary saved alongside the combos blob so metadata endpoints
        # never have to materialize the (multi-hundred-MB) result_json.
        "meta": SingletonState("playoff_best_team_meta", keyed=True),
    },
    "bounty": {
        "playoff": SingletonState("bounty_playoff_simulation_state", result_column="results_json", result_key="results", keyed=True),
        "completed": SingletonState("bounty_completed_bracket_state", keyed=True),
        "best": SingletonState("bounty_best_team_state", keyed=True),
        "meta": SingletonState("bounty_best_team_meta", keyed=True),
    },
}


def _variant(value) -> str:
    return "bounty" if str(value or "").strip().lower() == "bounty" else "main"


def _states(variant) -> dict:
    return _STATE_SETS[_variant(variant)]


def _event_key(event_id=None) -> int:
    """Row key for stored playoff state: the requested fantasy event, else the
    active one. Runs started from the operator app always target the active
    event; read endpoints accept ?event_id= so the public app can show any
    imported event's stored run."""
    from backend.data.event_db import get_active_event_id

    try:
        if event_id:
            return int(event_id)
    except (TypeError, ValueError):
        pass
    return int(get_active_event_id() or 1)


def ensure_playoff_schema() -> None:
    for states in _STATE_SETS.values():
        for state in states.values():
            state.ensure_table()
    # These tables were single-row until 2026-09-16; the row they held was the
    # active event's run, so it becomes that event's row (once).
    try:
        from backend.data.event_db import get_active_event_id

        active = get_active_event_id()
        if active:
            for state in _STATE_SETS["main"].values():
                state.migrate_legacy_row(int(active))
    except Exception:  # noqa: BLE001 - never block startup on the migration
        pass


def save_latest_playoff(payload: dict, results: dict, variant: str = "main", event_id=None) -> None:
    _states(variant)["playoff"].save(payload, results, key=_event_key(event_id))


def load_latest_playoff(variant: str = "main", event_id=None) -> dict | None:
    return _states(variant)["playoff"].load(key=_event_key(event_id))


def save_latest_completed_bracket(payload: dict, result: dict, variant: str = "main", event_id=None) -> None:
    _states(variant)["completed"].save(payload, result, key=_event_key(event_id))


def load_latest_completed_bracket(variant: str = "main", event_id=None) -> dict | None:
    return _states(variant)["completed"].load(key=_event_key(event_id))


def _best_team_meta_summary(result: dict) -> dict:
    return {
        "mode": result.get("mode"),
        "player_count": result.get("player_count"),
        "total_teams": len(result.get("all_teams") or []),
        "processed_combinations": result.get("processed_combinations"),
        "total_combinations": result.get("total_combinations"),
        # True when the pool was too large to store every roster and only the
        # strongest candidates were kept (16-team two-phase).
        "approximate": bool(result.get("approximate")),
        "candidate_count": result.get("candidate_count"),
    }


def save_latest_playoff_best_team(payload: dict, result: dict, variant: str = "main", event_id=None) -> None:
    key = _event_key(event_id)
    _states(variant)["best"].save(payload, result, key=key)
    _states(variant)["meta"].save(payload, _best_team_meta_summary(result), key=key)
    _query_cache_clear()
    # Warm the default Top 5 queries off the request path (the combos blob
    # parse plus the first sort is the slow part; the public app opens on it).
    threading.Thread(target=seed_playoff_queries, args=(variant, key), name="playoff-top5-warmup", daemon=True).start()


def load_latest_playoff_best_team(variant: str = "main", event_id=None) -> dict | None:
    return _states(variant)["best"].load(key=_event_key(event_id))


# Saved-combos query responses memoized per (variant, event, stored-run stamp,
# body). Responses are small (10 + one page of rosters); the work they save is
# the blob parse and the full sort of every stored roster.
_QUERY_CACHE: dict = {}
_QUERY_CACHE_LOCK = threading.Lock()
_QUERY_CACHE_MAX = 256


def _query_cache_clear() -> None:
    with _QUERY_CACHE_LOCK:
        _QUERY_CACHE.clear()
        _CONSTRAINED_CACHE.clear()


# Most-likely-winner rosters recomputed for a filter set (include/exclude):
# (variant, event, stored-run stamp, include, exclude) -> all_teams. A roster
# only "wins" an outcome against the other rosters the user can still pick, so
# the odds must be re-scored among those, not filtered from the stored list.
# Few entries: each holds every roster of a constrained pool.
_CONSTRAINED_CACHE: dict = {}
_CONSTRAINED_CACHE_MAX = 8


def _constrained_most_outcomes(variant: str, key: int, options: dict) -> list:
    """Rosters of the constrained pool scored in most-likely-winner mode, from
    the cache or recomputed against the stored outcome table (~6 s for an
    8-team double elimination; one compute at a time, like the groups page)."""
    from backend.routes import groups as groups_routes

    latest_playoff = load_latest_playoff(variant, key)
    if not latest_playoff:
        raise HTTPException(status_code=404, detail="No stored playoff simulation found.")
    include = {int(x) for x in options["include"]}
    exclude = {int(x) for x in options["exclude"]}
    cache_key = (
        _variant(variant),
        int(key),
        str(latest_playoff["updated_at"]),
        tuple(sorted(include)),
        tuple(sorted(exclude)),
        int(options["budget"]),
        int(options["max_per_team"]),
    )
    with _QUERY_CACHE_LOCK:
        cached = _CONSTRAINED_CACHE.get(cache_key)
    if cached is not None:
        return cached
    with groups_routes._LIVE_COMPUTE_LOCK:
        with _QUERY_CACHE_LOCK:
            cached = _CONSTRAINED_CACHE.get(cache_key)
        if cached is not None:
            return cached
        latest_results = latest_playoff.get("results", {}) or {}
        players_info = _build_players_info_from_sim_results(latest_results.get("teams", {}) or {}, exclude)
        result = _optimize_playoff_teams_by_outcomes(
            players_info, latest_results, include, options["budget"], options["max_per_team"], "most_outcomes"
        )
        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(status_code=400, detail=str(result["error"]))
        teams = list(result.get("all_teams") or [])
        with _QUERY_CACHE_LOCK:
            if len(_CONSTRAINED_CACHE) >= _CONSTRAINED_CACHE_MAX:
                _CONSTRAINED_CACHE.pop(next(iter(_CONSTRAINED_CACHE)))
            _CONSTRAINED_CACHE[cache_key] = teams
        return teams


def _query_cache_key(variant: str, key: int, updated_at, body: dict) -> tuple:
    return (_variant(variant), int(key), str(updated_at), json.dumps(body, sort_keys=True, default=str))


def seed_playoff_queries(variant: str = "main", event_id=None) -> dict:
    """Run the three default Top 5 queries for an event's stored combos so the
    first public request answers from cache. Returns seconds per mode."""
    import time as _time

    timings = {}
    key = _event_key(event_id)
    for mode in ("average", "single_outcome", "most_outcomes"):
        t0 = _time.time()
        try:
            query_latest_best_team_playoff({"mode": mode, "variant": _variant(variant), "page": 0}, event_id=key)
            timings[mode] = round(_time.time() - t0, 2)
        except HTTPException:
            timings[mode] = "none"
        except Exception as exc:  # noqa: BLE001
            timings[mode] = f"error: {exc}"
    return timings


def _saved_combo_metric(team: dict, mode: str) -> float:
    if mode == "single_outcome":
        return float(team.get("ceiling_points") or 0.0)
    if mode == "season_max":
        return float(team.get("season_points") or 0.0)
    if mode == "most_outcomes":
        # Rank by chance this roster ends up the winner; blobs saved before the
        # probability field existed fall back to the raw outcome-win count.
        prob = team.get("outcome_win_probability")
        if prob is not None:
            return float(prob or 0.0)
        return float(team.get("outcome_wins") or 0.0)
    return float(team.get("average_ev", team.get("total_ev", 0.0)) or 0.0)


def _filter_saved_combo_teams(teams: list[dict], include: set[int], exclude: set[int], search: str = "") -> list[dict]:
    """Search matches PLAYER NAMES only — team/id filtering has its own
    dedicated include/exclude controls."""
    q = str(search or "").strip().lower()
    filtered = []
    for team in teams or []:
        players = team.get("players") or []
        pids = {int(p.get("player_id") or 0) for p in players}
        if include and not include.issubset(pids):
            continue
        if exclude and pids.intersection(exclude):
            continue
        if q and not any(q in str(p.get("name") or "").lower() for p in players):
            continue
        filtered.append(team)
    return filtered


def _sort_saved_combo_teams(teams: list[dict], mode: str, sort_key: str = "ev_desc") -> list[dict]:
    def metric(team: dict) -> float:
        return _saved_combo_metric(team, mode)

    if sort_key == "cost_asc":
        return sorted(teams, key=lambda team: int(team.get("cost") or 0))
    if sort_key == "cost_desc":
        return sorted(teams, key=lambda team: int(team.get("cost") or 0), reverse=True)
    if sort_key == "cpp_asc":
        return sorted(teams, key=lambda team: metric(team) / float(team.get("cost") or 1))
    if sort_key == "cpp_desc":
        return sorted(teams, key=lambda team: metric(team) / float(team.get("cost") or 1), reverse=True)
    if sort_key == "ev_asc":
        return sorted(teams, key=metric)
    return sorted(teams, key=metric, reverse=True)


def _page_items(items: list[dict], page: int, page_size: int) -> list[dict]:
    safe_page = max(0, int(page or 0))
    safe_size = max(1, min(500, int(page_size or 200)))
    start = safe_page * safe_size
    return items[start : start + safe_size]


def _apply_elimination_penalty(team: TeamState, remaining_rounds: int) -> None:
    """
    Apply -3 points per remaining round to all players (win component/total).
    """
    if remaining_rounds <= 0:
        return
    penalty = compute_elimination_penalty_components(remaining_rounds)["win"]
    for p in team.players.values():
        p.win_points_total += penalty
        p.total_points += penalty
        p.point_breakdown.append(
            {
                "match_number": None,
                "match_type": "ELIMINATION",
                "opponent_team_id": None,
                "opponent_rank": None,
                "did_win": False,
                "win_probability": 0.0,
                "rating_used": None,
                "rating_points": 0.0,
                "win_points": float(penalty),
                "role_id": p.role_id,
                "role_major_pct": float(p.major_pct),
                "role_minor_pct": float(p.minor_pct),
                "role_points": 0.0,
                "booster_slot": None,
                "booster_id": None,
                "booster_name": None,
                "booster_trigger_rate": 0.0,
                "booster_points": 0.0,
                "total_points": float(penalty),
                "note": f"Elimination penalty for {remaining_rounds} unplayed round(s)",
            }
        )


def _booster_slots_for(ps: PlayerState | None) -> list[dict]:
    """The player's booster assignment per match slot: ranked by edge over the
    field-average trigger rate, best edge on the team's 1st match. Points still
    pay 5 x the player's raw rate."""
    if ps is None:
        return []
    slots = []
    for i, rate in enumerate(ps.boosters or []):
        bid_raw = ps.booster_ids[i] if i < len(ps.booster_ids or []) else -1
        bid = int(bid_raw) if bid_raw is not None and int(bid_raw) >= 0 else None
        edges = ps.booster_edges or []
        slots.append(
            {
                "slot": i + 1,
                "booster_id": bid,
                "booster_name": BOOSTER_NAMES.get(bid) if bid is not None else None,
                "booster_rate": float(rate or 0.0),
                "edge": float(edges[i]) if i < len(edges) else 0.0,
            }
        )
    return slots


def _role_info_for(ps: PlayerState | None) -> dict | None:
    """The player's best role as the modal shows it: id, trigger rates and
    points per match."""
    if ps is None:
        return None
    return {
        "role_id": ps.role_id,
        "major": float(ps.major_pct or 0.0),
        "minor": float(ps.minor_pct or 0.0),
        "points": float(compute_role_points(ps)),
    }


def _build_playoff_lookup_context(team_slots: List[int]) -> tuple[Dict[int, dict], Dict[int, int]]:
    conn = _connect()
    try:
        team_rows = conn.execute(
            """
            SELECT team_id, hltv_rank, player1_id, player2_id, player3_id, player4_id, player5_id
            FROM teams
            WHERE team_id IN ({})
            """.format(",".join("?" for _ in team_slots)),
            tuple(int(t) for t in team_slots),
        ).fetchall()

        team_rank_by_id: Dict[int, int] = {}
        player_ids = set()
        team_of_player: Dict[int, int] = {}
        for r in team_rows:
            tid = int(r["team_id"])
            rank = r["hltv_rank"]
            team_rank_by_id[tid] = int(rank) if rank is not None else 100
            # the event's own roster when the team plays the event being valued
            override = event_rosters.roster_for(tid)
            roster = list(override) if override is not None else [r[col] for col in ("player1_id", "player2_id", "player3_id", "player4_id", "player5_id")]
            for pid in roster:
                if pid:
                    player_ids.add(int(pid))
                    team_of_player[int(pid)] = tid

        player_rows_by_id: Dict[int, dict] = {}
        if player_ids:
            p_rows = conn.execute(
                """
                SELECT * FROM players
                WHERE player_id IN ({})
                """.format(",".join("?" for _ in player_ids)),
                tuple(player_ids),
            ).fetchall()
            player_rows_by_id = {int(r["player_id"]): dict(r) for r in p_rows}
    finally:
        conn.close()
    # the learned rating model's expected rating per opponent (travels with
    # the rows into the stored input snapshot; see services/rating_model)
    try:
        from backend.services import rating_model

        rating_model.attach_match_ratings(player_rows_by_id, team_of_player, team_rank_by_id)
    except Exception:  # noqa: BLE001 - the Top-X curve still prices the rows
        logging.getLogger(__name__).warning("Could not attach the learned match ratings", exc_info=True)
    return player_rows_by_id, team_rank_by_id


def _clone_team_states(team_states: Dict[int, TeamState]) -> Dict[int, TeamState]:
    cloned: Dict[int, TeamState] = {}
    for tid, ts in team_states.items():
        cloned_players: Dict[int, PlayerState] = {}
        for pid, p in ts.players.items():
            cloned_players[pid] = PlayerState(
                player_id=p.player_id,
                rating=p.rating,
                major_pct=p.major_pct,
                minor_pct=p.minor_pct,
                boosters=list(p.boosters),
                role_id=p.role_id,
                booster_ids=list(p.booster_ids),
                booster_rates=dict(p.booster_rates),
                total_points=p.total_points,
                rating_points_total=p.rating_points_total,
                win_points_total=p.win_points_total,
                role_points_total=p.role_points_total,
                booster_points_total=p.booster_points_total,
                point_breakdown=[dict(row) for row in p.point_breakdown],
            )
        cloned[tid] = TeamState(
            team_id=ts.team_id,
            vrs_rank=ts.vrs_rank,
            players=cloned_players,
            wins=ts.wins,
            losses=ts.losses,
            opponents_played=set(ts.opponents_played),
        )
    return cloned


def cached_win_prob(
    prob_cache: Dict[tuple, float] | None, a_id: int, b_id: int, match_type: str = "bo3"
) -> float:
    """P(a beats b) in a series of `match_type` (Bo3 unless a round says
    otherwise) via the shared cache, always evaluated in a canonical direction
    (lower team id first) and mirrored as 1 - p.

    The win-probability model is not symmetric — p(a, b) and 1 - p(b, a) can
    differ by a few percent — so caching whichever direction a caller asked
    for first made results depend on evaluation order (the exact playoff
    engine and the old seeding loop disagreed by up to 0.4 points per player
    for that reason alone). One canonical direction makes every path agree.
    """
    if a_id == b_id:
        return 0.5
    mt = str(match_type or "bo3").lower()
    tag = () if mt == "bo3" else (mt,)
    key = (a_id, b_id) + tag
    if prob_cache is not None and key in prob_cache:
        return prob_cache[key]
    lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    p_lo = calculate_win_probability(lo, hi, mt)
    if prob_cache is not None:
        prob_cache[(lo, hi) + tag] = p_lo
        prob_cache[(hi, lo) + tag] = 1.0 - p_lo
    return p_lo if a_id == lo else 1.0 - p_lo


def _play_match_deterministic(
    team_states: Dict[int, TeamState],
    a_id: int,
    b_id: int,
    winner_id: int,
    remaining_rounds_after: int,
    prob_cache: Dict[tuple[int, int], float] | None = None,
    player_rows_by_id: Dict[int, dict] | None = None,
    team_rank_by_id: Dict[int, int] | None = None,
    match_type: str = "bo3",
) -> tuple[int, int, float, float]:
    A = team_states[a_id]
    B = team_states[b_id]
    match_num_a = A.matches_played + 1
    match_num_b = B.matches_played + 1
    prob_a = cached_win_prob(prob_cache, a_id, b_id, match_type)

    if winner_id == a_id:
        winner, loser = A, B
        did_win_a, did_win_b = True, False
        branch_prob = prob_a
    elif winner_id == b_id:
        winner, loser = B, A
        did_win_a, did_win_b = False, True
        branch_prob = 1.0 - prob_a
    else:
        raise ValueError(f"winner_id {winner_id} is not in match ({a_id}, {b_id})")

    winner.record_win(loser.team_id)
    loser.record_loss(winner.team_id)

    apply_fantasy_points_for_team(
        A, B.team_id, prob_a, did_win_a, match_num_a, match_type,
        player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id
    )
    apply_fantasy_points_for_team(
        B, A.team_id, 1.0 - prob_a, did_win_b, match_num_b, match_type,
        player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id
    )
    _apply_elimination_penalty(loser, remaining_rounds_after)

    return winner.team_id, loser.team_id, prob_a, branch_prob


def _exact_weighted_player_totals(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    has_third_place_decider: bool = False,
    progress_callback=None,
    quarters_override: List[tuple] | None = None,
    sf_pairs_resolver=None,
    final_bo: int = 3,
) -> tuple[Dict[int, Dict], Dict, int, List[Dict]]:
    base_states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    accum: Dict[int, Dict[int, Dict[str, float]]] = {tid: {} for tid in team_slots}
    total_prob = 0.0
    processed = 0
    best_prob = -1.0
    best_bracket = {"quarters": [], "semis": [], "final": []}
    outcomes: List[Dict] = []

    quarters = [tuple(pair) for pair in quarters_override] if quarters_override else [
        (team_slots[0], team_slots[1]),
        (team_slots[2], team_slots[3]),
        (team_slots[4], team_slots[5]),
        (team_slots[6], team_slots[7]),
    ]
    total_outcomes = 256 if has_third_place_decider else 128
    prob_cache: Dict[tuple[int, int], float] = {}

    if progress_callback:
        progress_callback(0, total_outcomes)

    def capture_outcome(path_prob: float, states: Dict[int, TeamState], bracket: Dict) -> None:
        player_points: Dict[str, float] = {}
        player_components: Dict[str, Dict[str, float]] = {}
        player_breakdown: Dict[str, List[dict]] = {}
        for ts in states.values():
            for pid, p in ts.players.items():
                player_points[str(pid)] = float(p.total_points)
                player_components[str(pid)] = {
                    "total": float(p.total_points),
                    "total_without_booster": float(p.rating_points_total + p.win_points_total + p.role_points_total),
                    "rating": float(p.rating_points_total),
                    "win": float(p.win_points_total),
                    "role": float(p.role_points_total),
                    "booster": float(p.booster_points_total),
                }
                player_breakdown[str(pid)] = [dict(row) for row in p.point_breakdown]
        outcomes.append(
            {
                "probability": float(path_prob),
                "bracket": bracket,
                "players": player_points,
                "player_components": player_components,
                "player_breakdown": player_breakdown,
            }
        )

    def recurse_qf(idx: int, states: Dict[int, TeamState], prob: float, qf_winners: List[int], qf_matches: List[dict]):
        nonlocal total_prob, processed, best_prob, best_bracket
        if idx == 4:
            if sf_pairs_resolver:
                (sf1_a, sf1_b), (sf2_a, sf2_b) = sf_pairs_resolver(qf_winners)
            else:
                sf1_a, sf1_b = qf_winners[0], qf_winners[1]
                sf2_a, sf2_b = qf_winners[2], qf_winners[3]
            # Semifinal 1
            for semi1_winner in (sf1_a, sf1_b):
                s1_states = _clone_team_states(states)
                s1_winner, s1_loser, s1_p_win_a, s1_branch_p = _play_match_deterministic(
                    s1_states, sf1_a, sf1_b, semi1_winner, remaining_rounds_after=1, prob_cache=prob_cache,
                    player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id
                )
                semi1_match = {
                    "winner": s1_winner,
                    "loser": s1_loser,
                    "p_win_a": s1_p_win_a,
                    "teams": [sf1_a, sf1_b],
                }

                # Semifinal 2
                for semi2_winner in (sf2_a, sf2_b):
                    s2_states = _clone_team_states(s1_states)
                    s2_winner, s2_loser, s2_p_win_a, s2_branch_p = _play_match_deterministic(
                        s2_states, sf2_a, sf2_b, semi2_winner, remaining_rounds_after=1, prob_cache=prob_cache,
                        player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id
                    )
                    semi2_match = {
                        "winner": s2_winner,
                        "loser": s2_loser,
                        "p_win_a": s2_p_win_a,
                        "teams": [sf2_a, sf2_b],
                    }

                    # Final (and optional third-place decider)
                    for final_winner in (s1_winner, s2_winner):
                        f_states = _clone_team_states(s2_states)
                        f_winner, f_loser, f_p_win_a, f_branch_p = _play_match_deterministic(
                            f_states, s1_winner, s2_winner, final_winner, remaining_rounds_after=0, prob_cache=prob_cache,
                            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id, match_type=_bo_type(final_bo),
                        )

                        if has_third_place_decider:
                            for third_winner in (s1_loser, s2_loser):
                                t_states = _clone_team_states(f_states)
                                t_winner, t_loser, t_p_win_a, t_branch_p = _play_match_deterministic(
                                    t_states, s1_loser, s2_loser, third_winner, remaining_rounds_after=0, prob_cache=prob_cache,
                                    player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id
                                )
                                path_prob = prob * s1_branch_p * s2_branch_p * f_branch_p * t_branch_p
                                total_prob += path_prob
                                processed += 1

                                if path_prob > best_prob:
                                    best_prob = path_prob
                                    best_bracket = {
                                        "quarters": list(qf_matches),
                                        "semis": [semi1_match, semi2_match],
                                        "final": [
                                            {
                                                "winner": f_winner,
                                                "loser": f_loser,
                                                "p_win_a": f_p_win_a,
                                                "teams": [s1_winner, s2_winner],
                                            }
                                        ],
                                        "third_place": [
                                            {
                                                "winner": t_winner,
                                                "loser": t_loser,
                                                "p_win_a": t_p_win_a,
                                                "teams": [s1_loser, s2_loser],
                                            }
                                        ],
                                    }
                                outcome_bracket = {
                                    "quarters": list(qf_matches),
                                    "semis": [semi1_match, semi2_match],
                                    "final": [
                                        {
                                            "winner": f_winner,
                                            "loser": f_loser,
                                            "p_win_a": f_p_win_a,
                                            "teams": [s1_winner, s2_winner],
                                        }
                                    ],
                                    "third_place": [
                                        {
                                            "winner": t_winner,
                                            "loser": t_loser,
                                            "p_win_a": t_p_win_a,
                                            "teams": [s1_loser, s2_loser],
                                        }
                                    ],
                                }
                                capture_outcome(path_prob, t_states, outcome_bracket)

                                for tid, ts in t_states.items():
                                    for pid, p in ts.players.items():
                                        bucket = accum[tid].setdefault(
                                            pid,
                                            {"total": 0.0, "rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0},
                                        )
                                        bucket["total"] += path_prob * p.total_points
                                        bucket["rating"] += path_prob * p.rating_points_total
                                        bucket["win"] += path_prob * p.win_points_total
                                        bucket["role"] += path_prob * p.role_points_total
                                        bucket["booster"] += path_prob * p.booster_points_total

                                if progress_callback:
                                    progress_callback(processed, total_outcomes)
                        else:
                            path_prob = prob * s1_branch_p * s2_branch_p * f_branch_p
                            total_prob += path_prob
                            processed += 1

                            if path_prob > best_prob:
                                best_prob = path_prob
                                best_bracket = {
                                    "quarters": list(qf_matches),
                                    "semis": [semi1_match, semi2_match],
                                    "final": [
                                        {
                                            "winner": f_winner,
                                            "loser": f_loser,
                                            "p_win_a": f_p_win_a,
                                            "teams": [s1_winner, s2_winner],
                                        }
                                    ],
                                }
                            outcome_bracket = {
                                "quarters": list(qf_matches),
                                "semis": [semi1_match, semi2_match],
                                "final": [
                                    {
                                        "winner": f_winner,
                                        "loser": f_loser,
                                        "p_win_a": f_p_win_a,
                                        "teams": [s1_winner, s2_winner],
                                    }
                                ],
                            }
                            capture_outcome(path_prob, f_states, outcome_bracket)

                            for tid, ts in f_states.items():
                                for pid, p in ts.players.items():
                                    bucket = accum[tid].setdefault(
                                        pid,
                                        {"total": 0.0, "rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0},
                                    )
                                    bucket["total"] += path_prob * p.total_points
                                    bucket["rating"] += path_prob * p.rating_points_total
                                    bucket["win"] += path_prob * p.win_points_total
                                    bucket["role"] += path_prob * p.role_points_total
                                    bucket["booster"] += path_prob * p.booster_points_total

                            if progress_callback:
                                progress_callback(processed, total_outcomes)
            return

        a_id, b_id = quarters[idx]
        for winner_id in (a_id, b_id):
            branch_states = _clone_team_states(states)
            winner, loser, p_win_a, branch_prob = _play_match_deterministic(
                branch_states, a_id, b_id, winner_id, remaining_rounds_after=2, prob_cache=prob_cache,
                player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id
            )
            recurse_qf(
                idx + 1,
                branch_states,
                prob * branch_prob,
                qf_winners + [winner],
                qf_matches + [{"winner": winner, "loser": loser, "p_win_a": p_win_a, "teams": [a_id, b_id]}],
            )

    recurse_qf(0, base_states, 1.0, [], [])

    denom = total_prob if total_prob > 0 else 1.0
    results: Dict[int, Dict] = {}
    for tid in team_slots:
        players_out: Dict[int, Dict[str, float]] = {}
        state_players = base_states[tid].players
        for pid, sums in accum[tid].items():
            ps = state_players.get(pid)
            players_out[pid] = {
                "total_points": sums["total"] / denom,
                "rating_points_total": sums["rating"] / denom,
                "win_points_total": sums["win"] / denom,
                "role_points_total": sums["role"] / denom,
                "booster_points_total": sums["booster"] / denom,
                "total_points_without_booster": (sums["rating"] + sums["win"] + sums["role"]) / denom,
                "role_id": ps.role_id if ps else None,
                "booster_slots": _booster_slots_for(ps),
                "role_info": _role_info_for(ps),
            }
        results[tid] = {
            "team_id": tid,
            "wins": 0,
            "losses": 0,
            "players": players_out,
        }

    return results, best_bracket, total_outcomes, outcomes


def _exact_bracket6_player_totals(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    progress_callback=None,
    final_bo: int = 3,
) -> tuple[Dict[int, Dict], Dict, int, List[Dict]]:
    """Exact enumeration of the 6-team byes bracket (Porto/Cologne playoffs).

    Slot convention: [SEMI BYE 1, QF1a, QF1b, QF2a, QF2b, SEMI BYE 2] — the
    byes are the group winners, entering directly at the semi-finals (bye 1
    meets the QF1 winner, bye 2 the QF2 winner). 32 outcomes."""
    bye1, q1a, q1b, q2a, q2b, bye2 = (int(t) for t in team_slots)
    base_states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    accum: Dict[int, Dict[int, Dict[str, float]]] = {tid: {} for tid in team_slots}
    prob_cache: Dict[tuple[int, int], float] = {}
    outcomes: List[Dict] = []
    best_prob = -1.0
    best_bracket: Dict = {"quarters": [], "semis": [], "final": []}
    total_outcomes = 32
    processed = 0
    total_prob = 0.0

    def play(states, a, b, winner, remaining_after):
        return _play_match_deterministic(
            states, a, b, winner, remaining_rounds_after=remaining_after, prob_cache=prob_cache,
            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id,
            match_type=_bo_type(final_bo) if remaining_after == 0 else "bo3",
        )

    def mk(w, l, p, teams):
        return {"winner": w, "loser": l, "p_win_a": p, "teams": list(teams)}

    for w1 in (q1a, q1b):
        s1 = _clone_team_states(base_states)
        q1w, q1l, q1p, q1bp = play(s1, q1a, q1b, w1, 2)
        m_q1 = mk(q1w, q1l, q1p, [q1a, q1b])
        for w2 in (q2a, q2b):
            s2 = _clone_team_states(s1)
            q2w, q2l, q2p, q2bp = play(s2, q2a, q2b, w2, 2)
            m_q2 = mk(q2w, q2l, q2p, [q2a, q2b])
            for w3 in (bye1, q1w):
                s3 = _clone_team_states(s2)
                sf1w, sf1l, sf1p, sf1bp = play(s3, bye1, q1w, w3, 1)
                m_s1 = mk(sf1w, sf1l, sf1p, [bye1, q1w])
                for w4 in (bye2, q2w):
                    s4 = _clone_team_states(s3)
                    sf2w, sf2l, sf2p, sf2bp = play(s4, bye2, q2w, w4, 1)
                    m_s2 = mk(sf2w, sf2l, sf2p, [bye2, q2w])
                    for w5 in (sf1w, sf2w):
                        s5 = _clone_team_states(s4)
                        fw, fl, fp, fbp = play(s5, sf1w, sf2w, w5, 0)
                        m_f = mk(fw, fl, fp, [sf1w, sf2w])
                        # Bye teams get 6 padding points per player for the
                        # quarter-final they skip.
                        for bye_tid in (bye1, bye2):
                            bye_ts = s5.get(bye_tid)
                            if not bye_ts:
                                continue
                            for p in bye_ts.players.values():
                                p.win_points_total += 6.0
                                p.total_points += 6.0
                        path_prob = q1bp * q2bp * sf1bp * sf2bp * fbp
                        total_prob += path_prob
                        processed += 1
                        bracket = {"quarters": [m_q1, m_q2], "semis": [m_s1, m_s2], "final": [m_f]}
                        if path_prob > best_prob:
                            best_prob = path_prob
                            best_bracket = bracket
                        player_points: Dict[str, float] = {}
                        player_components: Dict[str, Dict[str, float]] = {}
                        player_breakdown: Dict[str, List[dict]] = {}
                        for ts in s5.values():
                            for pid, p in ts.players.items():
                                player_points[str(pid)] = float(p.total_points)
                                player_components[str(pid)] = {
                                    "total": float(p.total_points),
                                    "total_without_booster": float(
                                        p.rating_points_total + p.win_points_total + p.role_points_total
                                    ),
                                    "rating": float(p.rating_points_total),
                                    "win": float(p.win_points_total),
                                    "role": float(p.role_points_total),
                                    "booster": float(p.booster_points_total),
                                }
                                player_breakdown[str(pid)] = [dict(row) for row in p.point_breakdown]
                        outcomes.append(
                            {
                                "probability": float(path_prob),
                                "bracket": bracket,
                                "players": player_points,
                                "player_components": player_components,
                                "player_breakdown": player_breakdown,
                            }
                        )
                        for tid, ts in s5.items():
                            for pid, p in ts.players.items():
                                bucket = accum[tid].setdefault(
                                    pid,
                                    {"total": 0.0, "rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0},
                                )
                                bucket["total"] += path_prob * p.total_points
                                bucket["rating"] += path_prob * p.rating_points_total
                                bucket["win"] += path_prob * p.win_points_total
                                bucket["role"] += path_prob * p.role_points_total
                                bucket["booster"] += path_prob * p.booster_points_total
                        if progress_callback:
                            progress_callback(processed, total_outcomes)

    denom = total_prob if total_prob > 0 else 1.0
    results: Dict[int, Dict] = {}
    for tid in team_slots:
        players_out: Dict[int, Dict[str, float]] = {}
        state_players = base_states[tid].players
        for pid, sums in accum[tid].items():
            ps = state_players.get(pid)
            players_out[pid] = {
                "total_points": sums["total"] / denom,
                "rating_points_total": sums["rating"] / denom,
                "win_points_total": sums["win"] / denom,
                "role_points_total": sums["role"] / denom,
                "booster_points_total": sums["booster"] / denom,
                "total_points_without_booster": (sums["rating"] + sums["win"] + sums["role"]) / denom,
                "role_id": ps.role_id if ps else None,
                "booster_slots": _booster_slots_for(ps),
                "role_info": _role_info_for(ps),
            }
        results[tid] = {"team_id": tid, "wins": 0, "losses": 0, "players": players_out}

    return results, best_bracket, total_outcomes, outcomes


# --- General N-team single-elimination bracket (Monte-Carlo for large sizes) ---

_ROUND_NAME_BY_TEAMS = {2: "final", 4: "semis", 8: "quarters", 16: "round_of_16", 32: "round_of_32"}
# Fields with more than this many exact outcomes (2^(teams-1)) are sampled.
# 8 teams = 128 outcomes (exact); 16 teams = 32768 (Monte-Carlo).
_BRACKET_EXACT_OUTCOME_LIMIT = 1024
_BRACKET_MC_SIMS = 5000
# Bounds for a user-supplied Monte-Carlo sample count (large fields only).
_BRACKET_MC_SIMS_MIN = 500
_BRACKET_MC_SIMS_MAX = 200000
# Valid single-elim field sizes the UI/back-end accept.
_ALLOWED_BRACKET_SIZES = (6, 8, 16)  # 2- and 4-team brackets are not offered


def _clamp_mc_sims(value) -> int | None:
    """Clamp a requested Monte-Carlo sample count to a sane range, or None to use
    the default. More sims = smoother EVs but longer runs (~5000 ≈ 18s)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return max(_BRACKET_MC_SIMS_MIN, min(_BRACKET_MC_SIMS_MAX, n))


def _round_name_for(teams_in_round: int) -> str:
    return _ROUND_NAME_BY_TEAMS.get(int(teams_in_round), f"round_of_{int(teams_in_round)}")


def _reset_states_inplace(states: Dict[int, TeamState]) -> None:
    """Zero the accumulated points and records so the same state objects can be
    reused across Monte-Carlo sims (far cheaper than deep-cloning per sim)."""
    for ts in states.values():
        ts.wins = 0
        ts.losses = 0
        ts.opponents_played = set()
        for p in ts.players.values():
            p.total_points = 0.0
            p.rating_points_total = 0.0
            p.win_points_total = 0.0
            p.role_points_total = 0.0
            p.booster_points_total = 0.0
            p.point_breakdown = []


def _simulate_bracket_n(
    team_slots: List[int],
    team_states: Dict[int, TeamState],
    player_rows_by_id: Dict[int, dict],
    team_rank_by_id: Dict[int, int],
    has_third_place_decider: bool,
    rng,
    final_bo: int = 3,
) -> tuple[Dict[int, TeamState], Dict[str, List[dict]]]:
    """One random single-elim bracket for any power-of-two field. Round names
    generalise by size (round_of_16 → quarters → semis → final). The two
    semi-final losers play the optional third-place decider. The caller resets
    and reuses the same state objects across sims (a DB reload or deep clone per
    sim is far too slow)."""
    n = len(team_slots)
    total_rounds = int(round(math.log2(n)))
    match_results: Dict[str, List[dict]] = {}
    rand_fn = (rng or random).random

    def play(a_id: int, b_id: int, remaining_after: int, match_type: str = "bo3"):
        A, B = team_states[a_id], team_states[b_id]
        mna, mnb = A.matches_played + 1, B.matches_played + 1
        result = simulate_match_outcome(A, B, match_type, rng=rand_fn)
        if result.winner_id == A.team_id:
            winner, loser, dwa, dwb = A, B, True, False
        else:
            winner, loser, dwa, dwb = B, A, False, True
        winner.record_win(loser.team_id)
        loser.record_loss(winner.team_id)
        apply_fantasy_points_for_team(A, B.team_id, result.win_probability, dwa, mna, match_type, player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id)
        apply_fantasy_points_for_team(B, A.team_id, 1.0 - result.win_probability, dwb, mnb, match_type, player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id)
        _apply_elimination_penalty(loser, remaining_after)
        return winner.team_id, loser.team_id, result.win_probability

    current = list(team_slots)
    round_idx = 0
    semi_losers: List[int] = []
    while len(current) > 1:
        teams_in_round = len(current)
        rname = _round_name_for(teams_in_round)
        remaining_after = total_rounds - round_idx - 1
        winners, matches = [], []
        for i in range(0, len(current), 2):
            a, b = current[i], current[i + 1]
            w, l, p = play(a, b, remaining_after, _bo_type(final_bo) if remaining_after == 0 else "bo3")
            winners.append(w)
            matches.append({"winner": w, "loser": l, "p_win_a": p, "teams": [a, b]})
            if teams_in_round == 4:
                semi_losers.append(l)
        match_results[rname] = matches
        current = winners
        round_idx += 1

    if has_third_place_decider and len(semi_losers) == 2:
        w, l, p = play(semi_losers[0], semi_losers[1], 0)
        match_results["third_place"] = [{"winner": w, "loser": l, "p_win_a": p, "teams": list(semi_losers)}]
    return team_states, match_results


# Below this many sims the process-pool startup costs more than it saves.
_MC_PARALLEL_MIN_SIMS = 2000


def _mc_chunk_worker(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    has_third_place_decider: bool,
    n_sims: int,
    seed: int,
    store_outcomes: bool,
    total_sims: int,
    final_bo: int = 3,
) -> tuple[Dict[int, Dict[int, Dict[str, float]]], List[Dict]]:
    """One Monte-Carlo chunk, run in a worker process: n_sims random brackets,
    returning the raw accumulator sums plus (optionally) the sampled outcomes.
    Spawn-safe — builds its own states and DB lookup context."""
    rng = random.Random(seed)
    states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    accum: Dict[int, Dict[int, Dict[str, float]]] = {tid: {} for tid in team_slots}
    outcomes: List[Dict] = []
    for _ in range(n_sims):
        _reset_states_inplace(states)
        _, mr = _simulate_bracket_n(
            team_slots, states, player_rows_by_id, team_rank_by_id, has_third_place_decider, rng=rng, final_bo=final_bo
        )
        player_points: Dict[str, float] = {}
        player_components: Dict[str, Dict[str, float]] = {}
        for ts in states.values():
            for pid, p in ts.players.items():
                b = accum[ts.team_id].setdefault(pid, {"total": 0.0, "rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0})
                b["total"] += p.total_points
                b["rating"] += p.rating_points_total
                b["win"] += p.win_points_total
                b["role"] += p.role_points_total
                b["booster"] += p.booster_points_total
                if store_outcomes:
                    player_points[str(pid)] = float(p.total_points)
                    player_components[str(pid)] = {
                        "total": float(p.total_points),
                        "total_without_booster": float(p.rating_points_total + p.win_points_total + p.role_points_total),
                        "rating": float(p.rating_points_total),
                        "win": float(p.win_points_total),
                        "role": float(p.role_points_total),
                        "booster": float(p.booster_points_total),
                    }
        if store_outcomes:
            outcomes.append({
                "probability": 1.0 / total_sims,
                "bracket": mr,
                "players": player_points,
                "player_components": player_components,
                "player_breakdown": {},
            })
    return accum, outcomes


def _monte_carlo_parallel(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    has_third_place_decider: bool,
    n_sims: int,
    store_outcomes: bool,
    progress_callback,
    workers: int,
    final_bo: int = 3,
) -> tuple[Dict[int, Dict], Dict, int, List[Dict]]:
    """Fan the Monte-Carlo sims out over a process pool (Python threads can't
    speed up this CPU-bound loop) and merge the chunk accumulators."""
    base = n_sims // workers
    sizes = [base + (1 if i < n_sims % workers else 0) for i in range(workers)]
    sizes = [s for s in sizes if s > 0]
    seed_root = random.randrange(2**31)
    accum: Dict[int, Dict[int, Dict[str, float]]] = {tid: {} for tid in team_slots}
    outcomes: List[Dict] = []
    if progress_callback:
        progress_callback(0, n_sims)
    done = 0
    with ProcessPoolExecutor(max_workers=len(sizes)) as ex:
        fut_size = {
            ex.submit(
                _mc_chunk_worker,
                team_slots, vrs_ranks, has_third_place_decider,
                size, seed_root + i, store_outcomes, n_sims, final_bo,
            ): size
            for i, size in enumerate(sizes)
        }
        for fut in as_completed(fut_size):
            chunk_accum, chunk_outcomes = fut.result()
            for tid, players in chunk_accum.items():
                dst = accum.setdefault(tid, {})
                for pid, sums in players.items():
                    b = dst.setdefault(pid, {"total": 0.0, "rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0})
                    for k in b:
                        b[k] += sums[k]
            outcomes.extend(chunk_outcomes)
            done += fut_size[fut]
            if progress_callback:
                progress_callback(done, n_sims)

    denom = float(n_sims) if n_sims > 0 else 1.0
    base_states = initialize_teams(team_slots, vrs_ranks)
    results: Dict[int, Dict] = {}
    for tid in team_slots:
        players_out: Dict[int, Dict[str, float]] = {}
        state_players = base_states[tid].players
        for pid, sums in accum[tid].items():
            ps = state_players.get(pid)
            players_out[pid] = {
                "total_points": sums["total"] / denom,
                "rating_points_total": sums["rating"] / denom,
                "win_points_total": sums["win"] / denom,
                "role_points_total": sums["role"] / denom,
                "booster_points_total": sums["booster"] / denom,
                "total_points_without_booster": (sums["rating"] + sums["win"] + sums["role"]) / denom,
                "role_id": ps.role_id if ps else None,
                "booster_slots": _booster_slots_for(ps),
                "role_info": _role_info_for(ps),
            }
        results[tid] = {"team_id": tid, "wins": 0, "losses": 0, "players": players_out}

    best_bracket = outcomes[0]["bracket"] if outcomes else {}
    return results, best_bracket, n_sims, outcomes


def _monte_carlo_bracket_totals(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    has_third_place_decider: bool = False,
    n_sims: int = _BRACKET_MC_SIMS,
    store_outcomes: bool = True,
    progress_callback=None,
    final_bo: int = 3,
) -> tuple[Dict[int, Dict], Dict, int, List[Dict]]:
    """Monte-Carlo player EVs for a large bracket, matching the exact function's
    return contract. Each sampled bracket is one 'outcome' with probability
    1/n_sims (used by the ceiling / most-likely modes); average EV is the sample
    mean. Per-match breakdowns are dropped to keep the sample set light. Large
    sample counts fan out over a process pool."""
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    if n_sims >= _MC_PARALLEL_MIN_SIMS and workers > 1:
        return _monte_carlo_parallel(
            team_slots, vrs_ranks, has_third_place_decider, n_sims, store_outcomes, progress_callback, workers, final_bo=final_bo
        )
    accum: Dict[int, Dict[int, Dict[str, float]]] = {tid: {} for tid in team_slots}
    outcomes: List[Dict] = []
    # Build player states + lookup context ONCE; reset & reuse them per sim.
    states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    if progress_callback:
        progress_callback(0, n_sims)
    for s in range(n_sims):
        _reset_states_inplace(states)
        _, mr = _simulate_bracket_n(
            team_slots, states, player_rows_by_id, team_rank_by_id, has_third_place_decider, rng=random, final_bo=final_bo
        )
        player_points: Dict[str, float] = {}
        player_components: Dict[str, Dict[str, float]] = {}
        for ts in states.values():
            for pid, p in ts.players.items():
                b = accum[ts.team_id].setdefault(pid, {"total": 0.0, "rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0})
                b["total"] += p.total_points
                b["rating"] += p.rating_points_total
                b["win"] += p.win_points_total
                b["role"] += p.role_points_total
                b["booster"] += p.booster_points_total
                if store_outcomes:
                    player_points[str(pid)] = float(p.total_points)
                    player_components[str(pid)] = {
                        "total": float(p.total_points),
                        "total_without_booster": float(p.rating_points_total + p.win_points_total + p.role_points_total),
                        "rating": float(p.rating_points_total),
                        "win": float(p.win_points_total),
                        "role": float(p.role_points_total),
                        "booster": float(p.booster_points_total),
                    }
        if store_outcomes:
            outcomes.append({
                "probability": 1.0 / n_sims,
                "bracket": mr,
                "players": player_points,
                "player_components": player_components,
                "player_breakdown": {},
            })
        if progress_callback and (s + 1) % 500 == 0:
            progress_callback(s + 1, n_sims)

    denom = float(n_sims) if n_sims > 0 else 1.0
    results: Dict[int, Dict] = {}
    for tid in team_slots:
        players_out: Dict[int, Dict[str, float]] = {}
        state_players = states[tid].players
        for pid, sums in accum[tid].items():
            ps = state_players.get(pid)
            players_out[pid] = {
                "total_points": sums["total"] / denom,
                "rating_points_total": sums["rating"] / denom,
                "win_points_total": sums["win"] / denom,
                "role_points_total": sums["role"] / denom,
                "booster_points_total": sums["booster"] / denom,
                "total_points_without_booster": (sums["rating"] + sums["win"] + sums["role"]) / denom,
                "role_id": ps.role_id if ps else None,
                "booster_slots": _booster_slots_for(ps),
                "role_info": _role_info_for(ps),
            }
        results[tid] = {"team_id": tid, "wins": 0, "losses": 0, "players": players_out}

    best_bracket = outcomes[0]["bracket"] if outcomes else {}
    return results, best_bracket, n_sims, outcomes


def _fresh_bracket_state(base_states: Dict[int, TeamState], tid: int, prior_matches: int) -> TeamState:
    """A team state as it stands when it plays its (prior_matches + 1)-th
    match: zero points, every earlier match a win (alive in single elimination).
    That is all the scorer reads — the match number picks the booster slot."""
    ts = _clone_team_states({tid: base_states[tid]})[tid]
    ts.wins = int(prior_matches)
    ts.losses = 0
    return ts


_EXACT_N_MAX_TEAMS = 16  # 2^15 = 32,768 outcomes enumerated exactly; larger fields stay Monte-Carlo


def _exact_bracket_n_player_totals(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    has_third_place_decider: bool = False,
    progress_callback=None,
    final_bo: int = 3,
) -> tuple[Dict[int, Dict], Dict, int, List[Dict], Dict]:
    """Exact enumeration of a full power-of-two single-elimination bracket of
    any size (16 teams = 15 matches = 32,768 outcomes). A match's points depend
    only on (teams, winner, each side's match number, rounds remaining), so
    every distinct pairing is scored once with the deterministic scorer and
    memoised; the enumeration then just walks the result tree adding and
    subtracting those per-player rows. Every outcome is a bit code — one bit
    per match in play order, 0 = the first-listed team wins — and its
    per-player totals go into a float32 matrix (players × outcomes) that the
    roster optimiser reads directly. Returns (results, best_bracket, count,
    outcomes as light {probability, code} records, extra with the matrix and
    the bracket template)."""
    import numpy as np

    n = len(team_slots)
    rounds_total = int(round(math.log2(n)))
    if n < 2 or 2 ** rounds_total != n:
        raise ValueError(f"exact bracket enumeration needs a power-of-two field, got {n} teams")
    base_states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    prob_cache: Dict[tuple[int, int], float] = {}
    pids: List[int] = []
    pid_index: Dict[int, int] = {}
    team_pids: Dict[int, List[int]] = {}
    for tid in team_slots:
        team_pids[tid] = []
        for pid in base_states[tid].players:
            pid_index[int(pid)] = len(pids)
            pids.append(int(pid))
            team_pids[tid].append(int(pid))
    n_matches = (n - 1) + (1 if has_third_place_decider else 0)
    n_out = 2 ** n_matches
    n_players = len(pids)
    totals = np.zeros((n_players, n_out), dtype=np.float32)
    rw_mat = np.zeros((n_players, n_out), dtype=np.float32)  # rating + win (penalties included) per outcome
    rt_mat = np.zeros((n_players, n_out), dtype=np.float32)  # rating alone per outcome
    mp_mat = np.zeros((n_players, n_out), dtype=np.uint8)  # matches played per outcome
    comps_ev = np.zeros((n_players, 5), dtype=np.float64)  # Σ prob × [total, rating, win, role, booster]
    probs = np.zeros(n_out, dtype=np.float64)
    run = [[0.0] * n_players for _ in range(5)]  # running per-player components along the current path
    run_mp = [0] * n_players
    memo: Dict[tuple, tuple] = {}

    def pairing(a: int, b: int, winner: int, num_a: int, num_b: int, rem: int, match_type: str = "bo3") -> tuple:
        key = (a, b, winner, num_a, num_b, rem, match_type)
        hit = memo.get(key)
        if hit is not None:
            return hit
        states = {
            a: _fresh_bracket_state(base_states, a, num_a - 1),
            b: _fresh_bracket_state(base_states, b, num_b - 1),
        }
        _w, _l, p_a, _branch = _play_match_deterministic(
            states, a, b, winner, remaining_rounds_after=rem, prob_cache=prob_cache,
            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id, match_type=match_type,
        )
        rows = []
        for ts in states.values():
            for pid, p in ts.players.items():
                rows.append((
                    pid_index[int(pid)], float(p.total_points), float(p.rating_points_total),
                    float(p.win_points_total), float(p.role_points_total), float(p.booster_points_total),
                ))
        memo[key] = (rows, float(p_a))
        return memo[key]

    def apply_rows(rows, sign: float) -> None:
        r0, r1, r2, r3, r4 = run
        step = 1 if sign > 0 else -1
        for j, t, ra, wi, ro, bo in rows:
            r0[j] += sign * t
            r1[j] += sign * ra
            r2[j] += sign * wi
            r3[j] += sign * ro
            r4[j] += sign * bo
            run_mp[j] += step

    leaf_count = [0]

    def leaf(prob: float, code: int) -> None:
        idx = leaf_count[0]
        leaf_count[0] += 1
        probs[idx] = prob
        totals[:, idx] = run[0]
        rw_mat[:, idx] = np.asarray(run[1], dtype=np.float64) + np.asarray(run[2], dtype=np.float64)
        rt_mat[:, idx] = np.asarray(run[1], dtype=np.float64)
        mp_mat[:, idx] = run_mp
        for c in range(5):
            comps_ev[:, c] += prob * np.asarray(run[c], dtype=np.float64)
        if progress_callback and idx % 4096 == 0:
            progress_callback(idx, n_out)

    def play_rounds(r: int, alive: List[int], prob: float, code: int, semi_losers: List[int]) -> None:
        if len(alive) == 1:
            if has_third_place_decider and len(semi_losers) == 2:
                a, b = semi_losers
                num = rounds_total  # semi losers have played rounds_total - 1 matches
                for bit, w in enumerate((a, b)):
                    rows, p_a = pairing(a, b, w, num, num, 0)
                    branch = p_a if w == a else 1.0 - p_a
                    apply_rows(rows, 1.0)
                    leaf(prob * branch, (code << 1) | bit)
                    apply_rows(rows, -1.0)
            else:
                leaf(prob, code)
            return
        play_round(r, alive, 0, [], [], prob, code, semi_losers)

    def play_round(r: int, alive: List[int], i: int, winners: List[int], losers: List[int], prob: float, code: int, semi_losers: List[int]) -> None:
        if i == len(alive) // 2:
            play_rounds(r + 1, winners, prob, code, list(losers) if len(alive) == 4 else semi_losers)
            return
        a, b = alive[2 * i], alive[2 * i + 1]
        rem = rounds_total - r - 1
        num = r + 1
        for bit, w in enumerate((a, b)):
            rows, p_a = pairing(a, b, w, num, num, rem, _bo_type(final_bo) if rem == 0 else "bo3")
            branch = p_a if w == a else 1.0 - p_a
            apply_rows(rows, 1.0)
            winners.append(w)
            losers.append(b if w == a else a)
            play_round(r, alive, i + 1, winners, losers, prob * branch, (code << 1) | bit, semi_losers)
            winners.pop()
            losers.pop()
            apply_rows(rows, -1.0)

    if progress_callback:
        progress_callback(0, n_out)
    play_rounds(0, list(team_slots), 1.0, 0, [])
    assert leaf_count[0] == n_out, (leaf_count[0], n_out)

    results: Dict[int, Dict] = {}
    for tid in team_slots:
        players_out: Dict[int, Dict[str, float]] = {}
        for pid in team_pids[tid]:
            e = comps_ev[pid_index[pid]]
            ps = base_states[tid].players.get(pid)
            players_out[pid] = {
                "total_points": float(e[0]),
                "rating_points_total": float(e[1]),
                "win_points_total": float(e[2]),
                "role_points_total": float(e[3]),
                "booster_points_total": float(e[4]),
                "total_points_without_booster": float(e[1] + e[2] + e[3]),
                "role_id": ps.role_id if ps else None,
                "booster_slots": _booster_slots_for(ps),
                "role_info": _role_info_for(ps),
            }
        results[tid] = {"team_id": tid, "wins": 0, "losses": 0, "players": players_out}
    outcomes = [{"probability": float(probs[i]), "code": int(i)} for i in range(n_out)]
    template = {
        "team_slots": [int(t) for t in team_slots], "third_place": bool(has_third_place_decider), "rounds": rounds_total,
        "final_bo": int(_bo_type(final_bo)[2:]),
    }
    best_bracket = _decode_bracket_code(int(np.argmax(probs)), template, prob_cache)
    extra = {
        "outcome_matrix": {
            "pids": pids,
            "n": int(n_out),
            "f32_b64": base64.b64encode(totals.tobytes()).decode("ascii"),
            "rw_b64": base64.b64encode(rw_mat.tobytes()).decode("ascii"),
            "mp_b64": base64.b64encode(mp_mat.tobytes()).decode("ascii"),
            "rt_b64": base64.b64encode(rt_mat.tobytes()).decode("ascii"),
        },
        "bracket_template": template,
    }
    if progress_callback:
        progress_callback(n_out, n_out)
    return results, best_bracket, n_out, outcomes, extra


def _decode_bracket_code(code: int, template: Dict, prob_cache: Dict | None = None) -> Dict[str, List[dict]]:
    """The bracket dict (round name → matches with winner / loser / p_win_a /
    teams) for one outcome code of an exactly enumerated bracket."""
    team_slots = [int(t) for t in template.get("team_slots") or []]
    n = len(team_slots)
    decider = bool(template.get("third_place"))
    n_matches = (n - 1) + (1 if decider else 0)
    prob_cache = prob_cache if prob_cache is not None else {}
    final_type = _bo_type(template.get("final_bo"))
    match_results: Dict[str, List[dict]] = {}
    alive = list(team_slots)
    m = 0
    semi_losers: List[int] = []
    while len(alive) > 1:
        rname = _round_name_for(len(alive))
        match_type = final_type if len(alive) == 2 else "bo3"
        winners, matches = [], []
        for i in range(0, len(alive), 2):
            a, b = alive[i], alive[i + 1]
            bit = (code >> (n_matches - 1 - m)) & 1
            m += 1
            w, l = (b, a) if bit else (a, b)
            winners.append(w)
            matches.append({"winner": w, "loser": l, "p_win_a": cached_win_prob(prob_cache, a, b, match_type), "teams": [a, b]})
            if len(alive) == 4:
                semi_losers.append(l)
        match_results[rname] = matches
        alive = winners
    if decider and len(semi_losers) == 2:
        a, b = semi_losers
        bit = (code >> (n_matches - 1 - m)) & 1
        w, l = (b, a) if bit else (a, b)
        match_results["third_place"] = [{"winner": w, "loser": l, "p_win_a": cached_win_prob(prob_cache, a, b), "teams": [a, b]}]
    return match_results


def _stage_stats_from_codes(outcomes: List[Dict], template: Dict, results: Dict[int, Dict]) -> Dict:
    """Team-level stage stats (chance to appear in each round, title chance)
    for exactly enumerated brackets stored as outcome codes. Player rows stay
    empty, as for Monte-Carlo fields, because these outcomes carry no
    per-match breakdowns."""
    team_slots = [int(t) for t in template.get("team_slots") or []]
    n = len(team_slots)
    decider = bool(template.get("third_place"))
    n_matches = (n - 1) + (1 if decider else 0)
    stage_names: List[str] = []
    k = n
    while k > 1:
        stage_names.append(_round_name_for(k))
        k //= 2
    stages = stage_names + (["third_place"] if decider else [])
    team_ids = [int(t) for t in results.keys()]
    reach = {tid: {st: 0.0 for st in stages} for tid in team_ids}
    champion = {tid: 0.0 for tid in team_ids}
    total_p = 0.0
    for o in outcomes:
        p = float(o.get("probability") or 0.0)
        code = int(o.get("code") or 0)
        if p <= 0:
            continue
        total_p += p
        alive = list(team_slots)
        m = 0
        semi_losers: List[int] = []
        si = 0
        while len(alive) > 1:
            st = stage_names[si]
            si += 1
            winners = []
            for i in range(0, len(alive), 2):
                a, b = alive[i], alive[i + 1]
                reach[a][st] += p
                reach[b][st] += p
                bit = (code >> (n_matches - 1 - m)) & 1
                m += 1
                w = b if bit else a
                winners.append(w)
                if len(alive) == 4:
                    semi_losers.append(a if bit else b)
            alive = winners
        champion[alive[0]] += p
        if decider and len(semi_losers) == 2:
            for t in semi_losers:
                reach[t]["third_place"] += p
    denom = total_p if total_p > 0 else 1.0
    return {
        "stages": stages,
        "teams": {str(tid): {"reach": {st: reach[tid][st] / denom for st in stages}, "champion": champion[tid] / denom} for tid in team_ids},
        "players": {},
    }


def _outcome_matrix(results: Dict) -> tuple:
    """(pids, M, probs, RW, MP, MR) from a stored run: M = per-player totals as
    scored (each player's own best role and boosters), RW = rating + win
    (penalties and the byes' pad included), MP = matches played, MR = matches
    the role is scored for (equal to MP in brackets — no role padding). From
    the compact matrix of an exact enumeration, or built from the per-outcome
    player dicts of the older list format (RW / MP / MR are None when that
    format carries no components, e.g. Monte-Carlo runs)."""
    import numpy as np

    results = results or {}
    outcomes = results.get("outcomes") or []
    om = results.get("outcome_matrix") or {}
    if om.get("f32_b64") and outcomes:
        pids = [int(p) for p in om.get("pids") or []]
        n = int(om.get("n") or len(outcomes))
        M = np.frombuffer(base64.b64decode(om["f32_b64"]), dtype=np.float32).reshape(len(pids), n).astype(np.float64)
        probs = np.asarray([float(o.get("probability") or 0.0) for o in outcomes], dtype=np.float64)
        RW = MP = MR = RT = None
        if om.get("rw_b64") and om.get("mp_b64"):
            if om.get("rt_b64"):
                RT = np.frombuffer(base64.b64decode(om["rt_b64"]), dtype=np.float32).reshape(len(pids), n).astype(np.float64)
            RW = np.frombuffer(base64.b64decode(om["rw_b64"]), dtype=np.float32).reshape(len(pids), n).astype(np.float64)
            MP = np.frombuffer(base64.b64decode(om["mp_b64"]), dtype=np.uint8).reshape(len(pids), n).copy()
            if om.get("mr_b64"):
                # role matches differ from matches played when a champion is padded
                MR = np.frombuffer(base64.b64decode(om["mr_b64"]), dtype=np.uint8).reshape(len(pids), n).astype(np.float64)
            else:
                MR = MP.astype(np.float64)
        return pids, M, probs, RW, MP, MR, RT
    if not outcomes:
        return [], None, None, None, None, None, None
    pid_set = sorted({int(pid) for o in outcomes for pid in (o.get("players") or {})})
    idx = {pid: i for i, pid in enumerate(pid_set)}
    n = len(outcomes)
    M = np.zeros((len(pid_set), n), dtype=np.float64)
    probs = np.zeros(n, dtype=np.float64)
    has_components = all(o.get("player_components") for o in outcomes)
    RW = np.zeros((len(pid_set), n), dtype=np.float64) if has_components else None
    RT = np.zeros((len(pid_set), n), dtype=np.float64) if has_components else None
    MP = np.zeros((len(pid_set), n), dtype=np.uint8) if has_components else None
    pid_team = {}
    for tid, tdata in (results.get("teams") or {}).items():
        for pid in (tdata.get("players") or {}):
            pid_team[int(pid)] = int(tid)
    for c, o in enumerate(outcomes):
        probs[c] = float(o.get("probability") or 0.0)
        for pid, score in (o.get("players") or {}).items():
            M[idx[int(pid)], c] = float(score)
        if has_components:
            for pid, comp in (o.get("player_components") or {}).items():
                RW[idx[int(pid)], c] = float(comp.get("rating") or 0.0) + float(comp.get("win") or 0.0)
                RT[idx[int(pid)], c] = float(comp.get("rating") or 0.0)
            played: Dict[int, int] = {}
            for matches in (o.get("bracket") or {}).values():
                for m in matches or []:
                    for t in m.get("teams") or []:
                        played[int(t)] = played.get(int(t), 0) + 1
            for pid in pid_set:
                MP[idx[pid], c] = played.get(pid_team.get(pid, -1), 0)
    MR = MP.astype(np.float64) if has_components else None
    return pid_set, M, probs, RW, MP, MR, RT


def _bracket_player_totals(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    has_third_place_decider: bool = False,
    store_outcomes: bool = True,
    progress_callback=None,
    quarters_override: List[tuple] | None = None,
    sf_pairs_resolver=None,
    mc_sims: int | None = None,
    final_bo: int = 3,
) -> tuple[Dict[int, Dict], Dict, int, List[Dict], Dict | None]:
    """Route a bracket to the right engine. 6 teams: the byes enumerator.
    8 teams and the Bounty variant: the enumerator that keeps per-match
    breakdowns (128 / 256 outcomes). 2, 4 and 16 teams: the compact exact
    enumerator (up to 32,768 outcomes, memoised pairings). Larger fields:
    Monte-Carlo (`mc_sims` samples). Returns a fifth element, extra, with the
    compact outcome matrix when the compact enumerator ran, else None."""
    n = len(team_slots)
    if n == 6:
        results, best, count, outcomes = _exact_bracket6_player_totals(
            team_slots, vrs_ranks, progress_callback=progress_callback, final_bo=final_bo
        )
        return results, best, count, outcomes, None
    if n == 8 and (quarters_override or sf_pairs_resolver or True):
        results, best, count, outcomes = _exact_weighted_player_totals(
            team_slots, vrs_ranks,
            has_third_place_decider=has_third_place_decider,
            progress_callback=progress_callback,
            quarters_override=quarters_override,
            sf_pairs_resolver=sf_pairs_resolver,
            final_bo=final_bo,
        )
        return results, best, count, outcomes, None
    if n <= _EXACT_N_MAX_TEAMS and n >= 2 and (n & (n - 1)) == 0:
        return _exact_bracket_n_player_totals(
            team_slots, vrs_ranks, has_third_place_decider=has_third_place_decider, progress_callback=progress_callback,
            final_bo=final_bo,
        )
    results, best, count, outcomes = _monte_carlo_bracket_totals(
        team_slots, vrs_ranks,
        has_third_place_decider=has_third_place_decider,
        n_sims=int(mc_sims) if mc_sims else _BRACKET_MC_SIMS,
        store_outcomes=store_outcomes,
        progress_callback=progress_callback,
        final_bo=final_bo,
    )
    return results, best, count, outcomes, None


def _bo_type(final_bo) -> str:
    """Series type for a best-of value (Bo3 unless the page says otherwise)."""
    try:
        v = int(final_bo or 3)
    except Exception:  # noqa: BLE001
        v = 3
    return f"bo{v}" if v in (1, 3, 5) else "bo3"


def _detected_final_bo(event_id) -> int:
    """The grand final's best-of from the event page: the double-elimination
    bracket's own value, else the formats table's "Grand final BoN" line, else
    the playoff default (3)."""
    try:
        from backend.data.event_db import get_event_detail
        from backend.routes import events as events_routes

        event = get_event_detail(int(event_id))
        detected = events_routes._detect_event_tournament_kind_cached(event) if event else {}
        rules = detected.get("playoff_bo_rules") or {}
        value = int(detected.get("grand_final_bo") or rules.get("grandFinal") or rules.get("default") or 3)
    except Exception:  # noqa: BLE001
        value = 3
    return value if value in (1, 3, 5) else 3


def _detected_third_place(event_id) -> bool:
    """Whether the event page lists a third-place decider (formats table or an
    auxiliary bracket)."""
    try:
        from backend.data.event_db import get_event_detail
        from backend.routes import events as events_routes

        event = get_event_detail(int(event_id))
        return bool(event and events_routes._detect_event_tournament_kind_cached(event).get("playoff_third_place"))
    except Exception:  # noqa: BLE001
        return False


def _bracket_kind(value) -> str:
    """'double' for a whole-event double-elimination bracket, else 'single'."""
    return "double" if str(value or "").strip().lower() in ("double", "double_elim", "de8", "de8_full") else "single"


def _normalize_playoff_payload(payload: dict) -> dict:
    slots: List[int] = payload.get("team_slots") or []
    if len(slots) not in _ALLOWED_BRACKET_SIZES:
        raise HTTPException(status_code=400, detail="team_slots must contain 6, 8, or 16 team IDs")
    bracket_kind = _bracket_kind(payload.get("bracket_kind"))
    if bracket_kind == "double" and len(slots) != 8:
        raise HTTPException(status_code=400, detail="The double-elimination bracket needs exactly 8 teams")
    # The decider and the final's best-of come from the event page unless the
    # caller pins them (the operator's checkbox does).
    event_id = payload.get("event_id") or _event_key()
    if "has_third_place_decider" in payload:
        has_third_place_decider = bool(payload.get("has_third_place_decider"))
    else:
        has_third_place_decider = _detected_third_place(event_id)
    if len(slots) == 6 or bracket_kind == "double":
        has_third_place_decider = False  # the byes bracket and double elimination have no decider
    raw_bo = payload.get("final_bo")
    final_bo = int(raw_bo) if str(raw_bo) in ("1", "3", "5") else _detected_final_bo(event_id)
    normalized = {
        "team_slots": [int(x) for x in slots],
        "has_third_place_decider": has_third_place_decider,
        "bracket_kind": bracket_kind,
        "final_bo": final_bo,
    }
    mc_sims = _clamp_mc_sims(payload.get("mc_sims"))
    if mc_sims:
        normalized["mc_sims"] = mc_sims
    if _variant(payload.get("variant")) == "bounty":
        qf_pairs_raw = payload.get("qf_pairs") or []
        qf_pairs = [[int(a), int(b)] for a, b in qf_pairs_raw] if len(qf_pairs_raw) == 4 else []
        used = [tid for pair in qf_pairs for tid in pair]
        if sorted(used) != sorted(normalized["team_slots"]):
            raise HTTPException(status_code=400, detail="qf_pairs must pair all 8 teams exactly once (finish the draft first)")
        normalized["variant"] = "bounty"
        normalized["qf_pairs"] = qf_pairs
        normalized["sf_picks"] = dict(payload.get("sf_picks") or {})
        normalized["has_third_place_decider"] = False
        normalized["bracket_kind"] = "single"
    return normalized


def _bounty_sf_pairs_resolver(team_slots: List[int], sf_picks: dict):
    """SF pairings for a set of QF winners in the Bounty re-draft format.

    `sf_picks` maps a scenario key (the 4 surviving team ids, sorted ascending,
    joined with '-') to explicit SF pairs [[a, b], [c, d]]. Scenarios without a
    stored pick fall back to the default: the highest-seeded bottom-half
    survivor drafts the weakest (lowest-seeded) top-half survivor.
    """
    seed_index = {int(tid): idx for idx, tid in enumerate(team_slots)}

    def resolve(qf_winners: List[int]):
        winners = [int(t) for t in qf_winners]
        key = "-".join(str(t) for t in sorted(winners))
        picked = sf_picks.get(key)
        if picked and len(picked) == 2:
            pairs = [[int(a), int(b)] for a, b in picked]
            if sorted(t for pair in pairs for t in pair) == sorted(winners):
                return [(pairs[0][0], pairs[0][1]), (pairs[1][0], pairs[1][1])]
        surv = sorted(winners, key=lambda t: seed_index.get(t, 99))
        return [(surv[2], surv[1]), (surv[3], surv[0])]

    return resolve


# --- 8-team double elimination played as the whole event (StarLadder StarSeries) ---
#
# (match key, stage, feeder A, feeder B) in play order; feeders are ("seed",
# slot index), ("win", match key) or ("lose", match key). Lower semis are
# crossed (each upper-semi loser meets the OTHER side's lower-round-1 winner)
# and the consolidation final is the upper-final loser v the lower-final
# winner — HLTV's DoubleElimination8 routing, verified by the autofill parser.
_DOUBLE8_MATCHES: List[tuple] = [
    ("ub_r1_1", "ub_r1", ("seed", 0), ("seed", 1)),
    ("ub_r1_2", "ub_r1", ("seed", 2), ("seed", 3)),
    ("ub_r1_3", "ub_r1", ("seed", 4), ("seed", 5)),
    ("ub_r1_4", "ub_r1", ("seed", 6), ("seed", 7)),
    ("ub_sf_1", "ub_sf", ("win", "ub_r1_1"), ("win", "ub_r1_2")),
    ("ub_sf_2", "ub_sf", ("win", "ub_r1_3"), ("win", "ub_r1_4")),
    ("lb_r1_1", "lb_r1", ("lose", "ub_r1_1"), ("lose", "ub_r1_2")),
    ("lb_r1_2", "lb_r1", ("lose", "ub_r1_3"), ("lose", "ub_r1_4")),
    ("lb_sf_1", "lb_sf", ("lose", "ub_sf_2"), ("win", "lb_r1_1")),
    ("lb_sf_2", "lb_sf", ("lose", "ub_sf_1"), ("win", "lb_r1_2")),
    ("ub_final", "ub_final", ("win", "ub_sf_1"), ("win", "ub_sf_2")),
    ("lb_final", "lb_final", ("win", "lb_sf_1"), ("win", "lb_sf_2")),
    ("cons_final", "cons_final", ("lose", "ub_final"), ("win", "lb_final")),
    ("grand_final", "grand_final", ("win", "ub_final"), ("win", "cons_final")),
]
# Stages in schedule order, including the two rounds a team can sit out.
_DOUBLE8_STAGES = ["ub_r1", "ub_sf", "lb_r1", "bye_r3", "lb_sf", "ub_final", "lb_final", "bye_r5", "cons_final", "grand_final"]
# The event is scored as six rounds: 1 upper round 1 · 2 upper semis + lower
# round 1 · 3 lower semis · 4 upper final + lower final · 5 consolidation
# final · 6 grand final. A team knocked out in round r is charged -3 for each
# of the 6 - r rounds it misses (lower-bracket losses only; upper-bracket
# losers drop down). Upper-semi winners sit out round 3 and are padded with
# the per-match average of their first two games; the upper-final winner sits
# out round 5 and is padded with the average of its three real games. Pads
# carry rating, win and role, no booster.
_DOUBLE8_ROUNDS_TOTAL = 6
_DOUBLE8_REM = {"ub_r1": 0, "ub_sf": 0, "ub_final": 0, "lb_r1": 4, "lb_sf": 3, "lb_final": 2, "cons_final": 1, "grand_final": 0}
_DOUBLE8_PAD_AFTER = {"ub_sf": ("bye_r3", 2), "ub_final": ("bye_r5", 3)}  # stage -> (pad stage, real games averaged)
# Lower-bracket rounds a team knocked out at a stage then misses (-3 each; the
# stage stats credit them there, as the group stats do, instead of on the loss)
_DOUBLE8_MISSED_AFTER = {
    "lb_r1": ["lb_sf", "lb_final", "cons_final", "grand_final"],
    "lb_sf": ["lb_final", "cons_final", "grand_final"],
    "lb_final": ["cons_final", "grand_final"],
    "cons_final": ["grand_final"],
}
# How the Point Sources modal lays the stages out: two bracket sections
# (rounds 1-3, rounds 4-6) with upper and lower rows; the byes are pad cards.
_DOUBLE8_LAYOUT = [
    {"key": "ub_r1", "full": "Upper round 1", "section": "r13", "section_title": "Rounds 1 – 3", "bracket": "upper", "opener": True},
    {"key": "ub_sf", "full": "Upper semi-final", "section": "r13", "section_title": "Rounds 1 – 3", "bracket": "upper"},
    {"key": "bye_r3", "full": "Round 3 bye", "section": "r13", "section_title": "Rounds 1 – 3", "bracket": "upper", "pad": True},
    {"key": "lb_r1", "full": "Lower round 1", "section": "r13", "section_title": "Rounds 1 – 3", "bracket": "lower"},
    {"key": "lb_sf", "full": "Lower semi-final", "section": "r13", "section_title": "Rounds 1 – 3", "bracket": "lower"},
    {"key": "ub_final", "full": "Upper final", "section": "r46", "section_title": "Rounds 4 – 6", "bracket": "upper"},
    {"key": "bye_r5", "full": "Round 5 bye", "section": "r46", "section_title": "Rounds 4 – 6", "bracket": "upper", "pad": True},
    {"key": "grand_final", "full": "Grand final", "section": "r46", "section_title": "Rounds 4 – 6", "bracket": "upper"},
    {"key": "lb_final", "full": "Lower final", "section": "r46", "section_title": "Rounds 4 – 6", "bracket": "lower"},
    {"key": "cons_final", "full": "Consolidation final", "section": "r46", "section_title": "Rounds 4 – 6", "bracket": "lower"},
]
_DOUBLE8_PADDING_RULE = (
    "Six scheduled rounds: upper-semi winners sit out round 3 and are padded with the per-match average of their "
    "first two games; the upper-final winner sits out round 5 and is padded with the average of its three real games "
    "(rating, win, role, no booster). A team knocked out in round r is charged -3 for each of the 6 - r rounds it misses."
)


def _double8_feed(feeder, team_slots: List[int], winners: Dict[str, int], losers: Dict[str, int]) -> int:
    kind, ref = feeder[0], feeder[1]
    if kind == "seed":
        return int(team_slots[int(ref)])
    return int((winners if kind == "win" else losers)[str(ref)])


def _double8_template(team_slots: List[int]) -> Dict:
    return {
        "kind": "double",
        "team_slots": [int(t) for t in team_slots],
        "matches": [[k, s, list(fa), list(fb)] for k, s, fa, fb in _DOUBLE8_MATCHES],
        "stages": list(_DOUBLE8_STAGES),
        "third_place": False,
        "rounds": 0,
    }


def _decode_double8_code(code: int, template: Dict, prob_cache: Dict | None = None) -> Dict[str, List[dict]]:
    """The bracket dict (stage -> matches with winner / loser / p_win_a / teams)
    for one outcome code of the double-elimination enumeration (one bit per
    match in play order, 0 = the first-listed team wins)."""
    team_slots = [int(t) for t in template.get("team_slots") or []]
    matches = template.get("matches") or [[k, s, list(fa), list(fb)] for k, s, fa, fb in _DOUBLE8_MATCHES]
    prob_cache = prob_cache if prob_cache is not None else {}
    gf_type = _bo_type(template.get("grand_final_bo"))
    n_matches = len(matches)
    winners: Dict[str, int] = {}
    losers: Dict[str, int] = {}
    out: Dict[str, List[dict]] = {}
    for m, (key, stage, fa, fb) in enumerate(matches):
        a = _double8_feed(fa, team_slots, winners, losers)
        b = _double8_feed(fb, team_slots, winners, losers)
        bit = (int(code) >> (n_matches - 1 - m)) & 1
        w, l = (b, a) if bit else (a, b)
        winners[key] = w
        losers[key] = l
        p_a = cached_win_prob(prob_cache, a, b, gf_type if stage == "grand_final" else "bo3")
        out.setdefault(stage, []).append({"key": key, "winner": w, "loser": l, "p_win_a": p_a, "teams": [a, b]})
    return out


def _double8_match_winners_to_code(team_slots: List[int], match_winners: List[int]) -> int:
    """Outcome code of a fully picked bracket (raises HTTPException on a pick
    that is not in its match)."""
    winners: Dict[str, int] = {}
    losers: Dict[str, int] = {}
    code = 0
    for m, (key, _stage, fa, fb) in enumerate(_DOUBLE8_MATCHES):
        a = _double8_feed(fa, team_slots, winners, losers)
        b = _double8_feed(fb, team_slots, winners, losers)
        picked = int(match_winners[m])
        if picked not in (a, b):
            raise HTTPException(status_code=400, detail=f"{key}: winner {picked} is not in match ({a} vs {b}).")
        winners[key] = picked
        losers[key] = a if picked == b else b
        code = (code << 1) | (1 if picked == b else 0)
    return code


def _exact_double8_player_totals(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    progress_callback=None,
    grand_final_bo: int = 3,
) -> tuple[Dict[int, Dict], Dict, int, List[Dict], Dict]:
    """Exact enumeration of the whole 8-team double-elimination bracket: 14
    matches, 16,384 outcomes. Every distinct pairing (teams, winner, each
    side's match number, rounds the loser misses) is scored once with the
    deterministic scorer and memoised; the walk adds and subtracts those rows.
    The two round pads (_DOUBLE8_PAD_AFTER) are applied on the path as soon
    as the match that earns them is won, from the winner's real games so far.
    Outputs the compact per-outcome matrices (totals, rating+win, matches
    played, matches the role scores for) for the roster optimiser and the
    stage stats (team reach per stage, per-player expected points per stage
    with opponent rows; the pads sit on their own bye stages)."""
    import numpy as np

    n = len(team_slots)
    if n != 8:
        raise ValueError(f"the double-elimination bracket needs 8 teams, got {n}")
    team_slots = [int(t) for t in team_slots]
    base_states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    prob_cache: Dict[tuple[int, int], float] = {}
    pids: List[int] = []
    pid_index: Dict[int, int] = {}
    team_pids: Dict[int, List[int]] = {}
    team_of_pid: Dict[int, int] = {}
    for tid in team_slots:
        team_pids[tid] = []
        for pid in base_states[tid].players:
            pid_index[int(pid)] = len(pids)
            pids.append(int(pid))
            team_pids[tid].append(int(pid))
            team_of_pid[int(pid)] = tid
    n_players = len(pids)
    team_arr = np.asarray([team_of_pid[pid] for pid in pids], dtype=np.int64)
    team_idx = {tid: np.asarray([pid_index[p] for p in team_pids[tid]], dtype=np.int64) for tid in team_slots}
    n_matches = len(_DOUBLE8_MATCHES)
    n_out = 2 ** n_matches
    totals = np.zeros((n_players, n_out), dtype=np.float32)
    rw_mat = np.zeros((n_players, n_out), dtype=np.float32)
    rt_mat = np.zeros((n_players, n_out), dtype=np.float32)  # rating alone (pads included)
    mp_mat = np.zeros((n_players, n_out), dtype=np.uint8)
    mr_mat = np.zeros((n_players, n_out), dtype=np.uint8)
    comps_ev = np.zeros((n_players, 5), dtype=np.float64)  # Σ prob × [total, rating, win, role, booster]
    probs = np.zeros(n_out, dtype=np.float64)
    run = np.zeros((5, n_players), dtype=np.float64)  # real match rows along the path: total, rating, win, role, booster
    run_mp = np.zeros(n_players, dtype=np.int64)
    padv = np.zeros((3, n_players), dtype=np.float64)  # round pads along the path: rating, win, role
    pad_cnt = np.zeros(n_players, dtype=np.int64)
    memo: Dict[tuple, tuple] = {}
    pair_acc: Dict[tuple, float] = {}  # (stage, a, b, winner, num_a, num_b) -> probability mass
    pad_stages = sorted({st for st, _g in _DOUBLE8_PAD_AFTER.values()})
    pad_acc = {st: np.zeros((3, n_players), dtype=np.float64) for st in pad_stages}  # Σ prob × pad
    pad_reach = {st: {tid: 0.0 for tid in team_slots} for st in pad_stages}
    champion_acc: Dict[int, float] = {tid: 0.0 for tid in team_slots}
    played: Dict[int, int] = {tid: 0 for tid in team_slots}
    winners: Dict[str, int] = {}
    losers: Dict[str, int] = {}

    def pairing(a: int, b: int, winner: int, num_a: int, num_b: int, rem: int, match_type: str = "bo3") -> tuple:
        key = (a, b, winner, num_a, num_b, rem, match_type)
        hit = memo.get(key)
        if hit is not None:
            return hit
        states = {
            a: _fresh_bracket_state(base_states, a, num_a - 1),
            b: _fresh_bracket_state(base_states, b, num_b - 1),
        }
        _w, _l, p_a, _branch = _play_match_deterministic(
            states, a, b, winner, remaining_rounds_after=rem, prob_cache=prob_cache,
            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id, match_type=match_type,
        )
        idx: List[int] = []
        comps: List[List[float]] = []
        meta: List[dict] = []
        for ts in states.values():
            for pid, p in ts.players.items():
                idx.append(pid_index[int(pid)])
                comps.append([
                    float(p.total_points), float(p.rating_points_total), float(p.win_points_total),
                    float(p.role_points_total), float(p.booster_points_total),
                ])
                row = next((r for r in p.point_breakdown if r.get("match_number") is not None), {})
                meta.append({
                    "booster_id": row.get("booster_id"),
                    "booster_name": row.get("booster_name"),
                    "booster_rate": float(row.get("booster_trigger_rate") or 0.0),
                    "opponent_rank": row.get("opponent_rank"),
                })
        memo[key] = (np.asarray(idx, dtype=np.int64), np.asarray(comps, dtype=np.float64).T, float(p_a), meta)
        return memo[key]

    leaf_count = [0]

    def leaf(prob: float) -> None:
        idx = leaf_count[0]
        leaf_count[0] += 1
        probs[idx] = prob
        tot = run[0] + padv[0] + padv[1] + padv[2]
        totals[:, idx] = tot
        rw_mat[:, idx] = run[1] + run[2] + padv[0] + padv[1]
        rt_mat[:, idx] = run[1] + padv[0]
        mp_mat[:, idx] = run_mp
        mr_mat[:, idx] = run_mp + pad_cnt
        comps_ev[:, 0] += prob * tot
        comps_ev[:, 1] += prob * (run[1] + padv[0])
        comps_ev[:, 2] += prob * (run[2] + padv[1])
        comps_ev[:, 3] += prob * (run[3] + padv[2])
        comps_ev[:, 4] += prob * run[4]
        champion_acc[winners["grand_final"]] += prob
        if progress_callback and idx % 1024 == 0:
            progress_callback(idx, n_out)

    def play(m: int, prob: float) -> None:
        if m == n_matches:
            leaf(prob)
            return
        key, stage, fa, fb = _DOUBLE8_MATCHES[m]
        a = _double8_feed(fa, team_slots, winners, losers)
        b = _double8_feed(fb, team_slots, winners, losers)
        num_a = played[a] + 1
        num_b = played[b] + 1
        rem = _DOUBLE8_REM[stage]
        pad_spec = _DOUBLE8_PAD_AFTER.get(stage)
        for w in (a, b):
            idx, comps, p_a, _meta = pairing(a, b, w, num_a, num_b, rem, _bo_type(grand_final_bo) if stage == "grand_final" else "bo3")
            p_node = prob * (p_a if w == a else 1.0 - p_a)
            run[:, idx] += comps
            run_mp[idx] += 1
            winners[key] = w
            losers[key] = b if w == a else a
            played[a] += 1
            played[b] += 1
            pk = (stage, a, b, w, num_a, num_b)
            pair_acc[pk] = pair_acc.get(pk, 0.0) + p_node
            pad = None
            if pad_spec:
                pst, games = pad_spec
                widx = team_idx[w]
                pad = run[1:4, widx] / float(games)  # average rating / win / role of the real games so far
                padv[:, widx] += pad
                pad_cnt[widx] += 1
                pad_acc[pst][:, widx] += p_node * pad
                pad_reach[pst][w] += p_node
            play(m + 1, p_node)
            if pad is not None:
                padv[:, widx] -= pad
                pad_cnt[widx] -= 1
            played[a] -= 1
            played[b] -= 1
            run[:, idx] -= comps
            run_mp[idx] -= 1

    if progress_callback:
        progress_callback(0, n_out)
    play(0, 1.0)
    assert leaf_count[0] == n_out, (leaf_count[0], n_out)

    results: Dict[int, Dict] = {}
    for tid in team_slots:
        players_out: Dict[int, Dict[str, float]] = {}
        for pid in team_pids[tid]:
            e = comps_ev[pid_index[pid]]
            ps = base_states[tid].players.get(pid)
            players_out[pid] = {
                "total_points": float(e[0]),
                "rating_points_total": float(e[1]),
                "win_points_total": float(e[2]),
                "role_points_total": float(e[3]),
                "booster_points_total": float(e[4]),
                "total_points_without_booster": float(e[1] + e[2] + e[3]),
                "role_id": ps.role_id if ps else None,
                "booster_slots": _booster_slots_for(ps),
                "role_info": _role_info_for(ps),
            }
        results[tid] = {"team_id": tid, "wins": 0, "losses": 0, "players": players_out}
    outcomes = [{"probability": float(probs[i]), "code": int(i)} for i in range(n_out)]
    template = _double8_template(team_slots)
    template["grand_final_bo"] = int(_bo_type(grand_final_bo)[2:])
    best_bracket = _decode_double8_code(int(np.argmax(probs)), template, prob_cache)

    # ---- stage stats in the Point Sources modal's contract: per team the
    # rounds it can play (reach, win chance, opponents, elimination credit,
    # byes); per player the points per stage with the boosters seen and the
    # per-opponent sums. Knock-out penalties come off the losing match and are
    # credited to the lower-bracket rounds the team then misses (-3 each).
    total_p = float(probs.sum()) or 1.0
    reach = {tid: {st: 0.0 for st in _DOUBLE8_STAGES} for tid in team_slots}
    win_mass = {tid: {st: 0.0 for st in _DOUBLE8_STAGES} for tid in team_slots}
    team_opps: Dict[int, Dict[str, Dict[int, List[float]]]] = {tid: {} for tid in team_slots}
    elim_before: Dict[int, Dict[str, List[float]]] = {tid: {} for tid in team_slots}
    cells: Dict[str, Dict[str, Dict[str, float]]] = {
        str(pid): {st: {"rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0, "total": 0.0} for st in _DOUBLE8_STAGES}
        for pid in pids
    }
    opp_pts: Dict[str, Dict[str, Dict[int, List[float]]]] = {}
    boost_mass: Dict[tuple, Dict[tuple, float]] = {}  # (pid, stage) -> {(booster_id, name, rate, slot): prob}
    for (stage, a, b, w, num_a, num_b), mass in pair_acc.items():
        rem = _DOUBLE8_REM[stage]
        reach[a][stage] += mass
        reach[b][stage] += mass
        win_mass[w][stage] += mass
        for tid, opp in ((a, b), (b, a)):
            oc = team_opps[tid].setdefault(stage, {}).setdefault(opp, [0.0, 0.0])
            oc[0] += mass
            if tid == w:
                oc[1] += mass
        idx, comps, _p_a, meta = memo[(a, b, w, num_a, num_b, rem, _bo_type(grand_final_bo) if stage == "grand_final" else "bo3")]
        for r in range(len(idx)):
            j = int(idx[r])
            pid_str = str(pids[j])
            tid = int(team_arr[j])
            opp = b if tid == a else a
            rating, role, booster = float(comps[1, r]), float(comps[3, r]), float(comps[4, r])
            # the match's own win points: the knock-out penalty is credited to the missed rounds below
            win = float(comps[2, r]) + (3.0 * rem if (tid != w and rem > 0) else 0.0)
            cell = cells[pid_str][stage]
            cell["rating"] += mass * rating
            cell["win"] += mass * win
            cell["role"] += mass * role
            cell["booster"] += mass * booster
            cell["total"] += mass * (rating + win + role + booster)
            oc = opp_pts.setdefault(pid_str, {}).setdefault(stage, {}).setdefault(opp, [0.0, 0.0, 0.0, 0.0])
            oc[0] += mass * rating
            oc[1] += mass * win
            oc[2] += mass * role
            oc[3] += mass * booster
            bm = boost_mass.setdefault((pid_str, stage), {})
            slot = num_a if tid == a else num_b
            bkey = (meta[r].get("booster_id"), meta[r].get("booster_name"), float(meta[r].get("booster_rate") or 0.0), int(slot))
            bm[bkey] = bm.get(bkey, 0.0) + mass
        if rem > 0:
            loser = b if w == a else a
            for later in _DOUBLE8_MISSED_AFTER.get(stage, []):
                e = elim_before[loser].setdefault(later, [0.0, 0.0])
                e[0] += mass
                e[1] += -3.0 * mass
                for pid in team_pids[loser]:
                    c = cells[str(pid)][later]
                    c["win"] -= 3.0 * mass
                    c["total"] -= 3.0 * mass
    for pst in pad_stages:
        for tid in team_slots:
            reach[tid][pst] = pad_reach[pst][tid]
        for j, pid in enumerate(pids):
            pr, pw, po = float(pad_acc[pst][0, j]), float(pad_acc[pst][1, j]), float(pad_acc[pst][2, j])
            if pr or pw or po:
                cell = cells[str(pid)][pst]
                cell["rating"] += pr
                cell["win"] += pw
                cell["role"] += po
                cell["total"] += pr + pw + po
    players_stats: Dict[str, Dict[str, Any]] = {}
    for pid in pids:
        pid_str = str(pid)
        tid = team_of_pid[pid]
        stages_out: Dict[str, Dict[str, Any]] = {}
        for st in _DOUBLE8_STAGES:
            cell = cells[pid_str][st]
            entry: Dict[str, Any] = {k: v / total_p for k, v in cell.items()}
            bm = boost_mass.get((pid_str, st)) or {}
            bt = sum(bm.values())
            entry["boosters"] = (
                sorted(
                    [
                        {"booster_id": bid, "booster_name": name, "booster_rate": rate, "slot": slot, "share": m / bt}
                        for (bid, name, rate, slot), m in bm.items()
                        if bid is not None
                    ],
                    key=lambda x: -x["share"],
                )
                if bt > 0
                else []
            )
            entry["opponents"] = {
                str(o): {"rating": v[0] / total_p, "win": v[1] / total_p, "role": v[2] / total_p, "booster": v[3] / total_p, "total": sum(v) / total_p}
                for o, v in ((opp_pts.get(pid_str) or {}).get(st) or {}).items()
            }
            stages_out[st] = entry
        players_stats[pid_str] = {"stages": stages_out, "role": _role_info_for(base_states[tid].players.get(pid))}
    teams_stats: Dict[str, Dict[str, Any]] = {}
    for tid in team_slots:
        rounds: Dict[str, Dict[str, Any]] = {}
        for st in _DOUBLE8_STAGES:
            is_pad = st in pad_stages
            eb = elim_before[tid].get(st) or [0.0, 0.0]
            rounds[st] = {
                "play": 0.0 if is_pad else reach[tid][st] / total_p,
                "win": (win_mass[tid][st] / reach[tid][st]) if (reach[tid][st] > 0 and not is_pad) else 0.0,
                "opponents": {
                    str(o): {"play": c[0] / total_p, "win": (c[1] / c[0]) if c[0] > 0 else 0.0}
                    for o, c in (team_opps[tid].get(st) or {}).items()
                },
                "elim_before": {"prob": eb[0] / total_p, "points": eb[1] / total_p},
                "bye": (reach[tid][st] / total_p) if is_pad else 0.0,
            }
        teams_stats[str(tid)] = {
            "reach": {st: reach[tid][st] / total_p for st in _DOUBLE8_STAGES},
            "champion": champion_acc[tid] / total_p,
            "rounds": rounds,
            "rank": team_rank_by_id.get(tid),
            "padding_prob": 0.0,
            "playoff_padding_prob": 0.0,
        }
    stage_stats = {
        "stages": list(_DOUBLE8_STAGES),
        "layout": [dict(x) for x in _DOUBLE8_LAYOUT],
        "teams": teams_stats,
        "players": players_stats,
    }
    extra = {
        "outcome_matrix": {
            "pids": pids,
            "n": int(n_out),
            "f32_b64": base64.b64encode(totals.tobytes()).decode("ascii"),
            "rw_b64": base64.b64encode(rw_mat.tobytes()).decode("ascii"),
            "mp_b64": base64.b64encode(mp_mat.tobytes()).decode("ascii"),
            "mr_b64": base64.b64encode(mr_mat.tobytes()).decode("ascii"),
            "rt_b64": base64.b64encode(rt_mat.tobytes()).decode("ascii"),
        },
        "bracket_template": template,
        "stage_stats": stage_stats,
        "padding_rule": _DOUBLE8_PADDING_RULE,
    }
    if progress_callback:
        progress_callback(n_out, n_out)
    return results, best_bracket, n_out, outcomes, extra


def _double8_pad_team(ts: TeamState, games: int, stage_key: str) -> None:
    """Pad every player of a team sitting out a round with the per-match
    average of its real games so far (breakdown rows carrying a match
    number): rating, win and role, no booster."""
    label = {"bye_r3": "round 3", "bye_r5": "round 5"}.get(stage_key, stage_key)
    for p in ts.players.values():
        real = [r for r in p.point_breakdown if r.get("match_number") is not None]
        n = float(max(1, int(games)))
        pr = sum(float(r.get("rating_points") or 0.0) for r in real) / n
        pw = sum(float(r.get("win_points") or 0.0) for r in real) / n
        po = sum(float(r.get("role_points") or 0.0) for r in real) / n
        p.rating_points_total += pr
        p.win_points_total += pw
        p.role_points_total += po
        p.total_points += pr + pw + po
        p.point_breakdown.append(
            {
                "match_number": None,
                "match_type": "PADDING",
                "opponent_team_id": None,
                "opponent_rank": None,
                "did_win": None,
                "win_probability": 0.0,
                "rating_used": None,
                "rating_points": float(pr),
                "win_points": float(pw),
                "role_id": p.role_id,
                "role_major_pct": float(p.major_pct),
                "role_minor_pct": float(p.minor_pct),
                "role_points": float(po),
                "booster_slot": None,
                "booster_id": None,
                "booster_name": None,
                "booster_trigger_rate": 0.0,
                "booster_points": 0.0,
                "total_points": float(pr + pw + po),
                "note": f"Padding for {label} (sat out): per-match average of the {len(real)} played",
            }
        )


def _deterministic_double8_outcome(team_slots: List[int], vrs_ranks: Dict[int, int], match_winners: List[int], grand_final_bo: int = 3) -> Dict:
    """Play the double-elimination bracket implied by the user's 14 picks (play
    order) with real team states and return one outcome dict in the stored
    shape (probability, bracket, players, player_components, player_breakdown),
    the round pads and knock-out penalties included as breakdown rows."""
    team_slots = [int(t) for t in team_slots]
    if len(match_winners) != len(_DOUBLE8_MATCHES):
        raise HTTPException(status_code=400, detail=f"Completed bracket needs winners for all {len(_DOUBLE8_MATCHES)} matches; got {len(match_winners)}.")
    code = _double8_match_winners_to_code(team_slots, match_winners)
    states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    prob_cache: Dict[tuple[int, int], float] = {}
    winners: Dict[str, int] = {}
    losers: Dict[str, int] = {}
    bracket: Dict[str, List[dict]] = {}
    path_prob = 1.0
    for m, (key, stage, fa, fb) in enumerate(_DOUBLE8_MATCHES):
        a = _double8_feed(fa, team_slots, winners, losers)
        b = _double8_feed(fb, team_slots, winners, losers)
        picked = int(match_winners[m])
        w, l, p_win_a, branch_p = _play_match_deterministic(
            states, a, b, picked, remaining_rounds_after=_DOUBLE8_REM[stage], prob_cache=prob_cache,
            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id,
            match_type=_bo_type(grand_final_bo) if stage == "grand_final" else "bo3",
        )
        path_prob *= branch_p
        winners[key] = w
        losers[key] = l
        bracket.setdefault(stage, []).append({"key": key, "winner": w, "loser": l, "p_win_a": p_win_a, "teams": [a, b]})
        pad_spec = _DOUBLE8_PAD_AFTER.get(stage)
        if pad_spec:
            _double8_pad_team(states[w], pad_spec[1], pad_spec[0])
    player_points: Dict[str, float] = {}
    player_components: Dict[str, Dict[str, float]] = {}
    player_breakdown: Dict[str, List[dict]] = {}
    for ts in states.values():
        for pid, p in ts.players.items():
            player_points[str(pid)] = float(p.total_points)
            player_components[str(pid)] = {
                "total": float(p.total_points),
                "total_without_booster": float(p.rating_points_total + p.win_points_total + p.role_points_total),
                "rating": float(p.rating_points_total),
                "win": float(p.win_points_total),
                "role": float(p.role_points_total),
                "booster": float(p.booster_points_total),
            }
            player_breakdown[str(pid)] = [dict(row) for row in p.point_breakdown]
    return {
        "probability": float(path_prob),
        "code": int(code),
        "bracket": bracket,
        "players": player_points,
        "player_components": player_components,
        "player_breakdown": player_breakdown,
    }


_STAGE_MAIN_ORDER = ["round_of_32", "round_of_16", "quarters", "semis", "final"]


def _stage_stats_from_outcomes(outcomes: List[Dict], results: Dict[int, Dict]) -> Dict:
    """Per-round decomposition of the enumerated outcomes.

    Returns each team's probability of appearing in (and winning) every round,
    and each player's probability-weighted expected points per round split by
    component (rating/win/role/booster; total includes booster, so each round
    row is self-consistent and the rounds sum to the player's full total).
    Elimination penalties are split across the rounds the team missed; the
    byes' +6 pad (applied without a breakdown row) lands on the round they
    skipped. Player rows are empty when the outcomes carry no per-match
    breakdowns (Monte-Carlo fields)."""
    pid_team: Dict[str, int] = {}
    for tid, tdata in results.items():
        for pid in (tdata.get("players") or {}):
            pid_team[str(pid)] = int(tid)
    team_ids = [int(t) for t in results.keys()]

    main_present = [s for s in _STAGE_MAIN_ORDER if any((o.get("bracket") or {}).get(s) for o in outcomes)]
    has_third = any((o.get("bracket") or {}).get("third_place") for o in outcomes)
    stages = main_present + (["third_place"] if has_third else [])
    if not stages:
        return {"stages": [], "teams": {}, "players": {}}

    total_p = 0.0
    reach = {tid: {s: 0.0 for s in stages} for tid in team_ids}
    champion = {tid: 0.0 for tid in team_ids}
    player_stage: Dict[str, Dict[str, Dict[str, float]]] = {}
    # pid -> stage -> opponent tid -> weighted sums (for conditional matchup EVs)
    player_opps: Dict[str, Dict[str, Dict[int, Dict[str, float]]]] = {}
    # pid -> stage -> booster used that round (slot = team's Nth match, so it is
    # the same booster in every outcome that reaches the round)
    player_boost_meta: Dict[str, Dict[str, dict]] = {}

    for o in outcomes:
        p = float(o.get("probability") or 0.0)
        if p <= 0:
            continue
        total_p += p
        bracket = o.get("bracket") or {}
        stage_teams: Dict[str, set] = {}
        for s in stages:
            in_stage: set = set()
            for m in bracket.get(s) or []:
                in_stage.update(int(t) for t in m.get("teams") or [])
            stage_teams[s] = in_stage
            for tid in in_stage:
                if tid in reach:
                    reach[tid][s] += p
        final_matches = bracket.get("final") or []
        if final_matches:
            w = int(final_matches[0].get("winner") or 0)
            if w in champion:
                champion[w] += p

        breakdown = o.get("player_breakdown") or {}
        if not breakdown:
            continue
        components = o.get("player_components") or {}
        # Ordered rounds each team actually played this outcome (third-place
        # sorts last, matching play order).
        team_played: Dict[int, List[str]] = {}
        for s in stages:
            for tid in stage_teams[s]:
                team_played.setdefault(tid, []).append(s)

        for pid_str, rows in breakdown.items():
            tid = pid_team.get(pid_str)
            if tid is None:
                continue
            played = team_played.get(tid, [])
            main_played = [s for s in played if s != "third_place"]
            bucket = player_stage.setdefault(
                pid_str,
                {s: {"rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0, "total": 0.0} for s in stages},
            )

            def add(stage: str, rating: float, win: float, role: float, booster: float) -> None:
                cell = bucket[stage]
                cell["rating"] += p * rating
                cell["win"] += p * win
                cell["role"] += p * role
                cell["booster"] += p * booster
                cell["total"] += p * (rating + win + role + booster)

            stage_iter = iter(played)
            row_sums = {"rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0}
            for row in rows:
                rr = float(row.get("rating_points") or 0.0)
                rw = float(row.get("win_points") or 0.0)
                ro = float(row.get("role_points") or 0.0)
                rb = float(row.get("booster_points") or 0.0)
                row_sums["rating"] += rr
                row_sums["win"] += rw
                row_sums["role"] += ro
                row_sums["booster"] += rb
                if str(row.get("match_type") or "").upper() == "ELIMINATION":
                    # The penalty covers every main-line round after the team's
                    # last played one — spread it evenly across those rounds.
                    last = main_played[-1] if main_played else None
                    after = [
                        s
                        for s in main_present
                        if (last is None or main_present.index(s) > main_present.index(last))
                        and tid not in stage_teams[s]
                    ]
                    targets = after or (main_played[-1:] or main_present[-1:])
                    n = len(targets)
                    for s in targets:
                        add(s, rr / n, rw / n, ro / n, rb / n)
                else:
                    s = next(stage_iter, None)
                    if s is None:
                        s = played[-1] if played else main_present[0]
                    add(s, rr, rw, ro, rb)
                    opp_raw = row.get("opponent_team_id")
                    if opp_raw is not None:
                        om = (
                            player_opps.setdefault(pid_str, {})
                            .setdefault(s, {})
                            .setdefault(
                                int(opp_raw),
                                {
                                    "prob": 0.0,
                                    "rating": 0.0,
                                    "win": 0.0,
                                    "role": 0.0,
                                    "booster": 0.0,
                                    "total": 0.0,
                                    "win_prob": 0.0,
                                    "rank": row.get("opponent_rank"),
                                },
                            )
                        )
                        om["prob"] += p
                        om["rating"] += p * rr
                        om["win"] += p * rw
                        om["role"] += p * ro
                        om["booster"] += p * rb
                        om["total"] += p * (rr + rw + ro + rb)
                        if row.get("did_win"):
                            om["win_prob"] += p
                    bm = player_boost_meta.setdefault(pid_str, {})
                    if s not in bm and row.get("booster_id") is not None:
                        bm[s] = {
                            "booster_id": row.get("booster_id"),
                            "booster_name": row.get("booster_name"),
                            "booster_rate": float(row.get("booster_trigger_rate") or 0.0),
                        }
            # Points applied without a breakdown row (the byes' +6 pad) belong
            # to the round(s) the team skipped before its first match.
            comp = components.get(pid_str) or {}
            residuals = {
                "rating": float(comp.get("rating") or 0.0) - row_sums["rating"],
                "win": float(comp.get("win") or 0.0) - row_sums["win"],
                "role": float(comp.get("role") or 0.0) - row_sums["role"],
                "booster": float(comp.get("booster") or 0.0) - row_sums["booster"],
            }
            if any(abs(v) > 1e-9 for v in residuals.values()):
                first = main_played[0] if main_played else None
                before = [
                    s
                    for s in main_present
                    if first is not None
                    and main_present.index(s) < main_present.index(first)
                    and tid not in stage_teams[s]
                ]
                targets = before or (main_played[:1] or main_present[:1])
                n = len(targets)
                for s in targets:
                    add(s, residuals["rating"] / n, residuals["win"] / n, residuals["role"] / n, residuals["booster"] / n)

    denom = total_p if total_p > 0 else 1.0
    return {
        "stages": stages,
        "teams": {
            str(tid): {
                "reach": {s: reach[tid][s] / denom for s in stages},
                "champion": champion[tid] / denom,
            }
            for tid in team_ids
        },
        "players": {
            pid: {
                s: _finish_stage_cell(
                    cell,
                    denom,
                    (player_boost_meta.get(pid) or {}).get(s),
                    (player_opps.get(pid) or {}).get(s),
                )
                for s, cell in svals.items()
            }
            for pid, svals in player_stage.items()
        },
    }


def _finish_stage_cell(cell: Dict[str, float], denom: float, boost_meta: dict | None, opps: dict | None) -> dict:
    """Normalize one player-round cell and attach the round's booster plus the
    per-opponent matchup rows: `prob` is the unconditional chance of that
    matchup; the point values are conditional (expected in that match IF it
    happens), so a weak opponent showing a bigger number answers 'does he do
    better if a bad team makes it here'."""
    out: dict = {k: v / denom for k, v in cell.items()}
    if boost_meta:
        out.update(boost_meta)
    opp_list = []
    for otid, om in (opps or {}).items():
        q = float(om.get("prob") or 0.0)
        if q <= 0:
            continue
        opp_list.append(
            {
                "team_id": int(otid),
                "rank": om.get("rank"),
                "prob": q / denom,
                "win_chance": om["win_prob"] / q,
                "total": om["total"] / q,
                "rating": om["rating"] / q,
                "win": om["win"] / q,
                "role": om["role"] / q,
                "booster": om["booster"] / q,
            }
        )
    opp_list.sort(key=lambda x: -x["prob"])
    if opp_list:
        out["opponents"] = opp_list
    return out


def _compute_playoff_result(payload: dict, progress_callback=None) -> dict:
    slots = payload["team_slots"]
    has_third_place_decider = bool(payload.get("has_third_place_decider", False))
    final_bo = int(_bo_type(payload.get("final_bo"))[2:])

    # vrs_ranks not relevant here (use default 999)
    vrs_ranks = {tid: 999 for tid in slots}

    if _bracket_kind(payload.get("bracket_kind")) == "double":
        exact_players, best_bracket, outcomes_count, outcomes, extra = _exact_double8_player_totals(
            slots, vrs_ranks, progress_callback=progress_callback, grand_final_bo=final_bo
        )
        stage_stats = extra.pop("stage_stats")
        return {
            "bracket": best_bracket,
            "teams": exact_players,
            "method": "exact_enumeration",
            "outcomes_count": outcomes_count,
            "outcomes": outcomes,
            "has_third_place_decider": False,
            "final_bo": final_bo,
            "bracket_kind": "double",
            **extra,
            "stage_stats": stage_stats,
        }

    quarters_override = None
    sf_pairs_resolver = None
    if _variant(payload.get("variant")) == "bounty":
        quarters_override = [tuple(pair) for pair in payload.get("qf_pairs") or []]
        sf_pairs_resolver = _bounty_sf_pairs_resolver(slots, payload.get("sf_picks") or {})

    exact_players, best_bracket, outcomes_count, outcomes, extra = _bracket_player_totals(
        slots,
        vrs_ranks,
        has_third_place_decider=has_third_place_decider,
        progress_callback=progress_callback,
        quarters_override=quarters_override,
        sf_pairs_resolver=sf_pairs_resolver,
        mc_sims=_clamp_mc_sims(payload.get("mc_sims")),
        final_bo=final_bo,
    )
    n = len(slots)
    exact = extra is not None or n in (6, 8)
    out = {
        "bracket": best_bracket,
        "teams": exact_players,
        "method": "exact_enumeration" if exact else "monte_carlo",
        "outcomes_count": outcomes_count,
        "outcomes": outcomes,
        "has_third_place_decider": has_third_place_decider,
        "final_bo": final_bo,
    }
    if extra:
        out.update(extra)
        out["stage_stats"] = _stage_stats_from_codes(outcomes, extra["bracket_template"], exact_players)
    else:
        out["stage_stats"] = _stage_stats_from_outcomes(outcomes, exact_players)
    return out


@router.post("/autofill-from-hltv-event")
def autofill_playoff_from_hltv_event(payload: dict | None = None):
    """Scrape (or reuse the stored snapshot of) the linked HLTV event page and
    return the single-elimination playoff bracket seeding as this app's team IDs
    in bracket order, plus the field size (16/8). The frontend fills the seed
    slots and bracket-size selector from it."""
    # Lazy import: groups.py imports from this module at load time, so importing
    # it at module top here would be a circular import.
    from backend.routes.groups import autofill_event_playoff

    body = payload or {}
    result = autofill_event_playoff(
        hltv_event_url=str(body.get("hltv_event_url") or "").strip(),
        hltv_event_id=body.get("hltv_event_id"),
        fantasy_event_id=body.get("fantasy_event_id") or body.get("event_id"),
    )
    return {"status": "ok", **result}


def _store_playoff_combos(result: dict, variant: str = "main", event_id=None) -> dict:
    """Rank every valid roster against the stored bracket outcomes and store
    the set the Top 5 tab reads; the tab's filters and ranking modes are
    applied to it at query time."""
    body = {"mode": "most_outcomes", "variant": variant, "event_id": event_id}
    options = parse_optimizer_payload(body)
    players_info = _build_players_info_from_sim_results(result.get("teams", {}) or {}, options["exclude"])
    combos = _optimize_playoff_teams_by_outcomes(
        players_info, result, options["include"], options["budget"], options["max_per_team"], "most_outcomes"
    )
    save_latest_playoff_best_team(body, combos, variant, event_id)
    return combos


def _run_playoff_job(job_id: str, payload: dict) -> None:
    def _update_progress(processed: int, total: int) -> None:
        with PLAYOFF_JOBS_LOCK:
            job = PLAYOFF_JOBS.get(job_id)
            if not job:
                return
            job["processed_sims"] = int(processed)
            job["total_sims"] = int(total)
            job["progress"] = 0.0 if total <= 0 else float(processed) / float(total)
            job["updated_at"] = time.time()

    with PLAYOFF_JOBS_LOCK:
        job = PLAYOFF_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["updated_at"] = time.time()

    try:
        result = _compute_playoff_result(payload, progress_callback=_update_progress)
        save_latest_playoff(payload, result, payload.get("variant"))
        # the Top 5 tab's combinations follow every run, not a separate click
        try:
            _store_playoff_combos(result, _variant(payload.get("variant")))
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Combinations failed after a %s bracket run", _variant(payload.get("variant")), exc_info=True
            )
        with PLAYOFF_JOBS_LOCK:
            job = PLAYOFF_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "completed"
            job["result"] = result
            total = int(job.get("total_sims", 128))
            job["processed_sims"] = total
            job["total_sims"] = total
            job["progress"] = 1.0
            job["updated_at"] = time.time()
    except Exception as exc:
        with PLAYOFF_JOBS_LOCK:
            job = PLAYOFF_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "failed"
            job["error"] = str(exc)
            job["updated_at"] = time.time()


@router.post("/start")
def start_playoff(payload: dict):
    normalized = _normalize_playoff_payload(payload)
    n_slots = len(normalized.get("team_slots") or [])
    if normalized.get("bracket_kind") == "double":
        total_outcomes = 2 ** len(_DOUBLE8_MATCHES)
    elif 2 ** max(0, n_slots - 1) <= _BRACKET_EXACT_OUTCOME_LIMIT:
        total_outcomes = (2 ** max(0, n_slots - 1)) * (2 if normalized.get("has_third_place_decider") else 1)
    else:
        # Monte-Carlo sample count for large fields (user-configurable).
        total_outcomes = int(normalized.get("mc_sims") or _BRACKET_MC_SIMS)
    job_id = str(int(time.time() * 1000000))
    with PLAYOFF_JOBS_LOCK:
        PLAYOFF_JOBS[job_id] = {
            "status": "queued",
            "error": "",
            "progress": 0.0,
            "processed_sims": 0,
            "total_sims": int(total_outcomes),
            "result": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    worker = threading.Thread(target=_run_playoff_job, args=(job_id, normalized), daemon=True)
    worker.start()
    return {"job_id": job_id}


@router.get("/job/{job_id}")
def get_playoff_job(job_id: str):
    with PLAYOFF_JOBS_LOCK:
        job = PLAYOFF_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        out = dict(job)
    return {
        "job_id": job_id,
        "status": out.get("status", "queued"),
        "error": out.get("error", ""),
        "progress": out.get("progress", 0.0),
        "processed_sims": out.get("processed_sims", 0),
        "total_sims": out.get("total_sims", 0),
        "result": out.get("result"),
    }


def simulate_playoff_fantasy(team_slots: List[int], n_sims: int = 1, return_runs: bool = False):
    if n_sims <= 0:
        raise HTTPException(status_code=400, detail="n_sims must be positive")
    vrs_ranks = {tid: 999 for tid in team_slots}
    exact_results = _bracket_player_totals(team_slots, vrs_ranks, has_third_place_decider=False, store_outcomes=False)[0]

    results: Dict[int, Dict] = {}
    for tid in team_slots:
        players_out: Dict[int, Dict[str, float]] = {}
        for pid, comps in (exact_results.get(tid, {}).get("players", {}) or {}).items():
            players_out[pid] = {
                "total": float(comps.get("total_points", 0.0)),
                "rating": float(comps.get("rating_points_total", 0.0)),
                "win": float(comps.get("win_points_total", 0.0)),
                "role": float(comps.get("role_points_total", 0.0)),
                "booster": float(comps.get("booster_points_total", 0.0)),
            }
        results[tid] = {"players": players_out}

    if return_runs:
        return results, []
    return results


def _build_players_info_from_sim_results(sim_results: Dict, exclude: set[int]):
    players_info = []
    for tid, team_res in (sim_results or {}).items():
        tid_int = int(tid)
        for pid_key, comps in (team_res.get("players", {}) or {}).items():
            pid = int(pid_key)
            if pid in exclude:
                continue
            row = get_player(pid)
            if not row:
                continue
            rating_ev = float(comps.get("rating_points_total", comps.get("rating", 0.0)))
            win_ev = float(comps.get("win_points_total", comps.get("win", 0.0)))
            role_ev = float(comps.get("role_points_total", comps.get("role", 0.0)))
            players_info.append(
                {
                    "player_id": pid,
                    "name": row.get("name", f"Player {pid}"),
                    "team_id": tid_int,
                    "price": int(row.get("price", 0)),
                    "rating_ev": rating_ev,
                    "win_ev": win_ev,
                    "role_ev": role_ev,
                    "booster_ev": 0.0,
                    "raw_booster_ev": float(comps.get("booster_points_total", comps.get("booster", 0.0))),
                    "total_ev": rating_ev + win_ev + role_ev,
                }
            )
    return players_info


def _optimize_playoff_teams(
    players_info: list[dict],
    include: set[int],
    budget: int,
    max_per_team: int,
    progress_callback=None,
):
    return optimize_rosters(
        players_info,
        include,
        budget,
        max_per_team,
        progress_callback=progress_callback,
        include_error_suffix="in bracket teams",
    )


# Candidates deep-scored (ceiling / most-likely) in phase 2 of the two-phase run.
_PLAYOFF_TWO_PHASE_TOPK = 12000


def _optimize_playoff_teams_by_outcomes(
    players_info: list[dict],
    results: Dict,
    include: set[int],
    budget: int,
    max_per_team: int,
    mode: str,
    progress_callback=None,
):
    """Rosters scored against the stored outcome table, each under its own plan.

    A roster's plan is the exact clash-free role assignment plus the roster-wide
    booster assignment (each booster once across the five players, slots =
    the team's match numbers) maximising expected points. Average Value is the
    exact top list by that plan expectation — the groups tab's bounded search
    with the Lagrangian booster bound and exact per-roster scoring. Best Single
    Outcome and Most Likely Winner score every candidate roster in every
    outcome under its plan: rating and win as scored in the outcome, the
    assigned role's points for the matches played, the assigned boosters'
    points for the slots played. Candidates are the exact average list, the
    strongest rosters by per-player points, and the roster that tops each
    outcome when players carry their own best role and boosters (a strong
    proxy for high-ceiling rosters), so the true ceiling roster is present.
    Stored runs without the decomposition (older Monte-Carlo fields) fall back
    to per-player scoring."""
    import numpy as np

    from backend.routes.groups import LIVE_OPTIMIZER_MAX_K, _booster_prerequisites, _topk_rosters_bnb
    from backend.services import roster_kernels as rk
    from backend.services.roster_plan import match_reach_from_played, plan_for_roster, plan_metrics
    from backend.services.role_assignment import best_role_assignment_for_team, extract_role_scores_for_player

    include = {int(x) for x in (include or set())}
    if len(players_info) < 5:
        return {"error": "Not enough players after exclusions"}
    pids_all, M_all, probs, RW_all, MP_all, MR_all, RT_all = _outcome_matrix(results)
    if M_all is None or M_all.shape[1] == 0:
        if mode == "average":
            return _optimize_playoff_teams(players_info, include, budget, max_per_team, progress_callback=progress_callback)
        return {"error": "No playoff outcome table found. Re-run Playoff Bracket first."}
    available_ids = {int(p["player_id"]) for p in players_info}
    missing_includes = [pid for pid in include if pid not in available_ids]
    if missing_includes:
        return {"error": f"Included players not available in bracket teams: {missing_includes}"}
    idx_all = {pid: i for i, pid in enumerate(pids_all)}
    players = [p for p in players_info if int(p["player_id"]) in idx_all]
    if len(players) < 5:
        return {"error": "Not enough players in the outcome table"}
    pids = [int(p["player_id"]) for p in players]
    pid_idx = {pid: i for i, pid in enumerate(pids)}
    sel = [idx_all[pid] for pid in pids]
    M = np.ascontiguousarray(M_all[sel])
    N = M.shape[1]
    plans_on = RW_all is not None
    RW = np.ascontiguousarray(RW_all[sel]) if plans_on else None
    MP = np.ascontiguousarray(MP_all[sel]) if plans_on else None
    MR = np.ascontiguousarray(MR_all[sel]) if plans_on else None
    RT = np.ascontiguousarray(RT_all[sel]) if (plans_on and RT_all is not None) else None
    prices = np.asarray([int(p.get("price") or 0) for p in players], dtype=np.int64)
    teams_raw = [int(p.get("team_id") or 0) for p in players]
    tmap = {t: i for i, t in enumerate(sorted(set(teams_raw)))}
    team_of = np.asarray([tmap[t] for t in teams_raw], dtype=np.int64)
    team_of_pid = {pid: t for pid, t in zip(pids, teams_raw)}
    players_meta = {str(p["player_id"]): p for p in players}
    expected = M @ probs  # per-player expected points with their own best role and boosters
    n = len(players)
    total_combinations = math.comb(n, 5)
    forced = np.zeros(n, dtype=np.bool_)
    for pid in include:
        forced[pid_idx[pid]] = True
    options = {"budget": int(budget), "max_per_team": int(max_per_team), "include": set(include), "exclude": set()}

    # ---- plan prerequisites (trigger rates, role scores, match-reach odds, search bound)
    if plans_on:
        reach_by_team = match_reach_from_played(pids, team_of_pid, MP, probs)
        reach_by_team, rates_by_pid, role_scores, bound_offset = _booster_prerequisites(
            results, players, options, reach_by_team=reach_by_team
        )
    else:
        rates_by_pid, bound_offset = {}, 0.0
        role_scores = {int(p["player_id"]): extract_role_scores_for_player(get_player(int(p["player_id"])) or {}) for p in players}
        reach_by_team = {}

    # ---- candidates
    cand: Dict[tuple, None] = {}
    avg_exact = False
    if plans_on:
        bound_scores = {int(p["player_id"]): float(p.get("total_ev") or 0.0) + float(p.get("booster_ub_tight") or 0.0) for p in players}

        def exact_score(roster):
            plan = plan_for_roster(roster, reach_by_team, rates_by_pid, role_scores)
            rw = sum(float(p.get("rating_ev", 0.0)) + float(p.get("win_ev", 0.0)) for p in roster)
            return rw + plan["role_total"] + plan["booster_total"]

        avg_rosters, avg_exact = _topk_rosters_bnb(
            players, bound_scores, LIVE_OPTIMIZER_MAX_K, budget, max_per_team, include, exact_score,
            bound_offset=bound_offset, role_scores_by_player=role_scores,
        )
        for r in avg_rosters:
            cand[tuple(sorted(int(p["player_id"]) for p in r["players"]))] = None
    for _score, roster_idx in rk.top_rosters_one(expected, prices, team_of, budget, max_per_team, _PLAYOFF_TWO_PHASE_TOPK, forced):
        cand.setdefault(tuple(sorted(int(pids[j]) for j in roster_idx)))
    best = rk.best_rosters_batch(M, prices, team_of, int(budget), int(max_per_team), forced=forced)
    own_wins: Dict[tuple, float] = {}
    for c, row in enumerate(best):
        if row[0] < 0:
            continue
        key = tuple(sorted(int(pids[j]) for j in row))
        own_wins[key] = own_wins.get(key, 0.0) + float(probs[c])
        cand.setdefault(key)
    keys = list(cand)
    C = len(keys)
    if C == 0:
        return {"error": "No valid roster under these constraints"}

    # ---- score every candidate in every outcome (under its plan when available)
    avg = np.empty(C)
    ceil = np.empty(C)
    ceil_p = np.empty(C)
    argmax = np.empty(C, dtype=np.int64)
    plans: list = [None] * C
    scores_at_peak: list = [None] * C
    parts_at_peak: list = [None] * C
    parts_at_win: list = [None] * C
    wins_prob = np.zeros(C)
    wins_count = np.zeros(C)
    wins_idx: list = [[] for _ in range(C)]
    if plans_on:
        # every candidate under its plan in every outcome: the fused compiled
        # kernel shared with the groups tab
        metrics = plan_metrics(
            keys, players_meta, pid_idx, RW, MP, MR, probs, reach_by_team, rates_by_pid, role_scores,
            progress_callback=progress_callback, RT=RT,
        )
        for i, m in enumerate(metrics):
            plans[i] = m["plan"]
            avg[i] = float(m["avg"])
            ceil[i] = float(m["ceiling"])
            ceil_p[i] = float(m["ceiling_p"])
            argmax[i] = int(m["argmax"])
            scores_at_peak[i] = m["peak"]
            parts_at_peak[i] = m.get("peak_parts")
            parts_at_win[i] = m.get("win_parts")
            wins_prob[i] = float(m["wins_prob"])
            wins_count[i] = float(m["wins_count"])
            wins_idx[i] = list(m.get("wins_idx") or [])
    else:
        best_val = np.full(N, -np.inf)
        best_key = [-1] * N
        for i, key in enumerate(keys):
            rows = [pid_idx[pid] for pid in key]
            S = M[rows].sum(axis=0)
            avg[i] = float(S @ probs)
            o = int(np.argmax(S))
            argmax[i] = o
            mx = float(S[o])
            ceil[i] = mx
            ceil_p[i] = float(probs[S >= mx - 1e-9].sum())
            better = S > best_val + 1e-9
            if better.any():
                best_val[better] = S[better]
                for c in np.nonzero(better)[0]:
                    best_key[c] = i
            scores_at_peak[i] = {int(pid): float(M[j, o]) for pid, j in zip(key, rows)}
            if progress_callback and (i + 1) % 256 == 0:
                progress_callback(i + 1, C)
        for c in range(N):
            i = best_key[c]
            if i >= 0:
                wins_prob[i] += probs[c]
                wins_count[i] += 1
                wins_idx[i].append(int(c))
        for i in range(C):
            wins_idx[i].sort(key=lambda c: -float(probs[c]))

    # ---- serialise
    teams_out: list = []
    for i, key in enumerate(keys):
        pids_r = list(key)
        cost = int(sum(prices[pid_idx[pid]] for pid in pids_r))
        if plans_on:
            plan = plans[i]
            roles = [str(plan["role_of"].get(pid, "-")) for pid in pids_r]
            ev_no_booster = float(sum(float(players_meta[str(pid)].get("rating_ev", 0.0)) + float(players_meta[str(pid)].get("win_ev", 0.0)) for pid in pids_r) + plan["role_total"])
            serialized = serialize_roster(players_meta, pids_r, roles, ev_no_booster, cost, booster_assignments=plan["boosters"])
            for player in serialized.get("players") or []:
                pid = int(player.get("player_id") or 0)
                new_role_ev = plan["role_ev_of"].get(pid)
                if new_role_ev is not None:
                    old_role_ev = float(player.get("role_ev") or 0.0)
                    player["role_ev"] = float(new_role_ev)
                    player["total_ev"] = float(player.get("total_ev") or 0.0) - old_role_ev + float(new_role_ev)
        else:
            assignment, _ = best_role_assignment_for_team(pids_r, role_scores)
            roles = [str((assignment or {}).get(pid, "-")) for pid in pids_r]
            ev_no_booster = float(sum(float(players_meta[str(pid)].get("total_ev") or 0.0) for pid in pids_r))
            serialized = serialize_roster(players_meta, pids_r, roles, ev_no_booster, cost)
        peak = scores_at_peak[i] or {}
        for player in serialized.get("players") or []:
            pid = int(player.get("player_id") or 0)
            # the roster's best outcome and the outcomes it wins, per player
            roster_scores.refresh_average(player)
            roster_scores.set_kind(player, "outcome", (parts_at_peak[i] or {}).get(pid), float(peak.get(pid, 0.0)))
            roster_scores.set_kind(player, "winning", (parts_at_win[i] or {}).get(pid))
        serialized["ceiling_outcome_index"] = int(argmax[i])
        serialized["win_conditional_ev"] = float(sum(float(v.get("total") or 0.0) for v in (parts_at_win[i] or {}).values()))
        serialized["average_ev"] = float(avg[i])
        serialized["total_ev"] = float(avg[i])
        serialized["ceiling_points"] = float(ceil[i])
        serialized["ceiling_probability"] = float(ceil_p[i])
        serialized["outcome_wins"] = float(wins_count[i])
        serialized["winning_outcome_indexes"] = wins_idx[i]
        serialized["outcome_win_probability"] = float(wins_prob[i])
        serialized["mode"] = mode
        teams_out.append(serialized)

    if mode == "single_outcome":
        teams_out.sort(key=lambda t: (float(t.get("ceiling_points", 0.0)), float(t.get("average_ev", 0.0))), reverse=True)
    elif mode == "most_outcomes":
        teams_out.sort(
            key=lambda t: (float(t.get("outcome_win_probability", 0.0)), float(t.get("outcome_wins", 0.0)), float(t.get("average_ev", 0.0))),
            reverse=True,
        )
    else:
        teams_out.sort(key=lambda t: float(t.get("average_ev", 0.0)), reverse=True)
    return {
        "top_teams": teams_out[:10],
        "all_teams": teams_out,
        "player_count": n,
        "processed_combinations": int(len(teams_out)),
        "total_combinations": int(total_combinations),
        "outcomes_scored": int(N),
        "mode": mode,
        "plans": bool(plans_on),
        "average_exact": bool(avg_exact) if plans_on else False,
        # ceiling / most-likely are scored among the candidates (every roster
        # is evaluated under its own plan, so the winner of an outcome is only
        # defined among rosters that were planned)
        "approximate": True,
    }


def _run_playoff_best_team_job(job_id: str, payload: dict | None = None) -> None:
    def _update_progress(processed: int, total: int) -> None:
        with PLAYOFF_BEST_TEAM_JOBS_LOCK:
            job = PLAYOFF_BEST_TEAM_JOBS.get(job_id)
            if not job:
                return
            job["processed_combinations"] = int(processed)
            job["total_combinations"] = int(total)
            job["progress"] = 0.0 if total <= 0 else float(processed) / float(total)
            job["updated_at"] = time.time()

    with PLAYOFF_BEST_TEAM_JOBS_LOCK:
        job = PLAYOFF_BEST_TEAM_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["updated_at"] = time.time()

    try:
        body = payload or {}
        latest = load_latest_playoff(body.get("variant"), body.get("event_id"))
        if not latest:
            raise HTTPException(status_code=404, detail="No stored playoff simulation found. Run Playoff Bracket first.")

        options = parse_optimizer_payload(body)
        budget = options["budget"]
        max_per_team = options["max_per_team"]
        include = options["include"]
        exclude = options["exclude"]
        mode = str(body.get("mode") or "average").strip().lower()
        if mode not in {"average", "single_outcome", "most_outcomes"}:
            mode = "average"

        latest_results = latest.get("results", {}) or {}
        sim_results = latest_results.get("teams", {}) or {}
        players_info = _build_players_info_from_sim_results(sim_results, exclude)
        result = _optimize_playoff_teams_by_outcomes(
            players_info,
            latest_results,
            include,
            budget,
            max_per_team,
            mode,
            progress_callback=_update_progress,
        )
        with PLAYOFF_BEST_TEAM_JOBS_LOCK:
            job = PLAYOFF_BEST_TEAM_JOBS.get(job_id)
            if job:
                job["phase"] = "saving"
                job["updated_at"] = time.time()
        save_latest_playoff_best_team(body, result, body.get("variant"))

        with PLAYOFF_BEST_TEAM_JOBS_LOCK:
            job = PLAYOFF_BEST_TEAM_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "completed"
            job["result"] = result
            total = int(job.get("total_combinations", 0))
            done = int(job.get("processed_combinations", 0))
            if done <= 0 and total <= 0:
                done = int(result.get("processed_combinations", 0))
                total = int(result.get("total_combinations", 0))
            job["processed_combinations"] = done
            job["total_combinations"] = total
            job["progress"] = 0.0 if total <= 0 else min(1.0, float(done) / float(total))
            job["updated_at"] = time.time()
    except Exception as exc:
        with PLAYOFF_BEST_TEAM_JOBS_LOCK:
            job = PLAYOFF_BEST_TEAM_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "failed"
            job["error"] = str(exc)
            job["updated_at"] = time.time()


@router.post("/best-team")
def best_team_playoff(payload: dict):
    """
    Optimise a fantasy roster of 5 for the playoff bracket (Monte Carlo over the bracket).
    """
    slots: List[int] = payload.get("team_slots") or []
    if len(slots) not in _ALLOWED_BRACKET_SIZES:
        raise HTTPException(status_code=400, detail="team_slots must contain 2, 4, 8, or 16 team IDs")

    options = parse_optimizer_payload(payload)
    budget = options["budget"]
    max_per_team = options["max_per_team"]
    include = options["include"]
    exclude = options["exclude"]
    mode = str(payload.get("mode") or "average").strip().lower()
    if mode not in {"average", "single_outcome", "most_outcomes"}:
        mode = "average"

    if mode == "average":
        sim_results = simulate_playoff_fantasy(slots, return_runs=False)
        players_info = _build_players_info_from_sim_results(sim_results, exclude)
        return _optimize_playoff_teams_by_outcomes(players_info, {}, include, budget, max_per_team, mode)
    exact_players, _best_bracket, _outcomes_count, outcomes, extra = _bracket_player_totals(
        slots,
        {tid: 999 for tid in slots},
        has_third_place_decider=bool(payload.get("has_third_place_decider", False)),
    )
    players_info = _build_players_info_from_sim_results(exact_players, exclude)
    fresh = {"outcomes": outcomes}
    if extra:
        fresh.update(extra)
    return _optimize_playoff_teams_by_outcomes(players_info, fresh, include, budget, max_per_team, mode)


def _stored_outcome_detail(latest: dict, idx: int) -> dict:
    """One stored outcome as a full dict (bracket + per-player breakdown):
    the stored record when it carries a breakdown, else the code replayed
    deterministically (double elimination or knock-out)."""
    results = latest.get("results") or {}
    lp = latest.get("payload") or {}
    team_slots = [int(x) for x in (lp.get("team_slots") or [])]
    outcomes = results.get("outcomes") or []
    if idx < 0 or idx >= len(outcomes):
        raise HTTPException(status_code=400, detail="Outcome index out of range for the stored run.")
    stored = outcomes[idx]
    if stored.get("player_breakdown"):
        return stored
    vrs = {tid: 999 for tid in team_slots}
    code = int(stored.get("code") if stored.get("code") is not None else idx)
    if _bracket_kind(lp.get("bracket_kind")) == "double":
        template = results.get("bracket_template") or _double8_template(team_slots)
        br = _decode_double8_code(code, template)
        flat = [next(m["winner"] for m in br[stage] if m["key"] == key) for key, stage, _fa, _fb in _DOUBLE8_MATCHES]
        return _deterministic_double8_outcome(team_slots, vrs, flat, grand_final_bo=int(lp.get("final_bo") or 3))
    template = results.get("bracket_template") or {
        "team_slots": team_slots,
        "third_place": bool(lp.get("has_third_place_decider")),
        "rounds": int(round(math.log2(max(len(team_slots), 2)))),
        "final_bo": int(lp.get("final_bo") or 3),
    }
    br = _decode_bracket_code(code, template)
    round_winners: List[List[int]] = []
    alive = list(team_slots)
    while len(alive) > 1:
        ms = br.get(_round_name_for(len(alive))) or []
        round_winners.append([int(m["winner"]) for m in ms])
        alive = [int(m["winner"]) for m in ms]
    third = int(((br.get("third_place") or [{}])[0]).get("winner") or 0)
    return _deterministic_completed_outcome(
        team_slots, vrs, round_winners, bool(template.get("third_place")), third, final_bo=int(lp.get("final_bo") or 3)
    )


@router.post("/outcome-player-detail")
def outcome_player_detail(payload: dict | None = None, event_id: int | None = None):
    """A player's points match by match in one stored outcome (the roster's
    best bracket), under the roster's plan when role_id / booster_assignments
    are given: the assigned role's per-match points replace the player's own
    (pads included), and each match slot pays the roster's booster for it."""
    from backend.services.role_assignment import extract_role_scores_for_player
    from backend.services.swiss_booster_assignment import parse_booster_rates

    body = dict(payload or {})
    if event_id:
        body["event_id"] = int(event_id)
    latest = load_latest_playoff(body.get("variant"), body.get("event_id"))
    if not latest:
        raise HTTPException(status_code=404, detail="No stored playoff simulation found. Run Playoff Bracket first.")
    pid = int(body.get("player_id") or 0)
    if body.get("outcome_index") is None:
        raise HTTPException(status_code=400, detail="outcome_index is required")
    idx = int(body.get("outcome_index"))
    outcome = _stored_outcome_detail(latest, idx)
    rows = [dict(r) for r in ((outcome.get("player_breakdown") or {}).get(str(pid)) or [])]
    if not rows:
        raise HTTPException(status_code=404, detail="That player has no rows in this bracket.")
    prow = get_player(pid) or {}
    role_id = body.get("role_id")
    role_pm = None
    major = minor = None
    if role_id is not None and str(role_id).strip() != "":
        role_id = int(role_id)
        scores = extract_role_scores_for_player(prow)
        role_pm = float(scores.get(role_id, 0.0))
        try:
            roles_obj = json.loads(prow.get("roles_json") or "{}")
            rr = roles_obj.get(str(role_id)) or {}
            major, minor = float(rr.get("major") or 0.0), float(rr.get("minor") or 0.0)
        except Exception:
            major = minor = None
    assignments = [a for a in (body.get("booster_assignments") or []) if int(a.get("player_id") or 0) == pid]
    rates = parse_booster_rates(prow.get("boosters_json")) if assignments else {}
    by_slot = {int(a.get("match_number") or 0): int(a.get("booster_id")) for a in assignments if a.get("booster_id") is not None}
    for r in rows:
        real = r.get("match_number") is not None
        if role_pm is not None and (real or str(r.get("match_type") or "").upper() == "PADDING"):
            r["role_points"] = float(role_pm)
            r["role_id"] = role_id
            if major is not None:
                r["role_major_pct"] = major
                r["role_minor_pct"] = minor
        if assignments and real:
            bid = by_slot.get(int(r["match_number"]))
            if bid is None:
                r.update({"booster_id": None, "booster_name": "No booster left", "booster_trigger_rate": 0.0, "booster_points": 0.0})
            else:
                rate = float(rates.get(bid, 0.0))
                r.update({"booster_id": bid, "booster_name": BOOSTER_NAMES.get(bid, f"Booster {bid}"), "booster_trigger_rate": rate, "booster_points": BOOSTER_POINT_VALUE * rate})
        r["total_points"] = float(r.get("rating_points") or 0.0) + float(r.get("win_points") or 0.0) + float(r.get("role_points") or 0.0) + float(r.get("booster_points") or 0.0)
    comps = {
        "rating": sum(float(r.get("rating_points") or 0.0) for r in rows),
        "win": sum(float(r.get("win_points") or 0.0) for r in rows),
        "role": sum(float(r.get("role_points") or 0.0) for r in rows),
        "booster": sum(float(r.get("booster_points") or 0.0) for r in rows),
    }
    comps["total"] = comps["rating"] + comps["win"] + comps["role"] + comps["booster"]
    return {
        "outcome_index": idx,
        "probability": float(outcome.get("probability") or 0.0),
        "bracket": outcome.get("bracket") or {},
        "rows": rows,
        "components": comps,
    }


@router.post("/winning-player-detail")
def winning_player_detail(payload: dict | None = None, event_id: int | None = None):
    """A player's expected points per stage over a set of stored outcomes (the
    brackets a roster wins), weighted by outcome probability, under the
    roster's plan when role_id / booster_assignments are given. Returned in the
    Point Sources modal's contract (stage cells with boosters and opponents,
    team rounds with play / win / elimination credit / byes, DE layout)."""
    from backend.services.role_assignment import extract_role_scores_for_player
    from backend.services.swiss_booster_assignment import parse_booster_rates

    body = dict(payload or {})
    if event_id:
        body["event_id"] = int(event_id)
    latest = load_latest_playoff(body.get("variant"), body.get("event_id"))
    if not latest:
        raise HTTPException(status_code=404, detail="No stored playoff simulation found. Run Playoff Bracket first.")
    results = latest.get("results") or {}
    lp = latest.get("payload") or {}
    team_slots = [int(x) for x in (lp.get("team_slots") or [])]
    pid = int(body.get("player_id") or 0)
    idxs = [int(x) for x in (body.get("outcome_indexes") or [])]
    outcomes = results.get("outcomes") or []
    idxs = [i for i in idxs if 0 <= i < len(outcomes)]
    if not idxs:
        raise HTTPException(status_code=400, detail="outcome_indexes is required")
    tid = None
    for t, tdata in (results.get("teams") or {}).items():
        if str(pid) in {str(k) for k in (tdata.get("players") or {})}:
            tid = int(t)
            break
    if tid is None or tid not in team_slots:
        raise HTTPException(status_code=404, detail="That player is not in the stored bracket.")
    double = _bracket_kind(lp.get("bracket_kind")) == "double"
    final_bo = int(lp.get("final_bo") or 3)
    template = results.get("bracket_template") or (
        _double8_template(team_slots)
        if double
        else {"team_slots": team_slots, "third_place": bool(lp.get("has_third_place_decider")), "rounds": int(round(math.log2(max(len(team_slots), 2)))), "final_bo": int(lp.get("final_bo") or 3)}
    )
    # the rosters as the run valued them (the event's own), not today's teams table
    with event_rosters.for_event(_event_key(body.get("event_id"))):
        base_states = initialize_teams(team_slots, {t: 999 for t in team_slots})
        player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    prob_cache: Dict[tuple[int, int], float] = {}
    memo: Dict[tuple, dict] = {}

    def pairing(a: int, b: int, w: int, na: int, nb: int, rem: int, match_type: str = "bo3") -> dict:
        key = (a, b, w, na, nb, rem, match_type)
        hit = memo.get(key)
        if hit is not None:
            return hit
        states = {a: _fresh_bracket_state(base_states, a, na - 1), b: _fresh_bracket_state(base_states, b, nb - 1)}
        _play_match_deterministic(
            states, a, b, w, remaining_rounds_after=rem, prob_cache=prob_cache,
            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id, match_type=match_type,
        )
        rows: Dict[int, dict] = {}
        for ts in states.values():
            for p_id, p in ts.players.items():
                real = next((r for r in p.point_breakdown if r.get("match_number") is not None), {})
                rows[int(p_id)] = {
                    "rating": float(real.get("rating_points") or 0.0),
                    "win": float(real.get("win_points") or 0.0),
                    "role": float(real.get("role_points") or 0.0),
                    "booster": float(real.get("booster_points") or 0.0),
                    "booster_id": real.get("booster_id"),
                    "booster_name": real.get("booster_name"),
                    "booster_rate": float(real.get("booster_trigger_rate") or 0.0),
                    "opponent_rank": real.get("opponent_rank"),
                }
        memo[key] = rows
        return rows

    # plan adjustments
    prow = get_player(pid) or {}
    role_pm = None
    role_id = body.get("role_id")
    if role_id is not None and str(role_id).strip() != "":
        role_id = int(role_id)
        role_pm = float(extract_role_scores_for_player(prow).get(role_id, 0.0))
    assignments = [a for a in (body.get("booster_assignments") or []) if int(a.get("player_id") or 0) == pid]
    rates = parse_booster_rates(prow.get("boosters_json")) if assignments else {}
    by_slot = {int(a.get("match_number") or 0): int(a.get("booster_id")) for a in assignments if a.get("booster_id") is not None}

    def adjust(row: dict, slot: int, is_pad: bool) -> dict:
        r = dict(row)
        if role_pm is not None:
            r["role"] = float(role_pm)
        if assignments and not is_pad:
            bid = by_slot.get(int(slot))
            if bid is None:
                r.update({"booster": 0.0, "booster_id": None, "booster_name": "No booster left", "booster_rate": 0.0})
            else:
                rate = float(rates.get(bid, 0.0))
                r.update({"booster": BOOSTER_POINT_VALUE * rate, "booster_id": bid, "booster_name": BOOSTER_NAMES.get(bid, f"Booster {bid}"), "booster_rate": rate})
        if is_pad:
            r.update({"booster": 0.0, "booster_id": None, "booster_name": None, "booster_rate": 0.0})
        return r

    if double:
        stages = list(_DOUBLE8_STAGES)
        missed_after = _DOUBLE8_MISSED_AFTER
    else:
        n = len(team_slots)
        names: List[str] = []
        k = n
        while k > 1:
            names.append(_round_name_for(k))
            k //= 2
        stages = names + (["third_place"] if template.get("third_place") else [])
        missed_after = {st: [x for x in names[names.index(st) + 1 :]] for st in names}

    cells = {st: {"rating": 0.0, "win": 0.0, "role": 0.0, "booster": 0.0, "total": 0.0} for st in stages}
    play = {st: 0.0 for st in stages}
    wins = {st: 0.0 for st in stages}
    bye = {st: 0.0 for st in stages}
    elim = {st: [0.0, 0.0] for st in stages}
    opp_round: Dict[str, Dict[int, List[float]]] = {st: {} for st in stages}
    opp_pts: Dict[str, Dict[int, List[float]]] = {st: {} for st in stages}
    boost: Dict[str, Dict[tuple, float]] = {st: {} for st in stages}
    ranks: Dict[int, Any] = {}
    mass = 0.0
    # the team's runs through the bracket over these outcomes (the clear view)
    from backend.services import player_runs

    run_map: Dict[tuple, Dict[str, Any]] = {}
    cur: Dict[str, Any] = {"matches": [], "pads": []}

    def note_match(stage: str, row: dict, opp: int, won: bool, slot: int, rem: int) -> None:
        pen = (-3.0 * len(missed_after.get(stage, [])[: max(0, rem)])) if (not won and rem > 0) else 0.0
        cur["matches"].append({
            "stage": stage, "slot": slot, "opponent_team_id": int(opp), "won": bool(won),
            "rating": float(row["rating"]), "win": float(row["win"]), "role": float(row["role"]), "booster": float(row["booster"]),
            "booster_name": row.get("booster_name") if row.get("booster_id") is not None else None,
            "total": float(row["rating"]) + float(row["win"]) + float(row["role"]) + float(row["booster"]), "penalty": pen,
        })

    def add_match(stage: str, row: dict, opp: int, won: bool, weight: float) -> None:
        c = cells[stage]
        c["rating"] += weight * row["rating"]
        c["win"] += weight * row["win"]
        c["role"] += weight * row["role"]
        c["booster"] += weight * row["booster"]
        c["total"] += weight * (row["rating"] + row["win"] + row["role"] + row["booster"])
        play[stage] += weight
        if won:
            wins[stage] += weight
        oc = opp_round[stage].setdefault(opp, [0.0, 0.0])
        oc[0] += weight
        if won:
            oc[1] += weight
        op = opp_pts[stage].setdefault(opp, [0.0, 0.0, 0.0, 0.0])
        op[0] += weight * row["rating"]
        op[1] += weight * row["win"]
        op[2] += weight * row["role"]
        op[3] += weight * row["booster"]
        if row.get("booster_id") is not None:
            bk = (row["booster_id"], row.get("booster_name"), float(row.get("booster_rate") or 0.0), int(row.get("slot") or 0))
            boost[stage][bk] = boost[stage].get(bk, 0.0) + weight
        if row.get("opponent_rank") is not None:
            ranks[opp] = row.get("opponent_rank")

    def add_pad(stage: str, pad: dict, weight: float) -> None:
        c = cells[stage]
        c["rating"] += weight * pad["rating"]
        c["win"] += weight * pad["win"]
        c["role"] += weight * pad["role"]
        c["total"] += weight * (pad["rating"] + pad["win"] + pad["role"])
        bye[stage] += weight

    def add_penalty(stage: str, rem: int, weight: float) -> None:
        for later in missed_after.get(stage, [])[: max(0, rem)]:
            cells[later]["win"] -= 3.0 * weight
            cells[later]["total"] -= 3.0 * weight
            elim[later][0] += weight
            elim[later][1] += -3.0 * weight

    for idx in idxs:
        stored = outcomes[idx]
        p_c = float(stored.get("probability") or 0.0)
        if p_c <= 0:
            continue
        mass += p_c
        code = int(stored.get("code") if stored.get("code") is not None else idx)
        played = {t: 0 for t in team_slots}
        real_rows: List[dict] = []
        cur = {"matches": [], "pads": []}
        if double:
            br = _decode_double8_code(code, template)
            for key, stage, _fa, _fb in _DOUBLE8_MATCHES:
                m = next(x for x in br[stage] if x["key"] == key)
                a, b = int(m["teams"][0]), int(m["teams"][1])
                w = int(m["winner"])
                na, nb = played[a] + 1, played[b] + 1
                played[a] += 1
                played[b] += 1
                rem = _DOUBLE8_REM[stage]
                if tid in (a, b):
                    slot = na if tid == a else nb
                    prow_m = pairing(
                        a, b, w, na, nb, rem,
                        _bo_type(final_bo) if (stage == "grand_final" or (not double and rem == 0 and stage != "third_place")) else "bo3",
                    ).get(pid)
                    if prow_m is None:
                        continue  # the player is not on the team's roster for this run
                    row = adjust(prow_m, slot, False)
                    row["slot"] = slot
                    real_rows.append(row)
                    add_match(stage, row, b if tid == a else a, w == tid, p_c)
                    note_match(stage, row, b if tid == a else a, w == tid, slot, rem)
                    if w != tid and rem > 0:
                        add_penalty(stage, rem, p_c)
                pad_spec = _DOUBLE8_PAD_AFTER.get(stage)
                if pad_spec and w == tid:
                    games = float(pad_spec[1])
                    pad = {
                        "rating": sum(r["rating"] for r in real_rows) / games,
                        "win": sum(r["win"] for r in real_rows) / games,
                        "role": (float(role_pm) if role_pm is not None else sum(r["role"] for r in real_rows) / games),
                    }
                    add_pad(pad_spec[0], pad, p_c)
                    cur["pads"].append({
                        "stage": pad_spec[0], "label": "Bye", "rating": pad["rating"], "win": pad["win"], "role": pad["role"],
                        "booster": 0.0, "total": pad["rating"] + pad["win"] + pad["role"],
                    })
        else:
            br = _decode_bracket_code(code, template)
            rounds_total = int(template.get("rounds") or len([s for s in stages if s != "third_place"]))
            for r_i, stage in enumerate(stages):
                for m in br.get(stage) or []:
                    a, b = int(m["teams"][0]), int(m["teams"][1])
                    w = int(m["winner"])
                    na, nb = played[a] + 1, played[b] + 1
                    played[a] += 1
                    played[b] += 1
                    rem = 0 if stage == "third_place" else max(0, rounds_total - r_i - 1)
                    if tid in (a, b):
                        slot = na if tid == a else nb
                        prow_m = pairing(
                            a, b, w, na, nb, rem,
                            _bo_type(final_bo) if (stage == "grand_final" or (not double and rem == 0 and stage != "third_place")) else "bo3",
                        ).get(pid)
                        if prow_m is None:
                            continue
                        row = adjust(prow_m, slot, False)
                        row["slot"] = slot
                        add_match(stage, row, b if tid == a else a, w == tid, p_c)
                        note_match(stage, row, b if tid == a else a, w == tid, slot, rem)
                        if w != tid and rem > 0:
                            add_penalty(stage, rem, p_c)
        player_runs.add_run(
            run_map, cur["matches"], cur["pads"], sum(float(m.get("penalty") or 0.0) for m in cur["matches"]), p_c,
        )

    if mass <= 0:
        raise HTTPException(status_code=400, detail="The given outcomes carry no probability.")
    runs_out = player_runs.finish_runs(run_map, mass)
    stage_ev: Dict[str, Dict[str, Any]] = {}
    team_rounds: Dict[str, Dict[str, Any]] = {}
    for st in stages:
        entry: Dict[str, Any] = {k: v / mass for k, v in cells[st].items()}
        bt = sum(boost[st].values())
        entry["boosters"] = (
            sorted(
                [{"booster_id": bid, "booster_name": name, "booster_rate": rate, "slot": slot, "share": m / bt} for (bid, name, rate, slot), m in boost[st].items()],
                key=lambda x: -x["share"],
            )
            if bt > 0
            else []
        )
        entry["opponents"] = {
            str(o): {"rating": v[0] / mass, "win": v[1] / mass, "role": v[2] / mass, "booster": v[3] / mass, "total": sum(v) / mass}
            for o, v in opp_pts[st].items()
        }
        stage_ev[st] = entry
        team_rounds[st] = {
            "play": play[st] / mass,
            "win": (wins[st] / play[st]) if play[st] > 0 else 0.0,
            "opponents": {str(o): {"play": c[0] / mass, "win": (c[1] / c[0]) if c[0] > 0 else 0.0} for o, c in opp_round[st].items()},
            "elim_before": {"prob": elim[st][0] / mass, "points": elim[st][1] / mass},
            "bye": bye[st] / mass,
        }
    comps = {k: sum(stage_ev[st][k] for st in stages) for k in ("rating", "win", "role", "booster", "total")}
    return {
        "n_outcomes": len(idxs),
        "mass": mass,
        "stages": stages,
        "layout": [dict(x) for x in _DOUBLE8_LAYOUT] if double else None,
        "stage_ev": stage_ev,
        "team_rounds": team_rounds,
        "team_ranks": {str(t): r for t, r in ranks.items()},
        "components": comps,
        "team_id": tid,
        "runs": runs_out["runs"],
        "runs_rest": runs_out["rest"],
    }


@router.post("/best-team/from-latest")
def best_team_playoff_from_latest(payload: dict | None = None):
    body = payload or {}
    latest = load_latest_playoff(body.get("variant"), body.get("event_id"))
    if not latest:
        raise HTTPException(status_code=404, detail="No stored playoff simulation found. Run Playoff Bracket first.")
    options = parse_optimizer_payload(body)
    budget = options["budget"]
    max_per_team = options["max_per_team"]
    include = options["include"]
    exclude = options["exclude"]
    mode = str(body.get("mode") or "average").strip().lower()
    if mode not in {"average", "single_outcome", "most_outcomes"}:
        mode = "average"

    latest_results = latest.get("results", {}) or {}
    sim_results = latest_results.get("teams", {}) or {}
    players_info = _build_players_info_from_sim_results(sim_results, exclude)
    return _optimize_playoff_teams_by_outcomes(
        players_info,
        latest_results,
        include,
        budget,
        max_per_team,
        mode,
    )


def _completed_bracket_round_winners(payload: dict, team_slots: List[int]) -> tuple[List[List[int]], int]:
    """Normalise the completed-bracket picks into per-round winner lists in
    bracket order (round 0 first, final last). Accepts either a general
    ``round_winners`` field (list-of-lists, used by every field size) or the
    legacy 8-team ``qf_winners``/``sf_winners``/``final_winner`` fields.
    Returns ``(round_winners, third_place_winner)``."""
    n = len(team_slots)
    total_rounds = int(round(math.log2(n)))
    third_place_winner = int(payload.get("third_place_winner") or 0)

    raw_rounds = payload.get("round_winners")
    if raw_rounds:
        rounds = [[int(x) for x in (r or [])] for r in raw_rounds]
    else:
        rounds = []
        for key in ("r16_winners", "qf_winners", "sf_winners"):
            vals = payload.get(key)
            if vals:
                rounds.append([int(x) for x in vals])
        final_winner = int(payload.get("final_winner") or 0)
        if final_winner > 0:
            rounds.append([final_winner])

    if len(rounds) != total_rounds:
        raise HTTPException(
            status_code=400,
            detail=f"Completed bracket needs winners for all {total_rounds} rounds; got {len(rounds)}.",
        )
    expected = n // 2
    for idx, round_win in enumerate(rounds):
        if len(round_win) != expected:
            raise HTTPException(
                status_code=400,
                detail=f"Round {idx + 1} needs {expected} winner(s); got {len(round_win)}.",
            )
        expected //= 2
    return rounds, third_place_winner


def _deterministic_completed_outcome(
    team_slots: List[int],
    vrs_ranks: Dict[int, int],
    round_winners: List[List[int]],
    has_third_place_decider: bool,
    third_place_winner: int,
    final_bo: int = 3,
) -> Dict:
    """Play the single bracket implied by the user's picks and return one outcome
    dict in the same shape as a stored exact/Monte-Carlo outcome. Because it
    computes the picked bracket directly (rather than looking it up in the
    sampled outcome table), the Completed Bracket picker works for any field
    size — essential for Monte-Carlo 16-team runs, where the exact bracket is
    almost never among the samples. For 8-team fields this reuses the same
    ``_play_match_deterministic`` path as the exact enumerator, so the scores are
    byte-identical to the old lookup."""
    states = initialize_teams(team_slots, vrs_ranks)
    player_rows_by_id, team_rank_by_id = _build_playoff_lookup_context(team_slots)
    prob_cache: Dict[tuple[int, int], float] = {}
    n = len(team_slots)
    total_rounds = int(round(math.log2(n)))

    match_results: Dict[str, List[dict]] = {}
    path_prob = 1.0
    current = list(team_slots)
    semi_losers: List[int] = []
    for round_idx in range(total_rounds):
        teams_in_round = len(current)
        rname = _round_name_for(teams_in_round)
        remaining_after = total_rounds - round_idx - 1
        winners_this_round = round_winners[round_idx]
        winners, matches = [], []
        for i in range(0, len(current), 2):
            a, b = current[i], current[i + 1]
            picked = int(winners_this_round[i // 2])
            if picked not in (a, b):
                raise HTTPException(
                    status_code=400,
                    detail=f"{rname}: winner {picked} is not in match ({a} vs {b}).",
                )
            w, l, p_win_a, branch_p = _play_match_deterministic(
                states, a, b, picked, remaining_rounds_after=remaining_after,
                prob_cache=prob_cache, player_rows_by_id=player_rows_by_id,
                team_rank_by_id=team_rank_by_id,
                match_type=_bo_type(final_bo) if remaining_after == 0 else "bo3",
            )
            path_prob *= branch_p
            winners.append(w)
            matches.append({"winner": w, "loser": l, "p_win_a": p_win_a, "teams": [a, b]})
            if teams_in_round == 4:
                semi_losers.append(l)
        match_results[rname] = matches
        current = winners

    if has_third_place_decider and len(semi_losers) == 2:
        if third_place_winner not in semi_losers:
            raise HTTPException(
                status_code=400,
                detail="Third-place winner must be one of the two semi-final losers.",
            )
        w, l, p_win_a, branch_p = _play_match_deterministic(
            states, semi_losers[0], semi_losers[1], third_place_winner,
            remaining_rounds_after=0, prob_cache=prob_cache,
            player_rows_by_id=player_rows_by_id, team_rank_by_id=team_rank_by_id,
        )
        path_prob *= branch_p
        match_results["third_place"] = [
            {"winner": w, "loser": l, "p_win_a": p_win_a, "teams": list(semi_losers)}
        ]

    player_points: Dict[str, float] = {}
    player_components: Dict[str, Dict[str, float]] = {}
    player_breakdown: Dict[str, List[dict]] = {}
    for ts in states.values():
        for pid, p in ts.players.items():
            player_points[str(pid)] = float(p.total_points)
            player_components[str(pid)] = {
                "total": float(p.total_points),
                "total_without_booster": float(
                    p.rating_points_total + p.win_points_total + p.role_points_total
                ),
                "rating": float(p.rating_points_total),
                "win": float(p.win_points_total),
                "role": float(p.role_points_total),
                "booster": float(p.booster_points_total),
            }
            player_breakdown[str(pid)] = [dict(row) for row in p.point_breakdown]

    return {
        "probability": float(path_prob),
        "bracket": match_results,
        "players": player_points,
        "player_components": player_components,
        "player_breakdown": player_breakdown,
    }


def _outcome_matches_completed_bracket(outcome: Dict, picks: Dict[str, object]) -> bool:
    bracket = outcome.get("bracket") or {}
    qf_winners = [int(x) for x in (picks.get("qf_winners") or [])]
    sf_winners = [int(x) for x in (picks.get("sf_winners") or [])]
    final_winner = int(picks.get("final_winner") or 0)
    third_place_winner = int(picks.get("third_place_winner") or 0)
    quarters = bracket.get("quarters") or []
    semis = bracket.get("semis") or []
    finals = bracket.get("final") or []
    third_place = bracket.get("third_place") or []
    if len(qf_winners) != 4 or len(sf_winners) != 2 or final_winner <= 0:
        return False
    if len(quarters) < 4 or len(semis) < 2 or not finals:
        return False
    if [int(row.get("winner") or 0) for row in quarters[:4]] != qf_winners:
        return False
    if [int(row.get("winner") or 0) for row in semis[:2]] != sf_winners:
        return False
    if int(finals[0].get("winner") or 0) != final_winner:
        return False
    if third_place:
        return third_place_winner > 0 and int(third_place[0].get("winner") or 0) == third_place_winner
    return third_place_winner <= 0


def _selected_completed_outcome_from_latest(payload: dict | None = None) -> tuple[dict, dict]:
    latest = load_latest_playoff((payload or {}).get("variant"), (payload or {}).get("event_id"))
    if not latest:
        raise HTTPException(status_code=404, detail="No stored playoff simulation found. Run Playoff Bracket first.")
    body = payload or {}

    # Bounty brackets use a bespoke QF pairing + SF re-draft, so the picked
    # bracket is looked up among the stored exact outcomes (which were generated
    # with that pairing). Every other variant computes the picked bracket
    # deterministically — that works for any field size and, crucially, for the
    # Monte-Carlo 16-team runs whose sampled table almost never contains the
    # exact bracket the user selected.
    if _variant(body.get("variant")) == "bounty":
        latest_results = latest.get("results", {}) or {}
        outcomes = list(latest_results.get("outcomes") or [])
        if not outcomes:
            raise HTTPException(status_code=400, detail="Stored playoff run has no exact outcome table.")
        for outcome in outcomes:
            if _outcome_matches_completed_bracket(outcome, body):
                return latest, outcome
        raise HTTPException(status_code=404, detail="No stored outcome matches that completed bracket.")

    latest_payload = latest.get("payload", {}) or {}
    team_slots = [int(x) for x in (latest_payload.get("team_slots") or [])]
    if not team_slots:
        raise HTTPException(status_code=400, detail="Stored playoff run is missing its team slots.")
    vrs_ranks = {tid: 999 for tid in team_slots}
    if _bracket_kind(latest_payload.get("bracket_kind")) == "double":
        picks = [int(x) for x in (body.get("match_winners") or [])]
        return latest, _deterministic_double8_outcome(
            team_slots, vrs_ranks, picks, grand_final_bo=int(latest_payload.get("final_bo") or 3)
        )
    has_third_place = bool(latest_payload.get("has_third_place_decider", False))
    round_winners, third_place_winner = _completed_bracket_round_winners(body, team_slots)
    outcome = _deterministic_completed_outcome(
        team_slots, vrs_ranks, round_winners, has_third_place, third_place_winner,
        final_bo=int(latest_payload.get("final_bo") or 3),
    )
    return latest, outcome


def _player_team_map_from_latest(latest_results: Dict, latest_payload: Dict) -> Dict[int, int]:
    pid_to_team_id: Dict[int, int] = {}
    for tid_raw, team_data in (latest_results.get("teams") or {}).items():
        try:
            tid = int(tid_raw)
        except Exception:
            continue
        for pid_raw in ((team_data or {}).get("players") or {}).keys():
            try:
                pid_to_team_id[int(pid_raw)] = tid
            except Exception:
                continue

    for tid_raw in latest_payload.get("team_slots") or []:
        try:
            tid = int(tid_raw)
        except Exception:
            continue
        team = get_team_by_id(tid) or {}
        override = event_rosters.roster_for(tid)  # the event's own roster when it lists the team
        roster = list(override) if override is not None else [team.get(key) for key in ("player1_id", "player2_id", "player3_id", "player4_id", "player5_id")]
        for pid_raw in roster:
            try:
                pid = int(pid_raw or 0)
            except Exception:
                continue
            if pid > 0:
                pid_to_team_id.setdefault(pid, tid)
    return pid_to_team_id


def _compute_completed_bracket_from_latest(payload: dict | None = None, progress_callback=None) -> dict:
    body = payload or {}
    latest, selected = _selected_completed_outcome_from_latest(body)
    latest_results = latest.get("results", {}) or {}
    latest_payload = latest.get("payload", {}) or {}
    outcomes = list(latest_results.get("outcomes") or [])

    options = parse_optimizer_payload(body)
    components_by_pid = {
        int(pid): dict(comps or {})
        for pid, comps in ((selected.get("player_components") or {}).items())
    }
    breakdown_by_pid = {
        int(pid): list(rows or [])
        for pid, rows in ((selected.get("player_breakdown") or {}).items())
    }
    scores_by_pid = {
        int(pid): float(
            (components_by_pid.get(int(pid)) or {}).get(
                "total_without_booster",
                float((components_by_pid.get(int(pid)) or {}).get("rating", 0.0) or 0.0)
                + float((components_by_pid.get(int(pid)) or {}).get("win", 0.0) or 0.0)
                + float((components_by_pid.get(int(pid)) or {}).get("role", 0.0) or 0.0),
            )
        )
        for pid in (selected.get("players") or {}).keys()
    }
    pid_to_team_id = _player_team_map_from_latest(latest_results, latest_payload)
    players_info = []
    player_values = []
    for pid, score in scores_by_pid.items():
        if pid in options["exclude"]:
            continue
        row = ((latest_results.get("input_snapshot") or {}).get("players") or {}).get(str(int(pid))) or get_player(pid)  # the run's own prices
        if not row:
            continue
        comps = components_by_pid.get(pid) or {}
        components_available = bool(comps)
        rating_score = float(comps.get("rating", score if not comps else 0.0) or 0.0)
        win_score = float(comps.get("win", 0.0) or 0.0)
        role_score = float(comps.get("role", 0.0) or 0.0)
        booster_score = float(comps.get("booster", 0.0) or 0.0)
        price = int(row.get("price") or 0)
        team_id = int(pid_to_team_id.get(pid) or 0)
        players_info.append(
            {
                "player_id": pid,
                "name": row.get("name", f"Player {pid}"),
                "team_id": team_id,
                "price": price,
                "rating_ev": rating_score,
                "win_ev": win_score,
                "role_ev": role_score,
                "booster_ev": booster_score,
                "raw_booster_ev": booster_score,
                "total_ev": float(score),
                "components_available": components_available,
                "point_breakdown": breakdown_by_pid.get(pid, []),
            }
        )
        player_values.append(
            {
                "player_id": pid,
                "name": row.get("name", f"Player {pid}"),
                "team_id": team_id,
                "price": price,
                "points": float(score),
                "rating": rating_score,
                "win": win_score,
                "role": role_score,
                "booster": booster_score,
                "raw_booster": booster_score,
                "components_available": components_available,
                "point_breakdown": breakdown_by_pid.get(pid, []),
            }
        )

    if not players_info:
        raise HTTPException(status_code=400, detail="Selected bracket outcome has no available players.")
    if not any(int(player.get("team_id") or 0) > 0 for player in players_info):
        raise HTTPException(status_code=400, detail="Could not map selected bracket players back to their teams.")

    result = optimize_rosters(
        players_info,
        options["include"],
        options["budget"],
        options["max_per_team"],
        progress_callback=progress_callback,
        include_error_suffix="in selected bracket outcome",
    )
    # Scored against the one picked bracket: the flat fields hold that
    # outcome's components, so they are the player's `outcome` points.
    for team in result.get("all_teams") or result.get("top_teams") or []:
        for player in team.get("players") or []:
            player["outcome"] = roster_scores.parts_from_flat(player)
    # The completed-bracket UI only consumes top_teams/player_values; keeping
    # every roster would persist and return a second multi-hundred-MB blob.
    result.pop("all_teams", None)
    result["bracket_probability"] = float(selected.get("probability") or 0.0)
    result["bracket"] = selected.get("bracket") or {}
    result["outcomes_count"] = int(latest_results.get("outcomes_count") or len(outcomes))
    result["player_values"] = sorted(player_values, key=lambda row: float(row.get("points") or 0.0), reverse=True)
    result["mode"] = "completed_bracket"
    return result


def _run_completed_bracket_job(job_id: str, payload: dict | None = None) -> None:
    def _update_progress(processed: int, total: int) -> None:
        with PLAYOFF_COMPLETED_BRACKET_JOBS_LOCK:
            job = PLAYOFF_COMPLETED_BRACKET_JOBS.get(job_id)
            if not job:
                return
            job["processed_combinations"] = int(processed)
            job["total_combinations"] = int(total)
            job["progress"] = 0.0 if total <= 0 else float(processed) / float(total)
            job["updated_at"] = time.time()

    with PLAYOFF_COMPLETED_BRACKET_JOBS_LOCK:
        job = PLAYOFF_COMPLETED_BRACKET_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["updated_at"] = time.time()

    try:
        result = _compute_completed_bracket_from_latest(payload or {}, progress_callback=_update_progress)
        save_latest_completed_bracket(payload or {}, result, (payload or {}).get("variant"))
        with PLAYOFF_COMPLETED_BRACKET_JOBS_LOCK:
            job = PLAYOFF_COMPLETED_BRACKET_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "completed"
            job["phase"] = "completed"
            job["result"] = result
            total = int(job.get("total_combinations", 0))
            done = int(job.get("processed_combinations", 0))
            if done <= 0 and total <= 0:
                done = int(result.get("processed_combinations", 0))
                total = int(result.get("total_combinations", 0))
            job["processed_combinations"] = done
            job["total_combinations"] = total
            job["progress"] = 0.0 if total <= 0 else min(1.0, float(done) / float(total))
            job["updated_at"] = time.time()
    except Exception as exc:
        with PLAYOFF_COMPLETED_BRACKET_JOBS_LOCK:
            job = PLAYOFF_COMPLETED_BRACKET_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "failed"
            job["error"] = str(exc)
            job["updated_at"] = time.time()


@router.post("/best-team/bracket-from-latest")
def best_team_for_completed_bracket_from_latest(payload: dict | None = None):
    result = _compute_completed_bracket_from_latest(payload or {})
    save_latest_completed_bracket(payload or {}, result, (payload or {}).get("variant"))
    return result


@router.post("/best-team/bracket-from-latest/start")
def start_completed_bracket_from_latest(payload: dict | None = None):
    body = payload or {}
    latest = load_latest_playoff(body.get("variant"), body.get("event_id"))
    if not latest:
        raise HTTPException(status_code=404, detail="No stored playoff simulation found. Run Playoff Bracket first.")

    with PLAYOFF_COMPLETED_BRACKET_JOBS_LOCK:
        for existing_id, existing_job in PLAYOFF_COMPLETED_BRACKET_JOBS.items():
            if _variant((existing_job.get("payload") or {}).get("variant")) != _variant(body.get("variant")):
                continue
            if existing_job.get("status") in {"queued", "running"}:
                return {"job_id": existing_id, "reused": True}

        job_id = uuid.uuid4().hex
        PLAYOFF_COMPLETED_BRACKET_JOBS[job_id] = {
            "status": "queued",
            "error": "",
            "progress": 0.0,
            "processed_combinations": 0,
            "total_combinations": 0,
            "result": None,
            "payload": body,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    worker = threading.Thread(target=_run_completed_bracket_job, args=(job_id, body), daemon=True)
    worker.start()
    return {"job_id": job_id}


@router.get("/best-team/bracket-from-latest/job/{job_id}")
def get_completed_bracket_job(job_id: str):
    with PLAYOFF_COMPLETED_BRACKET_JOBS_LOCK:
        job = PLAYOFF_COMPLETED_BRACKET_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        out = dict(job)
    return {
        "job_id": job_id,
        "status": out.get("status", "queued"),
        "error": out.get("error", ""),
        "progress": out.get("progress", 0.0),
        "processed_combinations": out.get("processed_combinations", 0),
        "total_combinations": out.get("total_combinations", 0),
        "result_ready": out.get("result") is not None,
        "result": out.get("result"),
    }


@router.get("/best-team/bracket-from-latest/latest")
def get_latest_completed_bracket(variant: str = "main", event_id: int | None = None):
    latest = load_latest_completed_bracket(variant, event_id)
    if not latest:
        return {"exists": False}
    # Rows saved before all_teams was dropped carry every roster; serializing
    # that here breaks the response, and no caller uses it.
    result = dict(latest["result"] or {})
    result.pop("all_teams", None)
    return {
        "exists": True,
        "payload": latest["payload"],
        "result": result,
        "updated_at": latest["updated_at"],
    }


@router.post("/best-team/from-latest/start")
def start_best_team_playoff_from_latest(payload: dict | None = None):
    body = payload or {}
    latest = load_latest_playoff(body.get("variant"), body.get("event_id"))
    if not latest:
        raise HTTPException(status_code=404, detail="No stored playoff simulation found. Run Playoff Bracket first.")

    with PLAYOFF_BEST_TEAM_JOBS_LOCK:
        for existing_id, existing_job in PLAYOFF_BEST_TEAM_JOBS.items():
            if _variant((existing_job.get("payload") or {}).get("variant")) != _variant(body.get("variant")):
                continue
            if existing_job.get("status") in {"queued", "running"}:
                return {"job_id": existing_id, "reused": True}

        job_id = uuid.uuid4().hex
        PLAYOFF_BEST_TEAM_JOBS[job_id] = {
            "status": "queued",
            "error": "",
            "progress": 0.0,
            "processed_combinations": 0,
            "total_combinations": 0,
            "result": None,
            "payload": body,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    worker = threading.Thread(target=_run_playoff_best_team_job, args=(job_id, body), daemon=True)
    worker.start()
    return {"job_id": job_id}


@router.get("/best-team/from-latest/latest")
def get_latest_best_team_playoff_from_latest(variant: str = "main", event_id: int | None = None):
    states = _states(variant)
    key = _event_key(event_id)
    meta = states["meta"].load(key=key)
    if meta:
        summary = meta["result"] or {}
        return {
            "exists": True,
            "payload": meta["payload"],
            "mode": summary.get("mode"),
            "player_count": summary.get("player_count"),
            "total_teams": summary.get("total_teams"),
            "processed_combinations": summary.get("processed_combinations"),
            "total_combinations": summary.get("total_combinations"),
            "approximate": bool(summary.get("approximate")),
            "candidate_count": summary.get("candidate_count"),
            "updated_at": meta["updated_at"],
        }

    # Legacy row saved before the meta table existed. Summarize it with
    # SQLite's JSON functions (C-side parse, no giant Python objects) and
    # persist the summary so this path only ever runs once.
    best_table = states["best"].table
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT payload_json, updated_at FROM {best_table} WHERE singleton_id = ?", (key,)
        ).fetchone()
        if not row:
            return {"exists": False}
        payload = json.loads(row["payload_json"])
        updated_at = float(row["updated_at"])
        summary = {
            "mode": None,
            "player_count": None,
            "total_teams": None,
            "processed_combinations": None,
            "total_combinations": None,
        }
        try:
            extracted = conn.execute(
                f"""
                SELECT json_extract(result_json, '$.mode') AS mode,
                       json_extract(result_json, '$.player_count') AS player_count,
                       json_array_length(result_json, '$.all_teams') AS total_teams,
                       json_extract(result_json, '$.processed_combinations') AS processed_combinations,
                       json_extract(result_json, '$.total_combinations') AS total_combinations
                FROM {best_table} WHERE singleton_id = ?
                """,
                (key,),
            ).fetchone()
            if extracted:
                summary = {key: extracted[key] for key in summary}
        except Exception:
            pass  # blob too large to summarize here; exists/payload still useful
    finally:
        conn.close()
    states["meta"].save(payload, summary, key=key)
    return {
        "exists": True,
        "payload": payload,
        "mode": summary.get("mode"),
        "player_count": summary.get("player_count"),
        "total_teams": summary.get("total_teams"),
        "processed_combinations": summary.get("processed_combinations"),
        "total_combinations": summary.get("total_combinations"),
        "updated_at": updated_at,
    }


@router.post("/best-team/from-latest/query")
def query_latest_best_team_playoff(payload: dict | None = None, event_id: int | None = None):
    body = dict(payload or {})
    if event_id:
        body["event_id"] = int(event_id)
    key = _event_key(body.get("event_id"))
    latest = load_latest_playoff_best_team(body.get("variant"), key)
    if not latest:
        raise HTTPException(status_code=404, detail="No stored team combinations found. Run Combinations first.")
    cache_key = _query_cache_key(body.get("variant"), key, latest["updated_at"], body)
    with _QUERY_CACHE_LOCK:
        cached = _QUERY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    response = _query_saved_combos(body, latest)
    with _QUERY_CACHE_LOCK:
        if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
            _QUERY_CACHE.pop(next(iter(_QUERY_CACHE)))
        _QUERY_CACHE[cache_key] = response
    return response


def _query_saved_combos(body: dict, latest: dict) -> dict:
    options = parse_optimizer_payload(body)
    mode = str(body.get("mode") or "average").strip().lower()
    if mode not in {"average", "single_outcome", "most_outcomes"}:
        mode = "average"
    result = latest["result"] or {}
    teams = list(result.get("all_teams") or [])
    constrained = mode == "most_outcomes" and bool(options["include"] or options["exclude"])
    if constrained:
        # Average and ceiling are per-roster facts, so filtering the stored
        # list is exact; winner odds depend on the field, so re-score the
        # rosters the filters leave (excluded players cannot be picked, the
        # odds sum to 100% over what can).
        teams = _constrained_most_outcomes(body.get("variant"), _event_key(body.get("event_id")), options)
        filtered = _filter_saved_combo_teams(teams, set(), set(), str(body.get("search") or ""))
    else:
        filtered = _filter_saved_combo_teams(
            teams,
            options["include"],
            options["exclude"],
            str(body.get("search") or ""),
        )
    sorted_teams = _sort_saved_combo_teams(filtered, mode, str(body.get("sort") or "ev_desc"))
    page = int(body.get("page") or 0)
    page_size = int(body.get("page_size") or 200)
    return {
        "exists": True,
        "mode": mode,
        "updated_at": latest["updated_at"],
        "total_teams": len(teams),
        "filtered_count": len(sorted_teams),
        "top_teams": sorted_teams[:10],
        "page_teams": _page_items(sorted_teams, page, page_size),
        "page": max(0, page),
        "page_size": max(1, min(500, page_size)),
        "approximate": bool(result.get("approximate")),
        "total_combinations": result.get("total_combinations"),
    }


@router.post("/best-team/from-latest/completed-query")
def query_latest_best_team_for_completed_bracket(payload: dict | None = None, event_id: int | None = None):
    body = dict(payload or {})
    if event_id:
        body["event_id"] = int(event_id)
    latest_combos = load_latest_playoff_best_team(body.get("variant"), body.get("event_id"))
    if not latest_combos:
        raise HTTPException(status_code=404, detail="No stored team combinations found. Run Combinations first.")
    latest_playoff, selected = _selected_completed_outcome_from_latest(body)
    latest_results = latest_playoff.get("results", {}) or {}
    options = parse_optimizer_payload(body)
    components_by_pid = {
        int(pid): dict(comps or {})
        for pid, comps in ((selected.get("player_components") or {}).items())
    }
    breakdown_by_pid = {
        int(pid): list(rows or [])
        for pid, rows in ((selected.get("player_breakdown") or {}).items())
    }
    scores_by_pid = {
        int(pid): (
            float((components_by_pid.get(int(pid)) or {}).get("total_without_booster", 0.0) or 0.0)
            if (components_by_pid.get(int(pid)) or {}).get("total_without_booster") is not None
            else (
                float((components_by_pid.get(int(pid)) or {}).get("rating", 0.0) or 0.0)
                + float((components_by_pid.get(int(pid)) or {}).get("win", 0.0) or 0.0)
                + float((components_by_pid.get(int(pid)) or {}).get("role", 0.0) or 0.0)
            )
        )
        for pid in (selected.get("players") or {}).keys()
    }
    pid_to_team_id = _player_team_map_from_latest(latest_results, latest_playoff.get("payload", {}) or {})

    player_values = []
    for pid, score in scores_by_pid.items():
        row = ((latest_results.get("input_snapshot") or {}).get("players") or {}).get(str(int(pid))) or get_player(pid) or {}  # the run's own prices
        comps = components_by_pid.get(pid) or {}
        player_values.append(
            {
                "player_id": pid,
                "name": row.get("name", f"Player {pid}"),
                "team_id": int(pid_to_team_id.get(pid) or 0),
                "price": int(row.get("price") or 0),
                "points": float(score),
                "rating": float(comps.get("rating", 0.0) or 0.0),
                "win": float(comps.get("win", 0.0) or 0.0),
                "role": float(comps.get("role", 0.0) or 0.0),
                "booster": float(comps.get("booster", 0.0) or 0.0),
                "raw_booster": float(comps.get("booster", 0.0) or 0.0),
                "components_available": bool(comps),
                "point_breakdown": breakdown_by_pid.get(pid, []),
            }
        )

    teams = list((latest_combos.get("result") or {}).get("all_teams") or [])
    filtered = _filter_saved_combo_teams(teams, options["include"], options["exclude"], str(body.get("search") or ""))
    scored = []
    for team in filtered:
        players = []
        total = 0.0
        for player in team.get("players") or []:
            pid = int(player.get("player_id") or 0)
            score = float(scores_by_pid.get(pid, 0.0))
            comps = components_by_pid.get(pid) or {}
            player_out = {**player, "outcome": roster_scores.outcome_of(comps, score)}
            if comps:
                player_out.update(
                    {
                        "rating_ev": float(comps.get("rating", 0.0) or 0.0),
                        "win_ev": float(comps.get("win", 0.0) or 0.0),
                        "role_ev": float(comps.get("role", 0.0) or 0.0),
                        "booster_ev": float(comps.get("booster", 0.0) or 0.0),
                        "raw_booster_ev": float(comps.get("booster", 0.0) or 0.0),
                        "rating": float(comps.get("rating", 0.0) or 0.0),
                        "win": float(comps.get("win", 0.0) or 0.0),
                        "role": float(comps.get("role", 0.0) or 0.0),
                        "booster": float(comps.get("booster", 0.0) or 0.0),
                        "raw_booster": float(comps.get("booster", 0.0) or 0.0),
                        "components_available": True,
                        "point_breakdown": breakdown_by_pid.get(pid, []),
                    }
                )
            else:
                player_out["components_available"] = False
            total += score
            players.append(player_out)
        scored.append({**team, "players": players, "total_ev": total, "bracket_score": total})
    scored.sort(key=lambda team: float(team.get("bracket_score") or 0.0), reverse=True)
    page = int(body.get("page") or 0)
    page_size = int(body.get("page_size") or 200)
    return {
        "exists": True,
        "mode": "completed_bracket",
        "updated_at": latest_combos["updated_at"],
        "bracket_probability": float(selected.get("probability") or 0.0),
        "bracket": selected.get("bracket") or {},
        "outcomes_count": int(latest_results.get("outcomes_count") or len(latest_results.get("outcomes") or [])),
        "player_values": sorted(player_values, key=lambda row: float(row.get("points") or 0.0), reverse=True),
        "total_teams": len(teams),
        "filtered_count": len(scored),
        "top_teams": scored[:10],
        "page_teams": _page_items(scored, page, page_size),
        "page": max(0, page),
        "page_size": max(1, min(500, page_size)),
    }


@router.get("/latest")
def get_latest_playoff(variant: str = "main", event_id: int | None = None):
    latest = load_latest_playoff(variant, event_id)
    if not latest:
        return {"exists": False}
    results = latest["results"] or {}
    if "outcome_matrix" in results:
        # the compact score matrix is an optimiser input, not tab data
        results = {k: v for k, v in results.items() if k != "outcome_matrix"}
    return {
        "exists": True,
        "payload": latest["payload"],
        "results": results,
        "updated_at": latest["updated_at"],
    }


@router.delete("/latest")
def reset_latest_playoff(variant: str = "main"):
    states = _states(variant)
    key = _event_key(None)
    conn = _connect()
    try:
        for state in states.values():
            conn.execute(f"DELETE FROM {state.table} WHERE singleton_id = ?", (key,))
        conn.commit()
    finally:
        conn.close()
    for state in states.values():
        state.invalidate()
    return {"status": "ok"}
