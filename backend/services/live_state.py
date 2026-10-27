"""The Live page's data: each current fantasy event's stage as it actually
stands, built from the results the nightly import stored.

For every event with a stored valuation — live ones, every upcoming one
and the most recent finished ones — the stored results between the
event's own teams inside its dates are mapped onto the stage: a Swiss
stage round by round (a team's n-th match is its round n, the pool is its
record going in), a bracket match by match down the spec, a group stage
as each group's records. No fixtures are known ahead of time, so unplayed
matches show as the format's remaining slots. The list route serves
summaries; one event's full state is built on demand.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from backend.data import team_aliases
from backend.data.db import connect
from backend.services import live_pages

GRACE_DAYS = 1
PREVIOUS_LIMIT = 12
DAY = 86400.0
SUMMARY_KEYS = ("event_id", "name", "kind", "label", "group_format", "status", "start_at", "end_at", "played", "total", "team_count", "last_match", "live_count")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def _event_name(conn: sqlite3.Connection, event_id: int) -> Optional[str]:
    row = conn.execute("SELECT name FROM events WHERE event_id = ?", (int(event_id),)).fetchone()
    return str(row["name"]) if row and row["name"] else None


def _team_rows(conn: sqlite3.Connection, team_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not team_ids:
        return {}
    rows = conn.execute(
        f"SELECT team_id, name, hltv_team_id FROM teams WHERE team_id IN ({','.join('?' * len(team_ids))})", [int(t) for t in team_ids]
    ).fetchall()
    return {int(r["team_id"]): {"name": r["name"], "hltv_team_id": r["hltv_team_id"]} for r in rows}


def _event_matches(conn: sqlite3.Connection, key_to_tid: Dict[str, int], start: str, end: str) -> List[Dict[str, Any]]:
    """Stored results between two of the event's teams inside the dates,
    oldest first, with the winner as a team id."""
    rows = conn.execute(
        """SELECT match_url, match_id, match_date, team1, team2, score1, score2, winner, maps_json
           FROM hltv_results WHERE match_date >= ? AND match_date <= ? ORDER BY match_date, match_id""",
        (start, end),
    ).fetchall()
    out = []
    for r in rows:
        k1, k2 = team_aliases.key(str(r["team1"] or "")), team_aliases.key(str(r["team2"] or ""))
        if k1 not in key_to_tid or k2 not in key_to_tid or k1 == k2:
            continue
        a, b = key_to_tid[k1], key_to_tid[k2]
        s1, s2 = r["score1"], r["score2"]
        wk = team_aliases.key(str(r["winner"] or ""))
        winner = key_to_tid.get(wk)
        if winner is None and s1 is not None and s2 is not None and s1 != s2:
            winner = a if int(s1) > int(s2) else b
        try:
            maps = len(json.loads(r["maps_json"] or "[]"))
        except Exception:  # noqa: BLE001
            maps = 0
        out.append({
            "url": r["match_url"], "match_id": r["match_id"], "date": r["match_date"], "a": a, "b": b,
            "score_a": s1, "score_b": s2, "winner": winner, "maps": maps,
        })
    return out


def _swiss_state(matches: List[Dict[str, Any]], team_ids: List[int], bo3_mode: str, pending: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Rounds from the finished matches, then the pairings HLTV has made but
    not finished (live or scheduled) in the round their records put them."""
    from backend.services import swiss_engine

    wins = {int(t): 0 for t in team_ids}
    losses = {int(t): 0 for t in team_ids}
    rounds: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    picks: Dict[str, int] = {}
    for m in matches:
        a, b, w = int(m["a"]), int(m["b"]), m.get("winner")
        if w is None or a not in wins or b not in wins:
            continue
        if max(wins[a], losses[a], wins[b], losses[b]) >= swiss_engine.TARGET:
            continue  # a team already through or out: not a Swiss match
        r = max(wins[a] + losses[a], wins[b] + losses[b]) + 1
        if r > swiss_engine.ROUNDS:
            continue
        key = f"r{r}_{len(rounds[r])}"
        rounds[r].append({
            "key": key, "a": a, "b": b, "bo": int(swiss_engine.match_bo(wins[a], losses[a], bo3_mode)),
            "pool": f"{wins[a]}-{losses[a]}", "winner": int(w), "date": m["date"], "score": f"{m['score_a']}-{m['score_b']}",
            "url": m.get("url"), "match_id": m.get("match_id"),
        })
        picks[key] = int(w)
        loser = b if int(w) == a else a
        wins[int(w)] += 1
        losses[loser] += 1
    for pm in pending or []:
        a, b = int(pm["a"]), int(pm["b"])
        if a not in wins or b not in wins:
            continue
        if max(wins[a], losses[a], wins[b], losses[b]) >= swiss_engine.TARGET:
            continue
        r = max(wins[a] + losses[a], wins[b] + losses[b]) + 1
        if r > swiss_engine.ROUNDS:
            continue
        key = f"r{r}_{len(rounds[r])}"
        rounds[r].append({
            "key": key, "a": a, "b": b, "bo": int(swiss_engine.match_bo(wins[a], losses[a], bo3_mode)),
            "pool": f"{wins[a]}-{losses[a]}", "winner": None, "status": pm.get("status"), "start_at": pm.get("start_at"),
            "maps_a": pm.get("maps_a"), "maps_b": pm.get("maps_b"), "map_a": pm.get("map_a"), "map_b": pm.get("map_b"),
        })
    shape = swiss_engine._format_shape(len(team_ids))
    placeholders = []
    for rd in shape["rounds"]:
        played_in = defaultdict(int)
        for m in rounds.get(rd["round"], []):
            played_in[m["pool"]] += 1
        pools = [{"pool": p["pool"], "matches": p["matches"] - played_in[p["pool"]]} for p in rd["pools"] if p["matches"] - played_in[p["pool"]] > 0]
        if pools:
            placeholders.append({"round": rd["round"], "matches": sum(p["matches"] for p in pools), "pools": pools})
    complete = all(wins[t] >= swiss_engine.TARGET or losses[t] >= swiss_engine.TARGET for t in wins)
    return {
        "rounds": [{"round": r, "matches": rounds[r], "complete": all(m.get("winner") is not None for m in rounds[r])} for r in sorted(rounds)],
        "placeholders": placeholders,
        "final_slots": shape["final"],
        "records": {str(t): [wins[t], losses[t]] for t in wins},
        "complete": complete,
        "picks": picks,
        "played": len(picks),
        "total": int(sum(rd["matches"] for rd in shape["rounds"])),
        "stage_by_url": {str(m["url"]): f"Round {r}" for r in rounds for m in rounds[r] if m.get("url") and m.get("winner") is not None},
    }


