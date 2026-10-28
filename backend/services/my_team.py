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
REPORT_STALE = 120.0  # a shown report older than this is re-read in the background
DRAFT_MAX_AGE = 3600.0  # the draft page only answers "which team is mine" (and is stored once known)
# A page on the domain, so the session cookies can be swapped in before the
# real pages are read; the lightest one HLTV serves.
ENTRY_URL = "https://www.hltv.org/robots.txt"
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


def _forget_cached(user_key: str) -> None:
    """Everything remembered for the user: pages, job results and the stored
    team identities and reports (a reconnected account may be another one)."""
    from backend.data import my_team_cache

    with _LOCK:
        for k in [k for k in _CACHE if k[0] == user_key]:
            _CACHE.pop(k, None)
        for k in [k for k in _RESULTS if k.split("|")[1:2] == [user_key]]:
            _RESULTS.pop(k, None)
    my_team_cache.delete_user(user_key)


def save_account(user_key: str, cookies: List[Dict[str, Any]], username: str) -> Dict[str, Any]:
    from backend.data import hltv_accounts

    _forget_cached(user_key)
    return hltv_accounts.save(user_key, cookies, username)


def forget_account(user_key: str) -> bool:
    from backend.data import hltv_accounts

    _forget_cached(user_key)
    return hltv_accounts.delete(user_key)


# ---- Background reads. A browser read takes 30-90 s (the shared browser
# may be busy first), longer than a public request may last (the tunnel
# cuts at 100 s), so the routes answer at once with what is cached and
# start the read in a thread; the page polls while `pending`.
_RESULTS: Dict[str, Dict[str, Any]] = {}  # job key -> {"value", "at", "error"}
_JOBS: Dict[str, threading.Thread] = {}
_ERROR_RETRY = 60.0


def _job_key(kind: str, user_key: str, *parts: Any) -> str:
    return "|".join([kind, user_key, *[str(p) for p in parts]])


