"""Booster advice for the user's HLTV fantasy team in an event, and the
hands-off mode that sets the advised boosters on HLTV for the rest of it.

HLTV's rule (its own front end): every booster can be used once per team;
a player carries one booster per match, set before the match starts. The
advice is the roster-wide assignment of the boosters still free to the
players' remaining matches that maximises expected booster points
(5 × the player's trigger rate × the chance his team plays the match),
with the solver the Top 5 use; the suggestion for now is each player's
booster on his coming match.

Read as the user from the fantasy front end's own JSON feeds:
  /fantasy/{eid}/league/{lid}/overview/{tid}/json — the players, each one's
    coming match (matchId, opponent, startTime, isLive, assignedBooster,
    canAssignBooster), the booster options with their state (FREE when
    still unused) and the player tracker;
  /fantasy/{eid}/league/{lid}/details/{tid}/json — rounds and used boosters.
Set with POST /fantasy/{eid}/teams/boost {playerId, teamId, leagueId,
matchId, boostId}, the same call the page makes (boostId null removes).
HLTV's booster ids are not the engine's 0-17: they are matched by name,
with HLTV's id order as the fallback.

Rates: HLTV's own trigger-rates feed for the team
(/overview/{tid}/draft/{draftId}/triggerrates/json, a rate per booster per
player, refreshed by HLTV as maps are played) when it answers, else the
rates stored with the player at import. HLTV's rate is the chance over a
BEST-OF-THREE (the user's word, 2026-10-27); a best-of-three is expected
to run 2.4 maps, so the per-map miss chance is (1 - R)^(1/2.4) and a
series of m expected maps triggers with 1 - (1 - R)^(m/2.4): 1 map for a
best-of-one, 3.9 for a best-of-five, R itself for a best-of-three. The
coming match's series length comes from the Swiss record
(swiss_engine.match_bo).

Later rounds (Swiss): the chance a player plays each later round, and the
series length he plays it in, come from branching his team's REAL record
on the coming match's result (the win model's odds against the drawn
opponent, else against the field) and then on the field for the rounds
after; a player whose next pairing HLTV has not drawn yet keeps a column
for that round (played for sure, from the live record) so his booster is
reserved rather than handed to a teammate. Brackets still use the bake's
chance of reaching each later round.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from backend.services.swiss_booster_assignment import BOOSTER_NAMES, BOOSTER_POINT_VALUE, _max_weight_assignment, parse_booster_rates

logger = logging.getLogger(__name__)

# HLTV's booster ids in the engine's 0..17 order (seen 2026-10-27).
HLTV_ID_BY_INDEX = [2, 3, 5, 8, 9, 13, 16, 18, 19, 20, 21, 22, 23, 26, 27, 28, 29, 33]
FEED_MAX_AGE = 120.0
AUTO_TICK_SECONDS = 300.0
EXPECTED_MAPS = {1: 1.0, 2: 2.0, 3: 2.4, 5: 3.9}


def match_rate(rate_bo3: float, bo: int) -> float:
    """The chance a booster triggers in a series of this length, from
    HLTV's best-of-three rate."""
    maps = EXPECTED_MAPS.get(int(bo or 3), 2.4)
    r = min(1.0, max(0.0, float(rate_bo3)))
    if r >= 1.0:
        return 1.0
    return 1.0 - (1.0 - r) ** (maps / EXPECTED_MAPS[3])


def swiss_condition(wins: int, losses: int, rounds_ahead: int, target: int = 3) -> str:
    """On what results a Swiss team plays the match `rounds_ahead` matches
    after the coming one (1 = the round after next): '' when it always
    does, else words like 'if they lose the next one' or 'if they split
    the next two'; '' too when it is not a short sentence."""
    from itertools import product

    n = int(rounds_ahead)
    if n <= 0:
        return ""
    alive = []
    for seq in product(("W", "L"), repeat=n):
        w, l, ok = int(wins), int(losses), True
        for res in seq:
            if w >= target or l >= target:
                ok = False
                break
            w, l = (w + 1, l) if res == "W" else (w, l + 1)
        if ok and w < target and l < target:
            alive.append(seq)
    total = 2 ** n
    if not alive:
        return "never"
    if len(alive) == total:
        return ""
    word = {1: "one", 2: "two", 3: "three"}.get(n, str(n))
    if len(alive) == 1:
        seq = alive[0]
        if all(r == "W" for r in seq):
            return f"if they win the next {word}"
        if all(r == "L" for r in seq):
            return f"if they lose the next {word}"
        return ""
    if len(alive) == total - 1:
        missing = next(seq for seq in product(("W", "L"), repeat=n) if seq not in alive)
        if all(r == "W" for r in missing):
            return f"unless they win the next {word}"
        if all(r == "L" for r in missing):
            return f"unless they lose the next {word}"
        return ""
    if n == 2 and set(alive) == {("W", "L"), ("L", "W")}:
        return "if they split the next two"
    return ""

