"""The rating model the valuations use: a player's expected HLTV rating in a
match, learned from every stored per-map scoreboard.

It replaced the Top-X bucket curve on 2026-10-19. The study
behind it (rating_model_study, run by hand) measured it
walk-forward against that curve: a lower error every month and no bias
against weak or unranked opponents, where the curve under-predicted by
0.067 of rating.

Trained on all stored maps with the twelve inputs the study kept
(rating_model_study.FEATURES): the player's recent form, spread, activity,
career level and share of their team's rating, the strength of opposition
they have faced, and the opponent's form, win rate, activity and VRS rank
plus the HLTV rank difference. The fit takes a few seconds after the
30-second row build; the model and every player's and team's inputs as of
today are kept in memory and in .runtime/rating_model.pkl, keyed by the
scoreboards it saw, so a restart reloads instead of retraining and the
nightly map-model task retrains when new scoreboards arrived.

Valuations do not call the model match by match: when a bake builds its
player rows (playoff._build_playoff_lookup_context) each row gets
`model_ratings`, the player's expected rating against every other team of
the event, and that travels with the stored input snapshot — so a stored run
re-scores exactly, whatever the model learns later. The engine reads it
through match_rating().
"""
from __future__ import annotations

import logging
import math
import os
import pickle
import sqlite3
import threading
import time
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

from backend.services import rating_model_study as study

log = logging.getLogger(__name__)

MODEL_VERSION = 2  # monotone constraints
UNRANKED = study.UNRANKED
_RANK_PLACEHOLDER = 300  # ranks at or beyond this count as unranked

_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {}  # model, players, teams, rank_fit, signature, trained_at, n_rows


def _runtime_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, ".runtime", "rating_model.pkl")


def _connect() -> sqlite3.Connection:
    from backend.data.db import connect

    return connect()


def signature(conn: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
    own = conn is None
    conn = conn or _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(match_id), 0) AS m FROM hltv_results WHERE map_player_stats_json IS NOT NULL"
        ).fetchone()
    finally:
        if own:
            conn.close()
    return {"scoreboards": int(row[0] or 0), "last_match": int(row[1] or 0), "version": int(MODEL_VERSION), "features": len(study.FEATURES)}


# ------------------------------------------------------------------ train
def _rank_fit(teams: Dict[str, Dict[str, float]]) -> Dict[str, List[float]]:
    """Straight lines of each opponent input against log HLTV rank, across
    teams with a known rank: what a typical opponent of rank r looks like
    (used for the Top-X curve and for opponents with no stored history)."""
    xs, ys = [], {"opp_allowed_w": [], "opp_winrate_w": [], "log_n": []}
    for t in teams.values():
        rank = float(t.get("rank") or UNRANKED)
        if rank >= _RANK_PLACEHOLDER or (t.get("opp_n_w") or 0) < 5:
            continue
        if not all(math.isfinite(float(t.get(k) or float("nan"))) for k in ("opp_allowed_w", "opp_winrate_w")):
            continue
        xs.append(math.log(rank))
        ys["opp_allowed_w"].append(float(t["opp_allowed_w"]))
        ys["opp_winrate_w"].append(float(t["opp_winrate_w"]))
        ys["log_n"].append(math.log1p(float(t["opp_n_w"])))
    out: Dict[str, List[float]] = {}
    if len(xs) >= 10:
        x = np.asarray(xs)
        for k, v in ys.items():
            slope, intercept = np.polyfit(x, np.asarray(v), 1)
            out[k] = [float(slope), float(intercept)]
    return out


