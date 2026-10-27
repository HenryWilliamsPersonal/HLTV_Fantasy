import math
import random
import re
import json
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
import logging

from fastapi import APIRouter, HTTPException
from dateutil.relativedelta import relativedelta

from backend.routes.players import _INTERRUPTION_BOILERPLATE
from backend.routes.admin import _extract_hltv_event_ref_from_fantasy, _import_money_draft_data
from backend.services.hltv_rankings import (
    get_recent_hltv_results,
    get_hltv_match_details,
    get_hltv_team_map_stats_for_range,
    get_all_hltv_rankings_on_or_before_date,
    get_all_vrs_rankings_on_or_before_date,
    HLTVRankingError,
)
from backend.data.db import connect as event_db_connect
from backend.data.singleton_state import SingletonState
from backend.data import team_aliases
from backend.data.event_db import (
    get_historical_team_map_stats_keys,
    clear_hltv_results,
    count_hltv_results,
    dedupe_hltv_results_by_match_id,
    get_cached_hltv_rankings_on_or_before_date,
    get_cached_vrs_rankings_on_or_before_date,
    get_active_event_id,
    delete_event,
    get_event_detail,
    get_event_playoff_layout,
    get_event_tournament_kind,
    list_deleted_event_ids,
    set_event_playoff_layout,
    get_player_latest_teams,
    set_event_tournament_kind,
    get_hltv_result_by_url,
    get_imported_match_ids,
    get_all_historical_team_map_stats_json,
    get_historical_team_map_stats,
    update_hltv_result_details,
    list_hltv_results,
    set_hltv_result_maps,
    list_events,
    set_active_event,
    set_event_name,
    upsert_historical_team_map_stats,
    upsert_hltv_rankings_snapshot,
    upsert_vrs_rankings_snapshot,
    update_hltv_results_points,
    upsert_hltv_results,
)
from backend.data.team_db import get_all_teams
from backend.services.hltv_browser import HLTVBrowserError, fetch_hltv_json

router = APIRouter()
logger = logging.getLogger(__name__)
HLTV_RESULTS_IMPORT_JOBS: Dict[str, Dict[str, Any]] = {}
HLTV_RESULTS_IMPORT_JOBS_LOCK = threading.Lock()
HLTV_RESULTS_IMPORT_JOB_DIR = Path(__file__).resolve().parents[2] / ".runtime" / "hltv_results_import_jobs"


def _norm_team_name(name: str) -> str:
    """Normalised team name with renames resolved (data/team_aliases.py), so
    an organisation's earlier name keys the same team everywhere."""
    return team_aliases.canonical(re.sub(r"[^a-z0-9]+", "", str(name or "").strip().lower()))


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


# First 6 entries are the rank-only baseline (see _fit_round_share_logistic_2d's
# [:6] slice); map-informed features must be appended after them.
# Ridge strength for the map-model fit. Chosen 2026-09-21 on a held-out slice:
# 1e-2 with one pooled model beat 1e-4 and beat per-map models (a few hundred
# maps each against fourteen inputs overfit; coefficients flipped sign between
# maps and the held-out score fell below the rank-only baseline).
_MAP_MODEL_L2 = 1e-2
# The lab's evaluation is walk-forward: usable maps in date order, the oldest
# _MAP_MODEL_INITIAL_TRAIN share fits the first model, then each of
# _MAP_MODEL_BLOCKS chronological blocks is scored by a model fitted on
# everything before it. Every scored map is out of sample and the scored set
# is ~60% of the pool, so the calibration tails have samples (a 1,000-map
# holdout left 20-40 maps in the 75%+ buckets). Chosen 2026-09-24.
_MAP_MODEL_INITIAL_TRAIN = 0.4
_MAP_MODEL_BLOCKS = 6


def _lab_match_key(obj: Dict[str, Any]) -> str:
    """Stable per-match key shared by result rows and their map samples."""
    url = str(obj.get("match_url") or "").strip()
    if url:
        return url
    return f"{obj.get('team1')}|{obj.get('team2')}|{obj.get('match_date')}"

ROUND_SHARE_FEATURES: Tuple[Tuple[str, str], ...] = (
    ("hltv_gap", "hltv_gap"),
    ("hltv_gap_level", "hltv_gap_level"),
    ("vrs_gap", "vrs_gap"),
    ("vrs_gap_level", "vrs_gap_level"),
    ("log_rank_gap", "log_rank_gap"),
    ("log_rank_gap_level", "log_rank_gap_level"),
    ("map_win_gap", "map_win_gap"),
    ("pick_gap", "pick_gap"),
    ("ban_gap", "ban_gap"),
    ("played_pct_gap", "played_pct_gap"),
    ("picked_by_a", "picked_by_a"),
    ("player_rating_gap", "player_rating_gap"),
)
# Every feature is a signed team1-minus-team2 difference, so a map seen from
# team2's side is the same row negated (_fit_map_model_set). Level-only
# inputs (average rank, "map stats available") were dropped on 2026-09-23:
# under that mirroring they cannot carry weight, and walk-forward confirmed
# the fit is identical without them. The team Elo went the same day: with the
# per-player rating present its weight was ~0 and the score did not move.
# Bumped when the feature set changes so cached fits report stale.
_MAP_MODEL_FEATURE_VERSION = 7


def _log_rank_features(rank_a: float, rank_b: float) -> Dict[str, float]:
    """ln-rank gap and gap x level. Rank differences are not linear in
    strength: 1 vs 5 is a far bigger gap than 41 vs 45, and the linear gap
    features alone left the model timid about the top of the table (rank 1
    teams won 71% of out-of-sample maps against a 64% prediction). These
    removed that bias (ranks 1-5 off by +3.4 pts -> 0.0) in the 2026-09-23
    walk-forward factor test."""
    la = math.log(max(1.0, float(rank_a)))
    lb = math.log(max(1.0, float(rank_b)))
    return {"log_rank_gap": lb - la, "log_rank_gap_level": (lb - la) * (la + lb) / 2.0}


def _fit_round_share_logistic_2d(samples: List[Dict[str, float]]) -> Dict[str, float]:
    if len(samples) < 20:
        raise ValueError("Not enough round-share samples.")
    feature_defs = ROUND_SHARE_FEATURES
    ys = [float(s["round_share"]) for s in samples]
    ws = [max(1.0, float(s.get("weight") or 1.0)) for s in samples]
    l2 = float(_MAP_MODEL_L2)
    lr = 0.03
    iterations = 3500
    names = [name for name, _sample_key in feature_defs]
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy is in requirements.txt
        np = None
    if np is not None:
        # Same weighted z-scoring and full-batch gradient descent as the loop
        # below, vectorised: training on every stored match takes seconds.
        X = np.array([[float(sample.get(sample_key) or 0.0) for _name, sample_key in feature_defs] for sample in samples], dtype=float)
        y = np.array(ys, dtype=float)
        w = np.array(ws, dtype=float)
        total_w = max(1.0, float(w.sum()))
        mean = (X * w[:, None]).sum(axis=0) / total_w
        var = (w[:, None] * (X - mean) ** 2).sum(axis=0) / total_w
        std = np.where(var > 1e-12, np.sqrt(np.maximum(var, 0.0)), 1.0)
        Z = (X - mean) / std
        a = 0.0
        b = np.zeros(len(names), dtype=float)
        for _ in range(iterations):
            p = 1.0 / (1.0 + np.exp(-np.clip(a + Z @ b, -60.0, 60.0)))
            e = (p - y) * w
            ga = float(e.sum()) / total_w + l2 * a
            gb = (Z * e[:, None]).sum(axis=0) / total_w + l2 * b
            a -= lr * ga
            b -= lr * gb
        coefs = {name: float(b[i]) for i, name in enumerate(names)}
        means = {name: float(mean[i]) for i, name in enumerate(names)}
        stds = {name: float(std[i]) for i, name in enumerate(names)}
    else:
        total_w = max(1.0, sum(ws))
        raw_features: Dict[str, List[float]] = {
            name: [float(sample.get(sample_key) or 0.0) for sample in samples]
            for name, sample_key in feature_defs
        }
        means = {}
        stds = {}
        z_features: Dict[str, List[float]] = {}
        for name, values in raw_features.items():
            mean_v = sum(x * wt for x, wt in zip(values, ws)) / total_w
            var_v = sum(wt * (x - mean_v) ** 2 for x, wt in zip(values, ws)) / total_w
            std_v = math.sqrt(var_v) if var_v > 1e-12 else 1.0
            means[name] = mean_v
            stds[name] = std_v
            z_features[name] = [(x - mean_v) / std_v for x in values]
        a = 0.0
        coefs = {name: 0.0 for name in names}
        for _ in range(iterations):
            ga = 0.0
            gradients = {name: 0.0 for name in names}
            for idx, (yv, wt) in enumerate(zip(ys, ws)):
                z = a
                for name in names:
                    z += coefs[name] * z_features[name][idx]
                p = _sigmoid(z)
                e = (p - yv) * wt
                ga += e
                for name in names:
                    gradients[name] += e * z_features[name][idx]
            ga = ga / total_w + l2 * a
            a -= lr * ga
            for name in names:
                gradients[name] = gradients[name] / total_w + l2 * coefs[name]
                coefs[name] -= lr * gradients[name]

    model = {
        "a": a,
        "samples": len(samples),
        "rounds": int(round(total_w)),
    }
    for name, _sample_key in feature_defs:
        model[f"b_{name}"] = coefs[name]
        model[f"mean_{name}"] = means[name]
        model[f"std_{name}"] = stds[name]
    return model


_EVENT_NAME_ACRONYMS = {
    "iem", "esl", "pgl", "blast", "ewc", "cct", "cs2", "csgo", "rmr", "vrs",
    "eu", "na", "sa", "apac", "mena", "esea", "wesg", "lan",
}


def _event_name_from_url(url: str) -> str:
    """Readable event name from an HLTV event URL slug
    (…/events/7148/iem-cologne-2026 → 'IEM Cologne 2026')."""
    m = re.search(r"/events/\d+/([a-z0-9-]+)", str(url or "").lower())
    if not m:
        return ""
    words = [w for w in m.group(1).split("-") if w]
    return " ".join(
        w.upper() if w in _EVENT_NAME_ACRONYMS else (w if w.isdigit() else w.capitalize())
        for w in words
    )


_EVENT_NAMES_BACKFILLED = False


def _backfill_event_names() -> None:
    """One-shot per process: derive names for events missing one — from the
    stored HLTV event URL slug, or by re-parsing archived fantasy-page
    snapshots for events imported before the ref was captured. No scraping."""
    global _EVENT_NAMES_BACKFILLED
    if _EVENT_NAMES_BACKFILLED:
        return
    _EVENT_NAMES_BACKFILLED = True
    from backend.data.page_snapshots import get_page_snapshot
    from backend.routes.admin import _extract_hltv_event_ref

    for ev in list_events():
        if ev.get("name"):
            continue
        eid = int(ev["event_id"])
        name = _event_name_from_url(str(ev.get("hltv_event_url") or ""))
        ref: Dict[str, Any] = {}
        if not name:
            for candidate in (
                f"https://www.hltv.org/fantasy/{eid}",
                f"https://www.hltv.org/fantasy/{eid}/overview",
                f"https://www.hltv.org/fantasy/{eid}/leagues/create",
            ):
                snap = get_page_snapshot(candidate)
                if not snap:
                    continue
                try:
                    ref = _extract_hltv_event_ref(snap["html"]) or {}
                except Exception:
                    ref = {}
                name = _event_name_from_url(str(ref.get("hltv_event_url") or ""))
                if name:
                    break
        if name:
            set_event_name(
                eid,
                name,
                hltv_event_id=ref.get("hltv_event_id"),
                hltv_event_url=ref.get("hltv_event_url"),
            )


@router.get("/")
def list_all_events():
    try:
        _backfill_event_names()
    except Exception:
        logger.info("Event name backfill failed", exc_info=True)
    from backend.services import event_pipeline

    rows = list_events()
    for row in rows:
        try:
            row["valuation"] = event_pipeline.event_status(int(row["event_id"]))
        except Exception:  # noqa: BLE001 - the list must never fail on status
            row["valuation"] = None
    return {
        "active_event_id": get_active_event_id(),
        "events": rows,
    }


@router.get("/active")
def get_active_event():
    active_id = get_active_event_id()
    if active_id is None:
        return {"active_event_id": None, "event": None}
    event = get_event_detail(active_id)
    return {"active_event_id": active_id, "event": event}


@router.get("/hltv-recent-results")
def fetch_hltv_recent_results(limit: int = 100):
    try:
        return get_recent_hltv_results(limit=limit)
    except HLTVRankingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hltv_results_import_job_path(job_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "", str(job_id or ""))
    if not safe:
        safe = "job"
    return HLTV_RESULTS_IMPORT_JOB_DIR / f"{safe}.json"


def _persist_hltv_results_import_job(job_id: str, job: Dict[str, Any] | None = None) -> None:
    try:
        HLTV_RESULTS_IMPORT_JOB_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = dict(job or HLTV_RESULTS_IMPORT_JOBS.get(job_id) or {})
        if not snapshot:
            return
        snapshot.pop("thread", None)
        tmp = _hltv_results_import_job_path(job_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=True), encoding="utf-8")
        tmp.replace(_hltv_results_import_job_path(job_id))
    except Exception as exc:
        logger.warning("Failed to persist HLTV results import job %s: %s", job_id, exc)


