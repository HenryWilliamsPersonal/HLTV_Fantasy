"""Every user team of an event's fantasy league, with its points so far and
its expected points at the end of the tournament from the current point.

The current point is the engine's outcome space (the stored run's paths,
services/paths_engine) restricted to the outcomes consistent with every
finished match, reweighted. From those outcomes each player gets his
expected remaining rating + win points, booster points, matches and padded
matches. A team's projection adds, per player, those remaining points plus
its OWN role's points for the remaining matches: the role's per-match value
observed on the field (services/fantasy_field.match_facts: the points the
role scored in each match, identical for every team that used it), from
this event or, before the player has played here, from another stored field
of the same tournament (the Swiss stage before a playoff).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: Dict[int, Dict[str, Any]] = {}  # event_id -> {"key", "players", "built_at"}
_TEAMS_CACHE: Dict[int, Dict[str, Any]] = {}  # event_id -> {"key", "rows"}
PAGE_SIZE = 50


def _finished(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    ms = [m for m in (entry.get("matches") or []) if m.get("winner") is not None and m.get("a") and m.get("b")]
    return sorted(ms, key=lambda m: str(m.get("date") or ""))


def _remaining(event_id: int) -> Dict[str, Any]:
    """{players: {pid: {rw, booster, matches, pads}}, teams_alive, outcomes,
    constraints_used, constraints_skipped} — the expectations of what each
    player still scores, over the outcomes consistent with the results."""
    import numpy as np

    from backend.routes import groups
    from backend.services import live_state, paths_engine

    eid = int(event_id)
    latest = groups._GROUPS_STATE.load(key=eid) or {}
    results = latest.get("results") or {}
    if str(results.get("group_format") or "") not in ("swiss", "bracket"):
        return {"players": {}, "outcomes": 0}
    entry = live_state.live_event(eid, fetch_if_missing=False) or {}
    finished = _finished(entry)
    key = (latest.get("updated_at"), tuple((int(m["match_id"] or 0), int(m["winner"])) for m in finished))
    with _LOCK:
        hit = _CACHE.get(eid)
        if hit and hit["key"] == key:
            return hit["value"]
    sc = groups._paths_for(results)
    p = sc.paths
    pos = {int(t): i for i, t in enumerate(p.team_ids)}
    sel = np.ones(p.n, dtype=bool)
    used = skipped = 0
    played = {t: 0 for t in range(len(p.team_ids))}
    for m in finished:
        a, b, w = int(m["a"]), int(m["b"]), int(m["winner"])
        if a not in pos or b not in pos:
            skipped += 1
            continue
        ta, tb = pos[a], pos[b]
        played[ta] += 1
        played[tb] += 1
        hit_any = ((p.opp[:, ta, :] == tb) & (p.won[:, ta, :] == (1 if w == a else 0))).any(axis=1)
        nxt = sel & hit_any
        if nxt.any():
            sel, used = nxt, used + 1
        else:
            skipped += 1  # a pairing the engine never drew (sampled Swiss): left out
    weights = p.weight[sel].astype(np.float64)
    weights = weights / weights.sum() if weights.sum() > 0 else weights
    players: Dict[int, Dict[str, float]] = {}
    for pid in sc.pids:
        mx = paths_engine._player_matrix(sc, pid, sel)
        t = sc.team_pos_of[pid]
        future = mx["mask"] & (mx["match_no"] > played[t])  # the team's matches after the ones played
        comps = mx["comps"] * future[:, :, None]
        rw = comps[:, :, 0].sum(axis=1) + comps[:, :, 1].sum(axis=1)
        booster = comps[:, :, 3].sum(axis=1)
        n_future = future.sum(axis=1).astype(np.float64)
        pads = mx["pads"].sum(axis=1)
        pad_rw = mx["pad_comps"][:, :, 0].sum(axis=1) + mx["pad_comps"][:, :, 1].sum(axis=1)
        players[int(pid)] = {
            "rw": float(weights @ rw), "booster": float(weights @ booster), "matches": float(weights @ n_future),
            "pads_total": float(weights @ pads), "pad_rw_total": float(weights @ pad_rw),
            "pad_rw_each": float(weights @ pad_rw) / float(weights @ pads) if float(weights @ pads) > 0 else 0.0,
        }
    value = {"players": players, "outcomes": int(sel.sum()), "constraints_used": used, "constraints_skipped": skipped,
             "engine_role": {int(k): v for k, v in sc.role_of.items()}}
    with _LOCK:
        _CACHE[eid] = {"key": key, "value": value}
    return value


def _role_values(event_id: int) -> Dict[tuple, float]:
    """(player_id, role name) -> the role's mean points per match observed on
    the field: this event first, then the other stored fields (the earlier
    stage of the same tournament)."""
    from backend.services import fantasy_field

    out: Dict[tuple, float] = {}
    events = [int(event_id)] + [e for e in fantasy_field.stored_events() if int(e) != int(event_id)]
    for eid in events:
        facts = (fantasy_field.match_facts(eid).get("players") or {})
        for pid, f in facts.items():
            for r in f.get("roles") or []:
                vals = [v for v in (r.get("per_match") or []) if v is not None]
                k = (int(pid), str(r.get("role_name") or ""))
                if vals and k not in out:
                    out[k] = sum(vals) / len(vals)
    return out


def team_rows(event_id: int) -> Dict[str, Any]:
    """Every stored team with points so far, expected remaining and final,
    and the place the final projection gives it. Cached per facts and
    results key."""
    from backend.services import fantasy_field

    eid = int(event_id)
    stored = fantasy_field.teams(eid)
    if not stored:
        return {"rows": [], "teams": 0}
    facts_all = fantasy_field.match_facts(eid)
    rem = _remaining(eid)
    key = (facts_all.get("_key"), id(rem))
    with _LOCK:
        hit = _TEAMS_CACHE.get(eid)
        if hit and hit["key"] == key:
            return hit["value"]
    facts = facts_all.get("players") or {}
    role_pm = _role_values(eid)
    engine_role = rem.get("engine_role") or {}
    from backend.services.my_team import ROLE_NAMES

    rows = []
    for t in stored:
        now = 0.0
        exp = 0.0
        lineup = []
        for pl in t.get("players") or []:
            pid = pl.get("player_id")
            if pid is None:
                continue
            pid = int(pid)
            role = str(pl.get("role_name") or "")
            f = facts.get(pid) or {}
            # points so far: rating + win from the field (padding included),
            # the team's role as it scored, the team's own boosters
            rw_now = float(f.get("rating_points") or 0) + float(f.get("win_points") or 0)
            role_now = next((float(r.get("total") or 0) for r in (f.get("roles") or []) if r.get("role_name") == role), 0.0) if role else 0.0
            boost_now = float(sum(int(m.get("booster") or 0) for m in pl.get("matches") or []))
            pts = rw_now + role_now + boost_now
            r = (rem.get("players") or {}).get(pid) or {}
            pads_done = float(((f.get("padding") or {}).get("count")) or 0)
            pads_left = max(0.0, float(r.get("pads_total") or 0.0) - pads_done)
            if role:
                pm = role_pm.get((pid, role))
                if pm is None:
                    er = engine_role.get(pid) or {}
                    pm = float(er.get("points") or 0.0) if ROLE_NAMES.get(er.get("role_id")) == role else 0.0
            else:
                pm = 0.0
            left = float(r.get("rw") or 0.0) + float(r.get("booster") or 0.0) + (float(r.get("matches") or 0.0) + pads_left) * pm + pads_left * float(r.get("pad_rw_each") or 0.0)
            now += pts
            exp += left
            lineup.append({"player_id": pid, "name": pl.get("name"), "team": pl.get("team"), "role_name": role or None, "points": round(pts), "expected_left": round(left, 1)})
        rows.append({
            "team_id": int(t["team_id"]), "team_name": t.get("team_name") or "", "placement": t.get("placement"),
            "points": round(now), "hltv_points": t.get("total_points"), "expected_left": round(exp, 1), "expected_final": round(now + exp, 1),
            "players": lineup,
        })
    rows.sort(key=lambda r: (-r["points"], r["team_name"].lower()))
    for i, r in enumerate(rows, start=1):
        r["place_now"] = i
    for i, r in enumerate(sorted(rows, key=lambda r: (-r["expected_final"], -r["points"])), start=1):
        r["place_expected"] = i
    value = {"rows": rows, "teams": len(rows), "outcomes": rem.get("outcomes"), "built_at": time.time()}
    with _LOCK:
        _TEAMS_CACHE[eid] = {"key": key, "value": value}
    return value


def page(event_id: int, q: str = "", sort: str = "expected_final", desc: bool = True, page_no: int = 0, size: int = PAGE_SIZE, mine: Optional[List[int]] = None) -> Dict[str, Any]:
    data = team_rows(int(event_id))
    rows = data["rows"]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r["team_name"].lower()]
    keys = {"expected_final", "expected_left", "points", "team_name", "place_now", "place_expected"}
    sort = sort if sort in keys else "expected_final"
    if sort == "team_name":
        rows = sorted(rows, key=lambda r: r["team_name"].lower(), reverse=desc)
    else:
        rows = sorted(rows, key=lambda r: (float(r[sort] or 0), -r["place_now"]), reverse=desc)
    total = len(rows)
    size = max(1, min(200, int(size)))
    start = max(0, int(page_no)) * size
    mine_ids = set(int(x) for x in (mine or []))
    out = [dict(r, mine=r["team_id"] in mine_ids) for r in rows[start:start + size]]
    mine_rows = [dict(r, mine=True) for r in data["rows"] if r["team_id"] in mine_ids]
    return {"rows": out, "total": total, "teams": data["teams"], "page": int(page_no), "size": size, "outcomes": data.get("outcomes"), "mine": mine_rows}