_POST_JS = """
const [url, body, done] = arguments;
fetch(url, {method: 'POST', credentials: 'same-origin',
            headers: {'Accept': 'application/json', 'Content-Type': 'application/json; charset=UTF-8'},
            body: body})
  .then((r) => r.text().then((t) => done({status: r.status, text: t.slice(0, 4000)})))
  .catch((e) => done({status: 0, text: String(e)}));
"""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", str(text or "").lower().replace("mister/miss", "mister"))


def _json_from_page(page: str) -> Optional[Dict[str, Any]]:
    """The JSON a browser shows for a JSON URL (wrapped in <pre>)."""
    m = re.search(r"<pre[^>]*>(.*?)</pre>", page or "", flags=re.S)
    raw = htmllib.unescape(m.group(1)) if m else (page or "")
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, dict) else None


def _index_for(booster: Dict[str, Any]) -> Optional[int]:
    """The engine's booster index for one of HLTV's booster objects."""
    want = _norm((booster or {}).get("name"))
    for idx, name in BOOSTER_NAMES.items():
        if _norm(name) == want:
            return int(idx)
    try:
        return HLTV_ID_BY_INDEX.index(int((booster or {}).get("id")))
    except (ValueError, TypeError):
        return None


def _hltv_id(index: int, options: List[Dict[str, Any]]) -> int:
    for o in options:
        if o.get("index") == int(index) and o.get("hltv_id") is not None:
            return int(o["hltv_id"])
    return int(HLTV_ID_BY_INDEX[int(index)])


def _team_of(user_key: str, event_id: int) -> Dict[str, Any]:
    from backend.data import my_team_cache
    from backend.services import my_team

    mine = (my_team_cache.get(user_key, int(event_id)) or {}).get("team")
    if mine:
        return mine
    teams = my_team.my_teams_for([int(event_id)], user_key, 3600.0).get(int(event_id)) or []
    if not teams:
        raise RuntimeError("No team of yours in this event")
    my_team_cache.save_team(user_key, int(event_id), teams[0])
    return teams[0]


def _feed_urls(event_id: int, mine: Dict[str, Any]) -> Dict[str, str]:
    base = f"https://www.hltv.org/fantasy/{int(event_id)}/league/{mine['league_id']}"
    return {"overview": f"{base}/overview/{mine['team_id']}/json", "details": f"{base}/details/{mine['team_id']}/json"}


def feeds(event_id: int, user_key: str, max_age: float = FEED_MAX_AGE) -> Dict[str, Any]:
    from backend.services import my_team

    mine = _team_of(user_key, event_id)
    urls = _feed_urls(event_id, mine)
    pages = my_team._fetch_many([urls["overview"], urls["details"]], max_age, user_key)
    overview = _json_from_page(pages.get(urls["overview"], ""))
    rates = None
    draft_id = ((overview or {}).get("draftId") or {}).get("id")
    if draft_id is not None:
        rates_url = f"https://www.hltv.org/fantasy/{int(event_id)}/league/{mine['league_id']}/overview/{mine['team_id']}/draft/{int(draft_id)}/triggerrates/json"
        try:
            rates = _json_from_page(my_team._fetch_many([rates_url], max(max_age, 600.0), user_key).get(rates_url, ""))
        except Exception:  # noqa: BLE001 - the stored rates stand in
            logger.warning("Could not read HLTV's trigger rates", exc_info=True)
    return {"team": mine, "overview": overview, "details": _json_from_page(pages.get(urls["details"], "")), "trigger_rates": rates}


