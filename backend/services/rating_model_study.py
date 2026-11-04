"""Per-map player rating: the Top-X bucket curve the app uses today versus a
model learned from the stored per-map scoreboards, compared walk-forward.

Every stored match (hltv_results.map_player_stats_json) gives each of the ten
players a rating on each map, with both teams' HLTV / VRS ranks at the time.
That is the raw data HLTV's Top-X tiers summarise. Here each player-map is a
row whose inputs come only from matches played before it:

  * the player's form over the last 182 days (means over several horizons,
    a 60-day half-life weighted mean, spread, ADR, KAST, days since the last
    map) and over their whole stored career;
  * the strength of the opposition they faced in that window (mean log rank);
  * their team's trailing form and the player's share of it;
  * the opponent's trailing form, win rate and the rating it concedes;
  * the match: both ranks (log; unranked flagged), rank difference, VRS
    ranks, series length, map.

The baseline is the Top-X bucket curve the app priced ratings with until
2026-10-19 (topx_curve_baseline): the cumulative Top-X tiers computed from
the same window, its population curve refitted per fold, the overall rating
outside the top 50. Walk-forward: one fold per month,
each model fitted on the rows before that month and scored on the month's
rows with at least `min_maps` prior maps in the window, so every score is out
of sample. MAE is the mean absolute error of the predicted rating; RMSE the
root mean squared error (it punishes large misses more). Rating points are
(rating − 1) / 2 × 100, so 0.01 of rating is half a point per match.
"""
from __future__ import annotations

import bisect
import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional

import numpy as np

UNRANKED = 400.0  # the rank unranked teams take in every aggregate
WINDOW_DAYS = 182
MIN_MAPS = 5
FOLDS = 13
# Every input the row builder computes. Backward elimination (2026-10-19,
# walk-forward) showed the twelve in FEATURES match all 33 (per-map MAE
# 0.2811 vs 0.2812); the rest are near-copies of them (the window means
# correlate 0.9+ with the weighted recent rating, the opponent's mean rating
# with the rating it concedes).
ALL_FEATURES: List[str] = [
    "n_w", "mean_w", "mean_90", "mean_30", "ewm_60", "last10", "last30", "std_w", "adr_w", "kast_w",
    "career_n", "career_mean", "days_since", "trend", "opp_logrank_w",
    "team_mean_w", "star_w", "team_winrate_w", "team_n_w",
    "opp_team_mean_w", "opp_allowed_w", "opp_winrate_w", "opp_n_w", "opp_logrank_w180",
    "own_rank", "opp_rank", "own_unranked", "opp_unranked", "rank_diff", "own_vrs", "opp_vrs", "bo", "map_code",
]
# The inputs the learned model uses.
FEATURES: List[str] = [
    "ewm_60", "std_w", "n_w", "career_mean", "career_n", "star_w", "opp_logrank_w",
    "opp_allowed_w", "opp_winrate_w", "opp_n_w", "opp_vrs", "rank_diff",
]
RAW_K = 10  # raw map tuples the row builder keeps for the "raw maps" input set
FEATURE_LABELS: Dict[str, str] = {
    "n_w": "Maps in window", "mean_w": "Window mean rating", "mean_90": "90-day mean", "mean_30": "30-day mean",
    "ewm_60": "Weighted recent rating", "last10": "Last 10 maps", "last30": "Last 30 maps", "std_w": "Rating spread",
    "adr_w": "Window ADR", "kast_w": "Window KAST", "career_n": "Career maps", "career_mean": "Career mean rating",
    "days_since": "Days since last map", "trend": "Recent vs window", "opp_logrank_w": "Opposition faced (window)",
    "team_mean_w": "Team mean rating", "star_w": "Share of team rating", "team_winrate_w": "Team map win rate",
    "team_n_w": "Team maps in window", "opp_team_mean_w": "Opponent mean rating", "opp_allowed_w": "Rating opponent concedes",
    "opp_winrate_w": "Opponent win rate", "opp_n_w": "Opponent maps in window", "opp_logrank_w180": "Opponent's opposition faced",
    "own_rank": "Own HLTV rank", "opp_rank": "Opponent HLTV rank", "own_unranked": "Own team unranked",
    "opp_unranked": "Opponent unranked", "rank_diff": "Rank difference", "own_vrs": "Own VRS rank", "opp_vrs": "Opponent VRS rank",
    "bo": "Series length", "map_code": "Map",
}
MAP_CODES = {m: i for i, m in enumerate(["Ancient", "Anubis", "Dust2", "Inferno", "Mirage", "Nuke", "Overpass", "Train", "Vertigo", "Cache", "Cobblestone"])}
OPPONENT_FEATURES = {k for k in ALL_FEATURES if k.startswith("opp_") or k == "rank_diff"}
METHODS = [
    {"key": "curve", "label": "Top-X curve (replaced)"},
    {"key": "gbr", "label": "Learned model"},
    {"key": "gbr_no_opponent", "label": "Learned, no opponent inputs"},
    {"key": "ridge", "label": "Linear model"},
    {"key": "overall", "label": "Window mean"},
]
RANK_BUCKETS = ["1-10", "11-30", "31-50", "51-100", "100+ / unranked"]


