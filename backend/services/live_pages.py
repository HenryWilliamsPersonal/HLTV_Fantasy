"""What HLTV's event pages say about an event's matches right now — for the
Live page, between the nightly results imports.

Two pages per event, fetched through the shared browser and cached here:
- the event overview: its Swiss simulator JSON lists every pairing made so
  far by record pool (with start times) and every result; its slotted
  bracket JSON lists each bracket slot's teams, start time and score;
- the event's /matches page: the matches in play (current map score, maps
  won) and the scheduled ones.

The result is one list of matches per event — finished (with a score),
live (with the running score) or upcoming (with a start time) — keyed by
HLTV match id, with the app's team ids. A live event is re-read every
LIVE_REFRESH seconds in the background; an event starting within
UPCOMING_WINDOW every UPCOMING_REFRESH seconds; nothing is fetched for
finished events.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from backend.services.event_format import _BRACKET_JSON_RE, _SWISS_JSON_RE, _de_slots, _decode_attr_json, _slot_side_team

logger = logging.getLogger(__name__)

LIVE_REFRESH = 600.0
UPCOMING_REFRESH = 3600.0
UPCOMING_WINDOW = 2 * 86400.0

_CACHE: Dict[int, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_INFLIGHT: set = set()

_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_CARD_RE = re.compile(r'<div class="match-wrapper[^"]*"([^>]*)>')
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_UNIX_RE = re.compile(r'data-unix="(\d{10,13})"')
_MAP_SCORE_RE = re.compile(r'data-livescore-current-map-score="(\d+)"\s+data-livescore-team="(\d+)"')
_MAPS_WON_RE = re.compile(r'data-livescore-maps-won-for=""\s+data-livescore-team="(\d+)"[^>]*>(\d+)<')


def _pool_record(name: str) -> Optional[str]:
    """'OneZero' -> '1-0' (wins-losses before the match)."""
    words = re.findall(r"[A-Z][a-z]+", str(name or ""))
    if len(words) != 2 or any(w.lower() not in _WORDS for w in words):
        return None
    return f"{_WORDS[words[0].lower()]}-{_WORDS[words[1].lower()]}"


def _hltv_id(node: Any) -> Optional[int]:
    """The HLTV team id inside the Swiss JSON's nested ids."""
    cur = node
    for _ in range(4):
        if not isinstance(cur, dict):
            break
        if "teamId" in cur and not isinstance(cur["teamId"], dict):
            try:
                return int(cur["teamId"])
            except (TypeError, ValueError):
                return None
        cur = cur.get("teamId")
    return None


def _ts(value: Any) -> Optional[float]:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return ts / 1000.0 if ts > 10_000_000_000 else ts