def drop_feeds(event_id: int, user_key: str) -> None:
    """Forget the cached feeds and reports so the next read is fresh."""
    from backend.services import my_team

    try:
        mine = _team_of(user_key, event_id)
        urls = _feed_urls(event_id, mine)
        base = f"https://www.hltv.org/fantasy/{int(event_id)}/league/{mine['league_id']}/team/{mine['team_id']}"
        with my_team._LOCK:
            for url in (urls["overview"], urls["details"], base, base + "/details"):
                my_team._CACHE.pop((user_key, url), None)
            for k in [k for k in my_team._RESULTS if k.split("|")[1:3] == [user_key, str(int(event_id))]]:
                my_team._RESULTS.pop(k, None)
    except Exception:  # noqa: BLE001
        logger.warning("Could not drop the cached feeds", exc_info=True)


def _booster_obj(b: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not b:
        return None
    idx = _index_for(b)
    return {"index": idx, "hltv_id": b.get("id"), "name": BOOSTER_NAMES.get(idx, b.get("name")) if idx is not None else b.get("name")}


def _swiss_odds(event_id: int, results: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What the branch model needs: the bake's team order and win matrices
    per series type (a missing one derived from the other through the
    model's best-of formula), and from the live state each team's record,
    the pairings HLTV has made but not finished, and the team names."""
    from backend.data import team_aliases
    from backend.services import leader_role, live_state

    order = [int(t) for t in ((results.get("groups") or [[]])[0] or [])]
    wp = {str(k): v for k, v in (((results.get("swiss") or {}).get("win_probs")) or {}).items() if v}
    if not order or not wp:
        return None
    if "bo3" in wp and "bo1" not in wp:
        wp["bo1"] = [[leader_role.map_probability(float(p), 2) if i != j else 0.5 for j, p in enumerate(row)] for i, row in enumerate(wp["bo3"])]
    if "bo1" in wp and "bo3" not in wp:
        wp["bo3"] = [[leader_role.series_probability(float(p), 2) if i != j else 0.5 for j, p in enumerate(row)] for i, row in enumerate(wp["bo1"])]
    try:
        entry = live_state.live_event(int(event_id)) or {}
    except Exception:  # noqa: BLE001 - the advice still works from the fantasy feeds alone
        logger.warning("Live state unavailable for event %s", event_id, exc_info=True)
        entry = {}
    sw = entry.get("swiss") or {}
    records: Dict[int, tuple] = {}
    for t, r in (sw.get("records") or {}).items():
        try:
            records[int(t)] = (int(r[0]), int(r[1]))
        except (TypeError, ValueError, IndexError):
            continue
    pending_opp: Dict[int, int] = {}
    for rd in sw.get("rounds") or []:
        for m in rd.get("matches") or []:
            if m.get("winner") is None and m.get("a") is not None and m.get("b") is not None:
                pending_opp[int(m["a"])] = int(m["b"])
                pending_opp[int(m["b"])] = int(m["a"])
    names = {team_aliases.key(str(v.get("name") or "")): int(t) for t, v in (entry.get("teams") or {}).items() if v.get("name")}
    return {"order": order, "idx": {t: i for i, t in enumerate(order)}, "wp": wp, "records": records, "pending_opp": pending_opp, "name_to_tid": names}


def _p_win(odds: Dict[str, Any], a: int, b: int, bo: int) -> Optional[float]:
    """P(a beats b) in a series of this length from the bake's matrices."""
    m = odds["wp"].get(f"bo{int(bo)}")
    ia, ib = odds["idx"].get(int(a)), odds["idx"].get(int(b))
    if m is None or ia is None or ib is None or ia == ib:
        return None
    return float(m[ia][ib])


def _p_field(odds: Dict[str, Any], a: int, bo: int, pool: Optional[List[int]] = None) -> float:
    """P(a wins a series of this length) against the teams it may meet: the
    live teams on the same record when given, else the whole field."""
    cands = [int(b) for b in (pool or []) if int(b) != int(a) and int(b) in odds["idx"]] or [b for b in odds["order"] if b != int(a)]
    ps = [p for p in (_p_win(odds, a, b, bo) for b in cands) if p is not None]
    return float(sum(ps) / len(ps)) if ps else 0.5


def _branch_slots(start: Dict[tuple, float], p_win_at, bo3_mode: str, n_slots: int) -> List[List[tuple]]:
    """Per later slot, the states (prob, wins, losses, bo) a Swiss team can
    still be playing in, from a distribution over records at the first
    slot; each slot's match is then won with p_win_at(wins, losses, bo,
    first) and the record moves on. Stops once no state is alive."""
    from collections import defaultdict

    from backend.services import swiss_engine

    out: List[List[tuple]] = []
    dist = dict(start)
    for j in range(max(0, int(n_slots))):
        alive = {(w, l): p for (w, l), p in dist.items() if w < swiss_engine.TARGET and l < swiss_engine.TARGET and p > 1e-9}
        if not alive:
            break
        out.append([(float(p), int(w), int(l), int(swiss_engine.match_bo(w, l, bo3_mode))) for (w, l), p in alive.items()])
        nxt: Dict[tuple, float] = defaultdict(float)
        for (w, l), p in alive.items():
            bo = int(swiss_engine.match_bo(w, l, bo3_mode))
            pw = min(1.0, max(0.0, float(p_win_at(w, l, bo, j == 0))))
            nxt[(w + 1, l)] += p * pw
            nxt[(w, l + 1)] += p * (1.0 - pw)
        dist = dict(nxt)
    return out


def suggestions(event_id: int, user_key: str, max_age: float = FEED_MAX_AGE) -> Dict[str, Any]:
    """The advice: per player his coming match, the booster on it, the
    suggested booster (with its rate and expected points) and the later
    plan; plus the pool of free boosters."""
    from backend.data import team_aliases
    from backend.data.player_db import get_player
    from backend.routes.groups import _GROUPS_STATE
    from backend.services import event_rosters, swiss_engine

    eid = int(event_id)
    f = feeds(eid, user_key, max_age)
    ov = f.get("overview") or {}
    if not ov.get("teamOverviewPlayers"):
        raise RuntimeError("HLTV gave no team data")
    options = []
    for o in ov.get("assignableBoostOptions") or []:
        b = o.get("booster") or {}
        idx = _index_for(b)
        options.append({"index": idx, "hltv_id": b.get("id"), "name": BOOSTER_NAMES.get(idx, b.get("name")) if idx is not None else b.get("name"), "state": o.get("state"), "free": str(o.get("state") or "").upper() == "FREE"})
    free = {o["index"] for o in options if o["free"] and o["index"] is not None}

    results = ((_GROUPS_STATE.load(key=eid) or {}).get("results")) or {}
    stage_stats = results.get("stage_stats") or {}
    stage_order = [str(s.get("key")) for s in (stage_stats.get("stages") or []) if s.get("key") and s.get("key") != "padding"]
    stage_teams = stage_stats.get("teams") or {}
    team_of_player: Dict[int, int] = {}
    for tid, pids in (event_rosters.rosters(eid) or {}).items():
        for pid in pids:
            team_of_player[int(pid)] = int(tid)
    for tid, tv in (results.get("teams") or {}).items():
        for pid in (tv.get("players") or {}):
            team_of_player.setdefault(int(pid), int(tid))

    finished_by_pid: Dict[int, int] = {}
    record_by_pid: Dict[int, List[int]] = {}
    for item in ov.get("playerTrackerItems") or []:
        if str(item.get("type") or "").endswith("FinishedMatch"):
            pid = ((item.get("player") or {}).get("fantasyPlayerId") or {}).get("playerId")
            if pid is not None:
                finished_by_pid[int(pid)] = finished_by_pid.get(int(pid), 0) + 1
                rec = record_by_pid.setdefault(int(pid), [0, 0])
                if str(((item.get("match") or {}).get("result") or "")).upper() == "WON":
                    rec[0] += 1
                else:
                    rec[1] += 1
    # HLTV's live trigger rates per player (per map), keyed by its booster id
    live_rates: Dict[int, Dict[int, float]] = {}
    for row_ in ((f.get("trigger_rates") or {}).get("playerTriggerRates") or []):
        pid_ = (row_.get("playerId") or {}).get("playerId")
        if pid_ is None:
            continue
        by_idx: Dict[int, float] = {}
        for hid, v in (row_.get("boosterIdToTriggerRate") or {}).items():
            try:
                idx = HLTV_ID_BY_INDEX.index(int(hid))
            except ValueError:
                continue
            val = v.get("value") if isinstance(v, dict) else v
            if val is not None:
                by_idx[idx] = float(val)
        if by_idx:
            live_rates[int(pid_)] = by_idx
    swiss = results.get("swiss") or {}
    bo3_mode = str(swiss.get("bo3_mode") or "all")
    is_swiss = results.get("group_format") == "swiss"
    odds = _swiss_odds(eid, results) if is_swiss else None

    def series_len(wins: int, losses: int) -> int:
        """The coming match's series length from the Swiss record (brackets: Bo3)."""
        from backend.services import swiss_engine

        if not is_swiss:
            return 3
        return int(swiss_engine.match_bo(int(wins), int(losses), bo3_mode))

    def play_prob(tid: Optional[int], stage_key: str) -> float:
        rounds = ((stage_teams.get(str(tid)) or {}).get("rounds") or {}) if tid is not None else {}
        return float((rounds.get(stage_key) or {}).get("play") or 0.0)

    players: List[Dict[str, Any]] = []
    columns: List[Dict[str, Any]] = []  # (player, slot) columns for the solver
    pool = set(free)
    for p in ov.get("teamOverviewPlayers") or []:
        pl = p.get("player") or {}
        pid = (pl.get("fantasyPlayerId") or {}).get("playerId")
        pid = int(pid) if pid is not None else None
        name = str(pl.get("name") or "")
        tid = team_of_player.get(pid) if pid is not None else None
        row = get_player(pid) if pid is not None else None
        rates = live_rates.get(pid) or (parse_booster_rates((row or {}).get("boosters_json")) if row else {})
        um = ((p.get("upcomingMatches") or [None])[0]) or None
        assigned = _booster_obj(um.get("assignedBooster")) if um else None
        match = None
        if um:
            match = {
                "match_id": ((um.get("matchId") or {}).get("id")),
                "opponent": ((um.get("opponent") or {}).get("name")),
                "start_at": (float(um["startTime"]) / 1000.0) if um.get("startTime") else None,
                "live": bool(um.get("isLive")),
                "assignable": bool(um.get("canAssignBooster")) and not bool(um.get("isLive")),
                "url": um.get("link"),
            }
        played = finished_by_pid.get(pid, 0) if pid is not None else 0
        wins, losses = record_by_pid.get(pid, [0, 0]) if pid is not None else (0, 0)
        if not match and odds is not None and tid is not None and tid in odds["records"]:
            # between rounds HLTV's own pages know today's result before the
            # fantasy tracker does: the record with more games is the newer one
            lw, ll = odds["records"][tid]
            if lw + ll > wins + losses:
                wins, losses, played = int(lw), int(ll), int(lw + ll)
        if match:
            match["bo"] = series_len(wins, losses)
        entry = {
            "player_id": pid, "name": name, "team_id": tid,
            "match": match, "assigned": assigned, "played": played, "record": [wins, losses],
            "rates": {int(k): float(v) for k, v in rates.items()},
            "rates_source": "hltv" if pid in live_rates else "stored",
            "suggestion": None, "plan": [],
        }
        players.append(entry)
        # a booster already on the coming match may still be changed: it is in the pool
        if assigned and assigned.get("index") is not None and match and match["assignable"]:
            pool.add(int(assigned["index"]))
        if not rates:
            continue
        here = stage_order[played] if played < len(stage_order) else None
        if here is None:
            continue
        alive = (wins < swiss_engine.TARGET and losses < swiss_engine.TARGET) if is_swiss else True
        # the coming match (slot 0, played for sure) when it is drawn; when it
        # is not, and the team is still in, the next round itself is the
        # first column ("next": reserved, nothing to set yet)
        if match:
            if match["assignable"]:
                columns.append({"player_id": pid, "kind": "coming", "slot": 0, "stage": here, "branches": [(1.0, int(match["bo"]))]})
            base = played + 1
        elif is_swiss and alive and tid is not None and odds is not None:
            entry["next_round"] = {"stage": here, "bo": series_len(wins, losses)}
            base = played
        else:
            continue
        if not is_swiss or tid is None or odds is None:
            # brackets: the bake's chance of reaching each later round from this one
            base_prob = play_prob(tid, here)
            for k in range(base, len(stage_order)):
                prob = play_prob(tid, stage_order[k])
                rel = (prob / base_prob) if base_prob > 0 else prob
                if rel > 0.01:
                    columns.append({"player_id": pid, "kind": "later", "slot": k - played, "stage": stage_order[k], "branches": [(min(1.0, rel), 3)]})
            continue
        # Swiss: branch the team's real record on the coming match (the model's
        # odds against the drawn opponent) and then on the field
        if match:
            opp = odds["pending_opp"].get(tid)
            if opp is None and match.get("opponent"):
                opp = odds["name_to_tid"].get(team_aliases.key(str(match["opponent"])))
            p_first = _p_win(odds, tid, opp, int(match["bo"])) if opp is not None else None
            if p_first is None:
                p_first = _p_field(odds, tid, int(match["bo"]))
            match["win_chance"] = round(float(p_first), 3)
            start = {(wins + 1, losses): float(p_first), (wins, losses + 1): 1.0 - float(p_first)}
            pool_first: Optional[List[int]] = None
        else:
            start = {(wins, losses): 1.0}
            pool_first = [t for t, r in odds["records"].items() if tuple(r) == (wins, losses) and t != tid and t not in odds["pending_opp"]]
        slots = _branch_slots(start, lambda w, l, bo, first: _p_field(odds, tid, bo, pool_first if first else None), bo3_mode, len(stage_order) - base)
        for j, states in enumerate(slots):
            k = base + j
            prob = sum(p for p, _, _, _ in states)
            if prob <= 0.01:
                continue
            kind = "next" if (not match and j == 0) else "later"
            columns.append({"player_id": pid, "kind": kind, "slot": k - played + (0 if match else 1), "stage": stage_order[k], "branches": [(float(p), int(bo)) for p, _, _, bo in states]})

    boosters = sorted(pool)
    rates_of = {p["player_id"]: p["rates"] for p in players}

    def col_value(col: Dict[str, Any], rate: float) -> float:
        """The booster's expected points in a column: the chance of each
        branch × the trigger chance in that branch's series length."""
        return sum(float(pr) * match_rate(rate, int(bo)) for pr, bo in col["branches"]) * BOOSTER_POINT_VALUE

    if boosters and columns:
        weights = [[col_value(col, float(rates_of.get(col["player_id"], {}).get(b, 0.0))) for col in columns] for b in boosters]
        for b_row, c, w in _max_weight_assignment(weights):
            if w <= 1e-9:
                continue
            col = columns[c]
            b = boosters[b_row]
            entry = next(p for p in players if p["player_id"] == col["player_id"])
            rate = float(rates_of.get(col["player_id"], {}).get(b, 0.0))
            prob = float(sum(pr for pr, _ in col["branches"]))
            value = col_value(col, rate)
            per_match = (value / BOOSTER_POINT_VALUE / prob) if prob > 0 else 0.0  # P(trigger | the match is played)
            bo = max(col["branches"], key=lambda x: x[0])[1]
            item = {
                "index": b, "hltv_id": _hltv_id(b, options), "name": BOOSTER_NAMES.get(b), "rate": round(rate, 3),
                "match_rate": round(per_match, 3), "bo": int(bo), "expected": round(per_match * BOOSTER_POINT_VALUE, 2),
                "prob": round(prob, 3), "stage": col["stage"],
                "branches": [{"prob": round(float(pr), 3), "bo": int(bo_)} for pr, bo_ in col["branches"]],
            }
            if col["kind"] == "later" and is_swiss:
                w0, l0 = entry.get("record") or [0, 0]
                item["condition"] = swiss_condition(int(w0), int(l0), int(col["slot"]) - (0 if entry.get("match") else 1))
            if col["kind"] in ("coming", "next"):
                entry["suggestion"] = item
            else:
                entry["plan"].append({**item, "slot": col["slot"]})
    for p in players:
        p["plan"].sort(key=lambda x: x["slot"])
        s, a = p.get("suggestion"), p.get("assigned")
        p["change"] = bool(s and p["match"] and p["match"]["assignable"] and (not a or a.get("index") != s["index"]))
    return {
        "event_id": eid,
        "team": f["team"],
        "players": players,
        "pool": [o for o in options],
        "free_count": len(free),
        "read_at": time.time(),
    }


def apply(event_id: int, user_key: str, assignments: List[Dict[str, Any]], trigger: str = "manual") -> List[Dict[str, Any]]:
    """Set boosters on HLTV as the user: assignments = [{player_id, match_id,
    booster_index}] (booster_index None removes). Each result carries ok and
    HLTV's answer; every attempt is logged."""
    from backend.data import my_team_settings
    from backend.services import my_team

    eid = int(event_id)
    mine = _team_of(user_key, eid)
    f = feeds(eid, user_key, FEED_MAX_AGE)
    options = []
    for o in ((f.get("overview") or {}).get("assignableBoostOptions") or []):
        b = o.get("booster") or {}
        options.append({"index": _index_for(b), "hltv_id": b.get("id")})
    names = {p["player_id"]: p for p in suggestions(eid, user_key, FEED_MAX_AGE)["players"]} if assignments else {}
    url = f"https://www.hltv.org/fantasy/{eid}/teams/boost"

    def run(driver, _username):
        out = []
        for a in assignments:
            idx = a.get("booster_index")
            # the ids are objects, as in HLTV's own feeds (FantasyPlayerId
            # {playerId}, FantasyTeamId {id}, LeagueId {id}, MatchId {id})
            body = {
                "playerId": {"playerId": int(a["player_id"])}, "teamId": {"id": int(mine["team_id"])}, "leagueId": {"id": int(mine["league_id"])},
                "matchId": {"id": int(a["match_id"])}, "boostId": (_hltv_id(int(idx), options) if idx is not None else None),
            }
            try:
                driver.set_script_timeout(30)
                res = driver.execute_async_script(_POST_JS, url, json.dumps(body)) or {}
            except Exception as exc:  # noqa: BLE001
                res = {"status": 0, "text": str(exc)}
            status = int(res.get("status") or 0)
            text = str(res.get("text") or "")
            error = None
            try:
                parsed = json.loads(text) if text else {}
                error = parsed.get("error") if isinstance(parsed, dict) else None
            except Exception:  # noqa: BLE001
                parsed = {}
            ok = 200 <= status < 300 and not error
            out.append({"player_id": int(a["player_id"]), "match_id": int(a["match_id"]), "booster_index": idx, "ok": ok, "status": status, "message": str(error or ("" if ok else text[:200]))})
        return out

    results = my_team.with_personal_session(run, cookies=my_team.cookies_for(user_key), verify=True) or []
    for r in results:
        p = names.get(r["player_id"]) or {}
        my_team_settings.add_action(
            user_key, eid, trigger=trigger, player_id=r["player_id"], player_name=p.get("name"), match_id=r["match_id"],
            opponent=((p.get("match") or {}).get("opponent")), booster_index=r["booster_index"],
            booster_name=BOOSTER_NAMES.get(int(r["booster_index"])) if r["booster_index"] is not None else None,
            ok=r["ok"], message=r["message"],
        )
    drop_feeds(eid, user_key)
    return results


def apply_suggestions(event_id: int, user_key: str, trigger: str = "manual") -> List[Dict[str, Any]]:
    """Set every suggested booster whose match can still take one and that
    differs from what is on it."""
    sug = suggestions(int(event_id), user_key, 0.0)
    todo = [
        {"player_id": p["player_id"], "match_id": p["match"]["match_id"], "booster_index": p["suggestion"]["index"]}
        for p in sug["players"]
        if p.get("change") and p.get("match") and p["match"].get("match_id") is not None
    ]
    if not todo:
        return []
    return apply(int(event_id), user_key, todo, trigger=trigger)


# ---- auto mode: a loop that applies the advice for everyone who switched it on
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def auto_tick() -> None:
    from backend.data import my_team_settings
    from backend.services import live_state

    pairs = my_team_settings.auto_pairs()
    if not pairs:
        return
    current = {int(e["event_id"]): e.get("status") for e in live_state.live_events()}
    for user_key, eid in pairs:
        if current.get(int(eid)) not in ("live", "upcoming"):
            continue
        try:
            done = apply_suggestions(int(eid), user_key, trigger="auto")
            if done:
                logger.info("Auto boosters for %s in event %s: %s", user_key, eid, [(d["player_id"], d["booster_index"], d["ok"]) for d in done])
        except Exception:  # noqa: BLE001
            logger.warning("Auto boosters failed for %s in event %s", user_key, eid, exc_info=True)


def _loop() -> None:
    while not _stop.wait(AUTO_TICK_SECONDS):
        try:
            auto_tick()
        except Exception:  # noqa: BLE001
            logger.exception("Booster auto tick failed")


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="booster-auto", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