def _bracket_state(matches: List[Dict[str, Any]], bracket: Dict[str, Any]) -> Dict[str, Any]:
    slots = [int(t) for t in (bracket.get("team_slots") or [])]
    spec = bracket.get("matches") or []
    winners: Dict[str, int] = {}
    losers: Dict[str, int] = {}
    picks: Dict[str, int] = {}
    used = set()
    stage_by_url: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    try:
        from backend.services import stage_formats

        full = stage_formats.bracket_spec(str(bracket.get("kind") or "single"), slots, bool(bracket.get("third_place")), int(bracket.get("final_bo") or 3))
        labels = {str(st.key): str(st.full) for st in full["stages"]}
    except Exception:  # noqa: BLE001 - labels are cosmetic
        labels = {}
    for key, _stage, fa, fb in spec:
        def side(f):
            if f[0] == "seed":
                i = int(f[1])
                return slots[i] if 0 <= i < len(slots) else None
            return winners.get(str(f[1])) if f[0] == "win" else losers.get(str(f[1]))
        a, b = side(fa), side(fb)
        if not a or not b:
            continue
        for m in matches:
            if m["url"] in used or m.get("winner") is None:
                continue
            if {int(m["a"]), int(m["b"])} == {int(a), int(b)}:
                used.add(m["url"])
                w = int(m["winner"])
                picks[str(key)] = w
                winners[str(key)] = w
                losers[str(key)] = int(b) if w == int(a) else int(a)
                stage_by_url[str(m["url"])] = labels.get(str(_stage), str(_stage))
                break
    return {"picks": picks, "played": len(picks), "total": len(spec), "stage_by_url": stage_by_url}


