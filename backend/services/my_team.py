"""The operator's own HLTV fantasy team for an event — what HLTV says it has
scored (per player: rating, team, role and booster points, per match) set
against what the stored valuation expected, plus a prediction for each
player's next match.

Sources, read through the shared browser signed in as the USER's own HLTV
account (data/hltv_accounts: the app signs them in through a window on
hltv.org and hands the cookies over; the operator can also use
`scripts/hltv-login-handoff.ps1 --account mine`), whose cookies replace the
scraper account's for the duration of the read and are swapped back after:
- /fantasy/{eid}/leagues/create/json — `topMenuData.leagueTeams`: the
  account's teams for the event (team id, league id, names);
- /fantasy/{eid}/league/{lid}/team/{tid} — the five player blocks (role,
  booster, next match, total points), the key numbers (league placement,
  total) and the player tracker (one entry per player per match with the
  four point components and the match link);
- .../team/{tid}/details — the round-by-round breakdown.
Pages are re-read at most every REFRESH seconds.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REFRESH = 600.0
_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
SESSION_COOKIE_NAMES = ("autologin", "PHPSESSID")
OPERATOR_KEY = "operator"


class PersonalSessionError(RuntimeError):
    """No HLTV login for this user, or HLTV no longer accepts it."""


def _migrate_operator_file() -> None:
    """The v0.1.95 single personal session file becomes the operator's row."""
    from backend.data import hltv_accounts

    f = Path(__file__).resolve().parents[2] / ".runtime" / "my-hltv-session.json"
    if not f.exists() or hltv_accounts.get(OPERATOR_KEY):
        return
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("cookies"):
            hltv_accounts.save(OPERATOR_KEY, data["cookies"], str(data.get("username") or ""))
        f.unlink()
    except Exception:  # noqa: BLE001
        logger.warning("Could not migrate the operator's HLTV session file", exc_info=True)


def account(user_key: str) -> Optional[Dict[str, Any]]:
    from backend.data import hltv_accounts

    if user_key == OPERATOR_KEY:
        _migrate_operator_file()
    return hltv_accounts.get(user_key)


def account_status(user_key: str) -> Dict[str, Any]:
    from backend.data import hltv_accounts

    if user_key == OPERATOR_KEY:
        _migrate_operator_file()
    return hltv_accounts.status(user_key)


def cookies_for(user_key: str) -> List[Dict[str, Any]]:
    row = account(user_key)
    cookies = (row or {}).get("cookies") or []
    if not any(c.get("name") == "autologin" for c in cookies):
        raise PersonalSessionError("No HLTV login connected")
    return cookies


def save_account(user_key: str, cookies: List[Dict[str, Any]], username: str) -> Dict[str, Any]:
    from backend.data import hltv_accounts

    with _LOCK:
        for k in [k for k in _CACHE if k[0] == user_key]:
            _CACHE.pop(k, None)
    return hltv_accounts.save(user_key, cookies, username)


def forget_account(user_key: str) -> bool:
    from backend.data import hltv_accounts

    with _LOCK:
        for k in [k for k in _CACHE if k[0] == user_key]:
            _CACHE.pop(k, None)
    return hltv_accounts.delete(user_key)


def with_personal_session(callback, cookies: Optional[List[Dict[str, Any]]] = None):
    """Run `callback(driver)` in the shared browser signed in as the personal
    account: the scraper account's session cookies are set aside, the
    personal ones added, and the scraper's put back whatever happens."""
    from backend.services import hltv_session
    from backend.services.hltv_browser import run_hltv_browser_session

    personal = list(cookies or [])
    if not any(c.get("name") == "autologin" for c in personal):
        raise PersonalSessionError("No HLTV login connected")

    def session(driver):
        saved = [c for c in (driver.get_cookies() or []) if c.get("name") in SESSION_COOKIE_NAMES]
        try:
            for name in SESSION_COOKIE_NAMES:
                try:
                    driver.delete_cookie(name)
                except Exception:  # noqa: BLE001
                    pass
            for c in personal:
                cookie = {"name": c["name"], "value": c["value"], "domain": c.get("domain") or ".hltv.org", "path": c.get("path") or "/"}
                if c.get("expiry"):
                    cookie["expiry"] = int(c["expiry"])
                if c.get("secure"):
                    cookie["secure"] = True
                if c.get("httpOnly"):
                    cookie["httpOnly"] = True
                driver.add_cookie(cookie)
            driver.get("https://www.hltv.org/")
            time.sleep(1.5)
            if not hltv_session.is_logged_in(driver):
                raise PersonalSessionError("HLTV no longer accepts the personal login; hand it over again")
            return callback(driver, hltv_session.nav_username(driver))
        finally:
            for name in SESSION_COOKIE_NAMES:
                try:
                    driver.delete_cookie(name)
                except Exception:  # noqa: BLE001
                    pass
            for c in saved:
                try:
                    driver.add_cookie({k: v for k, v in c.items() if k in ("name", "value", "domain", "path", "expiry", "secure", "httpOnly")})
                except Exception:  # noqa: BLE001
                    logger.warning("Could not restore the scraper's %s cookie", c.get("name"))

    return run_hltv_browser_session("https://www.hltv.org/", session, wait_text=None, timeout_ms=60000)