def _load_hltv_results_import_job(job_id: str) -> Dict[str, Any] | None:
    path = _hltv_results_import_job_path(job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Failed to load HLTV results import job %s: %s", job_id, exc)
    return None


def _latest_hltv_results_import_job() -> Dict[str, Any] | None:
    try:
        files = sorted(HLTV_RESULTS_IMPORT_JOB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return None
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


def _hydrate_hltv_results_import_job(job_id: str) -> Dict[str, Any] | None:
    with HLTV_RESULTS_IMPORT_JOBS_LOCK:
        existing = HLTV_RESULTS_IMPORT_JOBS.get(job_id)
        if existing:
            return dict(existing)
    loaded = _load_hltv_results_import_job(job_id)
    if not loaded:
        return None
    loaded.pop("thread", None)
    status = str(loaded.get("status") or "")
    if status in {"queued", "running", "pausing"}:
        loaded["status"] = "queued"
        loaded["phase"] = loaded.get("phase") or "queued"
        loaded["current"] = "Restarting after app restart"
        loaded["pause_requested"] = False
        loaded["updated_at"] = _utc_now_iso()
    with HLTV_RESULTS_IMPORT_JOBS_LOCK:
        HLTV_RESULTS_IMPORT_JOBS[job_id] = loaded
    _persist_hltv_results_import_job(job_id, loaded)
    return dict(loaded)


def _normalize_hltv_results_import_payload(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    body = payload or {}
    start_offset = max(0, int(body.get("start_offset", 0)))
    page_stride = max(1, int(body.get("page_stride", 100)))
    per_page_limit = max(1, min(100, int(body.get("per_page_limit", 100))))
    import_mode = str(body.get("import_mode") or body.get("mode") or "").strip().lower()
    until_date = str(body.get("until_date") or "").strip()
    if until_date:
        try:
            until_date = date.fromisoformat(until_date).isoformat()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="until_date must be YYYY-MM-DD") from exc
    if import_mode not in {"max_pages", "until_date"}:
        import_mode = "until_date" if until_date else "max_pages"
    if import_mode == "until_date" and not until_date:
        raise HTTPException(status_code=400, detail="until_date is required in until-date import mode")
    pages = max(1, int(body.get("pages", 1)))
    try:
        max_hltv_rank = max(0, min(500, int(str(body.get("max_hltv_rank") or "0").strip() or "0")))
    except Exception:
        max_hltv_rank = 0
    rank_filter_mode = str(body.get("rank_filter_mode") or "both").strip().lower()
    if rank_filter_mode not in {"both", "either"}:
        rank_filter_mode = "both"
    offsets = [start_offset + i * page_stride for i in range(pages)] if import_mode == "max_pages" else []
    return {
        "pages": pages,
        "start_offset": start_offset,
        "page_stride": page_stride,
        "per_page_limit": per_page_limit,
        "max_hltv_rank": max_hltv_rank,
        "rank_filter_mode": rank_filter_mode,
        "import_mode": import_mode,
        "until_date": until_date,
        "offsets": offsets,
    }


def _attach_hltv_ranks_to_result_rows(
    rows: List[Dict[str, Any]],
    max_days_back: int = 7,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, int]:
    unique_dates = sorted({str(r.get("match_date")) for r in rows if r.get("match_date")})
    hltv_cache: Dict[str, Dict[str, Any]] = {}
    dates_ok = 0
    dates_total = len(unique_dates)
    for idx, ds in enumerate(unique_dates):
        if progress:
            progress(
                {
                    "phase": "filtering_by_rank",
                    "current": f"Checking ranking date {idx + 1} / {dates_total}: {ds}",
                    "ranking_dates_done": idx,
                    "ranking_dates_total": dates_total,
                }
            )
        cached = get_cached_hltv_rankings_on_or_before_date(ds, max_days_back=max_days_back)
        if cached and (cached.get("rankings_by_team") or {}):
            hltv_cache[ds] = cached
            dates_ok += 1
        else:
            try:
                snap = get_all_hltv_rankings_on_or_before_date(date.fromisoformat(ds), max_days_back=max_days_back)
                hltv_cache[ds] = snap
                dates_ok += 1
                upsert_hltv_rankings_snapshot(
                    str(snap.get("effective_date") or ds),
                    snap.get("rankings_by_team") or {},
                    str(snap.get("url") or ""),
                )
            except Exception:
                hltv_cache[ds] = {}
        if progress:
            progress(
                {
                    "phase": "filtering_by_rank",
                    "current": f"Checked ranking date {idx + 1} / {dates_total}: {ds}",
                    "ranking_dates_done": idx + 1,
                    "ranking_dates_total": dates_total,
                }
            )

    ranked_rows = 0
    for row in rows:
        ds = str(row.get("match_date") or "")
        if not ds:
            continue
        snap = hltv_cache.get(ds) or {}
        hltv_map = (snap.get("rankings_by_team") or {}) if isinstance(snap, dict) else {}
        team1 = str(row.get("team1") or "")
        team2 = str(row.get("team2") or "")
        r1 = (hltv_map.get(_norm_team_name(team1)) or {}).get("hltv_rating")
        r2 = (hltv_map.get(_norm_team_name(team2)) or {}).get("hltv_rating")
        p1 = (hltv_map.get(_norm_team_name(team1)) or {}).get("points")
        p2 = (hltv_map.get(_norm_team_name(team2)) or {}).get("points")
        if r1 is not None:
            row["hltv_rank_1"] = int(r1)
        if r2 is not None:
            row["hltv_rank_2"] = int(r2)
        if p1 is not None:
            row["hltv_points_1"] = int(p1)
        if p2 is not None:
            row["hltv_points_2"] = int(p2)
        if r1 is not None or r2 is not None:
            row["hltv_effective_date"] = snap.get("effective_date") if isinstance(snap, dict) else None
            ranked_rows += 1
    return {"dates_requested": len(unique_dates), "dates_ok": dates_ok, "ranked_rows": ranked_rows}


def _row_passes_hltv_rank_filter(row: Dict[str, Any], max_rank: int, mode: str) -> bool:
    if max_rank <= 0:
        return True
    ranks = []
    for key in ("hltv_rank_1", "hltv_rank_2"):
        try:
            value = int(row.get(key))
            if value > 0:
                ranks.append(value)
        except Exception:
            pass
    if mode == "either":
        return any(rank <= max_rank for rank in ranks)
    return len(ranks) == 2 and all(rank <= max_rank for rank in ranks)


_MATCH_PAGE_DATE_RE = re.compile(r'class="date"[^>]*data-unix="(\d{10,13})"')


def _match_date_from_match_page(match_url: str) -> str | None:
    """The match's day (UTC) from its archived HLTV match page — the header's
    data-unix stamp — for results the /results page listed without a day
    (its featured "big results" block carries no date). None when the page
    is not archived or has no stamp."""
    import datetime as _dt

    from backend.data.page_snapshots import get_page_snapshot

    try:
        snap = get_page_snapshot(match_url)
    except Exception:  # noqa: BLE001
        return None
    html = (snap or {}).get("html") or ""
    m = _MATCH_PAGE_DATE_RE.search(html) or re.search(r'data-unix="(\d{10,13})"', html)
    if not m:
        return None
    ts = int(m.group(1))
    if ts > 10_000_000_000:
        ts //= 1000
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).date().isoformat()


def _row_is_on_or_after(row: Dict[str, Any], cutoff: date) -> bool:
    raw = str(row.get("match_date") or "").strip()
    if not raw:
        return True
    try:
        return date.fromisoformat(raw) >= cutoff
    except Exception:
        return True


def _import_hltv_results_work(
    payload: Dict[str, Any] | None,
    progress: Callable[[Dict[str, Any]], None] | None = None,
    job_id: str | None = None,
) -> Dict[str, Any]:
    normalized = _normalize_hltv_results_import_payload(payload)
    pages = int(normalized["pages"])
    per_page_limit = int(normalized["per_page_limit"])
    offsets = list(normalized["offsets"])
    until_date = str(normalized.get("until_date") or "")
    until_date_obj = date.fromisoformat(until_date) if until_date else None
    import_mode = str(normalized.get("import_mode") or ("until_date" if until_date_obj else "max_pages"))
    max_hltv_rank = int(normalized.get("max_hltv_rank") or 0)
    rank_filter_mode = str(normalized.get("rank_filter_mode") or "both")

    job_state = _hydrate_hltv_results_import_job(job_id) if job_id else None
    rows: List[Dict[str, Any]] = list((job_state or {}).get("rows") or [])
    fetched_count = int((job_state or {}).get("fetched_count") or len(rows))
    rank_filtered_out = 0
    date_filtered_out = int((job_state or {}).get("date_filtered_out") or 0)
    rank_filter_meta = (job_state or {}).get("rank_filter_meta") or {"dates_requested": 0, "dates_ok": 0, "ranked_rows": 0}
    processed_units = int((job_state or {}).get("processed_units") or 0)
    total_units = int((job_state or {}).get("total_units") or (0 if import_mode == "until_date" else max(1, pages)))
    page_index = int((job_state or {}).get("page_index") or 0)
    offset = int((job_state or {}).get("next_offset") or normalized["start_offset"])
    fetch_complete = bool((job_state or {}).get("fetch_complete"))
    detail_index = int((job_state or {}).get("detail_index") or 0)
    detail_complete = bool((job_state or {}).get("detail_complete"))

    def pause_requested() -> bool:
        if not job_id:
            return False
        with HLTV_RESULTS_IMPORT_JOBS_LOCK:
            job = HLTV_RESULTS_IMPORT_JOBS.get(job_id) or {}
            return bool(job.get("pause_requested"))

    def cancel_requested() -> bool:
        if not job_id:
            return False
        with HLTV_RESULTS_IMPORT_JOBS_LOCK:
            job = HLTV_RESULTS_IMPORT_JOBS.get(job_id) or {}
            return bool(job.get("cancel_requested"))

    def stop_if_requested(where: str):
        """Return a terminal status dict when cancel/pause was requested, else None."""
        if cancel_requested():
            checkpoint(status="canceled", phase="canceled", current=f"Canceled {where}")
            return {"status": "canceled"}
        if pause_requested():
            checkpoint(status="paused", phase="paused", current=f"Paused {where}")
            return {"status": "paused"}
        return None

    def checkpoint(**updates: Any) -> None:
        if not job_id:
            return
        snapshot_updates = {
            "payload": normalized,
            "rows": rows,
            "fetched_count": fetched_count,
            "processed_units": processed_units,
            "total_units": total_units,
            "page_index": page_index,
            "next_offset": offset,
            "fetch_complete": fetch_complete,
            "detail_index": detail_index,
            "detail_complete": detail_complete,
            "date_filtered_out": date_filtered_out,
            "rank_filter_meta": rank_filter_meta,
        }
        snapshot_updates.update(updates)
        _update_hltv_results_import_job(job_id, **snapshot_updates)

    def report(**updates: Any) -> None:
        if progress is None:
            return
        progress(
            {
                "processed_units": processed_units,
                "total_units": total_units,
                "fetched": fetched_count or len(rows),
                "kept": len(rows),
                "rank_filtered_out": rank_filtered_out,
                "import_mode": import_mode,
                **updates,
            }
        )

    stopped = stop_if_requested("before starting")
    if stopped:
        return stopped

    if not fetch_complete:
        report(phase="fetching_results")
    while not fetch_complete:
        stopped = stop_if_requested(f"before page {page_index + 1}")
        if stopped:
            return stopped
        page_index += 1
        try:
            snapshot = get_recent_hltv_results(limit=per_page_limit, offsets=[offset])
        except HLTVRankingError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        page_rows = snapshot.get("results") or []
        rows.extend(page_rows)
        fetched_count += len(page_rows)
        processed_units += 1
        page_dates = []
        for row in page_rows:
            try:
                page_dates.append(date.fromisoformat(str(row.get("match_date") or "")))
            except Exception:
                pass
        report(
            phase="fetching_results",
            current=(
                f"Page {page_index} until {until_date}"
                if import_mode == "until_date"
                else f"Page {page_index} / {pages}"
            ),
            page_index=page_index,
            pages=pages if import_mode == "max_pages" else None,
        )
        if import_mode == "until_date":
            if not page_rows:
                fetch_complete = True
                break
            if page_dates and min(page_dates) <= until_date_obj:
                fetch_complete = True
                break
            offset += int(normalized["page_stride"])
            checkpoint(status="running", phase="fetching_results")
            continue
        if page_index >= pages:
            fetch_complete = True
            break
        offset += int(normalized["page_stride"])
        checkpoint(status="running", phase="fetching_results")

    checkpoint(status="running", phase="fetching_results")

    if not detail_complete and detail_index <= 0 and until_date_obj is not None and not (job_state or {}).get("date_filter_applied"):
        before_date_filter = len(rows)
        rows = [r for r in rows if _row_is_on_or_after(r, until_date_obj)]
        date_filtered_out = max(0, before_date_filter - len(rows))
        checkpoint(date_filter_applied=True)

    if not detail_complete and detail_index <= 0 and rows and not (job_state or {}).get("existing_filter_applied"):
        # Skip matches that were already fully imported (map details stored).
        imported_ids = get_imported_match_ids()
        before_existing_filter = len(rows)
        rows = [
            r
            for r in rows
            if not (r.get("match_id") is not None and int(r.get("match_id") or 0) in imported_ids)
        ]
        skipped_existing = max(0, before_existing_filter - len(rows))
        checkpoint(existing_filter_applied=True, skipped_existing=skipped_existing)
        report(
            phase="fetching_results",
            current=f"Skipped {skipped_existing} already-imported matches",
            skipped_existing=skipped_existing,
        )
    else:
        skipped_existing = int((job_state or {}).get("skipped_existing") or 0)

    if not detail_complete and detail_index <= 0 and max_hltv_rank > 0 and rows and not (job_state or {}).get("rank_filter_applied"):
        stopped = stop_if_requested("before rank filtering")
        if stopped:
            return stopped
        rank_filter_dates_total = len({str(r.get("match_date")) for r in rows if r.get("match_date")})
        total_units = max(
            1,
            processed_units
            + rank_filter_dates_total
            + max(0, len(rows) - detail_index)
            + 3,
        )
        checkpoint(total_units=total_units, phase="filtering_by_rank")
        report(
            phase="filtering_by_rank",
            current=f"Checking HLTV rank dates 0 / {rank_filter_dates_total}",
            total_units=total_units,
            ranking_dates_done=0,
            ranking_dates_total=rank_filter_dates_total,
        )

        def rank_filter_progress(updates: Dict[str, Any]) -> None:
            nonlocal processed_units
            done = int(updates.get("ranking_dates_done") or 0)
            processed = processed_units + done
            report(processed_units=processed, total_units=total_units, **updates)
            checkpoint(
                status="running",
                processed_units=processed,
                total_units=total_units,
                **updates,
            )

        rank_filter_meta = _attach_hltv_ranks_to_result_rows(rows, progress=rank_filter_progress)
        processed_units += rank_filter_dates_total
        before_filter = len(rows)
        rows = [r for r in rows if _row_passes_hltv_rank_filter(r, max_hltv_rank, rank_filter_mode)]
        rank_filtered_out = max(0, before_filter - len(rows))
        checkpoint(
            rank_filter_applied=True,
            rank_filtered_out=rank_filtered_out,
            processed_units=processed_units,
            current=f"Checked HLTV rank dates {rank_filter_dates_total} / {rank_filter_dates_total}",
        )

    total_units = max(1, processed_units + max(0, len(rows) - detail_index) + 3)
    checkpoint(total_units=total_units, phase="fetching_match_details")
    report(phase="fetching_match_details", detail_total=len(rows), detail_done=detail_index)
    for idx in range(detail_index, len(rows)):
        stopped = stop_if_requested(f"at match {idx + 1} / {len(rows)}")
        if stopped:
            return stopped
        r = rows[idx]
        url = str(r.get("match_url") or "").strip()
        if url:
            try:
                stored = get_hltv_result_by_url(url)
                if stored and stored.get("maps_json"):
                    r["maps_json"] = str(stored.get("maps_json") or "[]")
                    if stored.get("veto_json"):
                        r["veto_json"] = str(stored.get("veto_json"))
                    if stored.get("player_stats_json"):
                        r["player_stats_json"] = str(stored.get("player_stats_json"))
                    if stored.get("map_player_stats_json"):
                        r["map_player_stats_json"] = str(stored.get("map_player_stats_json"))
                else:
                    md = get_hltv_match_details(url)
                    maps = md.get("maps") or []
                    if maps:
                        r["maps_json"] = json.dumps(maps)
                    veto = md.get("veto") or []
                    if veto:
                        r["veto_json"] = json.dumps(veto)
                    player_stats = md.get("player_stats") or []
                    if player_stats:
                        r["player_stats_json"] = json.dumps(player_stats)
                    map_player_stats = md.get("map_player_stats") or {}
                    if map_player_stats:
                        r["map_player_stats_json"] = json.dumps(map_player_stats)
            except Exception:
                pass
        processed_units += 1
        detail_index = idx + 1
        report(
            phase="fetching_match_details",
            current=f"Match {detail_index} / {len(rows)}",
            detail_done=detail_index,
            detail_total=len(rows),
        )
        checkpoint(status="running", phase="fetching_match_details", current=f"Match {detail_index} / {len(rows)}")

    detail_complete = True
    checkpoint(detail_complete=True, phase="storing_results", current="Saving matches")
    report(phase="storing_results", current="Saving matches")
    # A match listed only in the featured block has no date from /results;
    # its match page (just fetched for the details) says when it was played.
    for r in rows:
        if not str(r.get("match_date") or "").strip():
            r["match_date"] = _match_date_from_match_page(str(r.get("match_url") or ""))
    stored = upsert_hltv_results(rows)
    processed_units += 1
    checkpoint(phase="deduping_results", current="Removing duplicates")
    report(phase="deduping_results", current="Removing duplicates")
    deduped_removed = dedupe_hltv_results_by_match_id()
    processed_units += 1
    checkpoint(phase="enriching_points", current="Enriching historical points")
    report(phase="enriching_points", current="Enriching historical points")
    enrich_limit = max(1, count_hltv_results())
    enrich_rows = list_hltv_results(limit=enrich_limit, offset=0)
    enrich_target_rows = [
        r
        for r in enrich_rows
        if str(r.get("match_date") or "").strip()
        and not any(
            r.get(k) is not None
            for k in ("hltv_points_1", "hltv_points_2", "vrs_points_1", "vrs_points_2")
        )
    ]
    enrich_ranking_dates_total = len({str(r.get("match_date")) for r in enrich_target_rows if r.get("match_date")}) * 2
    total_units = max(1, processed_units + max(1, enrich_ranking_dates_total))
    checkpoint(
        phase="enriching_points",
        current=f"Checking ranking dates 0 / {enrich_ranking_dates_total}",
        total_units=total_units,
        ranking_dates_done=0,
        ranking_dates_total=enrich_ranking_dates_total,
    )
    report(
        phase="enriching_points",
        current=f"Checking ranking dates 0 / {enrich_ranking_dates_total}",
        total_units=total_units,
        ranking_dates_done=0,
        ranking_dates_total=enrich_ranking_dates_total,
    )

    def enrich_progress(updates: Dict[str, Any]) -> None:
        done = int(updates.get("ranking_dates_done") or 0)
        processed = processed_units + done
        report(processed_units=processed, total_units=total_units, **updates)
        checkpoint(
            status="running",
            processed_units=processed,
            total_units=total_units,
            **updates,
        )

    enrich = _enrich_hltv_results_historical_points(
        {
            "limit": enrich_limit,
            "max_days_back": 7,
            "refresh_all": False,
            "auto_backfill_dates": True,
        },
        progress=enrich_progress,
    )
    processed_units += max(1, enrich_ranking_dates_total)

    result = {
        "status": "ok",
        "requested_pages": pages,
        "fetched_pages": page_index,
        "offsets": offsets,
        "fetched": int(fetched_count),
        "kept": len(rows),
        "rank_filtered_out": int(rank_filtered_out),
        "date_filtered_out": int(date_filtered_out),
        "skipped_existing": int(skipped_existing),
        "max_hltv_rank": max_hltv_rank,
        "rank_filter_mode": rank_filter_mode,
        "import_mode": import_mode,
        "until_date": until_date,
        "rank_filter_meta": rank_filter_meta,
        "inserted": int(stored.get("inserted", 0)),
        "updated": int(stored.get("updated", 0)),
        "deduped_removed": int(deduped_removed),
        "enriched_matches": int((enrich or {}).get("matches_updated", 0)),
    }
    report(phase="completed", current="Completed", result=result)
    return result


def _update_hltv_results_import_job(job_id: str, **updates: Any) -> None:
    snapshot: Dict[str, Any] | None = None
    with HLTV_RESULTS_IMPORT_JOBS_LOCK:
        job = HLTV_RESULTS_IMPORT_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = _utc_now_iso()
        snapshot = dict(job)
    _persist_hltv_results_import_job(job_id, snapshot)


def _run_hltv_results_import_job(job_id: str, payload: Dict[str, Any] | None) -> None:
    _update_hltv_results_import_job(job_id, status="running", phase="fetching_results", pause_requested=False)
    try:
        result = _import_hltv_results_work(payload, lambda updates: _update_hltv_results_import_job(job_id, **updates), job_id=job_id)
        if (result or {}).get("status") == "paused":
            _update_hltv_results_import_job(job_id, status="paused", phase="paused", current="Paused", pause_requested=False)
            return
        if (result or {}).get("status") == "canceled":
            _update_hltv_results_import_job(
                job_id,
                status="canceled",
                phase="canceled",
                current="Canceled",
                pause_requested=False,
                cancel_requested=False,
                finished_at=_utc_now_iso(),
            )
            return
        with HLTV_RESULTS_IMPORT_JOBS_LOCK:
            total_units = int((HLTV_RESULTS_IMPORT_JOBS.get(job_id) or {}).get("total_units") or 1)
        _update_hltv_results_import_job(
            job_id,
            status="completed",
            phase="completed",
            processed_units=total_units,
            result=result,
            error="",
            finished_at=_utc_now_iso(),
        )
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _update_hltv_results_import_job(
            job_id,
            status="failed",
            phase="failed",
            error=str(detail or "HLTV results import failed."),
            finished_at=_utc_now_iso(),
        )


def _start_hltv_results_import_thread(job_id: str) -> bool:
    with HLTV_RESULTS_IMPORT_JOBS_LOCK:
        job = HLTV_RESULTS_IMPORT_JOBS.get(job_id)
        if not job:
            return False
        status = str(job.get("status") or "")
        if status == "running":
            return True
        payload = dict(job.get("payload") or {})
        job["status"] = "queued"
        job["pause_requested"] = False
        job["updated_at"] = _utc_now_iso()
        snapshot = dict(job)
    _persist_hltv_results_import_job(job_id, snapshot)
    worker = threading.Thread(target=_run_hltv_results_import_job, args=(job_id, payload), daemon=True)
    worker.start()
    return True


@router.post("/hltv-results/import")
def import_hltv_results(payload: Dict[str, Any] | None = None):
    return _import_hltv_results_work(payload)


@router.post("/hltv-results/import/start")
def start_hltv_results_import(payload: Dict[str, Any] | None = None):
    with HLTV_RESULTS_IMPORT_JOBS_LOCK:
        for existing_id, existing_job in HLTV_RESULTS_IMPORT_JOBS.items():
            if existing_job.get("status") in {"queued", "running", "pausing", "canceling"}:
                return {"job_id": existing_id, "status": existing_job.get("status"), "reused": True}
        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        normalized = _normalize_hltv_results_import_payload(payload)
        HLTV_RESULTS_IMPORT_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "phase": "queued",
            "processed_units": 0,
            "total_units": 0 if normalized.get("import_mode") == "until_date" else int(normalized.get("pages", 1)),
            "fetched": 0,
            "error": "",
            "result": None,
            "rows": [],
            "page_index": 0,
            "next_offset": int(normalized.get("start_offset") or 0),
            "fetch_complete": False,
            "detail_index": 0,
            "detail_complete": False,
            "pause_requested": False,
            "cancel_requested": False,
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "payload": normalized,
        }
        snapshot = dict(HLTV_RESULTS_IMPORT_JOBS[job_id])

    _persist_hltv_results_import_job(job_id, snapshot)
    _start_hltv_results_import_thread(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/hltv-results/import/job/{job_id}")
def get_hltv_results_import_job(job_id: str):
    data = _hydrate_hltv_results_import_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job_id not found")
    if data.get("status") == "queued":
        _start_hltv_results_import_thread(job_id)
        data = _hydrate_hltv_results_import_job(job_id) or data
    processed = int(data.get("processed_units") or 0)
    total = int(data.get("total_units") or 0)
    data["progress"] = min(1.0, max(0.0, processed / total)) if total > 0 else 0.0
    data.pop("rows", None)
    return data


@router.get("/hltv-results/import/latest")
def get_latest_hltv_results_import_job():
    with HLTV_RESULTS_IMPORT_JOBS_LOCK:
        active = sorted(
            [dict(job) for job in HLTV_RESULTS_IMPORT_JOBS.values()],
            key=lambda job: str(job.get("updated_at") or ""),
            reverse=True,
        )
    data = active[0] if active else _latest_hltv_results_import_job()
    if not data:
        return {"exists": False}
    job_id = str(data.get("job_id") or "")
    hydrated = _hydrate_hltv_results_import_job(job_id) if job_id else data
    if hydrated and hydrated.get("status") == "queued":
        _start_hltv_results_import_thread(job_id)
        hydrated = _hydrate_hltv_results_import_job(job_id) or hydrated
    out = dict(hydrated or data)
    processed = int(out.get("processed_units") or 0)
    total = int(out.get("total_units") or 0)
    out["exists"] = True
    out["progress"] = min(1.0, max(0.0, processed / total)) if total > 0 else 0.0
    out.pop("rows", None)
    return out


@router.post("/hltv-results/import/job/{job_id}/pause")
def pause_hltv_results_import_job(job_id: str):
    data = _hydrate_hltv_results_import_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job_id not found")
    status = str(data.get("status") or "")
    if status in {"completed", "failed", "canceled", "paused"}:
        return get_hltv_results_import_job(job_id)
    _update_hltv_results_import_job(job_id, status="pausing", pause_requested=True, current="Pausing after current request")
    return get_hltv_results_import_job(job_id)


@router.post("/hltv-results/import/job/{job_id}/resume")
def resume_hltv_results_import_job(job_id: str):
    data = _hydrate_hltv_results_import_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job_id not found")
    status = str(data.get("status") or "")
    if status in {"completed", "canceled", "running"}:
        return get_hltv_results_import_job(job_id)
    _update_hltv_results_import_job(job_id, status="queued", pause_requested=False, cancel_requested=False, current="Resuming")
    _start_hltv_results_import_thread(job_id)
    return get_hltv_results_import_job(job_id)


@router.post("/hltv-results/import/job/{job_id}/cancel")
def cancel_hltv_results_import_job(job_id: str):
    data = _hydrate_hltv_results_import_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job_id not found")
    status = str(data.get("status") or "")
    if status in {"completed", "failed", "canceled"}:
        return get_hltv_results_import_job(job_id)
    if status in {"queued", "paused"}:
        # No worker is mid-request; cancel immediately.
        _update_hltv_results_import_job(
            job_id,
            status="canceled",
            phase="canceled",
            current="Canceled",
            pause_requested=False,
            cancel_requested=False,
            finished_at=_utc_now_iso(),
        )
        return get_hltv_results_import_job(job_id)
    _update_hltv_results_import_job(
        job_id, status="canceling", cancel_requested=True, current="Canceling after current request"
    )
    return get_hltv_results_import_job(job_id)


# Per-match detail blobs (maps, veto, scoreboards) are ~8 KB a row and only
# the match modal reads them; the list view fetches them on demand via
# /hltv-results/stored, so the browser page drops from ~680 KB to ~70 KB.
_RESULT_DETAIL_BLOBS = ("maps_json", "veto_json", "player_stats_json", "map_player_stats_json")


@router.get("/hltv-results")
def get_hltv_results(
    limit: int = 100,
    offset: int = 0,
    search: str = "",
    sort: str = "",
    vrs_rank: int = 0,
    vrs_scope: str = "",
    vrs_dir: str = "",
    details: bool = False,
):
    rows = list_hltv_results(
        limit=limit, offset=offset, search=search, sort=sort,
        vrs_rank=vrs_rank, vrs_scope=vrs_scope, vrs_dir=vrs_dir,
    )
    if not details:
        rows = [{k: v for k, v in row.items() if k not in _RESULT_DETAIL_BLOBS} for row in rows]
    return {
        "count": len(rows),
        "total": int(count_hltv_results()),
        "limit": int(limit),
        "offset": int(offset),
        "results": rows,
    }


@router.get("/map-play-distribution")
def get_map_play_distribution(months: int = 3, vrs_rank: int = 0, vrs_scope: str = "both", vrs_dir: str = "within"):
    """How often each map was played across stored matches in the window,
    with the same VRS-rank filter as the Matches browser (inside/outside top N,
    both teams or at least one; unranked counts as outside). Feeds the Maps
    page pie chart."""
    months = max(1, min(24, int(months or 3)))
    rank_n = max(0, int(vrs_rank or 0))
    scope = str(vrs_scope or "both").strip().lower()
    direction = str(vrs_dir or "within").strip().lower()
    cutoff = (date.today() - relativedelta(months=months)).isoformat()
    rows = list_hltv_results(limit=max(1, count_hltv_results()), offset=0)
    counts: Dict[str, int] = {}
    matches = 0
    for r in rows:
        md = str(r.get("match_date") or "")
        if not md or md < cutoff:
            continue
        if rank_n > 0:
            try:
                r1 = int(r.get("vrs_rank_1") or 0)
            except (TypeError, ValueError):
                r1 = 0
            try:
                r2 = int(r.get("vrs_rank_2") or 0)
            except (TypeError, ValueError):
                r2 = 0
            in1 = 0 < r1 <= rank_n
            in2 = 0 < r2 <= rank_n
            hit1 = in1 if direction == "within" else not in1
            hit2 = in2 if direction == "within" else not in2
            if not (hit1 and hit2 if scope == "both" else hit1 or hit2):
                continue
        try:
            maps = json.loads(str(r.get("maps_json") or "[]"))
        except Exception:
            maps = []
        counted = False
        for m in maps if isinstance(maps, list) else []:
            name = str((m or {}).get("map") or "").strip()
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
            counted = True
        if counted:
            matches += 1
    return {
        "status": "ok",
        "months": months,
        "vrs_rank": rank_n,
        "vrs_scope": scope,
        "vrs_dir": direction,
        "matches": matches,
        "total_maps": sum(counts.values()),
        "maps": [
            {"map": k, "played": v}
            for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }


@router.delete("/hltv-results")
def reset_hltv_results():
    deleted = clear_hltv_results()
    return {
        "status": "ok",
        "deleted": int(deleted),
    }


@router.post("/hltv-results/enrich-historical-points")
def enrich_hltv_results_historical_points(payload: Dict[str, Any] | None = None):
    return _enrich_hltv_results_historical_points(payload)


def _enrich_hltv_results_historical_points(
    payload: Dict[str, Any] | None = None,
    progress: Callable[[Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    body = payload or {}
    max_days_back = max(0, min(30, int(body.get("max_days_back", 7))))
    refresh_all = bool(body.get("refresh_all", False))
    auto_backfill_dates = bool(body.get("auto_backfill_dates", True))
    limit = int(body.get("limit", 0))
    if limit <= 0:
        limit = max(1, count_hltv_results())

    rows = list_hltv_results(limit=limit, offset=0)
    if not rows:
        raise HTTPException(status_code=400, detail="No stored HLTV results to enrich.")

    with_date = sum(1 for r in rows if str(r.get("match_date") or "").strip())
    logger.info(
        "HLTV historical enrich start: scanned=%d with_date=%d missing_date=%d max_days_back=%d refresh_all=%s",
        len(rows),
        with_date,
        max(0, len(rows) - with_date),
        max_days_back,
        refresh_all,
    )

    # Backfill match_date by re-fetching the known source offsets and upserting.
    if auto_backfill_dates and with_date == 0 and rows:
        offsets = sorted({int(r.get("source_offset") or 0) for r in rows})[:30]
        try:
            logger.info("HLTV historical enrich: attempting date backfill from live results offsets=%s", offsets)
            snapshot = get_recent_hltv_results(limit=len(offsets) * 100, offsets=offsets)
            fetched_rows = snapshot.get("results") or []
            stored = upsert_hltv_results(fetched_rows)
            logger.info(
                "HLTV historical enrich: date backfill completed fetched=%d inserted=%d updated=%d",
                len(fetched_rows),
                int(stored.get("inserted", 0)),
                int(stored.get("updated", 0)),
            )
            rows = list_hltv_results(limit=limit, offset=0)
            with_date = sum(1 for r in rows if str(r.get("match_date") or "").strip())
            logger.info(
                "HLTV historical enrich: post-backfill with_date=%d missing_date=%d",
                with_date,
                max(0, len(rows) - with_date),
            )
        except Exception as exc:
            logger.warning("HLTV historical enrich: date backfill attempt failed: %s", exc)

    target_rows = []
    for r in rows:
        if not str(r.get("match_date") or "").strip():
            continue
        has_any = any(
            r.get(k) is not None
            for k in ("hltv_points_1", "hltv_points_2", "vrs_points_1", "vrs_points_2")
        )
        if refresh_all or not has_any:
            target_rows.append(r)

    if not target_rows:
        sample_missing = [str(r.get("match_url") or "") for r in rows if not str(r.get("match_date") or "").strip()][:5]
        if sample_missing:
            logger.warning("HLTV historical enrich: no target rows. sample missing-date matches=%s", sample_missing)
        return {
            "status": "ok",
            "matches_scanned": len(rows),
            "matches_targeted": 0,
            "matches_updated": 0,
            "dates_requested": 0,
            "dates_hltv_ok": 0,
            "dates_vrs_ok": 0,
        }

    unique_dates = sorted({str(r.get("match_date")) for r in target_rows if r.get("match_date")})
    ranking_dates_total = len(unique_dates) * 2
    if progress:
        progress(
            {
                "phase": "enriching_points",
                "current": f"Checking ranking dates 0 / {ranking_dates_total}",
                "ranking_dates_done": 0,
                "ranking_dates_total": ranking_dates_total,
            }
        )
    hltv_cache: Dict[str, Dict[str, Any]] = {}
    vrs_cache: Dict[str, Dict[str, Any]] = {}
    dates_hltv_ok = 0
    dates_vrs_ok = 0
    dates_hltv_cache = 0
    dates_vrs_cache = 0
    dates_hltv_live = 0
    dates_vrs_live = 0

    ranking_dates_done = 0
    for ds in unique_dates:
        if progress:
            progress(
                {
                    "phase": "enriching_points",
                    "current": f"Checking HLTV ranking date {ranking_dates_done + 1} / {ranking_dates_total}: {ds}",
                    "ranking_dates_done": ranking_dates_done,
                    "ranking_dates_total": ranking_dates_total,
                }
            )
        cached_h = get_cached_hltv_rankings_on_or_before_date(ds, max_days_back=max_days_back)
        if cached_h and (cached_h.get("rankings_by_team") or {}):
            hltv_cache[ds] = cached_h
            dates_hltv_ok += 1
            dates_hltv_cache += 1
            logger.info(
                "HLTV historical enrich date cache hit: requested=%s effective=%s teams=%d",
                ds,
                cached_h.get("effective_date"),
                len(cached_h.get("rankings_by_team") or {}),
            )
        else:
            logger.info("HLTV historical enrich date cache miss: requested=%s", ds)
        try:
            d = date.fromisoformat(ds)
        except Exception:
            continue
        if ds not in hltv_cache:
            try:
                snap = get_all_hltv_rankings_on_or_before_date(d, max_days_back=max_days_back)
                hltv_cache[ds] = snap
                dates_hltv_ok += 1
                dates_hltv_live += 1
                upsert_hltv_rankings_snapshot(
                    str(snap.get("effective_date") or ds),
                    snap.get("rankings_by_team") or {},
                    str(snap.get("url") or ""),
                )
                logger.info(
                    "HLTV historical enrich date ok (live): requested=%s effective=%s teams=%d",
                    ds,
                    snap.get("effective_date"),
                    len(snap.get("rankings_by_team") or {}),
                )
            except Exception:
                hltv_cache[ds] = {}
                logger.warning("HLTV historical enrich date failed (HLTV): requested=%s", ds)

        ranking_dates_done += 1
        if progress:
            progress(
                {
                    "phase": "enriching_points",
                    "current": f"Checked HLTV ranking date {ranking_dates_done} / {ranking_dates_total}: {ds}",
                    "ranking_dates_done": ranking_dates_done,
                    "ranking_dates_total": ranking_dates_total,
                }
            )

        if progress:
            progress(
                {
                    "phase": "enriching_points",
                    "current": f"Checking VRS ranking date {ranking_dates_done + 1} / {ranking_dates_total}: {ds}",
                    "ranking_dates_done": ranking_dates_done,
                    "ranking_dates_total": ranking_dates_total,
                }
            )
        cached_v = get_cached_vrs_rankings_on_or_before_date(ds, max_days_back=max_days_back)
        if cached_v and (cached_v.get("rankings_by_team") or {}):
            vrs_cache[ds] = cached_v
            dates_vrs_ok += 1
            dates_vrs_cache += 1
            logger.info(
                "HLTV historical enrich date cache hit (VRS): requested=%s effective=%s teams=%d",
                ds,
                cached_v.get("effective_date"),
                len(cached_v.get("rankings_by_team") or {}),
            )
        else:
            logger.info("HLTV historical enrich date cache miss (VRS): requested=%s", ds)
            try:
                snap = get_all_vrs_rankings_on_or_before_date(d, max_days_back=max_days_back)
                vrs_cache[ds] = snap
                dates_vrs_ok += 1
                dates_vrs_live += 1
                upsert_vrs_rankings_snapshot(
                    str(snap.get("effective_date") or ds),
                    snap.get("rankings_by_team") or {},
                    str(snap.get("url") or ""),
                )
                logger.info(
                    "HLTV historical enrich date ok (VRS live): requested=%s effective=%s teams=%d",
                    ds,
                    snap.get("effective_date"),
                    len(snap.get("rankings_by_team") or {}),
                )
            except Exception:
                vrs_cache[ds] = {}
                logger.warning("HLTV historical enrich date failed (VRS): requested=%s", ds)

        ranking_dates_done += 1
        if progress:
            progress(
                {
                    "phase": "enriching_points",
                    "current": f"Checked VRS ranking date {ranking_dates_done} / {ranking_dates_total}: {ds}",
                    "ranking_dates_done": ranking_dates_done,
                    "ranking_dates_total": ranking_dates_total,
                }
            )

    updates: List[Dict[str, Any]] = []
    for r in target_rows:
        ds = str(r.get("match_date") or "")
        t1 = str(r.get("team1") or "")
        t2 = str(r.get("team2") or "")
        if not ds or not t1 or not t2:
            continue
        hk1 = _norm_team_name(t1)
        hk2 = _norm_team_name(t2)
        hltv_snap = hltv_cache.get(ds) or {}
        vrs_snap = vrs_cache.get(ds) or {}
        hltv_map = (hltv_snap.get("rankings_by_team") or {}) if isinstance(hltv_snap, dict) else {}
        vrs_map = (vrs_snap.get("rankings_by_team") or {}) if isinstance(vrs_snap, dict) else {}
        h1 = (hltv_map.get(hk1) or {}).get("points")
        h2 = (hltv_map.get(hk2) or {}).get("points")
        hr1 = (hltv_map.get(hk1) or {}).get("hltv_rating")
        hr2 = (hltv_map.get(hk2) or {}).get("hltv_rating")
        v1 = (vrs_map.get(hk1) or {}).get("points")
        v2 = (vrs_map.get(hk2) or {}).get("points")
        vr1 = (vrs_map.get(hk1) or {}).get("vrs_rank")
        vr2 = (vrs_map.get(hk2) or {}).get("vrs_rank")
        if h1 is None and h2 is None and v1 is None and v2 is None:
            continue
        updates.append(
            {
                "match_url": r.get("match_url"),
                "hltv_points_1": int(h1) if h1 is not None else None,
                "hltv_points_2": int(h2) if h2 is not None else None,
                "hltv_rank_1": int(hr1) if hr1 is not None else None,
                "hltv_rank_2": int(hr2) if hr2 is not None else None,
                "vrs_points_1": int(v1) if v1 is not None else None,
                "vrs_points_2": int(v2) if v2 is not None else None,
                "vrs_rank_1": int(vr1) if vr1 is not None else None,
                "vrs_rank_2": int(vr2) if vr2 is not None else None,
                "hltv_effective_date": hltv_snap.get("effective_date") if isinstance(hltv_snap, dict) else None,
                "vrs_effective_date": vrs_snap.get("effective_date") if isinstance(vrs_snap, dict) else None,
            }
        )

    matches_updated = update_hltv_results_points(updates)
    logger.info(
        "HLTV historical enrich complete: targeted=%d updates_prepared=%d matches_updated=%d dates=%d hltv_ok=%d(cache=%d live=%d) vrs_ok=%d(cache=%d live=%d)",
        len(target_rows),
        len(updates),
        int(matches_updated),
        len(unique_dates),
        dates_hltv_ok,
        dates_hltv_cache,
        dates_hltv_live,
        dates_vrs_ok,
        dates_vrs_cache,
        dates_vrs_live,
    )
    return {
        "status": "ok",
        "matches_scanned": len(rows),
        "matches_targeted": len(target_rows),
        "matches_updated": int(matches_updated),
        "dates_requested": len(unique_dates),
        "dates_hltv_ok": dates_hltv_ok,
        "dates_vrs_ok": dates_vrs_ok,
        "dates_hltv_cache": dates_hltv_cache,
        "dates_hltv_live": dates_hltv_live,
        "dates_vrs_cache": dates_vrs_cache,
        "dates_vrs_live": dates_vrs_live,
    }


MAP_POOL = ["Mirage", "Inferno", "Nuke", "Ancient", "Anubis", "Dust2", "Cache"]
# Maps outside the current pool still get their data stored and rated (Elo,
# vetoes, historical stats) so nothing is lost if they rotate back in.
KNOWN_MAPS = [*MAP_POOL, "Overpass", "Vertigo", "Train", "Cobblestone"]


def _canonical_map_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    aliases = {
        "dust 2": "Dust2",
        "dust2": "Dust2",
        "de_dust2": "Dust2",
    }
    key = raw.lower().replace("_", " ")
    if key.startswith("de "):
        key = key[3:]
    if key in aliases:
        return aliases[key]
    for name in KNOWN_MAPS:
        if key == name.lower():
            return name
    return raw[:32]


def _parse_stored_maps(value: Any) -> List[Dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        map_name = _canonical_map_name(item.get("map"))
        try:
            s1 = int(item.get("score1"))
            s2 = int(item.get("score2"))
        except Exception:
            continue
        if not map_name or s1 < 0 or s2 < 0:
            continue
        outcome = 1 if s1 > s2 else -1 if s1 < s2 else 0
        if s1 >= 12 and s2 >= 12:
            s1, s2 = 12, 12
        out.append({"map": map_name, "score1": s1, "score2": s2, "outcome": outcome})
    return out


def _parse_team_map_stats(value: Any) -> Dict[str, Dict[str, float]]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    if not isinstance(parsed, list):
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        map_name = _canonical_map_name(item.get("map"))
        if not map_name:
            continue
        pick_rate = _to_float_or_none(item.get("pick_rate"))
        ban_rate = _to_float_or_none(item.get("ban_rate"))
        win_rate = _to_float_or_none(item.get("win_rate"))
        played = _to_float_or_none(item.get("played"))
        out[map_name] = {
            "pick_rate": max(0.0, min(1.0, pick_rate if pick_rate is not None else 0.0)),
            "ban_rate": max(0.0, min(1.0, ban_rate if ban_rate is not None else 0.0)),
            "win_rate": max(0.0, min(1.0, win_rate if win_rate is not None else 0.0)),
            "played": max(0.0, played if played is not None else 0.0),
        }
    return out


def _historical_map_stats_window(match_date: Any) -> tuple[str, str] | None:
    try:
        played_on = date.fromisoformat(str(match_date or "")[:10])
    except Exception:
        return None
    end_date = played_on - relativedelta(days=1)
    start_date = end_date - relativedelta(months=6)
    return start_date.isoformat(), end_date.isoformat()


def _required_historical_windows(
    rows: List[Dict[str, Any]],
) -> tuple[Dict[tuple[str, str, str], Dict[str, Any]], set[str]]:
    """All (team, six-month window) pairs the given matches need pre-match map stats for."""
    teams_by_key = {_norm_team_name(team.get("name") or ""): team for team in get_all_teams()}
    required: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    missing_team_keys: set[str] = set()
    for row in rows:
        window = _historical_map_stats_window(row.get("match_date"))
        if not window:
            continue
        start_date, end_date = window
        for team_name in (row.get("team1"), row.get("team2")):
            key = _norm_team_name(team_name or "")
            if not key:
                continue
            team = teams_by_key.get(key)
            if not team or not team.get("hltv_team_id"):
                missing_team_keys.add(key)
                continue
            required[(key, start_date, end_date)] = {
                "key": key,
                "team_name": team.get("name") or team_name or key,
                "hltv_team_id": int(team.get("hltv_team_id")),
                "start_date": start_date,
                "end_date": end_date,
            }
    return required, missing_team_keys


def _build_historical_team_map_stats_by_window(
    rows: List[Dict[str, Any]],
    *,
    fetch_missing: bool = False,
) -> tuple[Dict[tuple[str, str, str], Dict[str, Dict[str, float]]], Dict[str, Any]]:
    required, missing_team_keys = _required_historical_windows(rows)
    stored_by_window = get_all_historical_team_map_stats_json()

    out: Dict[tuple[str, str, str], Dict[str, Dict[str, float]]] = {}
    fetched = 0
    failed = 0
    cache_hits = 0
    cache_misses = 0
    for cache_key, item in required.items():
        stored_json = stored_by_window.get((str(item["key"]), str(item["start_date"]), str(item["end_date"])))
        if stored_json is not None:
            cache_hits += 1
            out[cache_key] = _parse_team_map_stats(stored_json)
            continue
        cache_misses += 1
        if not fetch_missing:
            continue
        try:
            stats = get_hltv_team_map_stats_for_range(
                int(item["hltv_team_id"]),
                str(item["team_name"]),
                start_date=str(item["start_date"]),
                end_date=str(item["end_date"]),
            )
            maps_json = json.dumps(stats.get("maps") or [])
            upsert_historical_team_map_stats(
                normalized_name=str(item["key"]),
                team_name=str(item["team_name"]),
                hltv_team_id=int(item["hltv_team_id"]),
                start_date=str(item["start_date"]),
                end_date=str(item["end_date"]),
                maps_json=maps_json,
                source_url=str(stats.get("url") or ""),
            )
            out[cache_key] = _parse_team_map_stats(maps_json)
            fetched += 1
        except Exception as exc:
            logger.warning(
                "Historical map stats fetch failed: team=%s start=%s end=%s error=%s",
                item["team_name"],
                item["start_date"],
                item["end_date"],
                exc,
            )
            failed += 1

    return out, {
        "required_windows": len(required),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "fetched": fetched,
        "failed": failed,
        "unmapped_team_keys": len(missing_team_keys),
        "unmapped_team_examples": sorted(missing_team_keys)[:20],
        "fetch_missing": bool(fetch_missing),
    }


# --- Historical map-stats backfill as a pausable/cancelable background job ----
# Fills the permanent historical_team_map_stats table (one HLTV scrape per
# missing team/six-month-window pair) so the Model Lab can train on full data.

HISTORICAL_STATS_JOBS: Dict[str, Dict[str, Any]] = {}
HISTORICAL_STATS_WORKERS: Dict[str, threading.Thread] = {}
HISTORICAL_STATS_JOBS_LOCK = threading.Lock()


def ensure_historical_stats_job_schema() -> None:
    conn = event_db_connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS historical_map_stats_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                processed_items INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                current_item TEXT NOT NULL DEFAULT '',
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_historical_stats_jobs_updated ON historical_map_stats_jobs(updated_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def _historical_job_from_row(row: Any) -> Dict[str, Any]:
    out = dict(row)
    out["pause_requested"] = bool(out.get("pause_requested"))
    out["cancel_requested"] = bool(out.get("cancel_requested"))
    out["progress"] = float(out.get("progress") or 0.0)
    return out


def _get_stored_historical_job(job_id: str) -> Dict[str, Any] | None:
    conn = event_db_connect()
    try:
        row = conn.execute("SELECT * FROM historical_map_stats_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _historical_job_from_row(row) if row else None
    finally:
        conn.close()


def _save_historical_job(job: Dict[str, Any]) -> None:
    now = time.time()
    job["updated_at"] = now
    conn = event_db_connect()
    try:
        conn.execute(
            """
            INSERT INTO historical_map_stats_jobs (
                job_id, status, error, last_error, progress, processed_items, total_items,
                ok, failed, current_item, pause_requested, cancel_requested,
                created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                last_error = excluded.last_error,
                progress = excluded.progress,
                processed_items = excluded.processed_items,
                total_items = excluded.total_items,
                ok = excluded.ok,
                failed = excluded.failed,
                current_item = excluded.current_item,
                pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested,
                updated_at = excluded.updated_at,
                started_at = COALESCE(excluded.started_at, historical_map_stats_jobs.started_at),
                finished_at = excluded.finished_at
            """,
            (
                str(job["job_id"]),
                str(job.get("status") or "queued"),
                str(job.get("error") or ""),
                str(job.get("last_error") or ""),
                float(job.get("progress") or 0.0),
                int(job.get("processed_items") or 0),
                int(job.get("total_items") or 0),
                int(job.get("ok") or 0),
                int(job.get("failed") or 0),
                str(job.get("current_item") or ""),
                1 if job.get("pause_requested") else 0,
                1 if job.get("cancel_requested") else 0,
                float(job.get("created_at") or now),
                now,
                job.get("started_at"),
                job.get("finished_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _publish_historical_job(job: Dict[str, Any]) -> None:
    with HISTORICAL_STATS_JOBS_LOCK:
        HISTORICAL_STATS_JOBS[str(job["job_id"])] = dict(job)
    _save_historical_job(job)


def _historical_worker_is_active(job_id: str) -> bool:
    with HISTORICAL_STATS_JOBS_LOCK:
        worker = HISTORICAL_STATS_WORKERS.get(job_id)
        return bool(worker and worker.is_alive())


def _historical_job_response(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": str(job["job_id"]),
        "status": job.get("status", "queued"),
        "error": job.get("error", ""),
        "last_error": job.get("last_error", ""),
        "progress": job.get("progress", 0.0),
        "processed_items": job.get("processed_items", 0),
        "total_items": job.get("total_items", 0),
        "ok": job.get("ok", 0),
        "failed": job.get("failed", 0),
        "current_item": job.get("current_item", ""),
        "pause_requested": bool(job.get("pause_requested")),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _get_historical_job_for_response(job_id: str) -> Dict[str, Any]:
    with HISTORICAL_STATS_JOBS_LOCK:
        cached = HISTORICAL_STATS_JOBS.get(job_id)
        job = dict(cached) if cached else None
    if not job:
        job = _get_stored_historical_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.get("status") in {"queued", "running", "pausing", "canceling"} and not _historical_worker_is_active(job_id):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["last_error"] = job.get("last_error") or "Job was interrupted before completion. Resume to continue."
        job["error"] = ""
        job["pause_requested"] = False
        job["cancel_requested"] = False
        _publish_historical_job(job)
    return job


def _historical_missing_items() -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Current missing (team, window) pairs plus coverage counts, straight from the DB."""
    rows = list_hltv_results(limit=max(1, count_hltv_results()), offset=0)
    required, unmapped = _required_historical_windows(rows)
    stored_keys = get_historical_team_map_stats_keys()
    missing: List[Dict[str, Any]] = []
    cached = 0
    for cache_key, item in required.items():
        if cache_key in stored_keys:
            cached += 1
        else:
            missing.append(item)
    missing.sort(key=lambda it: (it["end_date"], it["key"]), reverse=True)
    coverage = {
        "required_windows": len(required),
        "cached_windows": cached,
        "missing_windows": len(missing),
        "unmapped_team_keys": len(unmapped),
        "unmapped_team_examples": sorted(unmapped)[:20],
    }
    return missing, coverage


def _run_historical_stats_job(job_id: str) -> None:
    job = _get_stored_historical_job(job_id)
    if not job:
        return

    def stop_requested() -> str:
        with HISTORICAL_STATS_JOBS_LOCK:
            live = HISTORICAL_STATS_JOBS.get(job_id) or {}
        if live.get("cancel_requested"):
            return "canceled"
        if live.get("pause_requested"):
            return "paused"
        return ""

    job["status"] = "running"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["started_at"] = job.get("started_at") or time.time()
    job["finished_at"] = None
    job["current_item"] = "Scanning stored matches for missing windows"
    _publish_historical_job(job)

    try:
        # Recomputed fresh on every (re)start, so already-fetched windows are
        # skipped automatically and resume always continues where it left off.
        missing, _coverage = _historical_missing_items()
        total = len(missing)
        processed = 0
        ok = int(job.get("ok") or 0)
        failed = int(job.get("failed") or 0)
        job["total_items"] = total
        job["processed_items"] = 0
        job["progress"] = 0.0 if total else 1.0
        _publish_historical_job(job)

        for item in missing:
            stopped = stop_requested()
            if stopped:
                job["status"] = stopped
                job["pause_requested"] = False
                job["cancel_requested"] = False
                job["last_error"] = "Canceled" if stopped == "canceled" else "Paused"
                job["finished_at"] = time.time()
                _publish_historical_job(job)
                return
            label = f"{item['team_name']} ({item['start_date']} to {item['end_date']})"
            job["current_item"] = label
            try:
                stats = get_hltv_team_map_stats_for_range(
                    int(item["hltv_team_id"]),
                    str(item["team_name"]),
                    start_date=str(item["start_date"]),
                    end_date=str(item["end_date"]),
                )
                upsert_historical_team_map_stats(
                    normalized_name=str(item["key"]),
                    team_name=str(item["team_name"]),
                    hltv_team_id=int(item["hltv_team_id"]),
                    start_date=str(item["start_date"]),
                    end_date=str(item["end_date"]),
                    maps_json=json.dumps(stats.get("maps") or []),
                    source_url=str(stats.get("url") or ""),
                )
                ok += 1
            except Exception as exc:
                failed += 1
                job["last_error"] = f"{label}: {exc}"
            processed += 1
            job["processed_items"] = processed
            job["ok"] = ok
            job["failed"] = failed
            job["progress"] = processed / float(total) if total else 1.0
            # Merge live pause/cancel requests before publishing, so this
            # snapshot does not erase a request made while we were fetching.
            with HISTORICAL_STATS_JOBS_LOCK:
                live = HISTORICAL_STATS_JOBS.get(job_id) or {}
            job["pause_requested"] = bool(live.get("pause_requested"))
            job["cancel_requested"] = bool(live.get("cancel_requested"))
            if job["cancel_requested"]:
                job["status"] = "canceling"
            elif job["pause_requested"]:
                job["status"] = "pausing"
            else:
                job["status"] = "running"
            _publish_historical_job(job)

        job["status"] = "completed"
        job["current_item"] = ""
        job["finished_at"] = time.time()
        _publish_historical_job(job)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = time.time()
        _publish_historical_job(job)
    finally:
        with HISTORICAL_STATS_JOBS_LOCK:
            HISTORICAL_STATS_WORKERS.pop(job_id, None)


def _start_historical_stats_worker(job_id: str) -> None:
    with HISTORICAL_STATS_JOBS_LOCK:
        worker = HISTORICAL_STATS_WORKERS.get(job_id)
        if worker and worker.is_alive():
            return
        worker = threading.Thread(target=_run_historical_stats_job, args=(job_id,), daemon=True)
        HISTORICAL_STATS_WORKERS[job_id] = worker
        worker.start()


def _get_latest_historical_job() -> Dict[str, Any] | None:
    conn = event_db_connect()
    try:
        row = conn.execute("SELECT job_id FROM historical_map_stats_jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return _get_historical_job_for_response(str(row["job_id"])) if row else None


@router.get("/hltv-results/historical-map-stats/coverage")
def get_historical_map_stats_coverage():
    _missing, coverage = _historical_missing_items()
    return {"status": "ok", **coverage}


@router.post("/hltv-results/historical-map-stats/start")
def start_historical_map_stats_job():
    latest = _get_latest_historical_job()
    if latest and latest.get("status") in {"queued", "running", "pausing", "canceling"}:
        return {"job_id": latest["job_id"], "status": latest.get("status"), "reused": True}
    now = time.time()
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    job = {
        "job_id": job_id,
        "status": "queued",
        "error": "",
        "last_error": "",
        "progress": 0.0,
        "processed_items": 0,
        "total_items": 0,
        "ok": 0,
        "failed": 0,
        "current_item": "",
        "pause_requested": False,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    _publish_historical_job(job)
    _start_historical_stats_worker(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/hltv-results/historical-map-stats/latest")
def get_latest_historical_map_stats_job():
    job = _get_latest_historical_job()
    if not job:
        return {"exists": False}
    return {"exists": True, **_historical_job_response(job)}


@router.get("/hltv-results/historical-map-stats/job/{job_id}")
def get_historical_map_stats_job(job_id: str):
    return _historical_job_response(_get_historical_job_for_response(job_id))


@router.post("/hltv-results/historical-map-stats/job/{job_id}/pause")
def pause_historical_map_stats_job(job_id: str):
    job = _get_historical_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed", "paused"}:
        return _historical_job_response(job)
    job["pause_requested"] = True
    job["status"] = "pausing" if _historical_worker_is_active(job_id) else "paused"
    _publish_historical_job(job)
    return _historical_job_response(job)


@router.post("/hltv-results/historical-map-stats/job/{job_id}/cancel")
def cancel_historical_map_stats_job(job_id: str):
    job = _get_historical_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed"}:
        return _historical_job_response(job)
    job["cancel_requested"] = True
    job["status"] = "canceling" if _historical_worker_is_active(job_id) else "canceled"
    if job["status"] == "canceled":
        job["cancel_requested"] = False
        job["finished_at"] = time.time()
    _publish_historical_job(job)
    return _historical_job_response(job)


@router.post("/hltv-results/historical-map-stats/job/{job_id}/resume")
def resume_historical_map_stats_job(job_id: str):
    job = _get_historical_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled"}:
        return _historical_job_response(job)
    if job.get("status") == "running" and _historical_worker_is_active(job_id):
        return _historical_job_response(job)
    job["status"] = "queued"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["finished_at"] = None
    _publish_historical_job(job)
    _start_historical_stats_worker(job_id)
    return _historical_job_response(_get_historical_job_for_response(job_id))


# --- Veto/match-detail backfill as a pausable/cancelable background job -------
# Re-fetches stored match pages to fill veto_json (and player stats / maps when
# missing) for rows imported before those columns were captured. A fetched page
# without a veto box stores '[]' so it is never refetched.

VETO_BACKFILL_JOBS: Dict[str, Dict[str, Any]] = {}
VETO_BACKFILL_WORKERS: Dict[str, threading.Thread] = {}
VETO_BACKFILL_JOBS_LOCK = threading.Lock()


def ensure_veto_backfill_job_schema() -> None:
    conn = event_db_connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS veto_backfill_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                processed_items INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                current_item TEXT NOT NULL DEFAULT '',
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _veto_job_from_row(row) -> Dict[str, Any]:
    job = dict(row)
    job["pause_requested"] = bool(job.get("pause_requested"))
    job["cancel_requested"] = bool(job.get("cancel_requested"))
    return job


def _get_stored_veto_job(job_id: str) -> Dict[str, Any] | None:
    conn = event_db_connect()
    try:
        row = conn.execute("SELECT * FROM veto_backfill_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _veto_job_from_row(row) if row else None
    finally:
        conn.close()


def _save_veto_job(job: Dict[str, Any]) -> None:
    now = time.time()
    job["updated_at"] = now
    conn = event_db_connect()
    try:
        conn.execute(
            """
            INSERT INTO veto_backfill_jobs (
                job_id, status, error, last_error, progress, processed_items, total_items,
                ok, failed, current_item, pause_requested, cancel_requested,
                created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                last_error = excluded.last_error,
                progress = excluded.progress,
                processed_items = excluded.processed_items,
                total_items = excluded.total_items,
                ok = excluded.ok,
                failed = excluded.failed,
                current_item = excluded.current_item,
                pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested,
                updated_at = excluded.updated_at,
                started_at = COALESCE(excluded.started_at, veto_backfill_jobs.started_at),
                finished_at = excluded.finished_at
            """,
            (
                str(job["job_id"]),
                str(job.get("status") or "queued"),
                str(job.get("error") or ""),
                str(job.get("last_error") or ""),
                float(job.get("progress") or 0.0),
                int(job.get("processed_items") or 0),
                int(job.get("total_items") or 0),
                int(job.get("ok") or 0),
                int(job.get("failed") or 0),
                str(job.get("current_item") or ""),
                1 if job.get("pause_requested") else 0,
                1 if job.get("cancel_requested") else 0,
                float(job.get("created_at") or now),
                now,
                job.get("started_at"),
                job.get("finished_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _publish_veto_job(job: Dict[str, Any]) -> None:
    with VETO_BACKFILL_JOBS_LOCK:
        VETO_BACKFILL_JOBS[str(job["job_id"])] = dict(job)
    _save_veto_job(job)


def _veto_worker_is_active(job_id: str) -> bool:
    with VETO_BACKFILL_JOBS_LOCK:
        worker = VETO_BACKFILL_WORKERS.get(job_id)
        return bool(worker and worker.is_alive())


def _veto_job_response(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": str(job["job_id"]),
        "status": job.get("status", "queued"),
        "error": job.get("error", ""),
        "last_error": job.get("last_error", ""),
        "progress": job.get("progress", 0.0),
        "processed_items": job.get("processed_items", 0),
        "total_items": job.get("total_items", 0),
        "ok": job.get("ok", 0),
        "failed": job.get("failed", 0),
        "current_item": job.get("current_item", ""),
        "pause_requested": bool(job.get("pause_requested")),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _get_veto_job_for_response(job_id: str) -> Dict[str, Any]:
    with VETO_BACKFILL_JOBS_LOCK:
        cached = VETO_BACKFILL_JOBS.get(job_id)
        job = dict(cached) if cached else None
    if not job:
        job = _get_stored_veto_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.get("status") in {"queued", "running", "pausing", "canceling"} and not _veto_worker_is_active(job_id):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["last_error"] = job.get("last_error") or "Job was interrupted before completion. Resume to continue."
        job["error"] = ""
        job["pause_requested"] = False
        job["cancel_requested"] = False
        _publish_veto_job(job)
    return job


def _veto_missing_items() -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = list_hltv_results(limit=max(1, count_hltv_results()), offset=0)
    missing: List[Dict[str, Any]] = []
    with_veto = 0
    for r in rows:
        url = str(r.get("match_url") or "").strip()
        if not url:
            continue
        if r.get("veto_json"):
            with_veto += 1
            continue
        missing.append(
            {
                "match_url": url,
                "match_date": str(r.get("match_date") or ""),
                "label": f"{r.get('team1')} vs {r.get('team2')} ({r.get('match_date')})",
                "has_maps": bool(r.get("maps_json")),
                "has_player_stats": bool(r.get("player_stats_json")),
            }
        )
    missing.sort(key=lambda it: it["match_date"], reverse=True)
    coverage = {
        "total_matches": len(rows),
        "with_veto": with_veto,
        "missing_veto": len(missing),
    }
    return missing, coverage


def _run_veto_backfill_job(job_id: str) -> None:
    job = _get_stored_veto_job(job_id)
    if not job:
        return

    def stop_requested() -> str:
        with VETO_BACKFILL_JOBS_LOCK:
            live = VETO_BACKFILL_JOBS.get(job_id) or {}
        if live.get("cancel_requested"):
            return "canceled"
        if live.get("pause_requested"):
            return "paused"
        return ""

    job["status"] = "running"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["started_at"] = job.get("started_at") or time.time()
    job["finished_at"] = None
    job["current_item"] = "Scanning stored matches for missing vetoes"
    _publish_veto_job(job)

    try:
        # Recomputed fresh on every (re)start; rows with veto_json (including
        # stored '[]' for pages without a veto box) are skipped automatically.
        missing, _coverage = _veto_missing_items()
        total = len(missing)
        processed = 0
        ok = int(job.get("ok") or 0)
        failed = int(job.get("failed") or 0)
        job["total_items"] = total
        job["processed_items"] = 0
        job["progress"] = 0.0 if total else 1.0
        _publish_veto_job(job)

        for item in missing:
            stopped = stop_requested()
            if stopped:
                job["status"] = stopped
                job["pause_requested"] = False
                job["cancel_requested"] = False
                job["last_error"] = "Canceled" if stopped == "canceled" else "Paused"
                job["finished_at"] = time.time()
                _publish_veto_job(job)
                return
            job["current_item"] = item["label"]
            try:
                md = get_hltv_match_details(item["match_url"])
                veto = md.get("veto") or []
                player_stats = md.get("player_stats") or []
                maps = md.get("maps") or []
                map_player_stats = md.get("map_player_stats") or {}
                update_hltv_result_details(
                    item["match_url"],
                    veto_json=json.dumps(veto),
                    player_stats_json=json.dumps(player_stats) if player_stats and not item["has_player_stats"] else None,
                    maps_json=json.dumps(maps) if maps and not item["has_maps"] else None,
                    map_player_stats_json=json.dumps(map_player_stats) if map_player_stats else None,
                )
                ok += 1
            except Exception as exc:
                failed += 1
                job["last_error"] = f"{item['label']}: {exc}"
            processed += 1
            job["processed_items"] = processed
            job["ok"] = ok
            job["failed"] = failed
            job["progress"] = processed / float(total) if total else 1.0
            # Merge live pause/cancel requests before publishing, so this
            # snapshot does not erase a request made while we were fetching.
            with VETO_BACKFILL_JOBS_LOCK:
                live = VETO_BACKFILL_JOBS.get(job_id) or {}
            job["pause_requested"] = bool(live.get("pause_requested"))
            job["cancel_requested"] = bool(live.get("cancel_requested"))
            if job["cancel_requested"]:
                job["status"] = "canceling"
            elif job["pause_requested"]:
                job["status"] = "pausing"
            else:
                job["status"] = "running"
            _publish_veto_job(job)

        job["status"] = "completed"
        job["current_item"] = ""
        job["finished_at"] = time.time()
        _publish_veto_job(job)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = time.time()
        _publish_veto_job(job)
    finally:
        with VETO_BACKFILL_JOBS_LOCK:
            VETO_BACKFILL_WORKERS.pop(job_id, None)


def _start_veto_backfill_worker(job_id: str) -> None:
    with VETO_BACKFILL_JOBS_LOCK:
        worker = VETO_BACKFILL_WORKERS.get(job_id)
        if worker and worker.is_alive():
            return
        worker = threading.Thread(target=_run_veto_backfill_job, args=(job_id,), daemon=True)
        VETO_BACKFILL_WORKERS[job_id] = worker
        worker.start()


def _get_latest_veto_job() -> Dict[str, Any] | None:
    conn = event_db_connect()
    try:
        row = conn.execute("SELECT job_id FROM veto_backfill_jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return _get_veto_job_for_response(str(row["job_id"])) if row else None


@router.get("/page-snapshots/summary")
def get_page_snapshots_summary():
    from backend.data.page_snapshots import snapshot_summary

    return {"status": "ok", **snapshot_summary()}


@router.post("/hltv-results/reparse-from-snapshots")
def reparse_match_details_from_snapshots(limit: int = 0):
    """Re-run the current match-page parsers over archived page snapshots and
    update stored rows — no scraping. Parsed fields only overwrite when the
    fresh parse found data, so a parser regression cannot wipe stored values.
    """
    from backend.data.page_snapshots import get_page_snapshot
    from backend.services.hltv_rankings import parse_hltv_match_details_html

    rows = list_hltv_results(limit=max(1, count_hltv_results()), offset=0)
    if limit and int(limit) > 0:
        rows = rows[: int(limit)]
    checked = 0
    with_snapshot = 0
    updated = 0
    for r in rows:
        url = str(r.get("match_url") or "").strip()
        if not url:
            continue
        checked += 1
        snap = get_page_snapshot(url)
        if not snap:
            continue
        with_snapshot += 1
        details = parse_hltv_match_details_html(snap["html"], url)
        maps = details.get("maps") or []
        veto = details.get("veto") or []
        player_stats = details.get("player_stats") or []
        map_player_stats = details.get("map_player_stats") or {}
        update_hltv_result_details(
            url,
            maps_json=json.dumps(maps) if maps else None,
            veto_json=json.dumps(veto) if veto else None,
            player_stats_json=json.dumps(player_stats) if player_stats else None,
            map_player_stats_json=json.dumps(map_player_stats) if map_player_stats else None,
        )
        if maps or veto or player_stats or map_player_stats:
            updated += 1
    return {
        "status": "ok",
        "matches_checked": checked,
        "matches_with_snapshot": with_snapshot,
        "matches_updated": updated,
    }


@router.get("/hltv-results/veto-backfill/coverage")
def get_veto_backfill_coverage():
    _missing, coverage = _veto_missing_items()
    return {"status": "ok", **coverage}


@router.post("/hltv-results/veto-backfill/start")
def start_veto_backfill_job():
    latest = _get_latest_veto_job()
    if latest and latest.get("status") in {"queued", "running", "pausing", "canceling"}:
        return {"job_id": latest["job_id"], "status": latest.get("status"), "reused": True}
    now = time.time()
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    job = {
        "job_id": job_id,
        "status": "queued",
        "error": "",
        "last_error": "",
        "progress": 0.0,
        "processed_items": 0,
        "total_items": 0,
        "ok": 0,
        "failed": 0,
        "current_item": "",
        "pause_requested": False,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    _publish_veto_job(job)
    _start_veto_backfill_worker(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/hltv-results/veto-backfill/latest")
def get_latest_veto_backfill_job():
    job = _get_latest_veto_job()
    if not job:
        return {"exists": False}
    return {"exists": True, **_veto_job_response(job)}


@router.get("/hltv-results/veto-backfill/job/{job_id}")
def get_veto_backfill_job(job_id: str):
    return _veto_job_response(_get_veto_job_for_response(job_id))


@router.post("/hltv-results/veto-backfill/job/{job_id}/pause")
def pause_veto_backfill_job(job_id: str):
    job = _get_veto_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed", "paused"}:
        return _veto_job_response(job)
    job["pause_requested"] = True
    job["status"] = "pausing" if _veto_worker_is_active(job_id) else "paused"
    _publish_veto_job(job)
    return _veto_job_response(job)


@router.post("/hltv-results/veto-backfill/job/{job_id}/cancel")
def cancel_veto_backfill_job(job_id: str):
    job = _get_veto_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed"}:
        return _veto_job_response(job)
    job["cancel_requested"] = True
    job["status"] = "canceling" if _veto_worker_is_active(job_id) else "canceled"
    if job["status"] == "canceled":
        job["cancel_requested"] = False
        job["finished_at"] = time.time()
    _publish_veto_job(job)
    return _veto_job_response(job)


@router.post("/hltv-results/veto-backfill/job/{job_id}/resume")
def resume_veto_backfill_job(job_id: str):
    job = _get_veto_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled"}:
        return _veto_job_response(job)
    if job.get("status") == "running" and _veto_worker_is_active(job_id):
        return _veto_job_response(job)
    job["status"] = "queued"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["finished_at"] = None
    _publish_veto_job(job)
    _start_veto_backfill_worker(job_id)
    return _veto_job_response(_get_veto_job_for_response(job_id))


# ---- per-map scoreboard backfill job ----------------------------------------
# Fills hltv_results.map_player_stats_json (per-map player scoreboards). Most
# rows are covered by re-parsing the archived page snapshot — no scraping —
# and only matches with no snapshot fall back to a live fetch. Same pause /
# cancel / resume contract as the veto backfill; the scheduler auto-resumes it
# after backend restarts.

MAP_SB_JOBS: Dict[str, Dict[str, Any]] = {}
MAP_SB_WORKERS: Dict[str, threading.Thread] = {}
MAP_SB_JOBS_LOCK = threading.Lock()


def ensure_map_sb_job_schema() -> None:
    conn = event_db_connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS map_scoreboard_backfill_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                processed_items INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                current_item TEXT NOT NULL DEFAULT '',
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _get_stored_map_sb_job(job_id: str) -> Dict[str, Any] | None:
    conn = event_db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM map_scoreboard_backfill_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _veto_job_from_row(row) if row else None
    finally:
        conn.close()


def _save_map_sb_job(job: Dict[str, Any]) -> None:
    now = time.time()
    job["updated_at"] = now
    conn = event_db_connect()
    try:
        conn.execute(
            """
            INSERT INTO map_scoreboard_backfill_jobs (
                job_id, status, error, last_error, progress, processed_items, total_items,
                ok, failed, current_item, pause_requested, cancel_requested,
                created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                last_error = excluded.last_error,
                progress = excluded.progress,
                processed_items = excluded.processed_items,
                total_items = excluded.total_items,
                ok = excluded.ok,
                failed = excluded.failed,
                current_item = excluded.current_item,
                pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested,
                updated_at = excluded.updated_at,
                started_at = COALESCE(excluded.started_at, map_scoreboard_backfill_jobs.started_at),
                finished_at = excluded.finished_at
            """,
            (
                str(job["job_id"]),
                str(job.get("status") or "queued"),
                str(job.get("error") or ""),
                str(job.get("last_error") or ""),
                float(job.get("progress") or 0.0),
                int(job.get("processed_items") or 0),
                int(job.get("total_items") or 0),
                int(job.get("ok") or 0),
                int(job.get("failed") or 0),
                str(job.get("current_item") or ""),
                1 if job.get("pause_requested") else 0,
                1 if job.get("cancel_requested") else 0,
                float(job.get("created_at") or now),
                now,
                job.get("started_at"),
                job.get("finished_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _publish_map_sb_job(job: Dict[str, Any]) -> None:
    with MAP_SB_JOBS_LOCK:
        MAP_SB_JOBS[str(job["job_id"])] = dict(job)
    _save_map_sb_job(job)


def _map_sb_worker_is_active(job_id: str) -> bool:
    with MAP_SB_JOBS_LOCK:
        worker = MAP_SB_WORKERS.get(job_id)
        return bool(worker and worker.is_alive())


def _get_map_sb_job_for_response(job_id: str) -> Dict[str, Any]:
    with MAP_SB_JOBS_LOCK:
        cached = MAP_SB_JOBS.get(job_id)
        job = dict(cached) if cached else None
    if not job:
        job = _get_stored_map_sb_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.get("status") in {"queued", "running", "pausing", "canceling"} and not _map_sb_worker_is_active(job_id):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["last_error"] = job.get("last_error") or "Job was interrupted before completion. Resume to continue."
        job["error"] = ""
        job["pause_requested"] = False
        job["cancel_requested"] = False
        _publish_map_sb_job(job)
    return job


def _map_sb_missing_items() -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = list_hltv_results(limit=max(1, count_hltv_results()), offset=0)
    missing: List[Dict[str, Any]] = []
    have = 0
    for r in rows:
        url = str(r.get("match_url") or "").strip()
        if not url:
            continue
        # Stored "{}" means the page was scanned and has no per-map tabs
        # (forfeit / no scoreboard) — counted as covered, never revisited.
        if r.get("map_player_stats_json"):
            have += 1
            continue
        missing.append(
            {
                "match_url": url,
                "match_date": str(r.get("match_date") or ""),
                "label": f"{r.get('team1')} vs {r.get('team2')} ({r.get('match_date')})",
                "has_maps": bool(r.get("maps_json")),
                "has_veto": bool(r.get("veto_json")),
                "has_player_stats": bool(r.get("player_stats_json")),
            }
        )
    missing.sort(key=lambda it: it["match_date"], reverse=True)
    coverage = {
        "total_matches": len(rows),
        "with_map_scoreboards": have,
        "missing_map_scoreboards": len(missing),
    }
    return missing, coverage


def _run_map_sb_job(job_id: str) -> None:
    from backend.data.page_snapshots import get_page_snapshot
    from backend.services.hltv_rankings import parse_hltv_match_details_html

    job = _get_stored_map_sb_job(job_id)
    if not job:
        return

    def stop_requested() -> str:
        with MAP_SB_JOBS_LOCK:
            live = MAP_SB_JOBS.get(job_id) or {}
        if live.get("cancel_requested"):
            return "canceled"
        if live.get("pause_requested"):
            return "paused"
        return ""

    job["status"] = "running"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["started_at"] = job.get("started_at") or time.time()
    job["finished_at"] = None
    job["current_item"] = "Scanning stored matches for missing per-map scoreboards"
    _publish_map_sb_job(job)

    try:
        missing, _coverage = _map_sb_missing_items()
        total = len(missing)
        processed = 0
        ok = int(job.get("ok") or 0)
        failed = int(job.get("failed") or 0)
        job["total_items"] = total
        job["processed_items"] = 0
        job["progress"] = 0.0 if total else 1.0
        _publish_map_sb_job(job)

        for item in missing:
            stopped = stop_requested()
            if stopped:
                job["status"] = stopped
                job["pause_requested"] = False
                job["cancel_requested"] = False
                job["last_error"] = "Canceled" if stopped == "canceled" else "Paused"
                job["finished_at"] = time.time()
                _publish_map_sb_job(job)
                return
            job["current_item"] = item["label"]
            try:
                snap = get_page_snapshot(item["match_url"])
                if snap:
                    details = parse_hltv_match_details_html(snap["html"], item["match_url"])
                else:
                    # No archived page — live fetch (which archives the page).
                    details = get_hltv_match_details(item["match_url"])
                map_player_stats = details.get("map_player_stats") or {}
                maps = details.get("maps") or []
                veto = details.get("veto") or []
                player_stats = details.get("player_stats") or []
                # Always store the per-map result ("{}" marks pages with no
                # per-map tabs as scanned); the page was parsed anyway, so also
                # fill any other details the row is missing.
                update_hltv_result_details(
                    item["match_url"],
                    map_player_stats_json=json.dumps(map_player_stats),
                    maps_json=json.dumps(maps) if maps and not item["has_maps"] else None,
                    veto_json=json.dumps(veto) if veto and not item["has_veto"] else None,
                    player_stats_json=json.dumps(player_stats) if player_stats and not item["has_player_stats"] else None,
                )
                ok += 1
            except Exception as exc:
                failed += 1
                job["last_error"] = f"{item['label']}: {exc}"
            processed += 1
            job["processed_items"] = processed
            job["ok"] = ok
            job["failed"] = failed
            job["progress"] = processed / float(total) if total else 1.0
            with MAP_SB_JOBS_LOCK:
                live = MAP_SB_JOBS.get(job_id) or {}
            job["pause_requested"] = bool(live.get("pause_requested"))
            job["cancel_requested"] = bool(live.get("cancel_requested"))
            if job["cancel_requested"]:
                job["status"] = "canceling"
            elif job["pause_requested"]:
                job["status"] = "pausing"
            else:
                job["status"] = "running"
            _publish_map_sb_job(job)

        job["status"] = "completed"
        job["current_item"] = ""
        job["finished_at"] = time.time()
        _publish_map_sb_job(job)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = time.time()
        _publish_map_sb_job(job)
    finally:
        with MAP_SB_JOBS_LOCK:
            MAP_SB_WORKERS.pop(job_id, None)


def _start_map_sb_worker(job_id: str) -> None:
    with MAP_SB_JOBS_LOCK:
        worker = MAP_SB_WORKERS.get(job_id)
        if worker and worker.is_alive():
            return
        worker = threading.Thread(target=_run_map_sb_job, args=(job_id,), daemon=True)
        MAP_SB_WORKERS[job_id] = worker
        worker.start()


def _get_latest_map_sb_job() -> Dict[str, Any] | None:
    ensure_map_sb_job_schema()
    conn = event_db_connect()
    try:
        row = conn.execute(
            "SELECT job_id FROM map_scoreboard_backfill_jobs ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return _get_map_sb_job_for_response(str(row["job_id"])) if row else None


@router.get("/hltv-results/map-scoreboards/coverage")
def get_map_scoreboards_coverage():
    _missing, coverage = _map_sb_missing_items()
    return {"status": "ok", **coverage}


@router.post("/hltv-results/map-scoreboards/start")
def start_map_scoreboards_job():
    ensure_map_sb_job_schema()
    latest = _get_latest_map_sb_job()
    if latest and latest.get("status") in {"queued", "running", "pausing", "canceling"}:
        return {"job_id": latest["job_id"], "status": latest.get("status"), "reused": True}
    now = time.time()
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    job = {
        "job_id": job_id,
        "status": "queued",
        "error": "",
        "last_error": "",
        "progress": 0.0,
        "processed_items": 0,
        "total_items": 0,
        "ok": 0,
        "failed": 0,
        "current_item": "",
        "pause_requested": False,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    _publish_map_sb_job(job)
    _start_map_sb_worker(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/hltv-results/map-scoreboards/latest")
def get_latest_map_scoreboards_job():
    ensure_map_sb_job_schema()
    job = _get_latest_map_sb_job()
    if not job:
        return {"exists": False}
    return {"exists": True, **_veto_job_response(job)}


@router.get("/hltv-results/map-scoreboards/job/{job_id}")
def get_map_scoreboards_job(job_id: str):
    return _veto_job_response(_get_map_sb_job_for_response(job_id))


@router.post("/hltv-results/map-scoreboards/job/{job_id}/pause")
def pause_map_scoreboards_job(job_id: str):
    job = _get_map_sb_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed", "paused"}:
        return _veto_job_response(job)
    job["pause_requested"] = True
    job["status"] = "pausing" if _map_sb_worker_is_active(job_id) else "paused"
    _publish_map_sb_job(job)
    return _veto_job_response(job)


@router.post("/hltv-results/map-scoreboards/job/{job_id}/cancel")
def cancel_map_scoreboards_job(job_id: str):
    job = _get_map_sb_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed"}:
        return _veto_job_response(job)
    job["cancel_requested"] = True
    job["status"] = "canceling" if _map_sb_worker_is_active(job_id) else "canceled"
    if job["status"] == "canceled":
        job["cancel_requested"] = False
        job["finished_at"] = time.time()
    _publish_map_sb_job(job)
    return _veto_job_response(job)


@router.post("/hltv-results/map-scoreboards/job/{job_id}/resume")
def resume_map_scoreboards_job(job_id: str):
    job = _get_map_sb_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled"}:
        return _veto_job_response(job)
    if job.get("status") == "running" and _map_sb_worker_is_active(job_id):
        return _veto_job_response(job)
    job["status"] = "queued"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["finished_at"] = None
    _publish_map_sb_job(job)
    _start_map_sb_worker(job_id)
    return _veto_job_response(_get_map_sb_job_for_response(job_id))


def _map_stat_features(
    team_a_stats: Dict[str, Dict[str, float]] | None,
    team_b_stats: Dict[str, Dict[str, float]] | None,
    map_name: str,
) -> Dict[str, float]:
    a_stats = (team_a_stats or {}).get(map_name) or {}
    b_stats = (team_b_stats or {}).get(map_name) or {}
    if not a_stats or not b_stats:
        return {
            "map_win_a": 0.0,
            "map_win_b": 0.0,
            "map_win_gap": 0.0,
            "pick_a": 0.0,
            "pick_b": 0.0,
            "pick_gap": 0.0,
            "ban_a": 0.0,
            "ban_b": 0.0,
            "ban_gap": 0.0,
            "played_pct_a": 0.0,
            "played_pct_b": 0.0,
            "played_pct_gap": 0.0,
            "map_stats_available": 0.0,
        }
    a_total_played = sum(max(0.0, float(row.get("played") or 0.0)) for row in (team_a_stats or {}).values())
    b_total_played = sum(max(0.0, float(row.get("played") or 0.0)) for row in (team_b_stats or {}).values())
    a_played_pct = max(0.0, float(a_stats.get("played") or 0.0)) / a_total_played if a_total_played > 0 else 0.0
    b_played_pct = max(0.0, float(b_stats.get("played") or 0.0)) / b_total_played if b_total_played > 0 else 0.0
    a_win = float(a_stats.get("win_rate") or 0.0)
    b_win = float(b_stats.get("win_rate") or 0.0)
    a_pick = float(a_stats.get("pick_rate") or 0.0)
    b_pick = float(b_stats.get("pick_rate") or 0.0)
    a_ban = float(a_stats.get("ban_rate") or 0.0)
    b_ban = float(b_stats.get("ban_rate") or 0.0)
    return {
        "map_win_a": a_win,
        "map_win_b": b_win,
        "map_win_gap": a_win - b_win,
        "pick_a": a_pick,
        "pick_b": b_pick,
        "pick_gap": a_pick - b_pick,
        "ban_a": a_ban,
        "ban_b": b_ban,
        "ban_gap": a_ban - b_ban,
        "played_pct_a": a_played_pct,
        "played_pct_b": b_played_pct,
        "played_pct_gap": a_played_pct - b_played_pct,
        "map_stats_available": 1.0,
    }


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _model_map_win_probability(model: Dict[str, Any], values: Dict[str, float]) -> float:
    """Map-win probability from a fitted model, honoring its training target.

    map_win models output the probability directly; legacy round-share models
    fall back to the independent-rounds conversion.
    """
    p = _predict_from_round_share_features(model, values)
    if str(model.get("target") or "round_share") == "map_win":
        return p
    return _round_win_to_map_win_probability(p)


def _round_win_to_map_win_probability(p_round: float) -> float:
    p = min(0.999, max(0.001, float(p_round)))
    q = 1.0 - p
    win_before_ot = sum(math.comb(12 + k, k) * (p ** 13) * (q ** k) for k in range(12))
    ot = math.comb(24, 12) * (p ** 12) * (q ** 12)
    return min(0.999, max(0.001, win_before_ot + 0.5 * ot))


_RATING_START = 1500.0
# Player ratings are Glicko-style: a mean and a deviation per player. The
# deviation starts at _PLAYER_SIGMA0, shrinks with every map down to
# _PLAYER_SIGMA_MIN, and sets the update size, so a new player moves fast and
# a veteran barely moves. No inflation for inactivity: walk-forward on
# 2026-09-23 every no-growth setting beat a fixed-K Elo (K 20, itself the best
# of a sweep) by 1.0-1.5 series-Brier points x1000, while idle growth of 15
# or 30 per root-day lost 0.6-1.3. Start 250-350 and a floor near 40 were
# within noise of each other; 300/40 sits in the middle.
_PLAYER_SIGMA0 = 300.0
_PLAYER_SIGMA_MIN = 40.0
_GLICKO_Q = math.log(10.0) / 400.0


def _scoreboard_team_ratings(row: Dict[str, Any], key1: str, key2: str) -> Dict[str, tuple[float, float]]:
    """{canonical map: (team1 mean player rating, team2 mean player rating)}
    from the stored per-map scoreboard; a side needs at least four rated
    players or the map is left out."""
    out: Dict[str, tuple[float, float]] = {}
    for canon, (side1, side2) in _scoreboard_lineups(row, key1, key2).items():
        r1 = [r for _pid, r in side1 if r is not None]
        r2 = [r for _pid, r in side2 if r is not None]
        if len(r1) >= 4 and len(r2) >= 4:
            out[canon] = (sum(r1) / len(r1), sum(r2) / len(r2))
    return out


def _scoreboard_lineups(row: Dict[str, Any], key1: str, key2: str) -> Dict[str, tuple[List[tuple[str, float | None]], List[tuple[str, float | None]]]]:
    """{canonical map: ([(player id, rating)] team1, [...] team2)} from the
    stored per-map scoreboard, at most five a side, in scoreboard order."""
    raw = row.get("map_player_stats_json")
    if not raw:
        return {}
    try:
        per_map = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}
    if not isinstance(per_map, dict):
        return {}
    out: Dict[str, tuple[List[tuple[str, float | None]], List[tuple[str, float | None]]]] = {}
    for map_name, players in per_map.items():
        canon = _canonical_map_name(map_name)
        if not canon:
            continue
        sides: Dict[str, List[tuple[str, float | None]]] = {key1: [], key2: []}
        for p in players or []:
            if not isinstance(p, dict):
                continue
            team_key = _norm_team_name(str(p.get("team") or ""))
            if team_key not in sides:
                continue
            pid = p.get("player_id")
            pid_key = str(pid) if pid not in (None, "") else "name:" + str(p.get("player") or "")
            sides[team_key].append((pid_key, _to_float_or_none(p.get("rating"))))
        if sides[key1] and sides[key2]:
            out[canon] = (sides[key1][:5], sides[key2][:5])
    return out


def _build_prematch_player_ratings(
    rows: List[Dict[str, Any]],
    current: Dict[str, Any] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Pre-match player-rating summary for each stored match, keyed by match_url.

    Matches are replayed chronologically, so every entry reflects only earlier
    results (no label leakage). Each snapshot carries "players": {map: (team1
    mean, team2 mean)} for the five who played that map, and
    "players_fallback" = the same for each side's most recent five (what a
    pre-match prediction has).

    Every player carries a Glicko-style mean and deviation (see
    _PLAYER_SIGMA0); a side's strength is the mean of its five and its
    variance the mean of their variances over five; each of the five takes
    the Glicko step against the opposing side. The result fed in is the team
    mean player-rating gap on the map, 0.5 + clamp(gap, -0.5..0.5): a win with
    a 0.1 rating edge counts 0.6, a stomp 1.0, and winning while outplayed on
    the scoreboard earns almost nothing (maps without a stored scoreboard use
    the plain win/loss). A player first seen starts at the current level of
    the team they join. Credit follows players across roster moves.

    Measured walk-forward on 2026-09-23 against the previous round-margin team
    Elo: series Brier 0.2184 -> 0.2150. With this rating present the team Elo
    carried no weight, so it was removed.

    When `current` is given it is filled with the state after the last match:
    {"players_by_team": {team key: mean rating of its last five},
     "last_five": {team key: [player ids]}} for live predictions.
    """
    player: Dict[str, float] = {}
    player_var: Dict[str, float] = {}
    last_five: Dict[str, List[str]] = {}
    out: Dict[str, Dict[str, Any]] = {}
    sigma0_sq = _PLAYER_SIGMA0 ** 2

    def _side(pids: List[str]) -> tuple[float, float, float] | None:
        """(mean rating, variance of the side mean, prior for unseen players)."""
        if not pids:
            return None
        known = [player[p] for p in pids if p in player]
        prior = (sum(known) / len(known)) if known else _RATING_START
        mean = sum(player.get(p, prior) for p in pids) / float(len(pids))
        var = sum(player_var.get(p, sigma0_sq) for p in pids) / float(len(pids) ** 2)
        return mean, var, prior

    def _side_mean(pids: List[str]) -> float | None:
        side = _side(pids)
        return side[0] if side else None

    def _g(var: float) -> float:
        return 1.0 / math.sqrt(1.0 + 3.0 * _GLICKO_Q * _GLICKO_Q * var / (math.pi ** 2))

    ordered = sorted(rows, key=lambda r: (str(r.get("match_date") or ""), str(r.get("match_id") or r.get("match_url") or "")))
    for r in ordered:
        url = str(r.get("match_url") or "").strip()
        k1 = _norm_team_name(str(r.get("team1") or ""))
        k2 = _norm_team_name(str(r.get("team2") or ""))
        if not url or not k1 or not k2:
            continue
        snapshot: Dict[str, Any] = {"players": {}, "players_fallback": None}
        lineups = _scoreboard_lineups(r, k1, k2)
        team_ratings = _scoreboard_team_ratings(r, k1, k2)
        fb1 = _side_mean(last_five.get(k1, []))
        fb2 = _side_mean(last_five.get(k2, []))
        if fb1 is not None and fb2 is not None:
            snapshot["players_fallback"] = (fb1, fb2)
        player_updates = []
        for m in _parse_stored_maps(r.get("maps_json")):
            map_name = _canonical_map_name(m.get("map"))
            # Off-pool maps still update ratings: a Vertigo win is evidence of
            # strength, and the ratings are ready if the map returns.
            if not map_name:
                continue
            try:
                s1 = int(m["score1"])
                s2 = int(m["score2"])
            except Exception:
                continue
            if s1 == s2:
                continue
            ratings = team_ratings.get(map_name)
            if ratings is not None:
                result = 0.5 + max(-0.5, min(0.5, float(ratings[0]) - float(ratings[1])))
            else:
                result = 1.0 if s1 > s2 else 0.0
            lineup = lineups.get(map_name)
            if lineup and len(lineup[0]) == 5 and len(lineup[1]) == 5:
                pids1 = [pid for pid, _r in lineup[0]]
                pids2 = [pid for pid, _r in lineup[1]]
                side1 = _side(pids1)
                side2 = _side(pids2)
                if side1 is not None and side2 is not None:
                    snapshot["players"][map_name] = (side1[0], side2[0])
                    player_updates.append((pids1, pids2, side1, side2, result))
        out[url] = snapshot
        # Update ratings only after the whole match is snapshotted.
        for pids1, pids2, side1, side2, result in player_updates:
            for pids, own, opp, res in ((pids1, side1, side2, result), (pids2, side2, side1, 1.0 - result)):
                g_opp = _g(opp[1])
                exp_side = 1.0 / (1.0 + 10.0 ** (-g_opp * (own[0] - opp[0]) / 400.0))
                d_sq = 1.0 / (_GLICKO_Q * _GLICKO_Q * g_opp * g_opp * exp_side * (1.0 - exp_side))
                for pid in pids:
                    old_var = player_var.get(pid, sigma0_sq)
                    new_var = max(_PLAYER_SIGMA_MIN ** 2, 1.0 / (1.0 / old_var + 1.0 / d_sq))
                    player[pid] = player.get(pid, own[2]) + _GLICKO_Q * new_var * g_opp * (res - exp_side)
                    player_var[pid] = new_var
            last_five[k1] = list(pids1)
            last_five[k2] = list(pids2)
    if current is not None:
        current["players_by_team"] = {team: mean for team, pids in last_five.items() if (mean := _side_mean(pids)) is not None}
        current["last_five"] = dict(last_five)
    return out


def _player_rating_gap(entry: Dict[str, Any] | None, map_name: str | None = None) -> float:
    """Team1 minus team2 mean player rating: the five who played that map when
    the scoreboard is stored, else each side's most recent five, else 0."""
    if not entry:
        return 0.0
    pair = (entry.get("players") or {}).get(map_name) if map_name else None
    if not pair:
        pair = entry.get("players_fallback")
    if not pair:
        return 0.0
    return float(pair[0]) - float(pair[1])


def _map_pickers_from_veto(veto_json: Any, team1: str, team2: str) -> Dict[str, float]:
    """{canonical map: +1 team1 picked, -1 team2 picked, 0 decider/unknown}."""
    try:
        steps = json.loads(veto_json) if veto_json else []
    except Exception:
        return {}
    k1 = _norm_team_name(team1)
    k2 = _norm_team_name(team2)
    out: Dict[str, float] = {}
    for step in steps or []:
        action = str((step or {}).get("action") or "").lower()
        map_name = _canonical_map_name((step or {}).get("map"))
        if not map_name:
            continue
        if action in ("picked", "pick"):
            team_key = _norm_team_name(str(step.get("team") or ""))
            if not team_key:
                continue
            if team_key == k1 or team_key in k1 or k1 in team_key:
                out[map_name] = 1.0
            elif team_key == k2 or team_key in k2 or k2 in team_key:
                out[map_name] = -1.0
        elif action == "leftover":
            out.setdefault(map_name, 0.0)
    return out


def _iter_ranked_map_samples(
    rows: List[Dict[str, Any]],
    team_map_stats_by_key: Dict[str, Dict[str, Dict[str, float]]] | None = None,
    *,
    historical_map_stats_by_window: Dict[tuple[str, str, str], Dict[str, Dict[str, float]]] | None = None,
    require_map_stats: bool = False,
    require_veto: bool = False,
    prematch_ratings_by_match: Dict[str, Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """One sample per played map. require_map_stats drops maps where either
    team lacks pre-match six-month stats for that map; require_veto drops
    every map of a match with no stored veto (so "picked by" is never a
    silent zero). Each sample also carries veto_available / vrs_substituted
    so a summary can say what was defaulted."""
    samples: List[Dict[str, Any]] = []
    for r in rows:
        t1 = str(r.get("team1") or "").strip()
        t2 = str(r.get("team2") or "").strip()
        maps = _parse_stored_maps(r.get("maps_json"))
        h1 = _to_float_or_none(r.get("hltv_rank_1"))
        h2 = _to_float_or_none(r.get("hltv_rank_2"))
        v1 = _to_float_or_none(r.get("vrs_rank_1"))
        v2 = _to_float_or_none(r.get("vrs_rank_2"))
        if not t1 or not t2 or not maps or h1 is None or h2 is None:
            continue
        # Valve's ranking barely exists before 2024; substitute the HLTV rank so
        # CS2-era matches from late 2023 stay usable as training data.
        vrs_substituted = v1 is None or v2 is None
        if vrs_substituted:
            v1, v2 = h1, h2
        k1 = _norm_team_name(t1)
        k2 = _norm_team_name(t2)
        window = _historical_map_stats_window(r.get("match_date")) if historical_map_stats_by_window is not None else None
        if window:
            start_date, end_date = window
            team1_stats = (historical_map_stats_by_window or {}).get((k1, start_date, end_date))
            team2_stats = (historical_map_stats_by_window or {}).get((k2, start_date, end_date))
        else:
            team1_stats = (team_map_stats_by_key or {}).get(k1)
            team2_stats = (team_map_stats_by_key or {}).get(k2)
        pickers = _map_pickers_from_veto(r.get("veto_json"), t1, t2)
        veto_available = bool(pickers)
        if require_veto and not veto_available:
            continue
        ratings_entry = (prematch_ratings_by_match or {}).get(str(r.get("match_url") or "").strip())
        series_score1 = _to_float_or_none(r.get("score1"))
        series_score2 = _to_float_or_none(r.get("score2"))
        for m in maps:
            map_name = _canonical_map_name(m.get("map"))
            if map_name not in MAP_POOL:
                continue
            try:
                s1 = int(m["score1"])
                s2 = int(m["score2"])
            except Exception:
                continue
            total_rounds = s1 + s2
            if total_rounds <= 0:
                continue
            outcome = int(m.get("outcome") or (1 if s1 > s2 else -1 if s1 < s2 else 0))
            map_stat_features = _map_stat_features(
                team1_stats,
                team2_stats,
                map_name,
            )
            if require_map_stats and float(map_stat_features.get("map_stats_available") or 0.0) <= 0.0:
                continue
            samples.append(
                {
                    "match_url": r.get("match_url"),
                    "match_date": r.get("match_date"),
                    "team1": t1,
                    "team2": t2,
                    "map": map_name,
                    "score1": s1,
                    "score2": s2,
                    "outcome": outcome,
                    "total_rounds": total_rounds,
                    "hltv_a": float(h1),
                    "hltv_b": float(h2),
                    "vrs_a": float(v1),
                    "vrs_b": float(v2),
                    "hltv_gap": float(h2 - h1),
                    "hltv_gap_level": float((h2 - h1) * ((h1 + h2) / 2.0)),
                    "vrs_gap": float(v2 - v1),
                    "vrs_gap_level": float((v2 - v1) * ((v1 + v2) / 2.0)),
                    **_log_rank_features(h1, h2),
                    "round_share": float(s1 / total_rounds),
                    "weight": float(total_rounds),
                    "hltv_rank_1": h1,
                    "hltv_rank_2": h2,
                    "vrs_rank_1": v1,
                    "vrs_rank_2": v2,
                    "map_win_gap": float(map_stat_features.get("map_win_gap") or 0.0),
                    "pick_gap": float(map_stat_features.get("pick_gap") or 0.0),
                    "ban_gap": float(map_stat_features.get("ban_gap") or 0.0),
                    "played_pct_gap": float(map_stat_features.get("played_pct_gap") or 0.0),
                    "map_stats_available": float(map_stat_features.get("map_stats_available") or 0.0),
                    "picked_by_a": float(pickers.get(map_name, 0.0)),
                    "player_rating_gap": _player_rating_gap(ratings_entry, map_name),
                    "veto_available": 1.0 if veto_available else 0.0,
                    "vrs_substituted": 1.0 if vrs_substituted else 0.0,
                    "series_score1": int(series_score1) if series_score1 is not None else None,
                    "series_score2": int(series_score2) if series_score2 is not None else None,
                }
            )
    return samples


def _fit_map_model_set(
    samples: List[Dict[str, Any]],
    target: str = "round_share",
) -> Dict[str, Dict[str, Any]]:
    """Fit one pooled logistic model over the shared feature set and serve it
    for every map (per-map fits overfit and scored worse held-out; see
    _MAP_MODEL_L2).

    target="round_share" fits round share weighted by rounds (scoreline model);
    target="map_win" fits the binary map result with unit weight (probability
    model) — measured to predict winners better than converting round share
    through an independent-rounds assumption.
    """
    is_win_target = str(target) == "map_win"
    symmetric: List[Dict[str, float]] = []
    by_map: Dict[str, List[Dict[str, float]]] = {m: [] for m in MAP_POOL}
    for sample in samples:
        if is_win_target:
            target_value = 1.0 if float(sample.get("outcome") or 0.0) > 0.5 else 0.0
            target_weight = 1.0
        else:
            target_value = float(sample["round_share"])
            target_weight = float(sample["weight"])
        row_a = {name: float(sample.get(key) or 0.0) for name, key in ROUND_SHARE_FEATURES}
        row_b = {name: -value for name, value in row_a.items()}
        row_a["round_share"] = target_value
        row_b["round_share"] = 1.0 - target_value
        row_a["weight"] = target_weight
        row_b["weight"] = target_weight
        symmetric.extend([row_a, row_b])
        by_map.setdefault(str(sample["map"]), []).extend([row_a, row_b])
    if len(symmetric) < 20:
        raise HTTPException(status_code=400, detail="Not enough training maps with stored scores and HLTV/VRS ranks.")
    global_model = {
        **_fit_round_share_logistic_2d(symmetric),
        "scope": "global",
        "samples": len(symmetric),
        "target": str(target),
    }
    models: Dict[str, Dict[str, Any]] = {"__global__": global_model}
    for map_name, rows in by_map.items():
        models[map_name] = {**global_model, "samples": len(rows)}
    return models


def _predict_from_round_share_features(model: Dict[str, Any], values: Dict[str, float]) -> float:
    z = float(model.get("a") or 0.0)
    for name, _sample_key in ROUND_SHARE_FEATURES:
        coef_key = f"b_{name}"
        if coef_key not in model:
            continue
        mean = float(model.get(f"mean_{name}", 0.0))
        std = float(model.get(f"std_{name}", 1.0)) or 1.0
        raw = float(values.get(name, mean))
        z += float(model.get(coef_key) or 0.0) * ((raw - mean) / std)
    return _sigmoid(z)


# ---- the veto model -------------------------------------------------------------
# At every veto step the acting team chooses one map from what is left. A
# conditional-logit scorer per action (ban, pick) ranks the alternatives from
# point-in-time inputs: both teams' six-month pick/ban/win/played share on the
# map, what each team played recently, each team's ban and pick habits over its
# last vetoes (its first ban above all), and the map model's win probability.
# Measured 2026-09-24 on a chronological split against the previous rule (ban
# the opponent's strongest map by win probability, pick your own): bans right
# 71% vs 43%, picks 57% vs 48%, maps actually played predicted 57% vs 38%.
VETO_FEATURES: Tuple[str, ...] = (
    "own_pick", "own_ban", "own_win", "own_share", "opp_pick", "opp_ban", "opp_win", "opp_share",
    "own_recent", "own_days", "own_ban5", "own_pick5", "opp_recent", "opp_days", "opp_ban5", "opp_pick5", "p_win",
    "own_ban3", "own_ban10", "own_pick3", "own_pick10", "own_first_ban", "own_first_pick", "opp_ban3", "opp_pick3", "opp_first_ban",
    "step_no", "n_remaining", "is_bo1",
)
# Standard HLTV orders, side 0 acting first: bans until one map is left (Bo1),
# ban ban pick pick ban ban + decider (Bo3), ban ban pick pick pick pick + decider (Bo5).
_VETO_ORDER: Dict[int, List[tuple[str, int]]] = {
    1: [("ban", 0), ("ban", 1)] * 3,
    2: [("ban", 0), ("ban", 1), ("pick", 0), ("pick", 1), ("ban", 0), ("ban", 1)],
    3: [("ban", 0), ("ban", 1), ("pick", 0), ("pick", 1), ("pick", 0), ("pick", 1)],
}
_VETO_MIN_CHOICES = 500


def _veto_empty_context() -> Dict[str, Any]:
    return {"maps": [], "vetoes": []}


def _veto_context_update(ctx: Dict[str, Any], on_date: str, played: List[str], bans: set, picks: set, first_ban: str | None, first_pick: str | None) -> None:
    """Append one match to a team's veto context (kept to the last 10 of each)."""
    for m in played:
        ctx["maps"].append([on_date, m])
    if bans or picks:
        ctx["vetoes"].append([on_date, sorted(bans), sorted(picks), first_ban, first_pick])
    del ctx["maps"][:-10]
    del ctx["vetoes"][:-10]


def _veto_stat_features(stats: Dict[str, Dict[str, float]] | None, map_name: str) -> List[float]:
    row = (stats or {}).get(map_name) or {}
    total = sum(max(0.0, float(x.get("played") or 0.0)) for x in (stats or {}).values())
    return [
        float(row.get("pick_rate") or 0.0),
        float(row.get("ban_rate") or 0.0),
        float(row.get("win_rate") or 0.0),
        (float(row.get("played") or 0.0) / total) if total > 0 else 0.0,
    ]


def _veto_history_features(ctx: Dict[str, Any], map_name: str, on_date: str) -> List[float]:
    """recent share, log days since last played, ban/pick habit over the last
    5, 3 and 10 vetoes, first-ban and first-pick habit over the last 5."""
    maps10 = [m for _d, m in ctx.get("maps", [])[-10:]]
    recent = maps10.count(map_name) / (len(maps10) or 1)
    last = None
    for d, m in ctx.get("maps", []):
        if m == map_name:
            last = d
    try:
        days = (date.fromisoformat(on_date[:10]) - date.fromisoformat(last[:10])).days if last else 180
    except Exception:
        days = 180
    days = math.log1p(min(180, max(0, days)))
    vetoes = ctx.get("vetoes", [])

    def share(window: int, pick_fn) -> float:
        v = vetoes[-window:]
        return (sum(1 for x in v if pick_fn(x)) / len(v)) if v else 0.0

    return [
        recent, days,
        share(5, lambda x: map_name in x[1]), share(5, lambda x: map_name in x[2]),
        share(3, lambda x: map_name in x[1]), share(10, lambda x: map_name in x[1]),
        share(3, lambda x: map_name in x[2]), share(10, lambda x: map_name in x[2]),
        share(5, lambda x: x[3] == map_name), share(5, lambda x: x[4] == map_name),
    ]


def _veto_feature_rows(
    own_stats, opp_stats, own_ctx, opp_ctx, on_date: str, remaining: List[str], p_win_fn, step_no: int, is_bo1: bool
) -> List[List[float]]:
    """One feature row per remaining map, in VETO_FEATURES order. p_win_fn(map)
    is the acting team's map win probability minus one half."""
    rows = []
    for m in remaining:
        own = _veto_history_features(own_ctx, m, on_date)
        opp = _veto_history_features(opp_ctx, m, on_date)
        rows.append(
            _veto_stat_features(own_stats, m) + _veto_stat_features(opp_stats, m)
            + own[:4] + opp[:4] + [float(p_win_fn(m))]
            + own[4:10] + [opp[4], opp[6], opp[8]]
            + [float(step_no), float(len(remaining)), 1.0 if is_bo1 else 0.0]
        )
    return rows


def _neutral_map_win_minus_half(model, ranks_own: tuple[float, float], ranks_opp: tuple[float, float], own_stats, opp_stats, player_gap: float, map_name: str) -> float:
    """The map model's probability that the acting team wins the map, neutral
    veto, minus 0.5. ranks are (hltv, vrs)."""
    h1, v1 = ranks_own
    h2, v2 = ranks_opp
    features = _map_stat_features(own_stats, opp_stats, map_name)
    values = {
        "hltv_gap": h2 - h1,
        "hltv_gap_level": (h2 - h1) * ((h1 + h2) / 2.0),
        "vrs_gap": v2 - v1,
        "vrs_gap_level": (v2 - v1) * ((v1 + v2) / 2.0),
        **_log_rank_features(h1, h2),
        "picked_by_a": 0.0,
        "player_rating_gap": player_gap,
        "map_win_gap": float(features.get("map_win_gap") or 0.0),
        "pick_gap": float(features.get("pick_gap") or 0.0),
        "ban_gap": float(features.get("ban_gap") or 0.0),
        "played_pct_gap": float(features.get("played_pct_gap") or 0.0),
    }
    return _model_map_win_probability(model, values) - 0.5


def _veto_side_of(step_team: Any, k1: str, k2: str) -> str | None:
    t = _norm_team_name(str(step_team or ""))
    if not t:
        return None
    if t == k1 or t in k1 or k1 in t:
        return k1
    if t == k2 or t in k2 or k2 in t:
        return k2
    return None


def _replay_vetoes(
    rows: List[Dict[str, Any]],
    historical_map_stats_by_window,
    ratings_by_match: Dict[str, Dict[str, Any]],
    map_model: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Walk the stored matches in date order and turn every stored veto step
    into a choice instance with point-in-time features. Returns (instances,
    {match_url: {team key: context before the match}}, {team key: context
    after the last match})."""
    import numpy as np

    contexts: Dict[str, Dict[str, Any]] = {}
    by_url: Dict[str, Dict[str, Any]] = {}
    instances: List[Dict[str, Any]] = []
    ordered = sorted(rows, key=lambda r: (str(r.get("match_date") or "")[:10], int(r.get("match_id") or 0)))
    for r in ordered:
        url = str(r.get("match_url") or "").strip()
        on_date = str(r.get("match_date") or "")[:10]
        k1 = _norm_team_name(str(r.get("team1") or ""))
        k2 = _norm_team_name(str(r.get("team2") or ""))
        if not url or not k1 or not k2 or not on_date:
            continue
        try:
            steps = json.loads(r.get("veto_json") or "[]") or []
        except Exception:
            steps = []
        steps = sorted([st for st in steps if isinstance(st, dict)], key=lambda st: int(st.get("order") or 0))
        pool = sorted({m for m in (_canonical_map_name(st.get("map")) for st in steps) if m})
        played = [m for m in (_canonical_map_name(x.get("map")) for x in _parse_stored_maps(r.get("maps_json"))) if m]
        ctx1 = contexts.setdefault(k1, _veto_empty_context())
        ctx2 = contexts.setdefault(k2, _veto_empty_context())
        by_url[url] = {k1: json.loads(json.dumps(ctx1)), k2: json.loads(json.dumps(ctx2))}
        bans: Dict[str, set] = {k1: set(), k2: set()}
        picks: Dict[str, set] = {k1: set(), k2: set()}
        first_ban: Dict[str, str | None] = {k1: None, k2: None}
        first_pick: Dict[str, str | None] = {k1: None, k2: None}
        h1 = _to_float_or_none(r.get("hltv_rank_1"))
        h2 = _to_float_or_none(r.get("hltv_rank_2"))
        if len(pool) >= 5 and h1 is not None and h2 is not None:
            v1 = _to_float_or_none(r.get("vrs_rank_1")) or h1
            v2 = _to_float_or_none(r.get("vrs_rank_2")) or h2
            window = _historical_map_stats_window(on_date)
            stats = {
                k1: (historical_map_stats_by_window or {}).get((k1,) + tuple(window)) if window else None,
                k2: (historical_map_stats_by_window or {}).get((k2,) + tuple(window)) if window else None,
            }
            fallback = (ratings_by_match.get(url) or {}).get("players_fallback")
            gap12 = (float(fallback[0]) - float(fallback[1])) if fallback else 0.0
            ranks = {k1: (float(h1), float(v1)), k2: (float(h2), float(v2))}
            remaining = list(pool)
            counts: Dict[tuple[str, str], int] = {}
            is_bo1 = len(played) == 1
            for st in steps:
                action = str(st.get("action") or "").lower()
                m = _canonical_map_name(st.get("map"))
                team = _veto_side_of(st.get("team"), k1, k2)
                if action in ("removed", "picked") and team and m in remaining:
                    opp = k2 if team == k1 else k1
                    kind = "ban" if action == "removed" else "pick"
                    counts[(team, kind)] = counts.get((team, kind), 0) + 1
                    gap = gap12 if team == k1 else -gap12
                    p_win = lambda mp, team=team, opp=opp, gap=gap: _neutral_map_win_minus_half(map_model, ranks[team], ranks[opp], stats[team], stats[opp], gap, mp)
                    rows_x = _veto_feature_rows(stats[team], stats[opp], by_url[url][team], by_url[url][opp], on_date, remaining, p_win, counts[(team, kind)], is_bo1)
                    instances.append({"date": on_date, "action": kind, "alts": list(remaining), "chosen": m, "X": np.array(rows_x, dtype=float)})
                    (bans if kind == "ban" else picks)[team].add(m)
                    if kind == "ban" and first_ban[team] is None:
                        first_ban[team] = m
                    if kind == "pick" and first_pick[team] is None:
                        first_pick[team] = m
                if m in remaining and action in ("removed", "picked", "leftover"):
                    remaining.remove(m)
        _veto_context_update(ctx1, on_date, played, bans[k1], picks[k1], first_ban[k1], first_pick[k1])
        _veto_context_update(ctx2, on_date, played, bans[k2], picks[k2], first_ban[k2], first_pick[k2])
    return instances, by_url, contexts


def _veto_bundles(
    rows: List[Dict[str, Any]],
    historical_map_stats_by_window,
    ratings_by_match: Dict[str, Dict[str, Any]],
    contexts_by_url: Dict[str, Dict[str, Any]],
    map_model: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Per stored veto, everything the distribution needs to be replayed in
    its actual step order: the pool, who acted when, the two sides' inputs,
    and the maps played. Used to calibrate P(map played)."""
    bundles: List[Dict[str, Any]] = []
    ordered = sorted(rows, key=lambda r: (str(r.get("match_date") or "")[:10], int(r.get("match_id") or 0)))
    for r in ordered:
        url = str(r.get("match_url") or "").strip()
        on_date = str(r.get("match_date") or "")[:10]
        k1 = _norm_team_name(str(r.get("team1") or ""))
        k2 = _norm_team_name(str(r.get("team2") or ""))
        h1 = _to_float_or_none(r.get("hltv_rank_1"))
        h2 = _to_float_or_none(r.get("hltv_rank_2"))
        veto_ctx = contexts_by_url.get(url)
        if not url or not k1 or not k2 or not on_date or h1 is None or h2 is None or not veto_ctx:
            continue
        try:
            steps = json.loads(r.get("veto_json") or "[]") or []
        except Exception:
            steps = []
        steps = sorted([st for st in steps if isinstance(st, dict)], key=lambda st: int(st.get("order") or 0))
        pool = sorted({m for m in (_canonical_map_name(st.get("map")) for st in steps) if m})
        played = [m for m in (_canonical_map_name(x.get("map")) for x in _parse_stored_maps(r.get("maps_json"))) if m]
        if len(pool) < 5 or not played:
            continue
        order = []
        for st in steps:
            action = str(st.get("action") or "").lower()
            team = _veto_side_of(st.get("team"), k1, k2)
            if action in ("removed", "picked") and team:
                order.append(("ban" if action == "removed" else "pick", 0 if team == k1 else 1))
        if not order:
            continue
        v1 = _to_float_or_none(r.get("vrs_rank_1")) or h1
        v2 = _to_float_or_none(r.get("vrs_rank_2")) or h2
        window = _historical_map_stats_window(on_date)
        stats1 = (historical_map_stats_by_window or {}).get((k1,) + tuple(window)) if window else None
        stats2 = (historical_map_stats_by_window or {}).get((k2,) + tuple(window)) if window else None
        fallback = (ratings_by_match.get(url) or {}).get("players_fallback")
        gap = (float(fallback[0]) - float(fallback[1])) if fallback else 0.0
        sides = [
            {"stats": stats1, "ctx": veto_ctx.get(k1) or _veto_empty_context(),
             "p_win": lambda m, a=(float(h1), float(v1)), b=(float(h2), float(v2)), s1=stats1, s2=stats2, g=gap: _neutral_map_win_minus_half(map_model, a, b, s1, s2, g, m)},
            {"stats": stats2, "ctx": veto_ctx.get(k2) or _veto_empty_context(),
             "p_win": lambda m, a=(float(h2), float(v2)), b=(float(h1), float(v1)), s1=stats2, s2=stats1, g=gap: _neutral_map_win_minus_half(map_model, a, b, s1, s2, -g, m)},
        ]
        bundles.append({"date": on_date, "pool": pool, "order": order, "played": played, "sides": sides, "wins_needed": _series_wins_needed(r.get("score1") or 1, r.get("score2") or 1) if r.get("score1") is not None and r.get("score2") is not None else 2})
    return bundles


def _fit_veto_model(instances: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Conditional logit per action: softmax over the remaining maps, features
    standardised, small ridge. None when there is too little."""
    import numpy as np
    from scipy.optimize import minimize

    model: Dict[str, Any] = {"features": list(VETO_FEATURES), "choices": {}}
    for action in ("ban", "pick"):
        subset = [i for i in instances if i["action"] == action]
        if len(subset) < _VETO_MIN_CHOICES:
            return None
        xs = [np.array(i["X"], dtype=float) for i in subset]
        ys = [i["alts"].index(i["chosen"]) for i in subset]
        stacked = np.vstack(xs)
        mu = stacked.mean(axis=0)
        sd = stacked.std(axis=0)
        sd[sd < 1e-9] = 1.0
        zs = [(x - mu) / sd for x in xs]
        d = len(VETO_FEATURES)
        l2 = 1e-3

        def loss(w):
            total = 0.0
            grad = np.zeros(d)
            for z, y in zip(zs, ys):
                score = z @ w
                score = score - score.max()
                p = np.exp(score)
                p /= p.sum()
                total -= math.log(max(float(p[y]), 1e-12))
                grad += p @ z - z[y]
            n = len(zs)
            return total / n + l2 * float(w @ w), grad / n + 2.0 * l2 * w

        fit = minimize(loss, np.zeros(d), jac=True, method="L-BFGS-B", options={"maxiter": 300})
        model[action] = {"w": [float(x) for x in fit.x], "mu": [float(x) for x in mu], "sd": [float(x) for x in sd]}
        model["choices"][action] = len(subset)
    return model


def _veto_step_probabilities(veto_model: Dict[str, Any], action: str, feature_rows: List[List[float]]) -> List[float]:
    part = veto_model[action]
    scores = [sum((float(x) - mu) / sd * w for x, mu, sd, w in zip(row, part["mu"], part["sd"], part["w"])) for row in feature_rows]
    top = max(scores)
    weights = [math.exp(x - top) for x in scores]
    total = sum(weights) or 1.0
    return [w / total for w in weights]


def _veto_distribution(
    veto_model: Dict[str, Any],
    pool: List[str],
    order: List[tuple[str, int]],
    on_date: str,
    sides: List[Dict[str, Any]],
    wins_needed: int,
    prune: float = 1e-4,
) -> List[tuple[float, List[tuple[str, float]]]]:
    """Every way the veto can go, with its probability: [(prob, [(map,
    picked_by_a)])] for the maps that would be played. order = [(action,
    side)], side 0 = team A. Each side: {"stats", "ctx", "p_win": map ->
    prob minus half}. Paths below `prune` are dropped and the rest
    renormalised. Feature rows are cached per (side, action, step, pool
    size, map), so a full Bo3 tree costs a few hundred rows."""
    is_bo1 = wins_needed == 1
    row_cache: Dict[tuple, List[float]] = {}

    def rows_for(side: int, action: str, step_no: int, remaining: List[str]) -> List[List[float]]:
        own, opp = sides[side], sides[1 - side]
        out = []
        for m in remaining:
            key = (side, action, step_no, len(remaining), m)
            if key not in row_cache:
                row_cache[key] = _veto_feature_rows(own["stats"], opp["stats"], own["ctx"], opp["ctx"], on_date, [m], own["p_win"], step_no, is_bo1)[0]
                row_cache[key][VETO_FEATURES.index("n_remaining")] = float(len(remaining))
            out.append(row_cache[key])
        return out

    paths: List[tuple[float, List[str], List[tuple[str, float]], Dict[tuple, int]]] = [(1.0, list(pool), [], {})]
    for action, side in order:
        nxt = []
        for prob, remaining, picks, counts in paths:
            if len(remaining) <= 1:
                nxt.append((prob, remaining, picks, counts))
                continue
            counts2 = dict(counts)
            counts2[(side, action)] = counts2.get((side, action), 0) + 1
            probs = _veto_step_probabilities(veto_model, action, rows_for(side, action, counts2[(side, action)], remaining))
            for m, p in zip(remaining, probs):
                q = prob * p
                if q < prune:
                    continue
                nxt.append((q, [x for x in remaining if x != m], picks + ([(m, 1.0 if side == 0 else -1.0)] if action == "pick" else []), counts2))
        paths = nxt
    total = sum(p for p, *_ in paths) or 1.0
    out = []
    for prob, remaining, picks, _ in paths:
        played = list(picks)
        if remaining and len(played) < 2 * wins_needed - 1:
            played.append((remaining[0], 0.0))
        out.append((prob / total, played))
    return out


def _veto_play_probabilities(veto_model: Dict[str, Any], distribution: List[tuple[float, List[tuple[str, float]]]]) -> Dict[str, Dict[str, float]]:
    """{map: {"play", "pick_a", "pick_b", "decider"}}, play calibrated when
    the model carries a calibration."""
    out: Dict[str, Dict[str, float]] = {}
    for prob, played in distribution:
        for m, flag in played:
            row = out.setdefault(m, {"play": 0.0, "pick_a": 0.0, "pick_b": 0.0, "decider": 0.0})
            row["play"] += prob
            row["pick_a" if flag > 0 else "pick_b" if flag < 0 else "decider"] += prob
    cal = veto_model.get("calibration")
    if cal:
        for row in out.values():
            p = min(1 - 1e-6, max(1e-6, row["play"]))
            row["play"] = 1.0 / (1.0 + math.exp(-(cal["a"] + cal["b"] * math.log(p / (1.0 - p)))))
    return out


def _fit_veto_calibration(veto_model: Dict[str, Any], bundles: List[Dict[str, Any]], last: int = 1500) -> None:
    """Platt map on the tree's P(map played): the step probabilities are
    calibrated one at a time but multiply into overconfident map-level
    probabilities (steps are not independent). Fitted on the last `last`
    vetoes, replayed in their actual order."""
    import numpy as np
    from scipy.optimize import minimize

    usable = bundles[-last:]
    xs: List[float] = []
    ys: List[float] = []
    veto_model.pop("calibration", None)
    for b in usable:
        dist = _veto_distribution(veto_model, b["pool"], b["order"], b["date"], b["sides"], b["wins_needed"])
        probs = _veto_play_probabilities(veto_model, dist)
        played = set(b["played"])
        for m in b["pool"]:
            p = min(1 - 1e-6, max(1e-6, (probs.get(m) or {}).get("play", 0.0)))
            xs.append(math.log(p / (1.0 - p)))
            ys.append(1.0 if m in played else 0.0)
    if len(xs) < 500:
        return
    x = np.array(xs)
    y = np.array(ys)

    def nll(ab):
        z = ab[0] + ab[1] * x
        p = 1.0 / (1.0 + np.exp(-z))
        return -float(np.mean(y * np.log(np.clip(p, 1e-9, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-9, 1))))

    a, b_ = minimize(nll, [0.0, 1.0], method="Nelder-Mead").x
    veto_model["calibration"] = {"a": float(a), "b": float(b_), "vetoes": len(usable)}


def _series_wins_needed(score1: Any, score2: Any) -> int:
    """Maps a side must win, from a stored series score. Bo1 rows keep the MAP
    score (13-9, 22-19) as the series score, so anything above 5 is one map;
    treating it as maps-to-win made a Bo1 a best-of-25 and turned the series
    metrics into near-certainties (found 2026-09-23)."""
    m = max(int(score1), int(score2))
    return 1 if m > 5 else max(1, m)


def _best_of_series_win_probability(p_map: float, wins_needed: int) -> float:
    """P(team wins a best-of-(2n-1) series) assuming per-map win probability p_map."""
    wins_needed = max(1, int(wins_needed))
    total_maps = 2 * wins_needed - 1
    p = min(1.0, max(0.0, float(p_map)))
    return float(
        sum(math.comb(total_maps, k) * p**k * (1.0 - p) ** (total_maps - k) for k in range(wins_needed, total_maps + 1))
    )


def _lab_data_signature() -> Dict[str, int]:
    """Cheap counts of what the lab trains on: stored matches, matches with a
    veto, historical map-stat windows."""
    conn = event_db_connect()
    try:
        matches = conn.execute("SELECT COUNT(*) AS c FROM hltv_results").fetchone()["c"]
        vetoes = conn.execute(
            "SELECT COUNT(*) AS c FROM hltv_results WHERE veto_json IS NOT NULL AND veto_json != '' AND veto_json != '[]'"
        ).fetchone()["c"]
        windows = conn.execute("SELECT COUNT(*) AS c FROM historical_team_map_stats").fetchone()["c"]
    finally:
        conn.close()
    return {
        "matches": int(matches or 0),
        "vetoes": int(vetoes or 0),
        "windows": int(windows or 0),
        "features": int(_MAP_MODEL_FEATURE_VERSION),
        "aliases": int(team_aliases.signature()),
    }


# Fantasy games that cover only one stage of a tournament are named after it in
# HLTV's fantasy nav ("Group Stage - BLAST Open Porto 2026", "Playoffs - ...",
# Majors' "Stage 1 - ..."); a whole-tournament game just carries the event name.
_STAGE_PREFIX_RE = re.compile(
    r"^\s*(group\s*stage|groups?|playoffs?|play-?ins?|swiss(\s*stage)?|stage\s*\d+|"
    r"quarter-?finals?|semi-?finals?|finals?|main\s*event)\s*[-–—:]",
    re.IGNORECASE,
)


def _fantasy_stage_scope(event: Dict[str, Any]) -> tuple[bool, str]:
    """Is this fantasy game scoped to ONE stage of its tournament (so a later
    stage is a separate fantasy game), or does one lineup score the whole
    event? Two independent signals: a stage prefix in the game's name, and
    sibling fantasy games pointing at the same HLTV event."""
    event_id = int(event.get("event_id") or 0)
    hltv_id = int(event.get("hltv_event_id") or 0)
    name = str(event.get("name") or "")
    siblings: List[str] = []
    for row in list_events():
        if int(row.get("event_id") or 0) == event_id:
            name = name or str(row.get("name") or "")
        elif hltv_id and int(row.get("hltv_event_id") or 0) == hltv_id:
            siblings.append(str(row.get("name") or row.get("event_id")))
    match = _STAGE_PREFIX_RE.match(name)
    if match:
        return True, f"the fantasy game is named for one stage ({match.group(1).strip()!r})"
    if siblings:
        return True, f"other fantasy game(s) cover the same HLTV event: {', '.join(siblings[:3])}"
    return False, "one fantasy game covers the whole tournament"


def _detect_event_tournament_kind(event: Dict[str, Any]) -> Dict[str, Any]:
    """Classify what stage THIS fantasy event covers, from the typed JSON
    HLTV embeds in its event pages (swiss simulator, slotted brackets, formats
    table) — see services/event_format.py.

    One tournament often has separate fantasy events per stage (groups +
    playoff pointing at the same HLTV event), so the stage is chosen by
    matching the fantasy event's PRICED TEAM ROSTER against each detected
    stage's roster, discounted by how far the stage size is from the priced
    team count (bracket rosters can still be TBD, so size proximity breaks
    those ties). Pages without structured data return kind "unknown" — use
    the manual override there; old archived events are not supported.
    """
    # Lazy import: groups.py must not be imported at module load here (cycle).
    from backend.routes.groups import _find_event_snapshot_html
    from backend.services.event_format import detect_event_structure

    teams = event.get("teams") or []
    team_count = len(teams) or int(event.get("team_count") or 0)
    fantasy_names = {_norm_team_name(t.get("team_name") or "") for t in teams}
    fantasy_names.discard("")

    html = _find_event_snapshot_html(event.get("hltv_event_id")) or ""
    structure = detect_event_structure(html)

    candidates: List[Dict[str, Any]] = []
    for sw in structure["swiss"]:
        roster = {_norm_team_name(n) for n in sw["teams"].values()}
        stage_name = str(sw.get("stage_name") or "").strip()
        label = f"Swiss{f' {stage_name}' if stage_name else ''} ({sw['team_count']} teams" + (
            f", top {sw['advance_count']})" if sw["advance_count"] else ")"
        )
        rules = sw.get("bo_rules")
        if rules:
            if rules["default"] == rules["progression"] == rules["elimination"]:
                label += f" — Bo{rules['default']}"
            else:
                label += f" — Bo{rules['default']}, deciders Bo{max(rules['progression'], rules['elimination'])}"
        candidates.append(
            {"kind": "swiss", "label": label, "group_format": None, "playoff_size": 0,
             "roster": roster, "size": int(sw["team_count"] or len(roster))}
        )
    all_de = [b for b in structure["brackets"] if b["bracket"] == "double_elim"]
    full_de = [
        b for b in all_de
        if str(b.get("variant") or "").endswith("_full") or "_qual" in str(b.get("variant") or "")
    ]
    group_brackets = [b for b in all_de if b not in full_de]
    for b in full_de:
        roster = {_norm_team_name(n) for n in b["teams"].values()}
        advance = b.get("advance") or {}
        label = f"Double-Elimination Bracket ({b['size']} teams"
        if "_qual" in str(b.get("variant") or "") and advance.get("count"):
            label += f", top {advance['count']} qualify"
        label += ")"
        full8 = str(b.get("variant") or "") == "de8_full" and int(b["size"] or 0) == 8
        if full8:
            gf_bo = int(b.get("grand_final_bo") or 0)
            group_rules = structure.get("group_bo_rules") or {}
            gf_bo = gf_bo or int(group_rules.get("grandFinal") or 0)
            if group_rules.get("default"):
                label += f" — Bo{group_rules['default']}"
                if gf_bo and gf_bo != group_rules["default"]:
                    label += f", grand final Bo{gf_bo}"
        candidates.append(
            {
                "kind": "double_elim",
                "label": label,
                "group_format": None,
                "group_variant": b.get("variant"),
                # the whole-event 8-team bracket is played by the playoff simulator
                "playoff_size": int(b["size"] or 0) if full8 else 0,
                "bracket_kind": "double" if full8 else None,
                "grand_final_bo": (int(b.get("grand_final_bo") or 0) or None) if full8 else None,
                "routing_error": b.get("routing_error") if full8 else None,
                "roster": roster,
                "size": int(b["size"] or len(roster)),
            }
        )
    if group_brackets:
        group_size = max((int(b["size"] or 0) for b in group_brackets), default=0)
        roster: set = set()
        for b in group_brackets:
            roster |= {_norm_team_name(n) for n in b["teams"].values()}
        style = "GSL Groups" if group_size == 4 else "Double-Elim Groups"
        label = f"{style} ({len(group_brackets)} groups of {group_size}"
        advance = (group_brackets[0].get("advance") or {})
        if advance.get("count"):
            label += f", top {advance['count']}"
            if advance.get("winner_to_semis"):
                label += ", winner → semis"
        label += ")"
        group_rules = structure.get("group_bo_rules")
        if group_rules:
            label += f" — Bo{group_rules['default']}"
            if group_rules.get("upperRound1") and group_rules["upperRound1"] != group_rules["default"]:
                label += f", openers Bo{group_rules['upperRound1']}"
        candidates.append(
            {
                "kind": "groups",
                "label": label,
                "group_format": "gsl4" if group_size == 4 else f"de{group_size}",
                "group_variant": group_brackets[0].get("variant"),
                "playoff_size": 0,
                "roster": roster,
                "size": len(roster) or len(group_brackets) * group_size,
            }
        )
    playoff_brackets = [b for b in structure["brackets"] if b["bracket"] == "single_elim" and not b["aux"]]
    if playoff_brackets:
        main_po = max(playoff_brackets, key=lambda b: int(b["size"] or 0))
        po_size = int(main_po["size"] or 0)
        byes = int(main_po.get("byes") or 0)
        roster = set()
        for b in playoff_brackets:
            roster |= {_norm_team_name(n) for n in b["teams"].values()}
        label = f"Playoff ({po_size} teams" + (f", {byes} byes to semis)" if byes else ")")
        po_rules = structure.get("playoff_bo_rules")
        if po_rules:
            label += f" — Bo{po_rules['default']}"
            if po_rules.get("grandFinal") and po_rules["grandFinal"] != po_rules["default"]:
                label += f", final Bo{po_rules['grandFinal']}"
        candidates.append(
            {"kind": "playoff", "label": label, "group_format": None,
             "playoff_size": po_size, "byes": byes, "roster": roster, "size": po_size}
        )

    def _score(c: Dict[str, Any]):
        matched = (
            len(fantasy_names & c["roster"]) / len(fantasy_names)
            if fantasy_names and c["roster"]
            else 0.0
        )
        size_diff = abs(int(c["size"] or 0) - team_count) if team_count else 0
        ratio = (size_diff / team_count) if team_count else 0.0
        return (matched - ratio, -size_diff)

    chosen = max(candidates, key=_score) if candidates else None
    kind = chosen["kind"] if chosen else "unknown"
    label = chosen["label"] if chosen else "Unknown format"
    if "bounty" in str(event.get("hltv_event_url") or "").lower() and kind in ("playoff", "unknown"):
        kind = "bounty"
        label = "Bounty Draft"

    # Combined stages: the page also has a playoff bracket, and nothing says
    # this fantasy game stops at the chosen stage (no stage-prefixed name, no
    # sibling fantasy game on the same HLTV event) — so the same lineup scores
    # the playoffs too. FISSURE Playground 3: 16-team top-3 groups + 6-team
    # playoff in ONE game; BLAST Open Porto: separate "Group Stage - " and
    # "Playoffs - " games.
    playoff_stage = next((c for c in candidates if c["kind"] == "playoff"), None)
    stage_scoped, scope_reason = _fantasy_stage_scope(event)
    combined = bool(chosen and kind in ("groups", "swiss", "double_elim") and playoff_stage and not stage_scoped)
    playoff_size = int((chosen or {}).get("playoff_size") or 0)
    playoff_byes = int((chosen or {}).get("byes") or 0)
    combined_supported = False
    combined_shape = ""
    if combined:
        label = f"{label} + {playoff_stage['label']}"
        playoff_size = int(playoff_stage["playoff_size"] or 0)
        playoff_byes = int(playoff_stage.get("byes") or 0)
        # Only shapes with a verified playoff seeding are simulated; the rest
        # are refused and flagged for development (groups.py allowlist).
        from backend.routes.groups import (
            combined_shape_label,
            combined_shape_supported,
            reject_unsupported_combined_shape,
        )

        if kind == "groups":
            fmt = str((chosen or {}).get("group_format") or "")
            if fmt == "de8" and (chosen or {}).get("group_variant") == "de8_top3":
                fmt = "de8_top3"
            combined_shape = combined_shape_label(fmt, len(group_brackets))
            combined_supported = combined_shape_supported(fmt, len(group_brackets))
            if not combined_supported:
                reject_unsupported_combined_shape(fmt, len(group_brackets), int(event.get("event_id") or 0) or None)
        else:
            combined_shape = f"{kind} + {playoff_stage['label']}"
            logger.warning("Event %s: %s — one fantasy game scoring a %s stage plus its playoff is not modelled", event.get("event_id"), combined_shape, kind)

    return {
        "kind": kind,
        "label": label,
        "group_format": (chosen or {}).get("group_format"),
        "group_variant": (chosen or {}).get("group_variant"),
        "playoff_size": playoff_size,
        "playoff_byes": playoff_byes,
        "bracket_kind": (chosen or {}).get("bracket_kind") or "single",
        "grand_final_bo": (chosen or {}).get("grand_final_bo"),
        "routing_error": (chosen or {}).get("routing_error"),
        "combined_playoffs": combined,
        "combined_supported": combined_supported,
        "combined_shape": combined_shape,
        # A third-place decider: the formats table's "3rd place decider match
        # BoN" line, or an auxiliary bracket named for it on the page.
        "playoff_third_place": bool((structure.get("playoff_bo_rules") or {}).get("thirdPlace"))
        or any(b.get("aux") for b in (structure.get("brackets") or []) if b.get("bracket") == "single_elim"),
        "stage_scoped": stage_scoped,
        "stage_scope_reason": scope_reason,
        "team_count": team_count,
        "stages": [
            {"kind": c["kind"], "label": c["label"], "size": c["size"], "roster_size": len(c["roster"])}
            for c in candidates
        ],
        "formats_table": structure["formats_table"],
        "swiss_bo_rules": structure["swiss_bo_rules"],
        "group_bo_rules": structure["group_bo_rules"],
        "playoff_bo_rules": structure["playoff_bo_rules"],
    }


@router.get("/player-latest-teams")
def player_latest_teams():
    """Each player's current team = the team they were rostered on in the most
    recent event (event_teams, highest event_id). Used by the players table so
    a player who moved shows only where they play now."""
    latest = get_player_latest_teams()
    return {"teams": {str(pid): row for pid, row in latest.items()}}


@router.get("/{event_id}/swiss-context")
def get_event_swiss_context(event_id: int):
    """Everything the swiss simulator needs, from the event page's structured
    data: the official seed order mapped to OUR team ids, and the Bo mode
    derived from the stage's format rules. 404s when the fantasy event has no
    detectable swiss stage."""
    from backend.routes.groups import _find_event_snapshot_html
    from backend.services.event_format import detect_event_structure

    event = get_event_detail(int(event_id))
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    html = _find_event_snapshot_html(event.get("hltv_event_id")) or ""
    structure = detect_event_structure(html)
    if not structure["swiss"]:
        raise HTTPException(status_code=404, detail="No swiss stage detected for this event")

    fantasy_names = {_norm_team_name(t.get("team_name") or "") for t in (event.get("teams") or [])}
    fantasy_names.discard("")

    def _overlap(stage) -> float:
        roster = {_norm_team_name(n) for n in stage["teams"].values()}
        if not fantasy_names or not roster:
            return 0.0
        return len(fantasy_names & roster) / len(fantasy_names)

    stage = max(structure["swiss"], key=_overlap)

    # Map HLTV team ids (seed order) -> our team ids. Several team rows can
    # share a normalised name (a re-spelt team imported as a new row), so a
    # name resolves like the event's own roster mapping does: the exact
    # spelling first, then the row the fantasy game prices, then any.
    from backend.services import event_rosters

    all_teams = list(get_all_teams())
    listed = set(event_rosters.rosters(int(event_id)).keys())
    by_norm: Dict[str, list] = {}
    for t in all_teams:
        by_norm.setdefault(_norm_team_name(str(t.get("name") or "")), []).append(t)

    def _resolve(hltv_tid, name: str):
        pool = list(by_norm.get(_norm_team_name(name), []))
        if not pool and hltv_tid:
            pool = [t for t in all_teams if str(t.get("hltv_team_id") or "") == str(hltv_tid)]
        for pick in (
            [t for t in pool if str(t.get("name") or "") == name],
            [t for t in pool if int(t["team_id"]) in listed],
            pool,
        ):
            if pick:
                return int(pick[0]["team_id"])
        return None

    seeds = []
    unmatched = []
    order = stage.get("seedings") or list(stage["teams"].keys())
    for ordinal, hltv_tid in enumerate(order, start=1):
        name = stage["teams"].get(hltv_tid) or ""
        our_id = _resolve(hltv_tid, name)
        seeds.append({"seed": ordinal, "hltv_team_id": hltv_tid, "name": name, "team_id": our_id})
        if not our_id:
            unmatched.append(name)

    rules = stage.get("bo_rules")
    bo3_mode = None
    if rules:
        values = (rules["default"], rules["progression"], rules["elimination"])
        if values == (3, 3, 3):
            bo3_mode = "all"
        elif values == (1, 3, 3):
            bo3_mode = "elim_qual"
        elif values == (1, 1, 1):
            bo3_mode = "none"
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Swiss Bo rules {rules} do not map to a simulator mode",
            )

    return {
        "status": "ok",
        "event_id": int(event_id),
        "stage_name": stage.get("stage_name") or stage.get("variant") or "",
        "team_count": stage["team_count"],
        "advance_count": stage["advance_count"],
        "first_round_rule": stage["first_round_rule"],
        "successor_rule": stage["successor_rule"],
        "bo3_mode": bo3_mode,
        "bo_rules": rules,
        "seeds": seeds,
        "team_ids": [s["team_id"] for s in seeds if s["team_id"]],
        "seed_by_team_id": {str(s["team_id"]): s["seed"] for s in seeds if s["team_id"]},
        "unmatched": unmatched,
    }


# Kind detection re-parses the archived event page (~600 KB of HTML + typed
# JSON) on every call, and the Events page asks for every event at once — ten
# concurrent parses took ~1.5 s each. The result is deterministic given the
# snapshot, the priced roster and the sibling fantasy games' names, so cache it
# on that signature (a new snapshot, roster or rename invalidates naturally).
_KIND_CACHE: Dict[int, tuple] = {}
_KIND_CACHE_LOCK = threading.Lock()


def _kind_signature(event: Dict[str, Any]) -> tuple:
    from backend.data.page_snapshots import latest_snapshot_time

    hid = int(event.get("hltv_event_id") or 0)
    snap_ts = latest_snapshot_time(f"https://www.hltv.org/events/{hid}/") if hid else None
    siblings = tuple(
        sorted(
            (int(row.get("event_id") or 0), str(row.get("name") or ""))
            for row in list_events()
            if hid and int(row.get("hltv_event_id") or 0) == hid
        )
    )
    roster = tuple(sorted(str(t.get("team_name") or "") for t in (event.get("teams") or [])))
    return (hid, snap_ts, roster, siblings)


def _detect_event_tournament_kind_cached(event: Dict[str, Any]) -> Dict[str, Any]:
    event_id = int(event.get("event_id") or 0)
    signature = _kind_signature(event)
    with _KIND_CACHE_LOCK:
        cached = _KIND_CACHE.get(event_id)
    if cached and cached[0] == signature:
        return cached[1]
    detected = _detect_event_tournament_kind(event)
    # Event start / end from the archived event page's date stamps, so bakes
    # can tell whether the event has started (inputs are frozen from then on).
    if "start_at" not in detected:
        detected["start_at"], detected["end_at"] = _event_page_dates(event)
    with _KIND_CACHE_LOCK:
        _KIND_CACHE[event_id] = (signature, detected)
    return detected


def _event_page_dates(event: Dict[str, Any]) -> tuple:
    """(start, end) as epoch seconds from the archived HLTV event page: the
    two `data-unix` stamps of its date cell (first / last day). (None, None)
    when the page is not archived or carries no stamps."""
    import re as _re

    from backend.routes.groups import _find_event_snapshot_html

    try:
        html = _find_event_snapshot_html(event.get("hltv_event_id")) or ""
    except Exception:  # noqa: BLE001
        return None, None
    if not html:
        return None, None
    cell = _re.search(r'class="eventdate"(.{0,600}?)</td>', html, flags=_re.S)
    scope = cell.group(1) if cell else html[:200000]
    stamps = [int(v) // 1000 for v in _re.findall(r'data-unix="(\d+)"', scope)[:2]]
    if not stamps:
        return None, None
    start = stamps[0]
    end = stamps[1] if len(stamps) > 1 else stamps[0]
    return float(min(start, end)), float(max(start, end))


def warm_kind_cache() -> None:
    """Detect every event's kind once in the background at startup so the
    Events page's burst of /kind calls never pays the cold parse (~1.2 s)."""
    try:
        for row in list_events():
            event = get_event_detail(int(row.get("event_id") or 0))
            if event:
                _detect_event_tournament_kind_cached(event)
    except Exception:
        logger.debug("kind cache warm-up failed", exc_info=True)


@router.get("/{event_id}/kind")
def get_event_kind(event_id: int):
    """Tournament kind for a fantasy event: auto-detected, with any manual
    override applied. The single Tournament page uses this to pick its UI."""
    event = get_event_detail(int(event_id))
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    detected = _detect_event_tournament_kind_cached(event)
    override = get_event_tournament_kind(int(event_id))
    kind = override or detected["kind"]
    if override and override != detected["kind"]:
        label = {
            "swiss": "Swiss Stage",
            "groups": "Group Stage",
            "playoff": "Playoff",
            "bounty": "Bounty Draft",
            "double_elim": "Double-Elimination Bracket",
        }.get(override, override)
    else:
        label = detected["label"]
    return {
        "event_id": int(event_id),
        "kind": kind,
        "label": label,
        "detected": detected["kind"],
        "override": override,
        "group_format": detected["group_format"],
        "group_variant": detected.get("group_variant"),
        "playoff_size": detected["playoff_size"],
        "playoff_byes": detected.get("playoff_byes", 0),
        "bracket_kind": detected.get("bracket_kind") or "single",
        "grand_final_bo": detected.get("grand_final_bo") or (detected.get("playoff_bo_rules") or {}).get("grandFinal"),
        "routing_error": detected.get("routing_error"),
        "combined_playoffs": detected.get("combined_playoffs", False),
        "combined_supported": detected.get("combined_supported", False),
        "combined_shape": detected.get("combined_shape", ""),
        "playoff_third_place": detected.get("playoff_third_place", False),
        "playoff_layout": _event_playoff_layout(int(event_id)),
        "stage_scoped": detected.get("stage_scoped", False),
        "stage_scope_reason": detected.get("stage_scope_reason", ""),
        "team_count": detected["team_count"],
        "stages": detected["stages"],
        "formats_table": detected["formats_table"],
        "swiss_bo_rules": detected.get("swiss_bo_rules"),
        "group_bo_rules": detected.get("group_bo_rules"),
        "playoff_bo_rules": detected.get("playoff_bo_rules"),
    }


def _event_playoff_layout(event_id: int) -> str:
    from backend.routes.groups import DEFAULT_PLAYOFF_LAYOUT, PLAYOFF_LAYOUTS

    value = str(get_event_playoff_layout(event_id) or "").strip().lower()
    return value if value in PLAYOFF_LAYOUTS else DEFAULT_PLAYOFF_LAYOUT


@router.post("/{event_id}/playoff-layout")
def set_event_playoff_layout_route(event_id: int, payload: Dict[str, Any] | None = None):
    """Set the semi-final layout of a 2-group combined playoff ("cross" or
    "same_group") and re-bake the event's valuations with it."""
    from backend.routes.groups import PLAYOFF_LAYOUTS
    from backend.services import event_pipeline

    event = get_event_detail(int(event_id))
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    layout = str((payload or {}).get("layout") or "").strip().lower()
    if layout not in PLAYOFF_LAYOUTS:
        raise HTTPException(status_code=400, detail=f"layout must be one of {', '.join(PLAYOFF_LAYOUTS)}")
    set_event_playoff_layout(int(event_id), layout)
    bake = event_pipeline.bake_event(int(event_id), trigger="manual")
    return {**get_event_kind(int(event_id)), "bake": bake}


@router.delete("/{event_id}")
def remove_event(event_id: int):
    """Remove a fantasy event the operator will not value (finished without a
    run, or imported by mistake) and every stored run keyed by it. The active
    event cannot be removed."""
    from backend.routes import groups as groups_routes
    from backend.routes import playoff as playoff_routes
    from backend.routes import simulation as simulation_routes
    from backend.services import event_rosters

    eid = int(event_id)
    if not get_event_detail(eid):
        raise HTTPException(status_code=404, detail="Event not found")
    if get_active_event_id() == eid:
        raise HTTPException(status_code=409, detail="The active event cannot be removed; activate another event first")
    stores = [
        groups_routes._GROUPS_STATE, groups_routes._GROUPS_BEST_STATE, groups_routes._GROUPS_BEST_META,
        groups_routes._INPUT_SNAPSHOT, simulation_routes._SIM_STATE,
    ] + [store for variant in playoff_routes._STATE_SETS.values() for store in variant.values()]
    for store in stores:
        try:
            store.delete(key=eid)
        except Exception:  # noqa: BLE001
            logger.warning("Could not drop %s for event %s", getattr(store, "table", store), eid, exc_info=True)
    removed = delete_event(eid)
    with _KIND_CACHE_LOCK:
        _KIND_CACHE.pop(eid, None)
    event_rosters.invalidate()
    return {"status": "ok", **removed}


@router.post("/{event_id}/kind")
def set_event_kind(event_id: int, payload: Dict[str, Any] | None = None):
    """Set (or clear with kind=null/'auto') the manual tournament-kind override."""
    event = get_event_detail(int(event_id))
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    kind = (payload or {}).get("kind")
    if isinstance(kind, str) and kind.strip().lower() in ("", "auto"):
        kind = None
    if kind is not None and str(kind).strip().lower() not in ("swiss", "groups", "playoff", "bounty", "double_elim"):
        raise HTTPException(status_code=400, detail="kind must be swiss, groups, playoff, bounty, double_elim, or auto")
    set_event_tournament_kind(int(event_id), kind)
    return get_event_kind(int(event_id))


@router.get("/{event_id}")
def fetch_event(event_id: int):
    event = get_event_detail(int(event_id))
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.post("/activate")
def activate_event(payload: Dict[str, Any]):
    event_id_raw = payload.get("event_id")
    if event_id_raw is None:
        raise HTTPException(status_code=400, detail="event_id is required")
    if not str(event_id_raw).isdigit():
        raise HTTPException(status_code=400, detail="event_id must be numeric")
    event_id = int(event_id_raw)

    event = get_event_detail(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    set_active_event(event_id)
    return {"status": "ok", "active_event_id": event_id}


@router.get("/hltv-results/stored")
def get_stored_hltv_result(match_url: str):
    """The stored result row for one match, straight from the DB — no live
    fetch. Used by the player card's Recent Matches click-through."""
    url = str(match_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="match_url is required")
    stored = get_hltv_result_by_url(url)
    if stored is None:
        m = re.search(r"/matches/(\d+)/", url)
        if m:
            mid = int(m.group(1))
            rows = list_hltv_results(limit=1000, offset=0)
            stored = next((r for r in rows if int(r.get("match_id") or 0) == mid), None)
    if stored is None:
        raise HTTPException(status_code=404, detail="No stored result for that match")
    return stored


@router.get("/hltv-results/match-details")
def get_hltv_result_match_details(match_url: str):
    url = str(match_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="match_url is required")

    stored = get_hltv_result_by_url(url)
    if stored is None:
        # Fallback to canonical match-id lookup if client passes variant URL.
        m = re.search(r"/matches/(\d+)/", url)
        if m:
            mid = int(m.group(1))
            rows = list_hltv_results(limit=500, offset=0)
            stored = next((r for r in rows if int(r.get("match_id") or 0) == mid), None)

    maps = []
    player_stats = []
    map_player_stats = {}
    live_detail_failed = False
    try:
        details = get_hltv_match_details(url)
        maps = details.get("maps") or []
        player_stats = details.get("player_stats") or []
        map_player_stats = details.get("map_player_stats") or {}
        if maps and stored and stored.get("match_url"):
            set_hltv_result_maps(str(stored.get("match_url")), maps)
    except HLTVRankingError:
        live_detail_failed = True
        maps = []
    except Exception:
        live_detail_failed = True
        maps = []

    if live_detail_failed and not maps and isinstance(stored, dict) and stored.get("maps_json"):
        try:
            import json
            parsed = json.loads(str(stored.get("maps_json")))
            if isinstance(parsed, list):
                maps = parsed
        except Exception:
            maps = []

    return {
        "match_url": url,
        "stored": stored,
        "maps": maps,
        # the page's scoreboards, for a match not stored yet (today's results)
        "player_stats": player_stats,
        "map_player_stats": map_player_stats,
    }


def discover_and_import_new_fantasy_events() -> Dict[str, Any]:
    """Pull https://www.hltv.org/fantasy and import any fantasy events not in
    the database yet (the front page links every open event as /fantasy/{id}).
    The normal import activates what it imports, so the previously active
    event is restored afterwards — an unattended nightly pull must not switch
    the user's active event. Raises when the page shows no fantasy links at
    all (layout change should fail loudly, not read as 'no new events')."""
    from backend.services.hltv_browser import fetch_hltv_html

    html = fetch_hltv_html("https://www.hltv.org/fantasy")
    found = sorted({int(m) for m in re.findall(r"/fantasy/(\d+)", html or "")})
    if not found:
        raise ValueError("No fantasy event links found on hltv.org/fantasy")
    # The nav dropdown carries each fantasy event's OFFICIAL name
    # ("Playoffs - BLAST Open Porto 2026") — the authoritative label, and the
    # only reliable one for events whose fantasy JSON has no HLTV event ref.
    nav_names: Dict[int, str] = {}
    for m in re.finditer(r'href="/fantasy/(\d+)/gameredirect"[^>]*>.*?text-ellipsis">([^<]+)<', html or ""):
        nav_names.setdefault(int(m.group(1)), m.group(2).strip())
    existing = {int(e["event_id"]) for e in list_events()}
    removed = set(list_deleted_event_ids())  # pruned by the operator: never re-imported
    new_ids = [fid for fid in found if fid not in existing and fid not in removed]
    prev_active = get_active_event_id()
    imported: List[int] = []
    failed: List[Dict[str, Any]] = []
    for fid in new_ids:
        try:
            import_hltv_event({"event_id": str(fid)})
            imported.append(fid)
        except Exception as exc:  # noqa: BLE001 — one bad event must not stop the rest
            failed.append({"event_id": fid, "error": str(exc)[:200]})
    if prev_active is not None and imported:
        try:
            set_active_event(int(prev_active))
        except Exception:
            logger.exception("Could not restore active event %s after auto-import", prev_active)
    # Sync names from the fantasy page nav for every event we hold — it names
    # per-stage events unambiguously where URL slugs cannot.
    renamed = []
    for fid, nav_name in nav_names.items():
        if fid in existing or fid in imported:
            try:
                set_event_name(fid, nav_name, overwrite_name=True)
                renamed.append(fid)
            except Exception:
                logger.exception("Could not sync name for fantasy event %s", fid)
    return {
        "status": "ok",
        "found": len(found),
        "new": new_ids,
        "imported": imported,
        "failed": failed,
        "renamed": renamed,
    }


@router.post("/pull-new")
def pull_new_fantasy_events():
    """Manual trigger for the nightly fantasy-event discovery."""
    return discover_and_import_new_fantasy_events()


@router.post("/import-hltv-event")
def import_hltv_event(payload: Dict[str, Any]):
    event_id = str(payload.get("event_id", "")).strip()
    if not event_id.isdigit():
        raise HTTPException(status_code=400, detail="event_id must be numeric")

    url = f"https://www.hltv.org/fantasy/{event_id}/leagues/create/json"
    try:
        data = fetch_hltv_json(url)
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch HLTV data with SeleniumBase UC: {exc}")

    money = data.get("moneyDraftData", {})
    if not money:
        raise HTTPException(status_code=400, detail="moneyDraftData missing in response")

    ref = _extract_hltv_event_ref_from_fantasy(int(event_id), data)
    counts = _import_money_draft_data(
        money,
        event_id=int(event_id),
        hltv_event_id=ref.get("hltv_event_id"),
        hltv_event_url=ref.get("hltv_event_url"),
    )
    derived_name = _event_name_from_url(str(ref.get("hltv_event_url") or ""))
    if derived_name:
        set_event_name(int(event_id), derived_name)

    # Best-effort: fetch the HLTV event page (which is snapshotted by the
    # browser fetch), detect the group format, and prefill the group seeds so
    # the Groups tab is ready without a manual autofill. Never fail the import
    # if the event has no group bracket (Swiss/single-elim) or the fetch is
    # blocked — the manual "Autofill from HLTV event" button still works.
    autofill = None
    try:
        from backend.routes.groups import store_event_groups_autofill

        autofill = store_event_groups_autofill(
            int(event_id),
            hltv_event_url=ref.get("hltv_event_url") or "",
            hltv_event_id=ref.get("hltv_event_id"),
        )
    except Exception:
        autofill = None

    return {
        "status": "ok",
        **counts,
        "event_id": int(event_id),
        "active_event_id": int(event_id),
        **ref,
        "group_format": (autofill or {}).get("group_format"),
        "group_count": len((autofill or {}).get("groups") or []) if autofill else 0,
    }


# ---- the model the app uses ---------------------------------------------------
_MAP_MODEL_PRODUCTION_STATE = SingletonState("map_model_production")
_MAP_MODEL_PRODUCTION_STATE_READY = False


def _production_state() -> SingletonState:
    global _MAP_MODEL_PRODUCTION_STATE_READY
    if not _MAP_MODEL_PRODUCTION_STATE_READY:
        _MAP_MODEL_PRODUCTION_STATE.ensure_table()
        _MAP_MODEL_PRODUCTION_STATE_READY = True
    return _MAP_MODEL_PRODUCTION_STATE
_PRODUCTION_CACHE: Dict[str, Any] = {"checked_at": 0.0, "updated_at": None, "value": None}
_PRODUCTION_LOCK = threading.Lock()


def train_production_map_model() -> Dict[str, Any]:
    """Fit the pooled map model on every usable stored map (the lab keeps a
    holdout; the app should not) and snapshot the current player ratings per
    team, then store both for the simulators (team_strength.get_team_winrate)."""
    db_matches = int(count_hltv_results())
    rows = list_hltv_results(limit=db_matches, offset=0) if db_matches > 0 else []
    historical, _summary = _build_historical_team_map_stats_by_window(rows, fetch_missing=False)
    current: Dict[str, Any] = {}
    ratings = _build_prematch_player_ratings(rows, current=current)
    samples = _iter_ranked_map_samples(
        rows,
        historical_map_stats_by_window=historical,
        require_map_stats=True,
        require_veto=True,
        prematch_ratings_by_match=ratings,
    )
    if len(samples) < 200:
        raise HTTPException(status_code=400, detail="Not enough usable maps to train the app model.")
    model = _fit_map_model_set(samples, target="map_win")["__global__"]
    teams = {key: {"players": float(mean)} for key, mean in (current.get("players_by_team") or {}).items()}
    veto_instances, veto_by_url, veto_contexts = _replay_vetoes(rows, historical, ratings, model)
    veto_model = _fit_veto_model(veto_instances)
    if veto_model:
        _fit_veto_calibration(veto_model, _veto_bundles(rows, historical, ratings, veto_by_url, model))
    for key, ctx in veto_contexts.items():
        teams.setdefault(key, {})["veto"] = ctx
    trained_at = time.time()
    signature = _lab_data_signature()
    result = {
        "model": model,
        "teams": teams,
        "trained_at": trained_at,
        "maps": len(samples),
        "matches": len({_lab_match_key(x) for x in samples}),
        "teams_rated": len(teams),
        "veto": veto_model,
        "veto_choices": dict((veto_model or {}).get("choices") or {}),
        "features": int(_MAP_MODEL_FEATURE_VERSION),
        "signature": signature,
    }
    _production_state().save({"trained_at": trained_at, "signature": signature}, result)
    with _PRODUCTION_LOCK:
        _PRODUCTION_CACHE.update({"checked_at": 0.0, "updated_at": None, "value": None})
    from backend.services import team_strength  # lazy: it imports this module lazily too

    team_strength.clear_caches()
    logger.info("App map model trained on %d maps (%d teams rated)", len(samples), len(teams))
    return {k: v for k, v in result.items() if k not in ("model", "teams", "veto")}


def get_production_map_model() -> Dict[str, Any] | None:
    """The stored app model, re-read from the state row at most every 30 s."""
    now = time.monotonic()
    with _PRODUCTION_LOCK:
        if _PRODUCTION_CACHE["value"] is not None and now - float(_PRODUCTION_CACHE["checked_at"]) < 30.0:
            return _PRODUCTION_CACHE["value"]
    saved = _production_state().load()
    value = (saved or {}).get("result") or None
    if value and not value.get("model"):
        value = None
    updated_at = (saved or {}).get("updated_at")
    with _PRODUCTION_LOCK:
        changed = updated_at != _PRODUCTION_CACHE["updated_at"]
        _PRODUCTION_CACHE.update({"checked_at": now, "updated_at": updated_at, "value": value})
    if changed:
        from backend.services import team_strength

        team_strength.clear_caches()
    return value


def ensure_production_map_model(force: bool = False) -> Dict[str, Any]:
    """Train the app model when there is none, when the data it trains on
    changed (matches, vetoes, map-stat windows, feature version), or on
    request. Returns the summary plus whether it trained."""
    saved = _production_state().load()
    result = (saved or {}).get("result") or {}
    then = ((saved or {}).get("payload") or {}).get("signature") or {}
    now = _lab_data_signature()
    stale = any(int(now.get(k, 0)) != int(then.get(k, 0)) for k in now)
    if force or not result.get("model") or stale:
        summary = train_production_map_model()
        return {"trained": True, **summary}
    return {"trained": False, **{k: v for k, v in result.items() if k not in ("model", "teams", "veto")}}


def production_series_probability(production: Dict[str, Any], team_a: Dict[str, Any], team_b: Dict[str, Any], wins_needed: int) -> float:
    """P(team A wins a best-of series) from the app model.

    team_a / team_b: {"key": normalised name, "hltv_rank": int, "vrs_rank":
    int | None, "map_stats": {map: {win_rate, pick_rate, ban_rate, played}}}.
    The map probability is averaged over the active pool (no veto is known
    pre-match; walk-forward this scored the same as a simulated veto) and
    pushed through the best-of formula."""
    model = production["model"]
    rated = production.get("teams") or {}
    h1 = float(max(1, int(team_a.get("hltv_rank") or 100)))
    h2 = float(max(1, int(team_b.get("hltv_rank") or 100)))
    v1 = float(team_a.get("vrs_rank") or h1)
    v2 = float(team_b.get("vrs_rank") or h2)
    pa = (rated.get(str(team_a.get("key") or "")) or {}).get("players")
    pb = (rated.get(str(team_b.get("key") or "")) or {}).get("players")
    shared = {
        "hltv_gap": h2 - h1,
        "hltv_gap_level": (h2 - h1) * ((h1 + h2) / 2.0),
        "vrs_gap": v2 - v1,
        "vrs_gap_level": (v2 - v1) * ((v1 + v2) / 2.0),
        **_log_rank_features(h1, h2),
        "picked_by_a": 0.0,
        "player_rating_gap": (float(pa) - float(pb)) if (pa is not None and pb is not None) else 0.0,
    }
    stats_a = team_a.get("map_stats") or {}
    stats_b = team_b.get("map_stats") or {}
    probs = []
    for map_name in MAP_POOL:
        features = _map_stat_features(stats_a, stats_b, map_name)
        values = {
            **shared,
            "map_win_gap": float(features.get("map_win_gap") or 0.0),
            "pick_gap": float(features.get("pick_gap") or 0.0),
            "ban_gap": float(features.get("ban_gap") or 0.0),
            "played_pct_gap": float(features.get("played_pct_gap") or 0.0),
        }
        probs.append(_model_map_win_probability(model, values))
    return _best_of_series_win_probability(sum(probs) / len(probs), wins_needed)


def predict_veto(production: Dict[str, Any], team_a: Dict[str, Any], team_b: Dict[str, Any], wins_needed: int) -> Dict[str, Any]:
    """The veto as probabilities for a series between two live teams (team A
    acting first): per map, the chance it is played and by whose pick, plus
    the most likely draft. Inputs as for production_series_probability."""
    veto_model = production.get("veto")
    if not veto_model:
        return {"maps": [], "most_likely": []}
    model = production["model"]
    rated = production.get("teams") or {}
    h1 = float(max(1, int(team_a.get("hltv_rank") or 100)))
    h2 = float(max(1, int(team_b.get("hltv_rank") or 100)))
    v1 = float(team_a.get("vrs_rank") or h1)
    v2 = float(team_b.get("vrs_rank") or h2)
    a_info = rated.get(str(team_a.get("key") or "")) or {}
    b_info = rated.get(str(team_b.get("key") or "")) or {}
    pa, pb = a_info.get("players"), b_info.get("players")
    gap = (float(pa) - float(pb)) if (pa is not None and pb is not None) else 0.0
    stats_a = team_a.get("map_stats") or {}
    stats_b = team_b.get("map_stats") or {}
    sides = [
        {"stats": stats_a, "ctx": a_info.get("veto") or _veto_empty_context(),
         "p_win": lambda m: _neutral_map_win_minus_half(model, (h1, v1), (h2, v2), stats_a, stats_b, gap, m)},
        {"stats": stats_b, "ctx": b_info.get("veto") or _veto_empty_context(),
         "p_win": lambda m: _neutral_map_win_minus_half(model, (h2, v2), (h1, v1), stats_b, stats_a, -gap, m)},
    ]
    pool = sorted(MAP_POOL)
    distribution = _veto_distribution(veto_model, pool, _VETO_ORDER.get(max(1, min(3, wins_needed)), _VETO_ORDER[2]), date.today().isoformat(), sides, wins_needed)
    probs = _veto_play_probabilities(veto_model, distribution)
    maps = []
    for m in pool:
        row = probs.get(m) or {"play": 0.0, "pick_a": 0.0, "pick_b": 0.0, "decider": 0.0}
        maps.append({"map": m, "p_a": 0.5 + sides[0]["p_win"](m), **{k: round(v, 4) for k, v in row.items()}})
    maps.sort(key=lambda x: -x["play"])
    likely = max(distribution, key=lambda x: x[0]) if distribution else (0.0, [])
    return {
        "maps": maps,
        "most_likely": {"probability": round(likely[0], 4), "maps": [{"map": m, "picked_by": "a" if f > 0 else "b" if f < 0 else "decider"} for m, f in likely[1]]},
    }


@router.get("/hltv-results/map-model/veto")
def get_map_model_veto(team_a: int, team_b: int, match_type: str = "bo3"):
    """The predicted veto for two teams from the teams table."""
    from backend.services import team_strength

    production = get_production_map_model()
    if not production:
        raise HTTPException(status_code=404, detail="No app model trained yet.")
    a = {**team_strength._team_profile(team_a), "hltv_rank": team_strength.get_hltv_rank(team_a)}
    b = {**team_strength._team_profile(team_b), "hltv_rank": team_strength.get_hltv_rank(team_b)}
    wins_needed = {"bo1": 1, "bo3": 2, "bo5": 3}.get(str(match_type or "bo3").lower(), 2)
    return {"match_type": f"bo{2 * wins_needed - 1}", **predict_veto(production, a, b, wins_needed)}