def _groups_state(matches: List[Dict[str, Any]], groups: List[List[int]]) -> List[Dict[str, Any]]:
    out = []
    for gi, g in enumerate(groups):
        ids = {int(t) for t in g}
        recs = {int(t): [0, 0] for t in g}
        played = 0
        stage_by_url: Dict[str, str] = {}
        for m in matches:
            a, b, w = int(m["a"]), int(m["b"]), m.get("winner")
            if a in ids and b in ids and w is not None:
                played += 1
                recs[int(w)][0] += 1
                recs[b if int(w) == a else a][1] += 1
                stage_by_url[str(m.get("url"))] = f"Group {chr(65 + gi)}"
        order = sorted(recs, key=lambda t: (-recs[t][0], recs[t][1]))
        out.append({"teams": order, "records": {str(t): recs[t] for t in recs}, "played": played, "stage_by_url": stage_by_url})
    return out


def _entry(conn: sqlite3.Connection, ev: Dict[str, Any], now: float, fetch_if_missing: bool = False) -> Optional[Dict[str, Any]]:
    from backend.data.event_db import get_event_detail
    from backend.routes import events as events_routes
    from backend.routes.groups import _GROUPS_STATE

    event_id = int(ev.get("event_id") or 0)
    if not event_id:
        return None
    detail = get_event_detail(event_id)
    if not detail:
        return None
    try:
        detected = events_routes._detect_event_tournament_kind_cached(detail)
    except Exception:  # noqa: BLE001
        return None
    start, end = detected.get("start_at"), detected.get("end_at")
    if not start:
        return None
    end = float(end or float(start) + 7 * DAY)
    stored = _GROUPS_STATE.load(key=event_id)
    results = (stored or {}).get("results") or {}
    if not results.get("teams"):
        return None
    status = "upcoming" if now < float(start) else "live" if now <= end + GRACE_DAYS * DAY else "finished"
    team_ids = [int(t) for t in results["teams"]]
    rows = _team_rows(conn, team_ids)
    key_to_tid = {team_aliases.key(str(r["name"] or "")): tid for tid, r in rows.items() if r.get("name")}
    matches = _event_matches(conn, key_to_tid, _iso(start), _iso(end + GRACE_DAYS * DAY))
    fmt = str(results.get("group_format") or "")

    # HLTV's own pages between the nightly imports: today's results, the
    # matches in play and the pairings already made (services/live_pages).
    tid_by_hltv = {int(r["hltv_team_id"]): tid for tid, r in rows.items() if r.get("hltv_team_id")}

    def to_tid(hltv_id):
        try:
            return tid_by_hltv.get(int(hltv_id)) if hltv_id else None
        except (TypeError, ValueError):
            return None

    page: List[Dict[str, Any]] = []
    hltv_event_id = int(detail.get("hltv_event_id") or 0)
    if hltv_event_id and status in ("live", "upcoming"):
        if live_pages.refresh_due(event_id, status, float(start), now):
            if fetch_if_missing and live_pages.cache_age(event_id) is None:
                page = live_pages.refresh(event_id, hltv_event_id, str(detail.get("hltv_event_url") or ""), to_tid)
            else:
                live_pages.refresh_in_background(event_id, hltv_event_id, str(detail.get("hltv_event_url") or ""), to_tid)
        page = page or live_pages.cached(event_id)
    known_ids = {int(m["match_id"]) for m in matches if m.get("match_id")}
    for pm in page:
        if pm["status"] == "finished" and int(pm["match_id"]) not in known_ids:
            matches.append({
                "url": pm["url"], "match_id": pm["match_id"], "date": _iso(pm["start_at"]) if pm.get("start_at") else _iso(now),
                "a": pm["a"], "b": pm["b"], "score_a": pm.get("score_a"), "score_b": pm.get("score_b"), "winner": pm.get("winner"),
                "maps": int(pm.get("score_a") or 0) + int(pm.get("score_b") or 0),
            })
    matches.sort(key=lambda m: (str(m.get("date") or ""), int(m.get("match_id") or 0)))
    pending = [pm for pm in page if pm["status"] in ("live", "upcoming")]
    live_now = [pm for pm in pending if pm["status"] == "live"]
    upcoming = sorted((pm for pm in pending if pm["status"] == "upcoming"), key=lambda pm: (pm.get("start_at") or 0.0))
    entry: Dict[str, Any] = {
        "event_id": event_id,
        "name": detail.get("name") or ev.get("name") or _event_name(conn, event_id) or f"Event {event_id}",
        "kind": str(detected.get("kind") or ""),
        "label": str(detected.get("label") or ""),
        "group_format": fmt,
        "status": status,
        "start_at": float(start),
        "end_at": end,
        "team_count": len(rows),
        "teams": {str(tid): {"name": r.get("name"), "hltv_team_id": r.get("hltv_team_id")} for tid, r in rows.items()},
        "matches": matches,
        "live_matches": live_now,
        "upcoming_matches": upcoming,
        "live_count": len(live_now),
        "pages_read_at": (now - live_pages.cache_age(event_id)) if live_pages.cache_age(event_id) is not None else None,
    }
    stage_by_url: Dict[str, str] = {}
    if fmt == "swiss":
        sw = results.get("swiss") or {}
        entry["swiss"] = _swiss_state(matches, [int(t) for t in (results.get("groups") or [[]])[0]], str(sw.get("bo3_mode") or "elim_qual"), pending)
        entry["played"], entry["total"] = entry["swiss"]["played"], entry["swiss"]["total"]
        stage_by_url = entry["swiss"].pop("stage_by_url", {})
    elif fmt == "bracket":
        entry["bracket"] = results.get("bracket") or {}
        st = _bracket_state(matches, entry["bracket"])
        entry["bracket_picks"] = st["picks"]
        entry["played"], entry["total"] = st["played"], st["total"]
        stage_by_url = st.get("stage_by_url") or {}
    else:
        entry["groups"] = _groups_state(matches, [[int(t) for t in g] for g in (results.get("groups") or [])])
        entry["played"] = sum(g["played"] for g in entry["groups"])
        entry["total"] = None
        for g in entry["groups"]:
            stage_by_url.update(g.pop("stage_by_url", {}))
    for m in matches:
        m["stage"] = stage_by_url.get(str(m.get("url")))
    last = matches[-1] if matches else None
    entry["last_match"] = (
        {"a": rows.get(last["a"], {}).get("name"), "b": rows.get(last["b"], {}).get("name"), "score_a": last["score_a"], "score_b": last["score_b"], "date": last["date"]}
        if last else None
    )
    return entry