def parse_swiss(html: str, to_tid: Callable[[Optional[int]], Optional[int]]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for raw in _SWISS_JSON_RE.findall(html or ""):
        data = _decode_attr_json(raw)
        if not data:
            continue
        results: Dict[int, Dict[str, Any]] = {}
        for r in data.get("results") or []:
            if not isinstance(r, dict) or r.get("team1Score") is None:
                continue
            mid = ((r.get("matchId") or {}).get("matchId")) if isinstance(r.get("matchId"), dict) else r.get("matchId")
            try:
                results[int(mid)] = {"score_a": int(r.get("team1Score") or 0), "score_b": int(r.get("team2Score") or 0), "a_hltv": _hltv_id(r.get("team1")), "b_hltv": _hltv_id(r.get("team2"))}
            except (TypeError, ValueError):
                continue
        for pool, matches in (data.get("matches") or {}).items():
            record = _pool_record(pool)
            for m in matches or []:
                if not isinstance(m, dict):
                    continue
                mid = ((m.get("matchId") or {}).get("matchId")) if isinstance(m.get("matchId"), dict) else m.get("matchId")
                try:
                    mid = int(mid)
                except (TypeError, ValueError):
                    continue
                a_h, b_h = _hltv_id(m.get("team1")), _hltv_id(m.get("team2"))
                a, b = to_tid(a_h), to_tid(b_h)
                if not a or not b:
                    continue
                entry = {"match_id": mid, "a": a, "b": b, "a_hltv": a_h, "b_hltv": b_h, "start_at": _ts(m.get("timestamp")), "pool": record, "status": "upcoming", "url": f"https://www.hltv.org/matches/{mid}/-"}
                res = results.get(mid)
                if res:
                    sa, sb = res["score_a"], res["score_b"]
                    if res.get("a_hltv") == b_h and res.get("b_hltv") == a_h:
                        sa, sb = sb, sa
                    entry.update({"status": "finished", "score_a": sa, "score_b": sb, "winner": a if sa > sb else b if sb > sa else None})
                out[mid] = entry
        break  # one Swiss stage per page
    return out


def parse_brackets(html: str, to_tid: Callable[[Optional[int]], Optional[int]]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for raw in _BRACKET_JSON_RE.findall(html or ""):
        data = _decode_attr_json(raw)
        if not data:
            continue
        sections: List[Any] = []
        for key, value in data.items():
            if key == "rounds" and isinstance(value, list):
                sections.extend(value)
            elif isinstance(value, dict):
                sections.append(value)
        for section in sections:
            for slot in _de_slots(section):
                if slot.get("hidden"):
                    continue
                t1, t2 = _slot_side_team(slot, "team1"), _slot_side_team(slot, "team2")
                if not t1 or not t2:
                    continue
                matchup = slot.get("matchup") if isinstance(slot.get("matchup"), dict) else {}
                match = matchup.get("match") if isinstance(matchup.get("match"), dict) else {}
                try:
                    mid = int(match.get("matchId") or 0)
                except (TypeError, ValueError):
                    mid = 0
                if not mid:
                    continue
                a, b = to_tid(t1["id"]), to_tid(t2["id"])
                if not a or not b:
                    continue
                url = str(match.get("matchPageURL") or "")
                entry = {"match_id": mid, "a": a, "b": b, "a_hltv": t1["id"], "b_hltv": t2["id"], "start_at": _ts(match.get("startTime")), "pool": None, "status": "upcoming", "url": f"https://www.hltv.org{url}" if url.startswith("/") else url, "bo": match.get("numberOfMaps")}
                score = matchup.get("score") if isinstance(matchup.get("score"), dict) else None
                if score and score.get("team1Score") is not None:
                    sa, sb = int(score.get("team1Score") or 0), int(score.get("team2Score") or 0)
                    entry.update({"status": "finished", "score_a": sa, "score_b": sb, "winner": a if sa > sb else b if sb > sa else None})
                out[mid] = entry
    return out


def parse_matches_page(html: str) -> Dict[int, Dict[str, Any]]:
    """{match_id: {live, start_at, a_hltv, b_hltv, map_a, map_b, maps_a, maps_b}} from /events/{id}/matches."""
    out: Dict[int, Dict[str, Any]] = {}
    text = html or ""
    cards = list(_CARD_RE.finditer(text))
    for i, m in enumerate(cards):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        try:
            mid = int(attrs.get("data-match-id") or 0)
        except ValueError:
            mid = 0
        if not mid:
            continue
        end = cards[i + 1].start() if i + 1 < len(cards) else min(len(text), m.end() + 6000)
        seg = text[m.start():end]
        a_h = int(attrs["team1"]) if str(attrs.get("team1") or "").isdigit() else None
        b_h = int(attrs["team2"]) if str(attrs.get("team2") or "").isdigit() else None
        unix = _UNIX_RE.search(seg)
        href = re.search(r'href="(/matches/\d+/[^"]+)"', seg)
        entry: Dict[str, Any] = {"live": str(attrs.get("live") or "").lower() == "true", "start_at": _ts(unix.group(1)) if unix else None, "a_hltv": a_h, "b_hltv": b_h, "url": f"https://www.hltv.org{href.group(1)}" if href else None}
        if entry["live"]:
            map_scores = {int(t): int(s) for s, t in _MAP_SCORE_RE.findall(seg)}
            maps_won = {int(t): int(n) for t, n in _MAPS_WON_RE.findall(seg)}
            entry.update({"map_a": map_scores.get(a_h), "map_b": map_scores.get(b_h), "maps_a": maps_won.get(a_h), "maps_b": maps_won.get(b_h)})
        out[mid] = entry
    return out


def fetch_event_matches(hltv_event_id: int, hltv_event_url: str, to_tid: Callable[[Optional[int]], Optional[int]]) -> List[Dict[str, Any]]:
    """Read both pages now and return the merged match list (also cached by
    the caller). Raises on a fetch failure."""
    from backend.services.hltv_browser import fetch_hltv_html

    overview_url = str(hltv_event_url or "") or f"https://www.hltv.org/events/{int(hltv_event_id)}/-"
    overview = fetch_hltv_html(overview_url)
    matches_page = fetch_hltv_html(f"https://www.hltv.org/events/{int(hltv_event_id)}/matches")
    merged = parse_swiss(overview, to_tid)
    for mid, entry in parse_brackets(overview, to_tid).items():
        merged.setdefault(mid, entry)
    page = parse_matches_page(matches_page)
    for mid, info in page.items():
        entry = merged.get(mid)
        if entry is None:
            a, b = to_tid(info.get("a_hltv")), to_tid(info.get("b_hltv"))
            if not a or not b:
                continue
            entry = {"match_id": mid, "a": a, "b": b, "a_hltv": info.get("a_hltv"), "b_hltv": info.get("b_hltv"), "start_at": info.get("start_at"), "pool": None, "status": "upcoming", "url": info.get("url") or f"https://www.hltv.org/matches/{mid}/-"}
            merged[mid] = entry
        if info.get("url") and str(entry.get("url") or "").endswith("/-"):
            entry["url"] = info["url"]  # the page's real slug over the placeholder
        if entry["status"] != "finished":
            if info.get("live"):
                entry["status"] = "live"
                entry["maps_a"], entry["maps_b"] = info.get("maps_a"), info.get("maps_b")
                entry["map_a"], entry["map_b"] = info.get("map_a"), info.get("map_b")
            if info.get("start_at") and not entry.get("start_at"):
                entry["start_at"] = info["start_at"]
    return sorted(merged.values(), key=lambda e: (e.get("start_at") or 0.0, e["match_id"]))


def cached(event_id: int) -> List[Dict[str, Any]]:
    with _LOCK:
        return list((_CACHE.get(int(event_id)) or {}).get("matches") or [])


def cache_age(event_id: int) -> Optional[float]:
    with _LOCK:
        entry = _CACHE.get(int(event_id))
    return (time.time() - float(entry["fetched_at"])) if entry else None


def refresh_due(event_id: int, status: str, start_at: float, now: Optional[float] = None) -> bool:
    now = float(now or time.time())
    if status == "live":
        interval = LIVE_REFRESH
    elif status == "upcoming" and float(start_at) - now <= UPCOMING_WINDOW:
        interval = UPCOMING_REFRESH
    else:
        return False
    age = cache_age(event_id)
    return age is None or age >= interval


def refresh(event_id: int, hltv_event_id: int, hltv_event_url: str, to_tid: Callable[[Optional[int]], Optional[int]], wait: float = 90.0) -> List[Dict[str, Any]]:
    """Read the pages now (one reader per event at a time) and cache the
    result. While another thread is already reading this event's pages, wait
    for it (up to `wait` seconds) and return what it stored."""
    eid = int(event_id)
    deadline = time.time() + float(wait)
    while True:
        with _LOCK:
            if eid not in _INFLIGHT:
                _INFLIGHT.add(eid)
                break
        if time.time() >= deadline:
            return cached(eid)
        time.sleep(0.5)
        if cache_age(eid) is not None and cache_age(eid) < 5.0:
            return cached(eid)
    try:
        matches = fetch_event_matches(int(hltv_event_id), hltv_event_url, to_tid)
        with _LOCK:
            _CACHE[eid] = {"fetched_at": time.time(), "matches": matches}
        logger.info("Live pages read for event %s: %d matches (%d live)", eid, len(matches), sum(1 for m in matches if m["status"] == "live"))
        return matches
    except Exception:  # noqa: BLE001
        logger.warning("Live pages read failed for event %s", eid, exc_info=True)
        with _LOCK:
            if eid in _CACHE:
                _CACHE[eid]["fetched_at"] = time.time()  # try again after the interval, not on every request
            else:
                _CACHE[eid] = {"fetched_at": time.time(), "matches": []}
        return cached(eid)
    finally:
        with _LOCK:
            _INFLIGHT.discard(eid)


def refresh_in_background(event_id: int, hltv_event_id: int, hltv_event_url: str, to_tid: Callable[[Optional[int]], Optional[int]]) -> None:
    with _LOCK:
        if int(event_id) in _INFLIGHT:
            return
    threading.Thread(target=refresh, args=(int(event_id), int(hltv_event_id), hltv_event_url, to_tid, 0.0), name=f"live-pages-{event_id}", daemon=True).start()
