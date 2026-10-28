"""The HLTV fantasy season as a signed-in account sees it.

HLTV's fantasy front page is an app fed by GET /fantasy/json, which answers
(for the signed-in account) with the season: its name and end, the prize
list, the season-points table (top 1% -> 25 ... top 80% -> 1), the season
leaderboard's top rows and the rows around the user, the user's own
placement, and every game of the season the user played — placement, top
percentage, season points earned and the team / role / booster points that
made them (`previousGames`), plus the games in play (`liveGames`). The feed
pages the standings with ?offset=N (10 rows a page).

Read under each app user's own HLTV login (services/my_team), parsed into a
slim dict, stored per user (data/season_cache) and served from the store at
once; re-read in the background when older than SEASON_STALE or on refresh,
and for every connected account by the nightly batch once the night's
events are scored (scheduler tail step `season`).
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SEASON_URL = "https://www.hltv.org/fantasy/json"
SEASON_STALE = 6 * 3600.0  # the stored copy is served at once and re-read in the background past this age
NOT_USERS = ("scraper",)  # hltv_accounts rows that are no app user


def _int(v: Any) -> Optional[int]:
    try:
        s = str(v).replace(",", "").strip()
        return int(float(s)) if s not in ("", "-", "None") else None
    except (TypeError, ValueError):
        return None


def _json_from_page(page: str) -> Optional[Dict[str, Any]]:
    """The JSON a browser shows for a JSON URL (wrapped in <pre>), or the raw text."""
    m = re.search(r"<pre[^>]*>(.*?)</pre>", page or "", flags=re.S)
    raw = htmllib.unescape(m.group(1)) if m else (page or "")
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, dict) else None


def _row(p: Dict[str, Any]) -> Dict[str, Any]:
    prize = p.get("prize") if isinstance(p.get("prize"), dict) else {}
    return {
        "placement": _int(p.get("placement")), "username": str(p.get("username") or ""), "user_id": _int(p.get("userId")),
        "leagues": _int(p.get("leaguesParticipated")), "live_points": _int(p.get("livePoints")) or 0, "season_points": _int(p.get("seasonPoints")) or 0,
        "prize": str(prize.get("prize") or "") or None, "prize_tied": bool(prize.get("tiedWithOthers")), "is_user": bool(p.get("isUser")),
        "flag": p.get("userFlag") or p.get("flag"),
    }


def _game(g: Dict[str, Any], live: bool) -> Dict[str, Any]:
    parts: Dict[str, Dict[str, Any]] = {}
    for k, v in (g.get("pointsSummaryData") or {}).items():
        if isinstance(v, dict) and "points" in v:
            name = k[: -len("PointsSummary")] if k.endswith("PointsSummary") else k
            parts[name] = {"points": _int(v.get("points")), "pct": v.get("formattedPercentage"), "pct_value": v.get("pointsPercentage")}
    return {
        "event_id": _int((g.get("gameId") or {}).get("id")), "name": str(g.get("name") or ""), "team_id": _int((g.get("teamId") or {}).get("id")),
        "placement": _int(g.get("placement")), "total": _int(g.get("totalPlayers")), "top_pct": g.get("topPercentage"),
        "season_points": _int(g.get("points")) or 0, "parts": parts, "live": bool(live),
    }


def parse(data: Dict[str, Any]) -> Dict[str, Any]:
    sl = data.get("seasonLeaderboardData") or {}
    me = sl.get("currentUserSeasonPlacement")
    games = [_game(g, True) for g in sl.get("liveGames") or []] + [_game(g, False) for g in reversed(sl.get("previousGames") or [])]
    return {
        "season_name": str(data.get("seasonName") or ""), "finished": bool(data.get("seasonFinished")),
        "ends_at": float(data["estimatedEndTime"]) / 1000.0 if data.get("estimatedEndTime") else None,
        "prizes": [str(p) for p in (data.get("firstPrize"), data.get("secondPrize"), data.get("thirdPrize")) if p],
        "tiers": [{"top_pct": t.get("formattedPercentage"), "points": _int(t.get("seasonPointsAwarded")) or 0} for t in data.get("sortedSeasonPointsDistributions") or []],
        "finished_games": _int(sl.get("finishedGamesInSeason")) or 0, "total_users": _int(sl.get("totalTeamsInSeason")),
        "me": _row(me) if isinstance(me, dict) else None,
        "leaders": [_row(p) for p in sl.get("seasonStanding") or []],
        "around": [_row(p) for p in sl.get("userStanding") or []],
        "games": games, "read_at": time.time(),
    }


def read(user_key: str) -> Dict[str, Any]:
    """One feed read under the user's login, parsed and stored."""
    from backend.data import season_cache
    from backend.services import my_team

    pages = my_team._fetch_many([SEASON_URL], 0.0, user_key)
    data = _json_from_page(pages.get(SEASON_URL, ""))
    if not data or not isinstance(data.get("seasonLeaderboardData"), dict):
        raise RuntimeError("HLTV's season feed did not come back as expected")
    season = parse(data)
    season_cache.save(user_key, season)
    return season


def season_for(user_key: str, refresh: bool = False) -> Dict[str, Any]:
    """{"value", "pending", "error"}: the stored season at once, re-read in
    the background when older than SEASON_STALE or on refresh."""
    from backend.data import season_cache
    from backend.services import my_team

    key = my_team._job_key("season", user_key)
    with my_team._LOCK:
        if key not in my_team._RESULTS:
            stored = season_cache.get(user_key)
            if stored and stored.get("season"):
                my_team._RESULTS[key] = {"value": stored["season"], "at": float(stored.get("read_at") or 0.0), "error": None}
    return my_team.run_in_background(key, lambda: read(user_key), SEASON_STALE, force=refresh)


def refresh_all() -> str:
    """Every connected account's season, read now (the nightly batch, after
    the night's events are scored). Returns the scheduler's note."""
    from backend.data import hltv_accounts

    notes: List[str] = []
    for meta in sorted(hltv_accounts.list_accounts(), key=lambda m: str(m.get("user_key") or "")):
        key = str(meta.get("user_key") or "")
        if not key or key in NOT_USERS:
            continue
        try:
            season = read(key)
            me = season.get("me") or {}
            notes.append(f"{meta.get('username') or key}: #{me.get('placement')} with {me.get('season_points')} season points")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Season read failed for %s", key, exc_info=True)
            notes.append(f"{meta.get('username') or key}: failed ({str(exc)[:80]})")
    return "; ".join(notes) or "no connected accounts"