# ------------------------------------------------------------------ rows
def _load_matches(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT match_id, match_date, team1, team2, score1, score2, hltv_rank_1, hltv_rank_2,
                  vrs_rank_1, vrs_rank_2, maps_json, map_player_stats_json
           FROM hltv_results
           WHERE map_player_stats_json IS NOT NULL AND match_date IS NOT NULL
           ORDER BY match_date, match_id"""
    ).fetchall()
    from backend.data import team_aliases

    out = []
    for r in rows:
        try:
            maps = json.loads(r["maps_json"] or "[]")
            mps = json.loads(r["map_player_stats_json"] or "{}")
            d = date.fromisoformat(str(r["match_date"])[:10])
        except Exception:  # noqa: BLE001 - a malformed row is skipped
            continue
        if not maps or not mps:
            continue
        wins_needed = max(int(r["score1"] or 0), int(r["score2"] or 0)) or 1
        t1, t2 = r["team1"], r["team2"]
        out.append(
            {
                "match_id": int(r["match_id"] or 0),
                "date": d,
                "team1": t1,
                "team2": t2,
                # the scoreboard names as stored (the player rows carry them) and
                # the history key each resolves to (renames merged, see team_aliases)
                "key": {t1: team_aliases.key(str(t1 or "")) or str(t1), t2: team_aliases.key(str(t2 or "")) or str(t2)},
                "rank": {t1: r["hltv_rank_1"], t2: r["hltv_rank_2"]},
                "vrs": {t1: r["vrs_rank_1"], t2: r["vrs_rank_2"]},
                "bo": {1: 1, 2: 3, 3: 5}.get(wins_needed, 3),
                "maps": maps,
                "mps": mps,
            }
        )
    return out


def _rank_val(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return UNRANKED
    return f if f > 0 else UNRANKED


class _PlayerHist:
    __slots__ = ("d", "r", "adr", "kast", "opp", "team_mean")

    def __init__(self):
        self.d, self.r, self.adr, self.kast, self.opp, self.team_mean = [], [], [], [], [], []


class _TeamHist:
    __slots__ = ("d", "won", "mean", "opp_mean", "opp_rank", "rank", "vrs")

    def __init__(self):
        self.d, self.won, self.mean, self.opp_mean, self.opp_rank, self.rank, self.vrs = [], [], [], [], [], [], []


def _mean(xs) -> float:
    return float(np.mean(xs)) if len(xs) else float("nan")


def match_maps(m: Dict[str, Any]) -> List[tuple]:
    """(map name, score1, score2, {scoreboard team name: player rows}) for each
    map of a stored match that has a full scoreboard for both sides."""
    out = []
    for mp in m["maps"]:
        plist = m["mps"].get(mp.get("map"))
        sc1, sc2 = mp.get("score1"), mp.get("score2")
        if not plist or sc1 is None or sc2 is None:
            continue
        by_team: Dict[str, list] = defaultdict(list)
        for p in plist:
            if p.get("rating") is None or not p.get("player_id"):
                continue
            by_team[p["team"]].append(p)
        if len(by_team) != 2:
            continue
        out.append((mp.get("map"), int(sc1), int(sc2), by_team))
    return out


def record_match(m: Dict[str, Any], map_rows: List[tuple], players: Dict[int, _PlayerHist], teams: Dict[str, _TeamHist]) -> None:
    """Append a match's maps to the player and team histories (after its rows
    were built, so nothing of the match leaks into its own inputs)."""
    dn = m["date"].toordinal()
    for name, sc1, sc2, by_team in map_rows:
        team_means = {t: _mean([p["rating"] for p in ps]) for t, ps in by_team.items()}
        for tname, ps in by_team.items():
            opp = m["team2"] if tname == m["team1"] else m["team1"]
            won = (sc1 > sc2) if tname == m["team1"] else (sc2 > sc1)
            th = teams[m["key"][tname]]
            th.d.append(dn)
            th.won.append(float(won))
            th.mean.append(team_means[tname])
            th.opp_mean.append(team_means[opp])
            th.opp_rank.append(_rank_val(m["rank"].get(opp)))
            th.rank.append(_rank_val(m["rank"].get(tname)))
            th.vrs.append(_rank_val(m["vrs"].get(tname)))
            for p in ps:
                h = players[int(p["player_id"])]
                h.d.append(dn)
                h.r.append(float(p["rating"]))
                h.adr.append(float(p["adr"]) if p.get("adr") is not None else float("nan"))
                h.kast.append(float(p["kast"]) if p.get("kast") is not None else float("nan"))
                h.opp.append(_rank_val(m["rank"].get(opp)))
                h.team_mean.append(team_means[tname])


def player_window_features(h: _PlayerHist, dn: int, window_days: int = WINDOW_DAYS) -> Dict[str, float]:
    """A player's own inputs as of day `dn` from their history (the same
    numbers build_rows computes for a row)."""
    start = dn - window_days
    k0 = bisect.bisect_left(h.d, start)
    r = np.asarray(h.r[k0:], dtype=np.float64)
    n = len(r)
    f: Dict[str, float] = {"n_w": float(n), "career_n": float(len(h.r)), "career_mean": _mean(h.r), "days_since": float(dn - h.d[-1]) if h.d else float("nan")}
    if n:
        dd = np.asarray(h.d[k0:], dtype=np.float64)
        opp_r = np.asarray(h.opp[k0:], dtype=np.float64)
        f["mean_w"] = float(r.mean())
        f["std_w"] = float(r.std()) if n > 1 else float("nan")
        m90, m30 = dd >= dn - 90, dd >= dn - 30
        f["mean_90"] = float(r[m90].mean()) if m90.any() else float("nan")
        f["mean_30"] = float(r[m30].mean()) if m30.any() else float("nan")
        wts = np.power(0.5, (dn - dd) / 60.0)
        f["ewm_60"] = float((wts * r).sum() / wts.sum())
        f["last10"] = float(r[-10:].mean())
        f["last30"] = float(r[-30:].mean())
        f["trend"] = f["last10"] - f["mean_w"]
        f["adr_w"] = float(np.nanmean(np.asarray(h.adr[k0:], dtype=np.float64)))
        f["kast_w"] = float(np.nanmean(np.asarray(h.kast[k0:], dtype=np.float64)))
        f["opp_logrank_w"] = float(np.log(opp_r).mean())
        f["star_w"] = float((r - np.asarray(h.team_mean[k0:], dtype=np.float64)).mean())
    else:
        for key in ("mean_w", "std_w", "mean_90", "mean_30", "ewm_60", "last10", "last30", "trend", "adr_w", "kast_w", "opp_logrank_w", "star_w"):
            f[key] = float("nan")
    return f


def team_window_features(th: _TeamHist, dn: int, window_days: int = WINDOW_DAYS, as_opponent: bool = True) -> Dict[str, float]:
    """A team's inputs as of day `dn`: its own form (team_*) or, as the
    opponent of the row's player, the opp_* set."""
    start = dn - window_days
    j0 = bisect.bisect_left(th.d, start)
    if as_opponent:
        return {
            "opp_team_mean_w": _mean(th.mean[j0:]), "opp_allowed_w": _mean(th.opp_mean[j0:]),
            "opp_winrate_w": _mean(th.won[j0:]), "opp_n_w": float(len(th.d) - j0),
            "opp_logrank_w180": float(np.log(th.opp_rank[j0:]).mean()) if len(th.d) > j0 else float("nan"),
        }
    return {"team_mean_w": _mean(th.mean[j0:]), "team_winrate_w": _mean(th.won[j0:]), "team_n_w": float(len(th.d) - j0)}