def train(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Fit on every stored map, snapshot every player's and team's inputs as
    of today, keep it in memory and on disk. Returns a short summary."""
    t0 = time.time()
    own = conn is None
    conn = conn or _connect()
    try:
        sig = signature(conn)
        matches = study._load_matches(conn)
    finally:
        if own:
            conn.close()
    data = study.build_rows(matches, study.WINDOW_DAYS)
    cols = {c: i for i, c in enumerate(data["columns"])}
    X = data["X"][:, [cols[k] for k in study.FEATURES]]
    y = data["y"]
    keep = X[:, study.FEATURES.index("n_w")] >= 1
    model = study.make_model([k == "map_code" for k in study.FEATURES], study.FEATURES).fit(X[keep], y[keep])
    # every player's and team's inputs as of today
    players: Dict[int, study._PlayerHist] = {}
    teams: Dict[str, study._TeamHist] = {}
    from collections import defaultdict

    players = defaultdict(study._PlayerHist)
    teams = defaultdict(study._TeamHist)
    for m in matches:
        study.record_match(m, study.match_maps(m), players, teams)
    today = date.today().toordinal()
    player_feats: Dict[int, Dict[str, float]] = {}
    for pid, h in players.items():
        f = study.player_window_features(h, today, study.WINDOW_DAYS)
        player_feats[int(pid)] = {k: float(f[k]) for k in ("ewm_60", "std_w", "n_w", "career_mean", "career_n", "star_w", "opp_logrank_w")}
        player_feats[int(pid)]["last_map"] = float(h.d[-1]) if h.d else float("nan")
    team_feats: Dict[str, Dict[str, float]] = {}
    for key, th in teams.items():
        f = study.team_window_features(th, today, study.WINDOW_DAYS, as_opponent=True)
        team_feats[key] = {k: float(f[k]) for k in ("opp_allowed_w", "opp_winrate_w", "opp_n_w")}
        team_feats[key]["rank"] = float(th.rank[-1]) if th.rank else UNRANKED
        team_feats[key]["vrs"] = float(th.vrs[-1]) if th.vrs else UNRANKED
    state = {
        "model": model, "players": player_feats, "teams": team_feats, "rank_fit": _rank_fit(team_feats),
        "signature": sig, "trained_at": time.time(), "n_rows": int(keep.sum()), "n_players": len(player_feats), "n_teams": len(team_feats),
        "elapsed": round(time.time() - t0, 1),
    }
    with _LOCK:
        _STATE.clear()
        _STATE.update(state)
    try:
        path = _runtime_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(state, fh, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:  # noqa: BLE001 - the in-memory model still serves
        log.warning("Could not store the rating model", exc_info=True)
    return summary()


def summary() -> Dict[str, Any]:
    with _LOCK:
        if not _STATE.get("model"):
            return {"loaded": False}
        return {
            "loaded": True, "trained_at": _STATE.get("trained_at"), "rows": _STATE.get("n_rows"), "players": _STATE.get("n_players"),
            "teams": _STATE.get("n_teams"), "signature": dict(_STATE.get("signature") or {}), "elapsed": _STATE.get("elapsed"),
            "features": list(study.FEATURES),
        }


def _load_from_disk() -> bool:
    path = _runtime_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as fh:
            state = pickle.load(fh)
    except Exception:  # noqa: BLE001
        log.warning("Could not read the stored rating model", exc_info=True)
        return False
    if not state.get("model"):
        return False
    with _LOCK:
        _STATE.clear()
        _STATE.update(state)
    return True


def ensure(force: bool = False) -> Dict[str, Any]:
    """Load the stored model, or train when there is none, the scoreboards
    changed since, or on request. Returns the summary plus whether it trained."""
    with _LOCK:
        now = signature()
        if not force:
            if not _STATE.get("model"):
                _load_from_disk()
            then = dict(_STATE.get("signature") or {})
            if _STATE.get("model") and all(int(then.get(k, -1)) == int(v) for k, v in now.items()):
                return {"trained": False, **summary()}
        return {"trained": True, **train()}


def loaded() -> bool:
    with _LOCK:
        return bool(_STATE.get("model"))


# ---------------------------------------------------------------- predict
def _log_rank(rank: Any) -> float:
    try:
        r = float(rank)
    except (TypeError, ValueError):
        r = UNRANKED
    if not (r > 0) or r >= _RANK_PLACEHOLDER:
        r = UNRANKED
    return math.log(r)


def typical_opponent(rank: Any) -> Dict[str, float]:
    """The opponent inputs of a typical team at that HLTV rank."""
    with _LOCK:
        fit = dict(_STATE.get("rank_fit") or {})
    lr = _log_rank(rank)
    out = {}
    for k in ("opp_allowed_w", "opp_winrate_w", "log_n"):
        if k in fit:
            out[k] = fit[k][0] * lr + fit[k][1]
    return {
        "opp_allowed_w": float(out.get("opp_allowed_w", 1.0)),
        "opp_winrate_w": float(min(1.0, max(0.0, out.get("opp_winrate_w", 0.5)))),
        "opp_n_w": float(max(0.0, math.expm1(out.get("log_n", math.log1p(30.0))))),
    }


def opponent_inputs(team_key: Optional[str], rank: Any, vrs: Any) -> Dict[str, float]:
    """A specific opponent's inputs: its stored history when it has one in
    the window, else a typical team of its rank."""
    feats = None
    if team_key:
        with _LOCK:
            feats = (_STATE.get("teams") or {}).get(str(team_key))
    if not feats or not (feats.get("opp_n_w") or 0) >= 1:
        feats = typical_opponent(rank)
    v = vrs
    if v is None or not (float(v) > 0) or float(v) >= _RANK_PLACEHOLDER:
        v = rank
    return {
        "opp_allowed_w": float(feats["opp_allowed_w"]), "opp_winrate_w": float(feats["opp_winrate_w"]), "opp_n_w": float(feats["opp_n_w"]),
        "opp_vrs": _log_rank(v),
    }


def predict_rows(rows: List[Dict[str, float]]) -> np.ndarray:
    with _LOCK:
        model = _STATE.get("model")
    X = np.asarray([[float(r.get(k, float("nan"))) for k in study.FEATURES] for r in rows], dtype=np.float64)
    return model.predict(X)


def player_inputs(player_id: int) -> Optional[Dict[str, float]]:
    with _LOCK:
        f = (_STATE.get("players") or {}).get(int(player_id))
    if not f or not (f.get("n_w") or 0) >= 1:
        return None
    return dict(f)


def predict(player_id: int, own_rank: Any, opp_rank: Any, opp_vrs: Any = None, opp_key: Optional[str] = None) -> Optional[float]:
    """The player's expected rating against that opponent; None when the
    model is not loaded or the player has no maps in the window."""
    if not loaded():
        return None
    pf = player_inputs(player_id)
    if pf is None:
        return None
    row = {**pf, **opponent_inputs(opp_key, opp_rank, opp_vrs), "rank_diff": _log_rank(own_rank) - _log_rank(opp_rank)}
    return float(predict_rows([row])[0])


# The curve averages the model over opponents around each rank (offsets in
# log rank with triangular weights): a rank-20 opponent is a rank 15–27 kind
# of team, and the averaging also irons out the steps a tree model has.
_CURVE_OFFSETS = [(-0.30, 0.25), (-0.15, 0.5), (0.0, 1.0), (0.15, 0.5), (0.30, 0.25)]


def rank_curve(player_id: int, own_rank: Any, ranks: Iterable[int] = range(1, 101)) -> List[Dict[str, float]]:
    """Expected rating against a typical opponent at each HLTV rank — the
    smooth replacement for the Top-X buckets on the player card."""
    if not loaded():
        return []
    pf = player_inputs(player_id)
    if pf is None:
        return []
    ranks = [int(r) for r in ranks]
    own_lr = _log_rank(own_rank)
    rows, weights = [], []
    for r in ranks:
        for off, w in _CURVE_OFFSETS:
            rr = max(1.0, min(float(_RANK_PLACEHOLDER - 1), math.exp(math.log(r) + off)))
            rows.append({**pf, **opponent_inputs(None, rr, None), "rank_diff": own_lr - math.log(rr)})
            weights.append(w)
    preds = predict_rows(rows).reshape(len(ranks), len(_CURVE_OFFSETS))
    wts = np.asarray(weights[: len(_CURVE_OFFSETS)])
    smoothed = (preds * wts).sum(axis=1) / wts.sum()
    return [{"rank": r, "rating": round(float(p), 4)} for r, p in zip(ranks, smoothed)]


def match_rating(row: Dict[str, Any], opponent_team_id: Any) -> float:
    """The rating the engine scores a player with against that opponent: the
    row's learned `model_ratings` entry (attached when the valuation's rows
    were built), else the row's stored rating (a player with no stored maps),
    else 1.0."""
    learned = row.get("model_ratings") if isinstance(row, dict) else None
    if opponent_team_id is not None and isinstance(learned, dict):
        value = learned.get(str(int(opponent_team_id)))
        if value is not None:
            return float(value)
    try:
        value = float(row.get("rating"))
        return value if value > 0 else 1.0
    except (TypeError, ValueError, AttributeError):
        return 1.0


def recent_maps(player_id: int, n: int = 10, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """The player's last `n` stored maps, oldest first: date, map, opponent,
    the opponent's HLTV rank at the time (None when unranked), the map score
    from the player's side, whether it was won, and the player's rating."""
    import json as _json

    own = conn is None
    conn = conn or _connect()
    try:
        rows = conn.execute(
            """SELECT match_url, match_date, team1, team2, hltv_rank_1, hltv_rank_2, maps_json, map_player_stats_json
               FROM hltv_results
               WHERE map_player_stats_json LIKE ? AND match_date IS NOT NULL
               ORDER BY match_date DESC, match_id DESC LIMIT ?""",
            (f'%"player_id": {int(player_id)},%', max(3, int(n))),
        ).fetchall()
    finally:
        if own:
            conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            maps = _json.loads(r[6] or "[]")
            mps = _json.loads(r[7] or "{}")
        except Exception:  # noqa: BLE001
            continue
        match_maps: List[Dict[str, Any]] = []
        for mp in maps:
            sc1, sc2 = mp.get("score1"), mp.get("score2")
            for p in mps.get(mp.get("map")) or []:
                if int(p.get("player_id") or 0) != int(player_id) or p.get("rating") is None:
                    continue
                is_t1 = p.get("team") == r[2]
                opp = r[3] if is_t1 else r[2]
                opp_rank = r[5] if is_t1 else r[4]
                try:
                    opp_rank = int(opp_rank) if opp_rank is not None and 0 < int(opp_rank) < _RANK_PLACEHOLDER else None
                except (TypeError, ValueError):
                    opp_rank = None
                own_s, opp_s = (sc1, sc2) if is_t1 else (sc2, sc1)
                match_maps.append({
                    "date": r[1], "map": mp.get("map"), "opponent": opp, "opponent_rank": opp_rank,
                    "score": f"{own_s}-{opp_s}" if own_s is not None and opp_s is not None else "",
                    "won": bool(own_s > opp_s) if own_s is not None and opp_s is not None else None,
                    "rating": float(p["rating"]), "match_url": r[0],
                })
        out.extend(reversed(match_maps))  # newest map of the match first, like the matches
        if len(out) >= n:
            break
    return list(reversed(out[:n]))


def attach_match_ratings(player_rows_by_id: Dict[int, dict], team_of_player: Dict[int, int], team_rank_by_id: Dict[int, int]) -> int:
    """Give every player row `model_ratings`: {opponent team id: expected
    rating} for each other team in the context. Rows of players without
    history are left alone (the engine falls back to the curve for them).
    Returns how many rows got ratings."""
    if not loaded():
        try:
            ensure()
        except Exception:  # noqa: BLE001
            log.warning("Rating model unavailable; valuations fall back to the Top-X curve", exc_info=True)
            return 0
        if not loaded():
            return 0
    from backend.services import team_strength

    team_ids = sorted({int(t) for t in team_rank_by_id} | {int(t) for t in team_of_player.values()})
    profiles = {}
    for tid in team_ids:
        try:
            profiles[tid] = team_strength._team_profile(tid)
        except Exception:  # noqa: BLE001
            profiles[tid] = {"key": "", "hltv_rank": team_rank_by_id.get(tid, UNRANKED), "vrs_rank": None}

    def _rank(tid: int) -> float:
        r = team_rank_by_id.get(int(tid))
        if r is None:
            r = profiles.get(tid, {}).get("hltv_rank")
        try:
            r = float(r)
        except (TypeError, ValueError):
            r = UNRANKED
        return UNRANKED if r >= _RANK_PLACEHOLDER or r <= 0 else r

    opp_inputs = {tid: opponent_inputs(profiles[tid].get("key"), _rank(tid), profiles[tid].get("vrs_rank")) for tid in team_ids}
    pending: List[tuple] = []
    rows: List[Dict[str, float]] = []
    for pid, row in player_rows_by_id.items():
        own = team_of_player.get(int(pid))
        if own is None:
            continue
        pf = player_inputs(int(pid))
        if pf is None:
            row.pop("model_ratings", None)
            continue
        own_lr = _log_rank(_rank(int(own)))
        for tid in team_ids:
            if int(tid) == int(own):
                continue
            rows.append({**pf, **opp_inputs[tid], "rank_diff": own_lr - _log_rank(_rank(tid))})
            pending.append((int(pid), int(tid)))
    if not rows:
        return 0
    preds = predict_rows(rows)
    done = set()
    for (pid, tid), p in zip(pending, preds):
        row = player_rows_by_id[pid]
        if pid not in done:
            row["model_ratings"] = {}
            done.add(pid)
        row["model_ratings"][str(tid)] = round(float(p), 4)
    return len(done)