ROLE_INDEX_BY_NAME = {
    "main awp": 0, "support": 1, "attacker": 2, "leader": 3, "stathunter": 4, "entry fragger": 5,
    "camper": 6, "defender": 7, "hs machine": 8, "noob": 9, "multi fragger": 10, "eco friendly": 11,
}
ROLE_NAMES = {v: k.title() if k not in ("main awp", "hs machine") else {"main awp": "Main AWP", "hs machine": "HS Machine"}[k] for k, v in ROLE_INDEX_BY_NAME.items()}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", str(text or "").lower().replace("mister/miss", "mister"))


def _booster_index(name: str) -> Optional[int]:
    from backend.services.match_engine import BOOSTER_NAMES

    key = _norm(name)
    for idx, label in BOOSTER_NAMES.items():
        if _norm(label) == key:
            return int(idx)
    return None


def _strip(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def _points(text: str) -> Optional[int]:
    m = re.search(r"(-?\d+)\s*points?", text or "")
    return int(m.group(1)) if m else None


def _fetch_many(urls: List[str], max_age: float, user_key: str) -> Dict[str, str]:
    """The pages, from the in-memory cache (per user) when younger than
    max_age; the stale ones are read in one visit of the shared browser
    signed in as that user's HLTV account (each archived as a page snapshot)."""
    from backend.data.page_snapshots import save_page_snapshot

    out: Dict[str, str] = {}
    stale: List[str] = []
    now = time.time()
    with _LOCK:
        for url in urls:
            hit = _CACHE.get((user_key, url))
            if hit and now - float(hit["at"]) < max_age:
                out[url] = str(hit["html"])
            else:
                stale.append(url)
    if not stale:
        return out
    cookies = cookies_for(user_key)

    def read(driver, _username):
        pages = {}
        for url in stale:
            driver.get(url)
            time.sleep(1.5)
            html = driver.page_source or ""
            pages[url] = html
            try:
                save_page_snapshot(url, html, kind="fantasy-mine")
            except Exception:  # noqa: BLE001
                pass
        return pages

    pages = with_personal_session(read, cookies=cookies) or {}
    with _LOCK:
        for url, html in pages.items():
            _CACHE[(user_key, url)] = {"at": time.time(), "html": html}
    out.update(pages)
    return out


def _draft_url(event_id: int) -> str:
    return f"https://www.hltv.org/fantasy/{int(event_id)}/leagues/create/json"


def my_teams_for(event_ids: List[int], user_key: str, max_age: float = REFRESH) -> Dict[int, List[Dict[str, Any]]]:
    """{event_id: the user's teams} for several events in one browser visit."""
    pages = _fetch_many([_draft_url(e) for e in event_ids], max_age, user_key)
    return {int(e): _teams_from_draft(pages.get(_draft_url(e), "")) for e in event_ids}


def _teams_from_draft(html: str) -> List[Dict[str, Any]]:
    m = re.search(r"<pre>(.*?)</pre>", html or "", flags=re.S)
    raw = htmllib.unescape(m.group(1)) if m else html
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in ((data.get("topMenuData") or {}).get("leagueTeams") or []):
        tid = ((t.get("teamId") or {}).get("id")) if isinstance(t.get("teamId"), dict) else t.get("teamId")
        lid = ((t.get("leagueId") or {}).get("id")) if isinstance(t.get("leagueId"), dict) else t.get("leagueId")
        if tid and lid:
            out.append({"team_id": int(tid), "league_id": int(lid), "team_name": str(t.get("teamName") or ""), "league_name": str(t.get("leagueName") or "")})
    return out


# ---- the team page ------------------------------------------------------------
_BLOCK_RE = re.compile(r'<div class="player-block[^"]*">(.*?)(?=<div class="player-block|<div class="fantasy-highlighted-stats-container)', re.S)
_STATS_LINK_RE = re.compile(r'/stats/players/(\d+)/')
_TRACKER_RE = re.compile(r'<div class="tracker-item[ "][^"]*">(.*?)(?=<div class="tracker-item[ "]|<div class="fantasy20padding">|</section>)', re.S)


def _block_field(block: str, cls: str) -> str:
    m = re.search(r'<div class="' + re.escape(cls) + r'[^"]*">(.*?)</div>', block, flags=re.S)
    return _strip(m.group(1)) if m else ""


def parse_team_page(html: str) -> Dict[str, Any]:
    core = re.sub(r"<script.*?</script>|<style.*?</style>", "", html or "", flags=re.S)
    players: List[Dict[str, Any]] = []
    i = core.find("team-lineup-flex")
    j = core.find("player-tracker-container", i)
    lineup = core[i:j] if i >= 0 else ""
    for m in _BLOCK_RE.finditer(lineup):
        block = m.group(0)
        pid = _STATS_LINK_RE.search(block)
        name = re.search(r'card-player-tag[^>]*>(?:<span[^>]*>)?([^<]+)<', block)
        role = ""
        if 'assign-role-button assigned' in block:
            role = _block_field(block, "assign-role-text")
        booster_text = _block_field(block, "assign-booster-button")
        booster = "" if _norm(booster_text) in ("addbooster", "noupcomingmatchnomatches", "nomatches", "noupcomingmatch", "") else booster_text
        upcoming = _block_field(block, "upcoming-match")
        players.append({
            "player_id": int(pid.group(1)) if pid else None,
            "name": _strip(name.group(1)) if name else "",
            "points": _points(_block_field(block, "total-points")),
            "role": role or None,
            "role_index": ROLE_INDEX_BY_NAME.get(role.lower()) if role else None,
            "booster": booster or None,
            "booster_index": _booster_index(booster) if booster else None,
            "next_match_text": upcoming or None,
        })
    key: Dict[str, Any] = {}
    kn = core.find("Key numbers")
    if kn >= 0:
        seg = _strip(core[kn:kn + 4000])
        m = re.search(r"League placement\s*(\d[\d,]*)\s*/\s*(\d[\d,]*)", seg)
        if m:
            key["placement"], key["league_size"] = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
        m = re.search(r"Role / boosters\s*(\d+)\s*/\s*(\d+)", seg)
        if m:
            key["roles_assigned"], key["boosters_assigned"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"Total points\s*(-?\d+)", seg)
        if m:
            key["total_points"] = int(m.group(1))
    tracker: List[Dict[str, Any]] = []
    for m in _TRACKER_RE.finditer(core[j:] if j >= 0 else ""):
        item = m.group(0)
        pid = _STATS_LINK_RE.search(item)
        name = re.search(r'card-player-tag[^>]*>(?:<span[^>]*>)?([^<]+)<', item)
        status = _block_field(item, "content-match-status")
        sm = re.match(r"([A-Z]+)(?:\s+vs\s+(.*))?$", status or "")
        parts = {}
        for pm in re.finditer(r'<div class="player-tracker-points">\s*(-?\d+)\s*</div>\s*<div class="player-tracker-point-description">([^<]+)</div>', item):
            parts[_norm(pm.group(2))] = int(pm.group(1))
        link = re.search(r'href="(/matches/\d+/[^"]*)"', item)
        if not (pid or name):
            continue
        tracker.append({
            "player_id": int(pid.group(1)) if pid else None,
            "name": _strip(name.group(1)) if name else "",
            "points": _points(_block_field(item, "content-points")),
            "result": (sm.group(1) if sm else (status or "")).upper() or None,
            "opponent": (sm.group(2) or "").strip() if sm else None,
            "rating_points": parts.get("ratingpoints"),
            "team_points": parts.get("teampoints"),
            "role_points": parts.get("rolepoints"),
            "booster_points": parts.get("boosterpoints"),
            "match_url": f"https://www.hltv.org{link.group(1)}" if link else None,
        })
    return {"players": players, "key": key, "tracker": tracker}


_ROUND_RE = re.compile(r'<div class="breakdown-round-row expandable">(.*?)(?=<div class="breakdown-round-row expandable">|<div class="booster-breakdown|$)', re.S)


def parse_details_page(html: str) -> List[Dict[str, Any]]:
    core = re.sub(r"<script.*?</script>|<style.*?</style>", "", html or "", flags=re.S)
    rounds = []
    for m in _ROUND_RE.finditer(core):
        row = m.group(0)
        top = row.split('<div class="breakdown-row-top">', 1)[-1]
        status = re.search(r'button-([A-Z]+)', top)
        name = re.search(r'breakdown-round-round-name[^>]*><div[^>]*>([^<]*)<', top)
        cells = re.findall(r'breakdown-round-(player|team|booster-role|round|total)-points">([^<]*)<', top)
        vals = {k: _strip(v) for k, v in cells}
        pl = re.search(r'breakdown-round-league-placement">([^<]*)<', top)
        vals["league-placement"] = _strip(pl.group(1)) if pl else ""

        def num(k):
            try:
                return int(vals.get(k, "").strip())
            except ValueError:
                return None

        rb = vals.get("booster-role", "")
        rbm = re.match(r"\s*(-?\d+)\s*/\s*(-?\d+)", rb)
        plm = re.match(r"\s*(\d[\d,]*)\s*/\s*(\d[\d,]*)", vals.get("league-placement", ""))
        rounds.append({
            "status": status.group(1) if status else None,
            "name": _strip(name.group(1)) if name else "",
            "rating_points": num("player"),
            "team_points": num("team"),
            "role_points": int(rbm.group(1)) if rbm else None,
            "booster_points": int(rbm.group(2)) if rbm else None,
            "subtotal": num("round"),
            "placement": int(plm.group(1).replace(",", "")) if plm else None,
            "league_size": int(plm.group(2).replace(",", "")) if plm else None,
            "total": num("total"),
        })
    return rounds


# ---- the report ----------------------------------------------------------------
def _win_probability(results: Dict[str, Any], a: int, b: int, bo: int) -> Optional[float]:
    """P(a beats b) from the stored run's win matrix for that series length."""
    for block_key, order_key in (("swiss", None), ("bracket", "team_slots")):
        block = results.get(block_key) or {}
        matrices = block.get("win_probs") or {}
        if not matrices:
            continue
        order = [int(t) for t in (block.get(order_key) if order_key else (results.get("groups") or [[]])[0])]
        if a not in order or b not in order:
            continue
        matrix = matrices.get(f"bo{int(bo)}") or next(iter(matrices.values()), None)
        if not matrix:
            continue
        try:
            return float(matrix[order.index(a)][order.index(b)])
        except (IndexError, TypeError, ValueError):
            return None
    return None


def _predict_match(player_row: Dict[str, Any], own_tid: int, opp_tid: int, bo: int, ranks: Dict[int, int], results: Dict[str, Any], role_index: Optional[int], booster_index: Optional[int]) -> Dict[str, Any]:
    from backend.services import rating_model
    from backend.services.role_assignment import extract_role_scores_for_player
    from backend.services.swiss_booster_assignment import BOOSTER_POINT_VALUE

    pid = int(player_row["player_id"])
    own_rank, opp_rank = ranks.get(own_tid, 999), ranks.get(opp_tid, 999)
    rating = rating_model.predict(pid, own_rank, opp_rank)
    if rating is None:
        try:
            rating = float(player_row.get("rating") or 1.0)
        except (TypeError, ValueError):
            rating = 1.0
    rating_points = (float(rating) - 1.0) / 2.0 * 100.0
    p = _win_probability(results, own_tid, opp_tid, bo)
    win_points = (6.0 * p - 3.0 * (1.0 - p)) if p is not None else 0.0
    role_points = 0.0
    if role_index is not None:
        leader = (results.get("leader_odds") or {}).get(str(own_tid)) if role_index == 3 else None
        role_points = float(extract_role_scores_for_player(player_row, leader=leader).get(int(role_index), 0.0))
    booster_points = 0.0
    if booster_index is not None:
        try:
            rates = json.loads(player_row.get("boosters_json") or "{}")
            booster_points = BOOSTER_POINT_VALUE * float(rates.get(str(int(booster_index)), 0.0))
        except Exception:  # noqa: BLE001
            booster_points = 0.0
    return {
        "rating": round(float(rating), 3), "rating_points": round(rating_points, 1), "win_probability": None if p is None else round(p, 3),
        "win_points": round(win_points, 1), "role_points": round(role_points, 1), "booster_points": round(booster_points, 1),
        "total": round(rating_points + win_points + role_points + booster_points, 1),
    }


def team_report(event_id: int, user_key: str, max_age: float = REFRESH) -> Optional[Dict[str, Any]]:
    """The user's team in the event with HLTV's scoring, the valuation's
    expectation and the next-match prediction per player. None when the
    account has no team there."""
    from backend.data.player_db import get_player
    from backend.data.team_db import get_all_teams
    from backend.routes.groups import _GROUPS_STATE
    from backend.services import event_rosters, live_pages

    eid = int(event_id)
    teams = my_teams_for([eid], user_key, max_age).get(eid) or []
    if not teams:
        return None
    mine = teams[0]
    base = f"https://www.hltv.org/fantasy/{eid}/league/{mine['league_id']}/team/{mine['team_id']}"
    pages = _fetch_many([base, base + "/details"], max_age, user_key)
    page = parse_team_page(pages.get(base, ""))
    try:
        rounds = parse_details_page(pages.get(base + "/details", ""))
    except Exception:  # noqa: BLE001
        rounds = []

    results = ((_GROUPS_STATE.load(key=eid) or {}).get("results")) or {}
    expected_by_pid: Dict[int, Dict[str, Any]] = {}
    team_of_player: Dict[int, int] = {}
    for tid, tv in (results.get("teams") or {}).items():
        for pid, ev in (tv.get("players") or {}).items():
            expected_by_pid[int(pid)] = {**ev, "team_id": int(tid)}
    for tid, pids in (event_rosters.rosters(eid) or {}).items():
        for pid in pids:
            team_of_player[int(pid)] = int(tid)
    for pid, ev in expected_by_pid.items():
        team_of_player.setdefault(pid, int(ev["team_id"]))
    all_teams = {int(t["team_id"]): t for t in get_all_teams()}
    ranks = {tid: int(t.get("hltv_rank") or 999) for tid, t in all_teams.items()}
    names = {tid: str(t.get("name") or tid) for tid, t in all_teams.items()}
    pending = [m for m in live_pages.cached(eid) if m.get("status") in ("live", "upcoming")]
    pending.sort(key=lambda m: (m.get("start_at") or 0.0))

    for pl in page["players"]:
        pid = pl.get("player_id")
        row = get_player(int(pid)) if pid else None
        tid = team_of_player.get(int(pid)) if pid else None
        pl["team_id"] = tid
        pl["team"] = names.get(tid) if tid else None
        ev = expected_by_pid.get(int(pid)) if pid else None
        pl["expected"] = (
            {
                "total": round(float(ev.get("total_points") or 0.0), 1),
                "rating_points": round(float(ev.get("rating_points_total") or 0.0), 1),
                "win_points": round(float(ev.get("win_points_total") or 0.0), 1),
                "role_points": round(float(ev.get("role_points_total") or 0.0), 1),
                "booster_points": round(float(ev.get("booster_points_total") or 0.0), 1),
                "role_index": ev.get("role_id"),
                "role": ROLE_NAMES.get(int(ev["role_id"])) if ev.get("role_id") is not None else None,
            }
            if ev else None
        )
        nxt = next((m for m in pending if tid and tid in (int(m["a"]), int(m["b"]))), None)
        if nxt and row:
            opp = int(m_b) if (m_b := nxt["b"]) != tid else int(nxt["a"])
            bo = int(nxt.get("bo") or 3)
            pl["next_match"] = {
                "opponent_id": opp, "opponent": names.get(opp), "start_at": nxt.get("start_at"), "status": nxt.get("status"), "bo": bo, "url": nxt.get("url"),
                "predicted": _predict_match(row, int(tid), opp, bo, ranks, results, pl.get("role_index"), pl.get("booster_index")),
            }
        else:
            pl["next_match"] = None
        pl["actual"] = {
            "rating_points": sum(int(t.get("rating_points") or 0) for t in page["tracker"] if t.get("player_id") == pid),
            "team_points": sum(int(t.get("team_points") or 0) for t in page["tracker"] if t.get("player_id") == pid),
            "role_points": sum(int(t.get("role_points") or 0) for t in page["tracker"] if t.get("player_id") == pid),
            "booster_points": sum(int(t.get("booster_points") or 0) for t in page["tracker"] if t.get("player_id") == pid),
            "matches": sum(1 for t in page["tracker"] if t.get("player_id") == pid and t.get("result") in ("WON", "LOST")),
        }
    return {
        "event_id": eid,
        "team": mine,
        "key": page["key"],
        "players": page["players"],
        "tracker": page["tracker"],
        "rounds": rounds,
        "expected_total": round(sum(float((p.get("expected") or {}).get("total") or 0.0) for p in page["players"]), 1),
        "read_at": time.time(),
        "account": (account(user_key) or {}).get("username") or "",
    }