# The direction each input may move the rating (0 = free): a weaker opponent
# (worse rank, concedes more, wins less) never lowers it, and better recent
# form or career level never lowers it. Measured walk-forward (2026-10-19):
# the constraints cost nothing (per-map MAE 0.2807 vs 0.2811 free) and make
# the per-rank curve monotone.
MONOTONE = {"rank_diff": -1, "opp_allowed_w": 1, "opp_winrate_w": -1, "opp_vrs": 1, "ewm_60": 1, "career_mean": 1}


def make_model(categorical: List[bool], columns: Optional[List[str]] = None):
    from sklearn.ensemble import HistGradientBoostingRegressor

    kw = {}
    if columns is not None:
        kw["monotonic_cst"] = [MONOTONE.get(k, 0) for k in columns]
    return HistGradientBoostingRegressor(
        loss="squared_error", learning_rate=0.05, max_iter=500, max_leaf_nodes=31, min_samples_leaf=80,
        l2_regularization=1.0, categorical_features=categorical, random_state=7, **kw,
    )


def extra_columns(raw_k: int = 0, half_lives: tuple = ()) -> List[str]:
    """Names of the optional inputs build_rows appends after FEATURES: one
    recency-weighted mean per extra half-life, then the last raw_k maps as
    (rating, days ago, log opponent rank) triples, most recent first."""
    cols = [f"ewm_{int(hl)}" for hl in half_lives]
    for i in range(int(raw_k)):
        cols += [f"raw_r_{i}", f"raw_age_{i}", f"raw_opp_{i}"]
    return cols


