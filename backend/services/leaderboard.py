"""The event leaderboard: every player in a live event scored the way HLTV
scores them, from the matches played so far.

To start, two of HLTV's four components (the user's choice, 2026-10-27):
  rating  (series rating - 1) x 50 per match, halved in a best-of-one,
          rounded to a point (checked against HLTV's own figures for the
          user's players: donk 0.84 -> -8, SunPayus 1.47 -> 23, arT 0.68
          -> -16); the stored rating has two decimals, so a match can be a
          point off when HLTV's unrounded rating sits on a half;
  win     +6 for a match won, -3 for a match lost.
Role and booster points are not scored here yet.

Inputs: the event's matches as the Live page knows them (nightly results
plus today's finished matches from HLTV's event pages, services/live_state)
and each match's stored scoreboard (hltv_results.player_stats_json, the
series rating per player). A finished match whose scoreboard is not stored
yet — today's, before the nightly import — is read from its match page in
the background (one page per match, low priority on the shared browser)
and stored like the import would, so the board fills in minutes.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.data.db import connect

logger = logging.getLogger(__name__)

RATING_SCALE = 50.0  # (rating - 1) x 50
WIN_POINTS = 6.0
LOSS_POINTS = -3.0
BO1_RATING_FACTOR = 0.5

_LOCK = threading.Lock()
_INFLIGHT: Set[int] = set()  # events whose missing scoreboards are being read
_FAILED: Dict[int, float] = {}  # match_id -> when its page read last failed (retry after a while)
_RETRY_AFTER = 900.0


def rating_points(rating: float, bo1: bool) -> int:
    pts = round((float(rating) - 1.0) * RATING_SCALE * (BO1_RATING_FACTOR if bo1 else 1.0), 6)  # 1.45 - 1 is 0.4499… in floating point
    return int(math.floor(pts + 0.5)) if pts >= 0 else -int(math.floor(-pts + 0.5))


def _scoreboards(match_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """match_id -> {winner, team1, team2, score1, score2, players: [...]} for
    the matches whose series scoreboard is stored."""
    if not match_ids:
        return {}
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT match_id, team1, team2, score1, score2, winner, player_stats_json FROM hltv_results "
            f"WHERE match_id IN ({','.join('?' * len(match_ids))}) AND player_stats_json IS NOT NULL",
            [int(m) for m in match_ids],
        ).fetchall()
    finally:
        conn.close()
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        try:
            players = json.loads(r["player_stats_json"] or "[]") or []
        except Exception:  # noqa: BLE001
            players = []
        if not players:
            continue
        out[int(r["match_id"])] = {
            "team1": r["team1"], "team2": r["team2"], "score1": r["score1"], "score2": r["score2"], "winner": r["winner"],
            "players": players,
        }
    return out


def _read_missing(event_id: int, matches: List[Dict[str, Any]], team_names: Dict[int, str], event_name: str) -> None:
    """Read and store the scoreboards of finished matches that have none,
    one match page each (background thread, one per event at a time)."""
    from backend.data.event_db import upsert_hltv_results
    from backend.services.hltv_rankings import get_hltv_match_details

    try:
        for m in matches:
            mid = int(m["match_id"])
            url = str(m.get("url") or "")
            if "/matches/" not in url:
                continue
            with _LOCK:
                if time.time() - _FAILED.get(mid, 0.0) < _RETRY_AFTER:
                    continue
            try:
                details = get_hltv_match_details(url)
            except Exception:  # noqa: BLE001
                logger.warning("Leaderboard: could not read the match page for %s", mid, exc_info=True)
                with _LOCK:
                    _FAILED[mid] = time.time()
                continue
            players = details.get("player_stats") or []
            if not players:
                with _LOCK:
                    _FAILED[mid] = time.time()
                continue
            a, b = team_names.get(int(m["a"]), ""), team_names.get(int(m["b"]), "")
            winner = team_names.get(int(m["winner"])) if m.get("winner") is not None else None
            upsert_hltv_results([{
                "match_url": url, "match_id": mid, "match_date": m.get("date"), "team1": a, "team2": b,
                "score1": m.get("score_a"), "score2": m.get("score_b"), "winner": winner, "event": event_name,
                "maps_json": json.dumps(details.get("maps") or []), "veto_json": json.dumps(details.get("veto") or []),
                "player_stats_json": json.dumps(players), "map_player_stats_json": json.dumps(details.get("map_player_stats") or {}) if details.get("map_player_stats") else None,
            }])
            logger.info("Leaderboard: stored the scoreboard of match %s for event %s", mid, event_id)
    finally:
        with _LOCK:
            _INFLIGHT.discard(int(event_id))


def _same_team(a: Any, b: Any) -> bool:
    from backend.data import team_aliases

    return bool(a) and bool(b) and team_aliases.key(str(a)) == team_aliases.key(str(b))


def _expectations(event_id: int):
    """A function (player, team, opponents in played order) -> the expected
    rating + win points of those matches, from the baked run's stage stats:
    each match takes the next stage whose figures list that opponent, the
    figure divided by the chance of that pairing (it is weighted by it), so
    it is the expectation given the match was played. None without a run."""
    from backend.routes.groups import _GROUPS_STATE

    ss = (((_GROUPS_STATE.load(key=int(event_id)) or {}).get("results")) or {}).get("stage_stats") or {}
    order = [str(s.get("key")) for s in (ss.get("stages") or []) if s.get("key") and s.get("key") != "padding"]
    sp, st = ss.get("players") or {}, ss.get("teams") or {}

    def value(pid: int, tid: int, stage: str, opp: Optional[int]) -> Optional[float]:
        ps = ((sp.get(str(pid)) or {}).get("stages") or {}).get(stage)
        if not ps:
            return None
        rnd = ((st.get(str(tid)) or {}).get("rounds") or {}).get(stage) or {}
        po = (ps.get("opponents") or {}).get(str(opp)) if opp is not None else None
        if po:
            play = float(((rnd.get("opponents") or {}).get(str(opp)) or {}).get("play") or 0.0)
            if play > 1e-6:
                return (float(po.get("rating") or 0.0) + float(po.get("win") or 0.0)) / play
        play = float(rnd.get("play") or 0.0)
        v = float(ps.get("rating") or 0.0) + float(ps.get("win") or 0.0)
        return v / play if play > 0.05 else v

    def expect(pid: int, tid: int, opponents: List[int]) -> Optional[float]:
        stages = (sp.get(str(pid)) or {}).get("stages") or {}
        if not order or not stages:
            return None
        total, at = 0.0, 0
        for opp in opponents:
            pick = next((i for i in range(at, len(order)) if str(opp) in ((stages.get(order[i]) or {}).get("opponents") or {})), None)
            if pick is None:
                pick = at if at < len(order) else None
            if pick is None:
                return None
            v = value(pid, tid, order[pick], opp)
            if v is None:
                return None
            total += v
            at = pick + 1
        return total

    return expect


def event_leaderboard(event_id: int, fetch_missing: bool = True) -> Dict[str, Any]:
    """{players: [{player_id, name, team_id, team, hltv_team_id, price, matches,
    rating_points, win_points, total}] best first, matches_scored,
    matches_pending, read_at}."""
    from backend.data.event_db import get_event_detail
    from backend.data.player_db import get_player
    from backend.services import event_rosters, live_state

    eid = int(event_id)
    entry = live_state.live_event(eid, fetch_if_missing=False) or {}
    teams = {int(t): v for t, v in (entry.get("teams") or {}).items()}
    team_names = {t: str(v.get("name") or "") for t, v in teams.items()}
    finished = [m for m in (entry.get("matches") or []) if m.get("winner") is not None and m.get("match_id")]
    boards = _scoreboards([int(m["match_id"]) for m in finished])
    missing = [m for m in finished if int(m["match_id"]) not in boards]

    # every player the event lists, with 0 until he plays
    detail = get_event_detail(eid) or {}
    price_of: Dict[int, Any] = {}
    for t in detail.get("teams") or []:
        for pid in t.get("player_ids") or []:
            price_of[int(pid)] = (t.get("prices") or {}).get(str(pid)) if isinstance(t.get("prices"), dict) else None
    rows: Dict[int, Dict[str, Any]] = {}
    for tid, pids in (event_rosters.rosters(eid) or {}).items():
        for pid in pids:
            p = get_player(int(pid)) or {}
            rows[int(pid)] = {
                "player_id": int(pid), "name": str(p.get("name") or pid), "team_id": int(tid), "team": team_names.get(int(tid), ""),
                "hltv_team_id": (teams.get(int(tid)) or {}).get("hltv_team_id"), "price": p.get("price") if p.get("price") is not None else price_of.get(int(pid)),
                "matches": 0, "wins": 0, "rating_points": 0, "win_points": 0, "total": 0, "rating_sum": 0.0,
                "played": [],
            }

    for m in finished:
        board = boards.get(int(m["match_id"]))
        if not board:
            continue
        bo1 = int(m.get("maps") or ((m.get("score_a") or 0) + (m.get("score_b") or 0)) or 0) == 1
        for ps in board["players"]:
            pid = ps.get("player_id")
            rating = ps.get("rating")
            if pid is None or rating is None:
                continue
            row = rows.get(int(pid))
            if row is None:
                continue  # a stand-in the fantasy game does not list
            won = _same_team(ps.get("team"), board.get("winner"))
            rp = rating_points(float(rating), bo1)
            wp = WIN_POINTS if won else LOSS_POINTS
            row["matches"] += 1
            row["wins"] += 1 if won else 0
            row["rating_points"] += rp
            row["win_points"] += int(wp)
            row["total"] += rp + int(wp)
            row["rating_sum"] += float(rating)
            mine = int(row["team_id"])
            opp = int(m["b"]) if int(m.get("a") or 0) == mine else int(m.get("a") or 0)
            row["played"].append({"date": str(m.get("date") or ""), "opponent": opp, "points": rp + int(wp)})

    # The field (services/fantasy_field), when it has been read: HLTV's own
    # rating and team points per player replace the computed ones, plus how
    # many teams own him and the team where he scored most — with its role
    # and the boosters it used on him, match by match.
    from backend.services import fantasy_field

    field = fantasy_field.player_summary(eid) if fantasy_field.status(eid).get("teams_stored") else None
    field_players = (field or {}).get("players") or {}
    facts = (fantasy_field.match_facts(eid).get("players") or {}) if field else {}
    expect = _expectations(eid)
    players = []
    for row in rows.values():
        played = sorted(row.pop("played"), key=lambda x: x["date"])  # stable: the list order within a day
        exp = expect(int(row["player_id"]), int(row["team_id"]), [x["opponent"] for x in played]) if played else None
        row["expected"] = round(exp, 1) if exp is not None else None
        row["actual"] = sum(int(x["points"]) for x in played)
        row["vs_expected"] = round(row["actual"] - exp, 1) if exp is not None else None
        row["rating_avg"] = round(row["rating_sum"] / row["matches"], 2) if row["matches"] else None
        row.pop("rating_sum", None)
        f = field_players.get(int(row["player_id"]))
        row["owned"] = int(f["owned"]) if f else 0
        row["best"] = (f or {}).get("best")
        fx = facts.get(int(row["player_id"]))
        if f and f.get("matches") and fx:
            # HLTV's own figures, padding rounds included (the leaderboard counts them)
            row["computed_rating_points"], row["computed_win_points"] = row["rating_points"], row["win_points"]
            row["rating_points"], row["win_points"] = int(fx["rating_points"]), int(fx["win_points"])
            row["matches"] = max(int(row["matches"]), len(fx["matches"]))
            row["wins"] = sum(1 for m in fx["matches"] if str(m.get("result") or "").upper() == "WON")
            row["padding"] = int(fx["padding"]["count"])
            row["max"] = fx["max"]
            row["breakdown"] = {k: fx[k] for k in ("matches", "padding", "roles", "boosters")}
            row["source"] = "hltv"
        else:
            row["padding"], row["max"], row["breakdown"] = 0, None, None
            row["source"] = "computed"
        base = int(row["rating_points"]) + int(row["win_points"])
        # with the field: the theoretical maximum (best role + a booster every match one triggered in)
        row["total"] = int(row["max"]["total"]) if row.get("max") else base
        row["base_total"] = base
        players.append(row)
    players.sort(key=lambda r: (-r["total"], -r["rating_points"], r["name"].lower()))
    for i, row in enumerate(players, start=1):
        row["rank"] = i

    if missing and fetch_missing:
        with _LOCK:
            start = eid not in _INFLIGHT
            if start:
                _INFLIGHT.add(eid)
        if start:
            threading.Thread(
                target=_read_missing, args=(eid, missing, team_names, str(entry.get("name") or detail.get("name") or "")),
                name=f"leaderboard-{eid}", daemon=True,
            ).start()
    return {
        "event_id": eid,
        "players": players,
        "matches_scored": len(finished) - len(missing),
        "matches_pending": len(missing),
        "components": ["rating", "win"] + (["role", "booster", "padding", "max"] if field else []),
        "field": {k: (field or {}).get(k) for k in ("teams", "league_size", "read_at", "running")} if field else None,
        "read_at": time.time(),
    }
