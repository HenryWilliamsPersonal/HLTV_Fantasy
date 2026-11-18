"""HLTV's per-map stats page (/stats/matches/mapstatsid/{id}/{slug}): the
total / CT / T tables of both teams, one row per player:
  Op. K-D (opening kills : deaths), MKs (multi-kill rounds), KAST, 1vsX
  (clutches won), K (hs), A (f) = assists (flash assists), D (t) = deaths
  (traded deaths), ADR, Swing, Rating 3.0.
The /stats/ pages need the app's browser (plain HTTP is refused)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_TABLE_RE = re.compile(r'<table class="stats-table (totalstats|ctstats|tstats)[^"]*">(.*?)</table>', re.S)
_ROW_RE = re.compile(r'<tr[^>]*>\s*<td class="st-player">(.*?)</tr>', re.S)
_PLAYER_RE = re.compile(r'href="/stats/players/(\d+)/[^"]*"[^>]*>([^<]+)</a>')


def _cell(row: str, cls: str) -> Optional[str]:
    m = re.search(rf'<td class="{cls}[^"]*">(.*?)</td>', row, re.S)
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip() if m else None


def _pair(text: Optional[str], sep: str):
    nums = re.findall(r"-?\d+(?:\.\d+)?", text or "")
    return [float(n) for n in nums[:2]] + [None] * (2 - len(nums[:2]))


def _num(text: Optional[str]) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


def map_name(html: str) -> Optional[str]:
    """The map's name from the match info box ('Map' then the name)."""
    m = re.search(r'class="match-info-box".*?>\s*Map\s*</span>\s*(?:<[^>]+>\s*)*([A-Za-z0-9 ]+?)\s*<', html or "", re.S)
    if m:
        return m.group(1).strip()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", html or ""))
    m = re.search(r"\| Map \| \| ([A-Za-z0-9 ]+?) \|", text)
    return m.group(1).strip() if m else None


def parse(html: str) -> Dict[str, List[Dict[str, Any]]]:
    """{'total' | 'ct' | 't': [row per player]} (both teams in each), plus 'map'."""
    out: Dict[str, Any] = {"total": [], "ct": [], "t": [], "map": map_name(html)}
    for kind, body in _TABLE_RE.findall(html or ""):
        key = {"totalstats": "total", "ctstats": "ct", "tstats": "t"}[kind]
        for row in _ROW_RE.findall(body):
            pm = _PLAYER_RE.search(row)
            if not pm:
                continue
            opk, opd = _pair(_cell(row, "st-opkd gtSmartphone-only traditional-data"), ":")
            k, hs = _pair(_cell(row, "st-kills traditional-data"), "(")
            a, f = _pair(_cell(row, "st-assists"), "(")
            d, t = _pair(_cell(row, "st-deaths traditional-data"), "(")
            kast = _cell(row, "st-kast gtSmartphone-only traditional-data")
            rating = _cell(row, "st-rating")
            out[key].append({
                "player_id": int(pm.group(1)), "name": pm.group(2).strip(),
                "opening_kills": opk, "opening_deaths": opd,
                "multi_kill_rounds": _num(_cell(row, "st-mks")),
                "kast": _num(kast),
                "clutches": _num(_cell(row, "st-clutches")),
                "kills": k, "hs_kills": hs, "assists": a, "flash_assists": f,
                "deaths": d, "traded_deaths": t,
                "rating": _num(rating),
            })
    return out


def match_ratings(match_html: str) -> Dict[int, Dict[str, Optional[float]]]:
    """{HLTV player id: {'total', 'ct', 't'}} Rating 3.0 over ALL maps of the
    match, from the match page's all-maps section (id="all-content"):
    Stathunter / Noob read the total, Attacker the T side, Defender the CT side."""
    html = match_html or ""
    i = html.find('id="all-content"')
    if i < 0:
        return {}
    seg = html[i:]
    j = seg.find('class="stats-content"', 10)  # the next section (map 1) starts here
    seg = seg[: j] if j > 0 else seg
    out: Dict[int, Dict[str, Optional[float]]] = {}
    for kind, body in re.findall(r'<table class="table (totalstats|ctstats|tstats)[^"]*">(.*?)</table>', seg, re.S):
        key = {"totalstats": "total", "ctstats": "ct", "tstats": "t"}[kind]
        for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            pm = re.search(r'href="/player/(\d+)/', row)
            rm = re.search(r'<td class="rating text-center[^"]*">\s*([\d.]+)', row)
            if pm and rm:
                out.setdefault(int(pm.group(1)), {})[key] = float(rm.group(1))
    return out