def build_rows(matches: List[Dict[str, Any]], window_days: int = WINDOW_DAYS, raw_k: int = 0, half_lives: tuple = ()) -> Dict[str, Any]:
    """One row per player-map: inputs from the matches before it only, the
    rating as the target, and the cumulative Top-X tiers the baseline needs.
    With raw_k / half_lives the optional inputs (extra_columns) follow
    FEATURES in each row; `columns` names every column."""
    players: Dict[int, _PlayerHist] = defaultdict(_PlayerHist)
    teams: Dict[str, _TeamHist] = defaultdict(_TeamHist)
    X: List[Any] = []
    raw_k = int(raw_k)
    half_lives = tuple(float(h) for h in half_lives)
    n_extra = len(half_lives) + 3 * raw_k
    y: List[float] = []
    meta: List[tuple] = []  # (date ordinal, match_id, pid, opp_rank)
    topx: List[Optional[Dict[str, Any]]] = []
    n_players = set()
    for m in matches:
        dn = m["date"].toordinal()
        start = dn - window_days
        map_rows = match_maps(m)

        for name, sc1, sc2, by_team in map_rows:
            for tname, ps in by_team.items():
                opp = m["team2"] if tname == m["team1"] else m["team1"]
                own_rank = _rank_val(m["rank"].get(tname))
                opp_rank = _rank_val(m["rank"].get(opp))
                th, oh = teams[m["key"][tname]], teams[m["key"][opp]]
                team_ctx = {
                    **team_window_features(th, dn, window_days, as_opponent=False),
                    **team_window_features(oh, dn, window_days, as_opponent=True),
                    "own_rank": math.log(own_rank), "opp_rank": math.log(opp_rank),
                    "own_unranked": float(own_rank >= UNRANKED), "opp_unranked": float(opp_rank >= UNRANKED),
                    "rank_diff": math.log(own_rank) - math.log(opp_rank),
                    "own_vrs": math.log(_rank_val(m["vrs"].get(tname))), "opp_vrs": math.log(_rank_val(m["vrs"].get(opp))),
                    "bo": float(m["bo"]), "map_code": float(MAP_CODES.get(name, len(MAP_CODES))),
                }
                for p in ps:
                    pid = int(p["player_id"])
                    n_players.add(pid)
                    h = players[pid]
                    k0 = bisect.bisect_left(h.d, start)
                    f: Dict[str, float] = dict(team_ctx)
                    f.update(player_window_features(h, dn, window_days))
                    if len(h.r) > k0:
                        r = np.asarray(h.r[k0:], dtype=np.float64)
                        opp_r = np.asarray(h.opp[k0:], dtype=np.float64)
                        tiers = {}
                        for t in (5, 10, 20, 30, 50):
                            msk = opp_r <= t
                            tiers[t] = (float(r[msk].mean()) if msk.any() else None, int(msk.sum()))
                        topx.append({"rating": float(r.mean()), "tiers": tiers})
                    else:
                        topx.append(None)
                    row_vals = [f[k] for k in ALL_FEATURES]
                    if n_extra:
                        ex = np.full(n_extra, np.nan, dtype=np.float32)
                        if h.d:
                            dd_all = np.asarray(h.d, dtype=np.float64)
                            r_all = np.asarray(h.r, dtype=np.float64)
                            for j, hl in enumerate(half_lives):
                                wts = np.power(0.5, (dn - dd_all) / hl)
                                ex[j] = float((wts * r_all).sum() / wts.sum())
                            kk = min(raw_k, len(h.r))
                            base = len(half_lives)
                            for i in range(kk):
                                ex[base + 3 * i] = h.r[-1 - i]
                                ex[base + 3 * i + 1] = dn - h.d[-1 - i]
                                ex[base + 3 * i + 2] = math.log(h.opp[-1 - i])
                        X.append(np.concatenate([np.asarray(row_vals, dtype=np.float32), ex]))
                    else:
                        X.append(row_vals)
                    y.append(float(p["rating"]))
                    meta.append((dn, m["match_id"], pid, opp_rank))

        record_match(m, map_rows, players, teams)
    return {
        "X": np.asarray(X, dtype=np.float32 if n_extra else np.float64), "y": np.asarray(y, dtype=np.float64), "meta": meta, "topx": topx,
        "columns": ALL_FEATURES + extra_columns(raw_k, half_lives),
        "n_matches": len(matches), "n_players": len(n_players),
        "first_date": matches[0]["date"].isoformat() if matches else None,
        "last_date": matches[-1]["date"].isoformat() if matches else None,
    }