def summary(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: entry.get(k) for k in SUMMARY_KEYS}


def live_events(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every live and upcoming event plus the PREVIOUS_LIMIT most recently
    finished ones, full entries, live first."""
    from backend.data.event_db import list_events

    now = float(now or time.time())
    out = []
    conn = connect()
    try:
        for ev in list_events():
            entry = _entry(conn, ev, now)
            if entry:
                out.append(entry)
    finally:
        conn.close()
    order = {"live": 0, "upcoming": 1, "finished": 2}
    live_and_next = sorted((e for e in out if e["status"] != "finished"), key=lambda e: (order[e["status"]], e["start_at"]))
    previous = sorted((e for e in out if e["status"] == "finished"), key=lambda e: -e["end_at"])[:PREVIOUS_LIMIT]
    return live_and_next + previous


def live_event(event_id: int, now: Optional[float] = None, fetch_if_missing: bool = True) -> Optional[Dict[str, Any]]:
    """One event's full state, or None when it has no valuation. With
    fetch_if_missing an event whose HLTV pages were never read gets them
    read now (seconds in the browser); without, the state comes from the
    database alone and the pages are read in the background."""
    now = float(now or time.time())
    conn = connect()
    try:
        return _entry(conn, {"event_id": int(event_id)}, now, fetch_if_missing=fetch_if_missing)
    finally:
        conn.close()
