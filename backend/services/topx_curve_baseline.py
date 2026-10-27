"""The Top-X bucket curve the app priced ratings with until 2026-10-19, kept
only as the baseline the Rating Model study (rating_model_study, run by
hand) scores the learned model against. Nothing else reads it.

Per player: HLTV's cumulative "rating vs top N" tiers become non-overlapping
bands, each band shrinks toward a population curve (the typical fractional
drop vs stronger opposition, refit from the players in scope) by its map
count, and the band ratings interpolate at the opponent's rank; outside the
top 50 the overall rating stands.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


TOP_TIERS = (5, 10, 20, 30, 50)


BUCKET_PRIOR_MAPS = 20.0


PERSONAL_PRIOR_MAPS = 25.0


_AVERAGE_BUCKET_PCT: Dict[int, float] = {}


def set_average_bucket_pct(pct: Dict[int, float]) -> None:
    global _AVERAGE_BUCKET_PCT
    _AVERAGE_BUCKET_PCT = {int(k): float(v) for k, v in (pct or {}).items()}


def get_average_bucket_pct() -> Dict[int, float]:
    return dict(_AVERAGE_BUCKET_PCT)


def fit_average_bucket_pct(player_rows) -> Dict[int, float]:
    """Maps-weighted mean of (bucket_delta / overall) per tier — the fractional
    amount the typical player's rating deviates from their overall vs that tier,
    across players with real per-tier data."""
    sums = {t: 0.0 for t in TOP_TIERS}
    weights = {t: 0.0 for t in TOP_TIERS}
    for row in player_rows or []:
        overall = _safe_float(row.get("rating"))
        if overall is None or overall <= 0:
            continue
        for bucket in build_player_bucket_rows(row):
            tier = int(bucket.get("tier") or 0)
            if tier not in TOP_TIERS:
                continue
            maps = _safe_float(bucket.get("maps")) or 0.0
            delta = _safe_float(bucket.get("raw_bucket_delta"))
            if maps <= 0 or delta is None:
                continue
            sums[tier] += (float(delta) / float(overall)) * float(maps)
            weights[tier] += float(maps)
    return {t: sums[t] / weights[t] for t in TOP_TIERS if weights[t] > 0}


BUCKET_RANGES = {
    5: (1, 5),
    10: (6, 10),
    20: (11, 20),
    30: (21, 30),
    50: (31, 50),
}


TIER_LABELS = {
    5: "Top 5",
    10: "6-10",
    20: "11-20",
    30: "21-30",
    50: "31-50",
    100: "Overall",
}


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, float) and math.isnan(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_tier_rating(row: Dict[str, Any], tier: int) -> Optional[float]:
    return _safe_float(row.get(f"rating_top{tier}"))


def _get_tier_maps(row: Dict[str, Any], tier: int) -> Optional[float]:
    return _safe_float(row.get(f"maps_top{tier}"))


def _tier_label(tier: int) -> str:
    return TIER_LABELS.get(int(tier), f"Top {int(tier)}")


def _derive_bucket_from_cumulative(
    prev_rating: Optional[float],
    prev_maps: Optional[float],
    cur_rating: Optional[float],
    cur_maps: Optional[float],
) -> Optional[Tuple[float, float]]:
    if cur_rating is None or cur_maps is None or cur_maps <= 0:
        return None
    if prev_rating is None or prev_maps is None:
        return float(cur_rating), float(cur_maps)
    if cur_maps <= prev_maps:
        return None

    bucket_maps = float(cur_maps - prev_maps)
    weighted_sum = float(cur_rating) * float(cur_maps) - float(prev_rating) * float(prev_maps)
    bucket_rating = weighted_sum / bucket_maps
    if not math.isfinite(bucket_rating):
        return None
    return float(bucket_rating), float(bucket_maps)


def build_player_delta_anchors(row: Dict[str, Any]) -> List[Tuple[float, float, float]]:
    """
    Build (rank, delta, weight) where Top-X values are normalized into
    non-overlapping buckets:
    Top5, 6-10, 11-20, 21-30, 31-50.
    delta = bucket_rating - overall_rating.
    Includes Top 100 anchor at delta=0.
    """
    anchors: List[Tuple[float, float, float]] = []
    top_map_counts: List[float] = []
    overall = _safe_float(row.get("rating"))
    if overall is None:
        return anchors
    prev_rating: Optional[float] = None
    prev_maps: Optional[float] = None
    for tier in TOP_TIERS:
        cur_rating = _get_tier_rating(row, tier)
        cur_maps = _get_tier_maps(row, tier)
        derived = _derive_bucket_from_cumulative(prev_rating, prev_maps, cur_rating, cur_maps)
        if cur_rating is not None and cur_maps is not None and cur_maps > 0:
            prev_rating, prev_maps = cur_rating, cur_maps
        if derived is None:
            continue
        rating, maps = derived
        if rating is None or maps is None or maps <= 0:
            continue
        w = max(1.0, float(maps))
        delta = float(rating) - float(overall)
        anchors.append((float(tier), delta, w))
        top_map_counts.append(w)

    maps_50 = _get_tier_maps(row, 50)
    if maps_50 is not None and maps_50 > 0:
        w100 = float(maps_50)
    elif top_map_counts:
        w100 = max(top_map_counts)
    else:
        w100 = 1.0
    anchors.append((100.0, 0.0, w100))

    anchors.sort(key=lambda x: x[0])
    return anchors


def _bucket_shrinkage_weight(maps: Any) -> float:
    """Confidence in a band's own rating: maps / (maps + BUCKET_PRIOR_MAPS).
    0 at no data, → 1 as real maps accumulate."""
    m = _safe_float(maps)
    if m is None or m <= 0:
        return 0.0
    return float(m) / (float(m) + BUCKET_PRIOR_MAPS)


def _personal_offset(base: float, pct: Dict[int, float], anchor_map: Dict[int, Tuple[float, float]]) -> float:
    """A player-level shift for the prior: the maps-weighted mean amount this
    player's REAL tiers deviate from the population average curve, shrunk by the
    player's total ranked maps (PERSONAL_PRIOR_MAPS).

    Rationale: the per-tier prior is anchored to the player's overall rating,
    which is inflated by games vs unranked/weak teams. A player with lots of
    ranked maps running below (or above) the average curve is strong evidence
    their whole curve should shift — so their sparse tiers (e.g. a 3-map top-10)
    are judged against their demonstrated ranked level, not the population's.
    """
    num = 0.0
    total_maps = 0.0
    for tier in TOP_TIERS:
        entry = anchor_map.get(int(tier))
        if not entry:
            continue
        raw_delta, maps = entry
        if maps <= 0:
            continue
        prior_delta = base * float(pct.get(int(tier), 0.0))
        num += (float(raw_delta) - prior_delta) * float(maps)
        total_maps += float(maps)
    if total_maps <= 0:
        return 0.0
    raw_offset = num / total_maps
    return raw_offset * total_maps / (total_maps + PERSONAL_PRIOR_MAPS)


def build_player_bucket_rows(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    overall = _safe_float(row.get("rating"))
    base = float(overall) if overall is not None else 1.0
    rows: List[Dict[str, Any]] = []
    anchor_map = {int(x): (float(y), float(w)) for x, y, w in build_player_delta_anchors(row)}
    # Each band shrinks toward the player's ADJUSTED prior: the average-player
    # curve for their overall rating (base * (1 + pct[tier])) shifted by a
    # player-level offset that captures how far their ranked sample as a whole
    # sits from that curve. So a small good/bad tier sample is judged against the
    # player's demonstrated ranked level, not just the (inflatable) overall.
    pct = get_average_bucket_pct()
    offset = _personal_offset(base, pct, anchor_map)
    for tier in (*TOP_TIERS, 100):
        entry = anchor_map.get(int(tier))
        # prior_delta = population avg-curve prior (shown as "Predicted");
        # adj_prior_delta adds the player-level offset (what shrinkage targets).
        prior_delta = 0.0 if int(tier) == 100 else base * float(pct.get(int(tier), 0.0))
        adj_prior_delta = prior_delta if int(tier) == 100 else prior_delta + offset
        if entry:
            raw_delta, maps = entry
            shrinkage_weight = _bucket_shrinkage_weight(maps)
            delta = adj_prior_delta + (float(raw_delta) - adj_prior_delta) * shrinkage_weight
            estimated = False
        elif int(tier) == 100:
            # Overall anchor is always present via build_player_delta_anchors.
            continue
        else:
            # No maps in this tier — estimate from the adjusted prior so the
            # curve reflects the player's ranked level, not a flat-extrapolated
            # neighbour or the un-shifted population prior.
            raw_delta = adj_prior_delta
            maps = 0.0
            shrinkage_weight = 0.0
            delta = adj_prior_delta
            estimated = True
        rank_min, rank_max = BUCKET_RANGES.get(int(tier), (int(tier), int(tier)))
        midpoint = (float(rank_min) + float(rank_max)) / 2.0
        rows.append(
            {
                "tier": int(tier),
                "tier_label": _tier_label(int(tier)),
                "rank_min": int(rank_min),
                "rank_max": int(rank_max),
                "rank_midpoint": midpoint,
                "raw_bucket_delta": float(raw_delta),
                "raw_bucket_rating": float(base + raw_delta),
                "prior_delta": float(prior_delta),
                "prior_rating": float(base + prior_delta),
                "personal_offset": float(offset),
                "adjusted_prior_rating": float(base + adj_prior_delta),
                "shrinkage_weight": float(shrinkage_weight),
                "bucket_delta": float(delta),
                "bucket_rating": float(base + delta),
                "maps": float(maps),
                "estimated": bool(estimated),
            }
        )
    return rows


def _linear_interpolate(points: List[Tuple[float, float]], x: float) -> float:
    if not points:
        return 1.0
    ordered = sorted(points, key=lambda p: p[0])
    if x <= ordered[0][0]:
        return float(ordered[0][1])
    if x >= ordered[-1][0]:
        return float(ordered[-1][1])

    for idx in range(1, len(ordered)):
        x0, y0 = ordered[idx - 1]
        x1, y1 = ordered[idx]
        if x <= x1:
            if x1 == x0:
                return float(y1)
            t = (float(x) - float(x0)) / (float(x1) - float(x0))
            return float(y0) + t * (float(y1) - float(y0))
    return float(ordered[-1][1])


def predict_rating_vs_rank(row: Dict[str, Any], opponent_rank: Any) -> Optional[float]:
    """
    Predict player rating against an opponent at HLTV rank X.

    Uses the same non-overlapping Top-X bucket subtraction as the graph,
    then linearly interpolates between Top-X boundary anchors. Ranks beyond the
    stored Top-50 range return None so callers can fall back to overall rating.
    """
    try:
        rank = float(opponent_rank)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rank) or rank <= 0 or rank > 50:
        return None

    points: List[Tuple[float, float]] = []
    for bucket in build_player_bucket_rows(row):
        tier = int(bucket.get("tier") or 0)
        if tier not in TOP_TIERS:
            continue
        rating = _safe_float(bucket.get("bucket_rating"))
        if rating is None:
            continue
        points.append((float(tier), float(rating)))

    if not points:
        # No per-tier data: estimate from the player's overall rating scaled by
        # the average fractional deprivation curve, overall * (1 + pct[tier]), so
        # rank still matters instead of a flat overall rating for everyone.
        overall = _safe_float(row.get("rating"))
        if overall is not None and _AVERAGE_BUCKET_PCT:
            est = [(float(t), overall * (1.0 + pct)) for t, pct in _AVERAGE_BUCKET_PCT.items()]
            return _linear_interpolate(est, rank)
        return None
    return _linear_interpolate(points, rank)