def run_in_background(key: str, fn, max_age: float, force: bool = False) -> Dict[str, Any]:
    """{"value", "pending", "error"}: fn's cached result when younger than
    max_age (an error is kept for a minute); otherwise the stale one, or
    None, with pending=True while one thread per key computes a fresh one.
    `force` re-reads whatever the age, still handing out the stale value."""
    now = time.time()
    with _LOCK:
        hit = _RESULTS.get(key)
        thread = _JOBS.get(key)
        running = thread is not None and thread.is_alive()
        if hit and not force and not running and now - float(hit["at"]) < (_ERROR_RETRY if hit.get("error") else max_age):
            return {"value": hit.get("value"), "pending": False, "error": hit.get("error")}
        if not running:

            def work() -> None:
                try:
                    value, err = fn(), None
                except PersonalSessionError as exc:
                    value, err = None, str(exc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("My Team background read failed (%s)", key, exc_info=True)
                    value, err = None, f"Could not read HLTV: {exc}"
                with _LOCK:
                    _RESULTS[key] = {"value": value, "at": time.time(), "error": err}
                    _JOBS.pop(key, None)

            thread = threading.Thread(target=work, name=f"my-team:{key}", daemon=True)
            _JOBS[key] = thread
            thread.start()
    return {"value": hit.get("value") if hit else None, "pending": True, "error": hit.get("error") if hit else None}


def _job_running(key: str) -> bool:
    with _LOCK:
        t = _JOBS.get(key)
    return t is not None and t.is_alive()


def status_for(user_key: str) -> Dict[str, Any]:
    """The account's connection state plus whether its login is still being
    verified, and the verification's error once a login was rejected."""
    st = account_status(user_key)
    key = _job_key("verify", user_key)
    st["verifying"] = _job_running(key)
    with _LOCK:
        hit = _RESULTS.get(key)
    if hit and hit.get("error") and not st.get("connected"):
        st["error"] = hit["error"]
    return st


def connect_account(user_key: str, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Store the cookies now and verify them with HLTV in the background: the
    account name arrives on a later status read; a rejected login removes
    the row and leaves its error on the status."""
    from backend.data import hltv_accounts

    previous = account(user_key) or {}
    save_account(user_key, cookies, str(previous.get("username") or ""))

    def verify() -> str:
        try:
            username = with_personal_session(lambda _driver, name: name, cookies=cookies)
        except PersonalSessionError:
            forget_account(user_key)
            raise
        hltv_accounts.save(user_key, cookies, str(username or ""))
        return str(username or "")

    key = _job_key("verify", user_key)
    with _LOCK:
        _RESULTS.pop(key, None)
    run_in_background(key, verify, 0.0)
    return status_for(user_key)


def report_for(event_id: int, user_key: str, refresh: bool = False) -> Dict[str, Any]:
    """Background wrapper of team_report: the last report (from memory, or
    the one stored across restarts) at once, re-read in the background when
    older than REPORT_STALE or on refresh."""
    from backend.data import my_team_cache

    key = _job_key("report", user_key, int(event_id))
    with _LOCK:
        if key not in _RESULTS:
            stored = my_team_cache.get(user_key, int(event_id))
            if stored and stored.get("report"):
                _RESULTS[key] = {"value": stored["report"], "at": float(stored.get("read_at") or 0.0), "error": None}
    return run_in_background(key, lambda: team_report(int(event_id), user_key), REPORT_STALE, force=refresh)


def with_personal_session(callback, cookies: Optional[List[Dict[str, Any]]] = None, verify: bool = True):
    """Run `callback(driver, username)` in the shared browser signed in as the
    personal account: the scraper account's session cookies are set aside,
    the personal ones added, and the scraper's put back whatever happens.
    With `verify` the front page is loaded first to confirm the login and
    read the account name (one page more); without, the callback checks the
    login on a page it loads anyway and the username is ""."""
    from backend.services import hltv_session
    from backend.services.hltv_browser import run_hltv_browser_session

    personal = list(cookies or [])
    if not any(c.get("name") == "autologin" for c in personal):
        raise PersonalSessionError("No HLTV login connected")

    def session(driver):
        saved = [c for c in (driver.get_cookies() or []) if c.get("name") in SESSION_COOKIE_NAMES]
        if not any(c.get("name") == "autologin" for c in saved):
            # the browser has lost its own login (a hard kill can drop the
            # profile's cookies): put the stored one back afterwards instead
            stored = hltv_session.stored_login() or {}
            saved = [c for c in (stored.get("cookies") or []) if c.get("name") in SESSION_COOKIE_NAMES]
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
            if not verify:
                return callback(driver, "")
            driver.get("https://www.hltv.org/")
            time.sleep(1.0)
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

    return run_hltv_browser_session(ENTRY_URL, session, wait_text=None, timeout_ms=60000, priority=True)

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


def _fetch_many(urls: List[str], max_age: float, user_key: str, follow=None) -> Dict[str, str]:
    """The pages, from the in-memory cache (per user) when younger than
    max_age; the stale ones are read in one visit of the shared browser
    signed in as that user's HLTV account (each archived as a page snapshot).
    `follow(pages)` may name more (url, max_age) pairs once the batch is
    read — a feed whose URL comes from another feed — read in the same visit
    unless the cache has them."""
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
    from backend.services import hltv_session

    cookies = cookies_for(user_key)
    # The login is checked once per visit, on the first page read: an HTML
    # page's navigation bar names the account; a feed that came back as JSON
    # is proof enough (HLTV answers a signed-out feed request with its sign-in
    # page, not JSON), and one that did not gets the front-page check — so a
    # feed read costs no page beyond the feeds themselves.
    followed = [follow]

    def looks_like_json(html: str) -> bool:
        return bool(re.search(r"<pre[^>]*>\s*[\[{]", html or "", flags=re.S)) or (html or "").lstrip()[:1] in ("{", "[")

    def check_login(driver, url: str, html: str) -> None:
        if url.endswith("/json"):
            if looks_like_json(html):
                return
            driver.get("https://www.hltv.org/")
            time.sleep(1.0)
        if not hltv_session.is_logged_in(driver):
            raise PersonalSessionError("HLTV no longer accepts the personal login; hand it over again")

    def read(driver, _username):
        pages = {}
        checked = False
        queue = list(stale)
        while queue:
            url = queue.pop(0)
            driver.get(url)
            time.sleep(0.5)
            html = driver.page_source or ""
            if not checked:
                check_login(driver, url, html)
                checked = True
            pages[url] = html
            try:
                save_page_snapshot(url, html, kind="fantasy-mine")
            except Exception:  # noqa: BLE001
                pass
            if not queue and followed[0] is not None:
                more, followed[0] = followed[0]({**out, **pages}) or [], None
                now2 = time.time()
                with _LOCK:
                    for u, age in more:
                        hit = _CACHE.get((user_key, u))
                        if hit and now2 - float(hit["at"]) < float(age):
                            out[u] = str(hit["html"])
                        elif u not in pages:
                            queue.append(u)
        return pages

    t0 = time.time()
    pages = with_personal_session(read, cookies=cookies, verify=False) or {}
    logger.info("Personal read for %s: %d page(s) in %.1f s", user_key, len(pages), time.time() - t0)
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
        # An assigned booster shows only its icon (boost/<slug>.png) and the
        # word "assigned"; the slug names the booster. Anything else ("Add
        # booster", "No booster assigned", "No upcoming match") means none.
        booster = ""
        if "assign-booster-button assigned" in block:
            slug = re.search(r'boost/([a-z0-9_\-]+)\.png', block)
            booster = _booster_name_from_slug(slug.group(1)) if slug else "Booster"
        # <div class="upcoming-match">Today - 17:00<br><div class="opponent">vs. X</div></div>
        upcoming_html = re.search(r'<div class="upcoming-match[^"]*">(.*?)<div class="assign-booster-button', block, flags=re.S)
        upcoming_html = upcoming_html.group(1) if upcoming_html else ""
        # ...or, for a scheduled match, "Today - 17:00<br> vs. X" with no div.
        opp = re.search(r'<div class="opponent">\s*(?:vs\.?\s*)?([^<]+?)\s*</div>', upcoming_html) or re.search(r"<br\s*/?>\s*vs\.?\s*([^<]+?)\s*(?:<|$)", upcoming_html)
        start_text = _strip(re.split(r"<br\s*/?>|<div", upcoming_html, 1)[0]) if upcoming_html else ""
        level = re.search(r"player-card-level-(\w+)", block)
        players.append({
            "player_id": int(pid.group(1)) if pid else None,
            "name": _strip(name.group(1)) if name else "",
            "points": _points(_block_field(block, "total-points")),
            "role": role or None,
            "role_index": ROLE_INDEX_BY_NAME.get(role.lower()) if role else None,
            "booster": booster or None,
            "booster_index": _booster_index(booster) if booster else None,
            "next_match_text": _strip(upcoming_html) or None,
            "next_opponent": _strip(opp.group(1)) if opp else None,
            "next_start_text": start_text or None,
            "in_match": "player-in-match" in block,
            "level": level.group(1) if level else None,
        })
    key: Dict[str, Any] = {"rows": []}
    kn = core.find("Key numbers")
    if kn >= 0:
        seg = core[kn:kn + 6000]
        for rm in re.finditer(r'<div class="key-number-description">([^<]*)</div>\s*<div class="key-number-data">([^<]*)</div>', seg):
            key["rows"].append({"label": _strip(rm.group(1)), "value": _strip(rm.group(2))})
        text = _strip(seg)
        m = re.search(r"League placement\s*(\d[\d,]*)\s*/\s*(\d[\d,]*)", text)
        if m:
            key["placement"], key["league_size"] = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
        m = re.search(r"Role / boosters\s*(\d+)\s*/\s*(\d+)", text)
        if m:
            key["roles_assigned"], key["boosters_assigned"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"Total points\s*(-?\d+)", text)
        if m:
            key["total_points"] = int(m.group(1))
        m = re.search(r"Money drafted teams\s*(\d[\d,]*)\s*/\s*(\d[\d,]*)", text)
        if m:
            key["money_placement"], key["money_size"] = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    messages = [_strip(t) for t in re.findall(r'<div class="team-message">(.*?)</div>', core, flags=re.S)]
    messages = [t for t in messages if t]
    tracker = _parse_tracker_items(core[j:] if j >= 0 else "")
    return {"players": players, "key": key, "tracker": tracker, "messages": messages}


def _parse_tracker_items(segment: str) -> List[Dict[str, Any]]:
    """The player match cards ("tracker items") in a page segment: the team
    page's tracker, or one round's cards on the details page. A played card
    carries the result, the opponent and the four point components; a
    coming one the opponent and the start text."""
    items: List[Dict[str, Any]] = []
    # the pattern needs something after the last card to stop at
    for m in _TRACKER_RE.finditer((segment or "") + "</section>"):
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
        content_points = _block_field(item, "content-points")
        if re.search(r"next\s*match", status or "", re.I):
            # A coming match: the opponent sits where the points go, and the
            # bottom row carries "Today - 13:40 / Match start".
            start = re.search(r'<div class="player-tracker-points">\s*([^<]+?)\s*</div>\s*<div class="player-tracker-points-description">\s*Match start', item)
            items.append({
                "player_id": int(pid.group(1)) if pid else None,
                "name": _strip(name.group(1)) if name else "",
                "points": None,
                "result": "NEXT",
                "opponent": content_points or None,
                "start_text": _strip(start.group(1)) if start else None,
                "rating_points": None, "team_points": None, "role_points": None, "booster_points": None,
                "match_url": f"https://www.hltv.org{link.group(1)}" if link else None,
            })
            continue
        items.append({
            "player_id": int(pid.group(1)) if pid else None,
            "name": _strip(name.group(1)) if name else "",
            "points": _points(content_points),
            "result": (sm.group(1) if sm else (status or "")).upper() or None,
            "opponent": (sm.group(2) or "").strip() if sm else None,
            "start_text": None,
            "rating_points": parts.get("ratingpoints"),
            "team_points": parts.get("teampoints"),
            "role_points": parts.get("rolepoints"),
            "booster_points": parts.get("boosterpoints"),
            "match_url": f"https://www.hltv.org{link.group(1)}" if link else None,
        })
    return items


# HLTV names a booster's icon after what it depicts, not after the booster
# (boost/trade.png is the Avenger, boost/knife.png the Rambo); the official
# names are the ones in match_engine.BOOSTER_NAMES. Seen on HLTV's pages
# 2026-10-26; a slug not listed here falls back to a name match.
_BOOSTER_ICON_NAMES = {
    "trade": "Avenger",
    "knife": "Rambo",
    "baiter": "Bait",
    "consistent": "Mister consistent",
    "aimbot": "Aim bot",
    "cannonfodder": "Cannon fodder",
    "hellcase_booster": "Hellcase",
    "carry": "Carry",
    "clutch": "Clutch",
    "assist": "Assist",
    "saver": "Saver",
    "flash": "Flash",
    "farmer": "Farmer",
    "kobe": "Kobe",
    "quad": "Quad",
    "pistol": "Best Pistol Round",
    "bottom": "Bottom of scoreboard",
    "top": "Top of scoreboard",
}


def _booster_name_from_slug(slug: str) -> str:
    """The booster's official name for its icon slug."""
    from backend.services.match_engine import BOOSTER_NAMES

    key = str(slug or "").strip().lower()
    if key in _BOOSTER_ICON_NAMES:
        return _BOOSTER_ICON_NAMES[key]
    want = _norm(key.replace("_", "").replace("-", ""))
    for name in BOOSTER_NAMES.values():
        if _norm(name) == want or _norm(name).startswith(want) or want.startswith(_norm(name)):
            return str(name)
    return key.replace("_", " ").replace("-", " ").strip().capitalize()


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
        # the round's player cards sit under the numbers (breakdown-row-bottom)
        bottom = row.split('<div class="breakdown-row-bottom">', 1)
        rounds.append({
            "players": _parse_tracker_items(bottom[1]) if len(bottom) > 1 else [],
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


def team_report(event_id: int, user_key: str) -> Optional[Dict[str, Any]]:
    """The user's team in the event with HLTV's scoring, the valuation's
    expectation and the next-match prediction per player, freshly read
    (two pages; the team's identity comes from the store after the first
    read) and stored. None when the account has no team there."""
    from backend.data import my_team_cache
    from backend.data.player_db import get_player
    from backend.data.team_db import get_all_teams
    from backend.routes.groups import _GROUPS_STATE
    from backend.services import event_rosters, live_pages, live_state

    eid = int(event_id)
    mine = (my_team_cache.get(user_key, eid) or {}).get("team")
    if not mine:
        teams = my_teams_for([eid], user_key, DRAFT_MAX_AGE).get(eid) or []
        if not teams:
            return None
        mine = teams[0]
        my_team_cache.save_team(user_key, eid, mine)
    base = f"https://www.hltv.org/fantasy/{eid}/league/{mine['league_id']}/team/{mine['team_id']}"
    pages = _fetch_many([base, base + "/details"], 0.0, user_key)
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
    hltv_ids = {tid: t.get("hltv_team_id") for tid, t in all_teams.items()}
    tid_by_name = {str(t.get("name") or "").strip().lower(): tid for tid, t in all_teams.items() if t.get("name")}
    # Rounds whose cards HLTV did not render (its collapsed rows) get them
    # from the tracker: a player's k-th played match is his team's k-th
    # round, and his coming match belongs to the first round he has not
    # played. Cards keep the lineup's order.
    lineup_order = {int(p["player_id"]): n for n, p in enumerate(page["players"]) if p.get("player_id") is not None}
    by_player: Dict[int, List[Dict[str, Any]]] = {}
    for t in page["tracker"]:
        if t.get("player_id") is not None:
            by_player.setdefault(int(t["player_id"]), []).append(t)
    for i, r in enumerate(rounds):
        if r.get("players"):
            continue
        cards = []
        for pid_key, entries in by_player.items():
            played = [t for t in entries if t.get("result") in ("WON", "LOST")]
            if i < len(played):
                cards.append(played[i])
            elif i == len(played) and r.get("status") != "ENDED":
                nxt = next((t for t in entries if t.get("result") == "NEXT"), None)
                if nxt:
                    cards.append(nxt)
        r["players"] = sorted(cards, key=lambda t: lineup_order.get(int(t.get("player_id") or -1), 99))
    for t in page["tracker"] + [c for r in rounds for c in (r.get("players") or [])]:
        opp_tid = tid_by_name.get(str(t.get("opponent") or "").strip().lower())
        t["opponent_team_id"] = opp_tid
        t["opponent_hltv_team_id"] = hltv_ids.get(opp_tid) if opp_tid else None
    # The next matches come from the event's live pages; right after a start
    # nothing has read them yet, so ask for the event (which reads them).
    if not live_pages.cached(eid):
        try:
            live_state.live_event(eid, fetch_if_missing=False)  # the live watch reads the pages; the report never waits for them
        except Exception:  # noqa: BLE001 - the report still has HLTV's own numbers
            logger.warning("Could not read the live pages for event %s", eid, exc_info=True)
    pending = [m for m in live_pages.cached(eid) if m.get("status") in ("live", "upcoming")]
    pending.sort(key=lambda m: (m.get("start_at") or 0.0))

    # The run's per-round expectations (stage_stats.players[pid].stages[r]):
    # a player's expectation "so far" is the sum over the rounds his team has
    # played, so a team two matches in is judged against two rounds' worth.
    stage_stats = results.get("stage_stats") or {}
    stage_order = [str(s.get("key")) for s in (stage_stats.get("stages") or []) if s.get("key") and s.get("key") != "padding"]
    stage_players = stage_stats.get("players") or {}
    stage_teams = stage_stats.get("teams") or {}

    def round_ev(pid: Optional[int], tid: Optional[int], stage_key: str, comp: str) -> Optional[float]:
        """The player's expectation for one round GIVEN his team plays it:
        the run's figure is weighted by the chance of reaching the round, so
        it is divided by that chance (1 for the opening rounds)."""
        stages = ((stage_players.get(str(pid)) or {}).get("stages") or {}) if pid is not None else {}
        st = stages.get(stage_key)
        if not st:
            return None
        play = float((((stage_teams.get(str(tid)) or {}).get("rounds") or {}).get(stage_key) or {}).get("play") or 0.0) if tid is not None else 0.0
        value = float(st.get(comp) or 0.0)
        return value / play if play > 0.05 else value

    for pl in page["players"]:
        pid = pl.get("player_id")
        row = get_player(int(pid)) if pid else None
        tid = team_of_player.get(int(pid)) if pid else None
        pl["team_id"] = tid
        pl["team"] = names.get(tid) if tid else None
        pl["hltv_team_id"] = hltv_ids.get(tid) if tid else None
        played = sum(1 for t in page["tracker"] if t.get("player_id") == pid and t.get("result") in ("WON", "LOST"))
        ev = expected_by_pid.get(int(pid)) if pid else None
        stages = ((stage_players.get(str(pid)) or {}).get("stages") or {}) if pid else {}
        so_far = {
            comp: round(sum(float(round_ev(pid, tid, k, comp) or 0.0) for k in stage_order[:played]), 1)
            for comp in ("total", "rating", "win", "role", "booster")
        }
        pl["expected"] = (
            {
                "total": round(float(ev.get("total_points") or 0.0), 1),
                "rating_points": round(float(ev.get("rating_points_total") or 0.0), 1),
                "win_points": round(float(ev.get("win_points_total") or 0.0), 1),
                "role_points": round(float(ev.get("role_points_total") or 0.0), 1),
                "booster_points": round(float(ev.get("booster_points_total") or 0.0), 1),
                "role_index": ev.get("role_id"),
                "role": ROLE_NAMES.get(int(ev["role_id"])) if ev.get("role_id") is not None else None,
                "so_far": so_far["total"] if stages else None,
                "so_far_rounds": played,
                "so_far_components": {"rating_points": so_far["rating"], "win_points": so_far["win"], "role_points": so_far["role"], "booster_points": so_far["booster"]} if stages else None,
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
    # Each round's expectation: per card (the player's figure for that round,
    # given his team plays it) and for the lineup as a whole, against the
    # round's actual subtotal.
    lineup = [(p.get("player_id"), p.get("team_id")) for p in page["players"] if p.get("player_id") is not None]
    comps = (("rating", "rating"), ("team", "win"), ("role", "role"), ("booster", "booster"), ("total", "total"))
    actual_keys = {"rating": "rating_points", "team": "team_points", "role": "role_points", "booster": "booster_points", "total": "points"}
    round_actual_keys = {"rating": "rating_points", "team": "team_points", "role": "role_points", "booster": "booster_points", "total": "subtotal"}
    running_exp = {k: 0.0 for k, _ in comps}
    running_act = {k: 0.0 for k, _ in comps}
    for i, r in enumerate(rounds):
        stage_key = stage_order[i] if i < len(stage_order) else None
        for c in r.get("players") or []:
            c_tid = team_of_player.get(int(c["player_id"])) if c.get("player_id") is not None else None
            parts = {k: round_ev(c.get("player_id"), c_tid, stage_key, src) for k, src in comps} if stage_key else {}
            known = parts and parts.get("total") is not None
            c["expected"] = round(float(parts["total"]), 1) if known else None
            c["expected_parts"] = {k: (round(float(v), 1) if v is not None else None) for k, v in parts.items()} if known else None
            c["difference"] = round(float(c["points"]) - float(parts["total"]), 1) if known and c.get("points") is not None else None
            c["difference_parts"] = (
                {k: (round(float(c.get(actual_keys[k]) or 0) - float(parts[k]), 1) if parts.get(k) is not None else None) for k, _ in comps}
                if known and c.get("points") is not None else None
            )
        # the round for the lineup: expected by component, actual by component, running sums
        exp_parts = {k: sum(float(round_ev(pid, tid, stage_key, src) or 0.0) for pid, tid in lineup) for k, src in comps} if stage_key else None
        played_round = r.get("status") != "TBA" and r.get("subtotal") is not None
        act_parts = {k: float(r.get(round_actual_keys[k]) or 0.0) for k, _ in comps} if played_round else None
        if exp_parts:
            for k, _ in comps:
                running_exp[k] += exp_parts[k]
        if act_parts:
            for k, _ in comps:
                running_act[k] += act_parts[k]
        r["expected_parts"] = {k: round(v, 1) for k, v in exp_parts.items()} if exp_parts else None
        r["expected_total"] = r["expected_parts"]["total"] if exp_parts else None
        r["actual_parts"] = {k: round(v, 1) for k, v in act_parts.items()} if act_parts else None
        r["difference"] = round(act_parts["total"] - exp_parts["total"], 1) if exp_parts and act_parts else None
        r["running_expected_parts"] = {k: round(v, 1) for k, v in running_exp.items()} if exp_parts else None
        r["running_actual_parts"] = {k: round(v, 1) for k, v in running_act.items()} if act_parts else None
    report = {
        "event_id": eid,
        "team": mine,
        "key": page["key"],
        "players": page["players"],
        "tracker": page["tracker"],
        "messages": page.get("messages") or [],
        "rounds": rounds,
        "expected_total": round(sum(float((p.get("expected") or {}).get("total") or 0.0) for p in page["players"]), 1),
        "expected_so_far": round(sum(float((p.get("expected") or {}).get("so_far") or 0.0) for p in page["players"]), 1),
        "read_at": time.time(),
        "account": (account(user_key) or {}).get("username") or "",
    }
    my_team_cache.save_report(user_key, eid, mine, report, report["read_at"])
    return report