# -------------------------------------------------------------- baseline
def _topx_row(tx: Dict[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {"rating": tx["rating"]}
    for t, (rt, nm) in tx["tiers"].items():
        row[f"rating_top{t}"] = rt
        row[f"maps_top{t}"] = float(nm)
    return row


def _curve_predict(curve_mod, tx: Dict[str, Any], opp_rank: float) -> float:
    pred = curve_mod.predict_rating_vs_rank(_topx_row(tx), opp_rank if opp_rank < UNRANKED else None)
    return float(pred) if pred is not None else float(tx["rating"])


def _rank_bucket_masks(opp_rank: np.ndarray) -> Dict[str, np.ndarray]:
    return {
        "1-10": opp_rank <= 10,
        "11-30": (opp_rank > 10) & (opp_rank <= 30),
        "31-50": (opp_rank > 30) & (opp_rank <= 50),
        "51-100": (opp_rank > 50) & (opp_rank <= 100),
        "100+ / unranked": opp_rank > 100,
    }


# ----------------------------------------------------------------- study
def run_study(conn: sqlite3.Connection, window_days: int = WINDOW_DAYS, folds: int = FOLDS, min_maps: int = MIN_MAPS) -> Dict[str, Any]:
    from sklearn.inspection import permutation_importance
    from sklearn.linear_model import Ridge

    from backend.services import topx_curve_baseline as curve_mod

    t_start = time.time()
    matches = _load_matches(conn)
    data = build_rows(matches, window_days, raw_k=RAW_K)
    X_all, y, meta, topx = data["X"], data["y"], data["meta"], data["topx"]
    if len(y) == 0:
        return {"error": "no stored per-map player stats"}
    col = {c: i for i, c in enumerate(data["columns"])}
    X = X_all[:, [col[k] for k in FEATURES]]
    dates = np.asarray([m[0] for m in meta])
    match_ids = np.asarray([m[1] for m in meta])
    pids = np.asarray([m[2] for m in meta])
    opp_rank = np.asarray([m[3] for m in meta])
    n_w = X_all[:, col["n_w"]]
    cat_mask = [k == "map_code" for k in FEATURES]
    keep_no_opp = [i for i, k in enumerate(FEATURES) if k not in OPPONENT_FEATURES]
    # other input sets, scored the same way (the used set's numbers are "gbr")
    raw_cols = [c for c in data["columns"] if c.startswith("raw_")]
    variant_sets = [
        ("All 33 inputs", ALL_FEATURES),
        (f"Raw last {RAW_K} maps + all 33", ALL_FEATURES + raw_cols),
        ("6 inputs", ["career_mean", "opp_n_w", "opp_logrank_w", "ewm_60", "opp_allowed_w", "rank_diff"]),
        ("3 inputs", ["ewm_60", "opp_allowed_w", "rank_diff"]),
    ]
    variant_errs: Dict[str, List[np.ndarray]] = defaultdict(list)
    variant_series: Dict[str, List[np.ndarray]] = defaultdict(list)

    last = date.fromordinal(int(dates.max()))
    cutoffs = []
    yy, mm = last.year, last.month
    for _ in range(folds):
        cutoffs.append(date(yy, mm, 1))
        mm -= 1
        if mm == 0:
            yy, mm = yy - 1, 12
    cutoffs.sort()

    _gbr = make_model

    saved_curve = curve_mod.get_average_bucket_pct()
    fold_rows: List[Dict[str, Any]] = []
    errs: Dict[str, List[np.ndarray]] = defaultdict(list)
    series_errs: Dict[str, List[np.ndarray]] = defaultdict(list)
    bucket_errs: Dict[str, Dict[str, List[np.ndarray]]] = defaultdict(lambda: defaultdict(list))
    faced_vals: List[np.ndarray] = []
    last_fit = None
    pct_last: Dict[int, float] = {}
    try:
        for cut in cutoffs:
            nxt = date(cut.year + (cut.month == 12), cut.month % 12 + 1, 1)
            c0, c1 = cut.toordinal(), nxt.toordinal()
            tr = (dates < c0) & (n_w >= 1)
            te = (dates >= c0) & (dates < c1) & (n_w >= min_maps)
            if te.sum() == 0 or tr.sum() < 1000:
                continue
            te_idx = np.flatnonzero(te)
            # the production curve, fitted the production way on the training period
            latest: Dict[int, Dict[str, Any]] = {}
            for i in np.flatnonzero(tr):
                if topx[i] is not None:
                    latest[int(pids[i])] = topx[i]
            pct_last = curve_mod.fit_average_bucket_pct([_topx_row(tx) for tx in latest.values()])
            curve_mod.set_average_bucket_pct(pct_last)
            preds: Dict[str, np.ndarray] = {}
            preds["overall"] = np.asarray([topx[i]["rating"] for i in te_idx])
            preds["curve"] = np.asarray([_curve_predict(curve_mod, topx[i], opp_rank[i]) for i in te_idx])
            Xtr, ytr, Xte = X[tr], y[tr], X[te_idx]
            gbr = _gbr(cat_mask, FEATURES).fit(Xtr, ytr)
            preds["gbr"] = gbr.predict(Xte)
            preds["gbr_no_opponent"] = _gbr([cat_mask[i] for i in keep_no_opp], [FEATURES[i] for i in keep_no_opp]).fit(Xtr[:, keep_no_opp], ytr).predict(Xte[:, keep_no_opp])
            med = np.nanmedian(Xtr, axis=0)
            med = np.where(np.isnan(med), 0.0, med)
            Xtr_l = np.where(np.isnan(Xtr), med, Xtr)
            Xte_l = np.where(np.isnan(Xte), med, Xte)
            mu, sd = Xtr_l.mean(axis=0), Xtr_l.std(axis=0) + 1e-9
            preds["ridge"] = Ridge(alpha=10.0).fit((Xtr_l - mu) / sd, ytr).predict((Xte_l - mu) / sd)

            yt = y[te_idx]
            key = match_ids[te_idx] * 100000 + pids[te_idx]
            _, inv = np.unique(key, return_inverse=True)
            cnt = np.bincount(inv)
            sy = np.bincount(inv, weights=yt) / cnt
            for vlabel, vcols in variant_sets:
                vi = [col[c] for c in vcols]
                vp = _gbr([c == "map_code" for c in vcols], vcols).fit(X_all[tr][:, vi], ytr).predict(X_all[te_idx][:, vi])
                variant_errs[vlabel].append(vp - yt)
                variant_series[vlabel].append(np.bincount(inv, weights=vp) / cnt - sy)
            masks = _rank_bucket_masks(opp_rank[te_idx])
            row: Dict[str, Any] = {"month": cut.strftime("%Y-%m"), "n_test": int(te.sum()), "n_train": int(tr.sum()), "mae": {}, "series_mae": {}}
            for k, p in preds.items():
                e = p - yt
                errs[k].append(e)
                se = np.bincount(inv, weights=p) / cnt - sy
                series_errs[k].append(se)
                row["mae"][k] = float(np.abs(e).mean())
                row["series_mae"][k] = float(np.abs(se).mean())
                for b, msk in masks.items():
                    if msk.any():
                        bucket_errs[k][b].append(e[msk])
            faced_vals.append(X[te_idx, FEATURES.index("opp_logrank_w")])
            fold_rows.append(row)
            last_fit = (gbr, te_idx)
    finally:
        curve_mod.set_average_bucket_pct(saved_curve)

    summary: Dict[str, Any] = {}
    for k, es in errs.items():
        e = np.concatenate(es)
        se = np.concatenate(series_errs[k])
        summary[k] = {
            "mae": float(np.abs(e).mean()), "rmse": float(np.sqrt((e ** 2).mean())), "bias": float(e.mean()),
            "series_mae": float(np.abs(se).mean()), "series_rmse": float(np.sqrt((se ** 2).mean())),
        }
    by_bucket: Dict[str, Dict[str, Any]] = {}
    for k in errs:
        by_bucket[k] = {}
        for b in RANK_BUCKETS:
            es = bucket_errs[k].get(b)
            if es:
                e = np.concatenate(es)
                by_bucket[k][b] = {"bias": float(e.mean()), "mae": float(np.abs(e).mean()), "n": int(len(e))}
    faced = np.concatenate(faced_vals) if faced_vals else np.asarray([])
    by_faced: Dict[str, List[Dict[str, Any]]] = {}
    if len(faced):
        qs = np.nanquantile(faced, [0.0, 0.25, 0.5, 0.75, 1.0])
        for k, es in errs.items():
            e = np.concatenate(es)
            by_faced[k] = []
            for qi in range(4):
                msk = (faced >= qs[qi]) & (faced <= qs[qi + 1])
                by_faced[k].append({
                    "quartile": qi + 1, "rank_lo": float(math.exp(qs[qi])), "rank_hi": float(math.exp(qs[qi + 1])),
                    "bias": float(e[msk].mean()) if msk.any() else None, "mae": float(np.abs(e[msk]).mean()) if msk.any() else None, "n": int(msk.sum()),
                })
    fold_wins = {k: int(sum(1 for r in fold_rows if r["mae"][k] < r["mae"]["curve"])) for k in errs if k != "curve"}

    importance: List[Dict[str, Any]] = []
    if last_fit is not None:
        gbr, te_idx = last_fit
        rng = np.random.RandomState(1)
        sub = te_idx[rng.choice(len(te_idx), size=min(12000, len(te_idx)), replace=False)]
        pi = permutation_importance(gbr, X[sub], y[sub], n_repeats=2, random_state=1, scoring="neg_mean_absolute_error")
        for i in np.argsort(-pi.importances_mean)[:15]:
            importance.append({"feature": FEATURES[i], "label": FEATURE_LABELS.get(FEATURES[i], FEATURES[i]), "mae_increase": float(pi.importances_mean[i])})

    variants: List[Dict[str, Any]] = []
    if "gbr" in summary:
        variants.append({"label": f"{len(FEATURES)} inputs (used)", "inputs": len(FEATURES), "used": True, **summary["gbr"]})
    for vlabel, vcols in variant_sets:
        if variant_errs.get(vlabel):
            e = np.concatenate(variant_errs[vlabel])
            se = np.concatenate(variant_series[vlabel])
            variants.append({
                "label": vlabel, "inputs": len(vcols), "used": False,
                "mae": float(np.abs(e).mean()), "rmse": float(np.sqrt((e ** 2).mean())), "bias": float(e.mean()),
                "series_mae": float(np.abs(se).mean()), "series_rmse": float(np.sqrt((se ** 2).mean())),
            })
    eligible = np.concatenate(errs["curve"]) if errs.get("curve") else np.asarray([])
    return {
        "variants": variants,
        "data": {
            "matches": int(data["n_matches"]), "rows": int(len(y)), "players": int(data["n_players"]),
            "first_date": data["first_date"], "last_date": data["last_date"],
            "eligible_rows": int(len(eligible)), "eligible_series": int(sum(len(s) for s in series_errs.get("curve", []))),
        },
        "methods": METHODS,
        "folds": fold_rows,
        "fold_wins": fold_wins,
        "summary": summary,
        "rank_buckets": RANK_BUCKETS,
        "by_opp_rank": by_bucket,
        "by_opposition_faced": by_faced,
        "importance": importance,
        "features": [{"key": k, "label": FEATURE_LABELS.get(k, k)} for k in FEATURES],
        "all_features": [{"key": k, "label": FEATURE_LABELS.get(k, k)} for k in ALL_FEATURES],
        "curve_last_fold": {str(k): float(v) for k, v in pct_last.items()},
        "window_days": int(window_days), "min_maps": int(min_maps),
        "elapsed_seconds": round(time.time() - t_start, 1),
    }


if __name__ == "__main__":  # python -m backend.services.rating_model_study
    import os

    db = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fantasy_players.db")
    res = run_study(sqlite3.connect(f"file:{db}?mode=ro", uri=True))
    print(json.dumps({k: v for k, v in res.items() if k in ("data", "summary", "fold_wins", "elapsed_seconds")}, indent=1))
