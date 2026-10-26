import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Dict, List

from dateutil.relativedelta import relativedelta

from backend.services.hltv_browser import HLTVBrowserError, fetch_hltv_html


class HLTVRankingError(RuntimeError):
    """Base error for HLTV team ranking lookups."""


class TeamNotRankedError(HLTVRankingError):
    """Raised when a team is not present on the selected ranking date."""


class RankingPageParseError(HLTVRankingError):
    """Raised when HLTV ranking page format cannot be parsed."""


logger = logging.getLogger(__name__)


_TEXT_ENTRY_RE = re.compile(
    r"#\s*(?P<rank>\d+)\s+"
    r"(?P<name>[A-Za-z0-9 .'\-+&/]+?)\s*"
    r"\(\s*(?P<points>[\d,]+)\s+HLTV\s+points\s*\)",
    re.IGNORECASE,
)
_HLTV_TEAM_LINK_RE = re.compile(
    r'href=["\']/team/(?P<team_id>\d+)/(?P<slug>[^"\'>?#]+)',
    re.IGNORECASE,
)
_VRS_TEXT_ENTRY_RE = re.compile(
    r"#\s*(?P<rank>\d+)\s+"
    r"(?P<name>[^()#]+?)\s*"
    r"\(\s*(?P<points>[\d,]+)\s+(?:VRS|Valve)\s+points\s*\)",
    re.IGNORECASE,
)
_VRS_TEXT_ENTRY_FALLBACK_RE = re.compile(
    r"#\s*(?P<rank>\d+)\s+"
    r"(?P<name>[^()#]+?)\s+"
    r"(?P<points>[\d,]+)\s+(?:VRS|Valve)\s+points",
    re.IGNORECASE,
)


_RESULT_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>/matches/[^"]+)"[^>]*>(?P<body>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_EVENT_LINK_RE = re.compile(
    r'<a[^>]+href="/events/\d+/[^"]*"[^>]*>(?P<name>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_UNIX_ATTR_RE = re.compile(r"data-zonedgrouping-entry-unix\s*=\s*['\"](?P<ts>\d{10,13})['\"]", re.IGNORECASE)
_DATE_HEADER_RE = re.compile(
    r"(Results\s+for\s+[A-Za-z,\s]*\b[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+\d{4}|[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+\d{4})",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+\d{4}\b",
    re.IGNORECASE,
)
_MAP_LINE_RE = re.compile(
    r"\b(?P<map>Mirage|Inferno|Nuke|Ancient|Anubis|Dust2|Train|Overpass|Vertigo|Cache|Cobblestone)\b"
    r"[^0-9]{0,32}(?P<score1>\d{1,2})\s*[-:]\s*(?P<score2>\d{1,2})",
    re.IGNORECASE,
)
_HLTV_MAP_NAMES = {
    "mirage": "Mirage",
    "inferno": "Inferno",
    "nuke": "Nuke",
    "ancient": "Ancient",
    "anubis": "Anubis",
    "dust2": "Dust2",
    "dust 2": "Dust2",
    "de dust2": "Dust2",
    "train": "Train",
    "overpass": "Overpass",
    "vertigo": "Vertigo",
    "cache": "Cache",
    "cobblestone": "Cobblestone",
}
_TEAM_MAP_STATS_RE = re.compile(
    r"\b(?P<map>Mirage|Inferno|Nuke|Ancient|Anubis|Dust2|Train|Overpass|Vertigo|Cache|Cobblestone)\b\s+"
    r"Wins\s*/\s*draws\s*/\s*losses\s+"
    r"(?P<wins>\d+)\s*/\s*(?P<draws>\d+)\s*/\s*(?P<losses>\d+)\s+"
    r"Win\s+rate\s+(?P<win_rate>\d+(?:\.\d+)?)%\s+"
    r"Total\s+rounds\s+(?P<rounds>\d+).*?"
    r"Pick\s*%\s*(?P<pick>\d+(?:\.\d+)?)%\s+"
    r"Ban\s*%\s*(?P<ban>\d+(?:\.\d+)?)%",
    re.IGNORECASE | re.DOTALL,
)
_TEAM_MAP_NAME_RE = re.compile(
    r"\b(Mirage|Inferno|Nuke|Ancient|Anubis|Dust2|Train|Overpass|Vertigo|Cache|Cobblestone)\b",
    re.IGNORECASE,
)


def _build_hltv_ranking_url(on_date: date) -> str:
    month = on_date.strftime("%B").lower()
    return f"https://www.hltv.org/ranking/teams/{on_date.year}/{month}/{on_date.day}"


_HLTV_WORLD_RANKING_WEEKDAY = 0  # Monday


def _iter_hltv_world_ranking_probe_dates(
    on_or_before_date: date, max_days_back: int
) -> list[tuple[date, int]]:
    """
    HLTV world ranking archive snapshots are weekly Monday pages, not daily pages.
    Return candidate snapshot dates on or before the requested date within range.
    """
    days_since_ranking_update = (on_or_before_date.weekday() - _HLTV_WORLD_RANKING_WEEKDAY) % 7
    probe_date = on_or_before_date - timedelta(days=days_since_ranking_update)
    earliest_date = on_or_before_date - timedelta(days=max_days_back)
    probes: list[tuple[date, int]] = []

    while probe_date >= earliest_date:
        probes.append((probe_date, (on_or_before_date - probe_date).days))
        probe_date -= timedelta(days=7)

    return probes


def _build_hltv_ranking_url_undated() -> str:
    return "https://www.hltv.org/ranking/teams"


def _build_vrs_ranking_url(on_date: date) -> str:
    month = on_date.strftime("%B").lower()
    return f"https://www.hltv.org/valve-ranking/teams/{on_date.year}/{month}/{on_date.day}"


def slugify_hltv_team_name(name: str | None) -> str:
    raw = str(name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or "team"


def _build_hltv_team_maps_url(team_id: int, slug: str, start_date: str | None = None, end_date: str | None = None) -> str:
    url = f"https://www.hltv.org/stats/teams/maps/{int(team_id)}/{slugify_hltv_team_name(slug)}"
    if start_date and end_date:
        return f"{url}?startDate={start_date}&endDate={end_date}&csVersion=CS2"
    return url


def _build_vrs_ranking_url_undated() -> str:
    return "https://www.hltv.org/valve-ranking/teams"


def _build_hltv_team_url(team_id: int, slug: str) -> str:
    return f"https://www.hltv.org/team/{int(team_id)}/{slugify_hltv_team_name(slug) or 'team'}"


_TEAM_LINEUP_ANCHOR_RE = re.compile(
    r'<a[^>]*href="/player/(?P<player_id>\d+)/(?P<slug>[^"/?#]+)"[^>]*class="col-custom"[^>]*>(?P<inner>.*?)</a>',
    re.S,
)


def _extract_team_lineup(html: str) -> List[Dict[str, object]]:
    """The team page's current five-player lineup from its `bodyshot-team`
    block: one /player/{id}/{slug} anchor per starter (title = nickname), with
    an optional role pill (IGL / AWPer). Coaches and former players live in
    other blocks and are not included."""
    start = html.find('class="bodyshot-team')
    if start < 0:
        return []
    block = html[start : start + 20000]
    end = block.find('class="bodyshot-team-bg"', 10)
    if end > 0:
        block = block[:end]
    out: List[Dict[str, object]] = []
    seen: set[int] = set()
    for m in _TEAM_LINEUP_ANCHOR_RE.finditer(block):
        player_id = int(m.group("player_id"))
        if player_id in seen:
            continue
        seen.add(player_id)
        anchor = m.group(0)
        title = re.search(r'title="([^"]*)"', anchor)
        name = _strip_html(title.group(1)) if title else ""
        if not name:
            bold = re.search(r'class="text-ellipsis bold">([^<]*)<', m.group("inner"))
            name = _strip_html(bold.group(1)) if bold else m.group("slug")
        role = ""
        if "role-pill--igl" in m.group("inner"):
            role = "igl"
        elif "role-pill--awp" in m.group("inner"):
            role = "awp"
        out.append({"player_id": player_id, "name": name.strip(), "slug": m.group("slug"), "role": role})
        if len(out) >= 5:
            break
    return out


def get_hltv_team_lineup(hltv_team_id: int, team_name: str) -> Dict[str, object]:
    """Fetch a team's HLTV page and return its current lineup."""
    url = _build_hltv_team_url(int(hltv_team_id), team_name or "team")
    html = _fetch_html_with_uc_driver(url, wait_text=None)
    lineup = _extract_team_lineup(html)
    logger.info("HLTV team lineup parsed: team_id=%s players=%d url=%s", hltv_team_id, len(lineup), url)
    if not lineup:
        if _is_cloudflare_challenge_html(html):
            raise HLTVRankingError(
                "HLTV team page is behind a Cloudflare/interstitial challenge. "
                "Open once in visible browser mode and complete challenge, then retry."
            )
        # A team with no confirmed lineup shows five "?" silhouettes in the
        # grid: a real answer ("no current lineup"), not a parse failure.
        if 'class="bodyshot-team' in html and "player_silhouette" in html:
            return {"team_id": int(hltv_team_id), "team_name": team_name or "", "url": url, "lineup": [], "no_lineup": True}
        raise RankingPageParseError("Could not find the lineup on the HLTV team page.")
    return {"team_id": int(hltv_team_id), "team_name": team_name or "", "url": url, "lineup": lineup}


def _build_results_url(offset: int = 0) -> str:
    offset = max(0, int(offset))
    if offset <= 0:
        return "https://www.hltv.org/results"
    return f"https://www.hltv.org/results?offset={offset}"


def _is_cloudflare_challenge_html(html: str) -> bool:
    # The passive '/cdn-cgi/challenge-platform' script ships on every normal
    # page, so only genuine interstitial markers should count.
    html_l = (html or "").lower()
    if len(html_l) > 120000:
        return False
    return (
        "just a moment" in html_l
        or "cf-chl-" in html_l
        or "challenge-running" in html_l
        or 'id="challenge-form"' in html_l
        or "checking your browser before accessing" in html_l
    )


def _extract_hltv_team_ids_by_slug(html: str) -> Dict[str, int]:
    ids: Dict[str, int] = {}
    for match in _HLTV_TEAM_LINK_RE.finditer(html or ""):
        slug = unescape(match.group("slug") or "").strip().lower()
        if not slug:
            continue
        try:
            team_id = int(match.group("team_id"))
        except Exception:
            continue
        ids.setdefault(slug, team_id)
    return ids


def _extract_rankings(html: str) -> Dict[str, Dict[str, int | str]]:
    t0 = time.perf_counter()
    rankings: Dict[str, Dict[str, int | str]] = {}
    found_any = False
    html_l = html.lower()
    hltv_ids_by_slug = _extract_hltv_team_ids_by_slug(html)

    # Parse normalized visible text only. The old HTML-structure regex could
    # catastrophically backtrack and hang on modern HLTV pages.
    t_text0 = time.perf_counter()
    html_no_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html_no_scripts = re.sub(r"<style\b[^>]*>.*?</style>", " ", html_no_scripts, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html_no_scripts)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    logger.info("HLTV ranking text normalization complete in %.3fs", time.perf_counter() - t_text0)

    t_re0 = time.perf_counter()
    for match in _TEXT_ENTRY_RE.finditer(text):
        found_any = True
        name = unescape(match.group("name")).strip()
        rank = int(match.group("rank"))
        points = int(match.group("points").replace(",", ""))
        hltv_team_id = hltv_ids_by_slug.get(slugify_hltv_team_name(name))
        rankings[name.lower()] = {
            "team_name": name,
            "hltv_rating": rank,
            "points": points,
        }
        if hltv_team_id:
            rankings[name.lower()]["hltv_team_id"] = hltv_team_id
    logger.info("HLTV ranking text regex pass complete in %.3fs (teams=%d)", time.perf_counter() - t_re0, len(rankings))

    if not found_any:
        if "404" in html_l and "page not found" in html_l:
            raise RankingPageParseError("HLTV returned 404 page for ranking URL.")
        if "cloudflare" in html_l or "just a moment" in html_l or "/cdn-cgi/challenge-platform" in html_l:
            raise HLTVRankingError(
                "HLTV page is behind a Cloudflare/interstitial challenge. "
                "Open once in visible browser mode and complete challenge, then retry."
            )
        raise RankingPageParseError("Could not parse team ranking entries from HLTV page.")

    logger.info("HLTV ranking extraction complete in %.3fs (teams=%d)", time.perf_counter() - t0, len(rankings))
    return rankings


def _extract_vrs_rankings(html: str) -> Dict[str, Dict[str, int | str]]:
    rankings: Dict[str, Dict[str, int | str]] = {}
    html_l = html.lower()

    html_no_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html_no_scripts = re.sub(r"<style\b[^>]*>.*?</style>", " ", html_no_scripts, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html_no_scripts)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    for rx in (_VRS_TEXT_ENTRY_RE, _VRS_TEXT_ENTRY_FALLBACK_RE):
        for match in rx.finditer(text):
            name = unescape(match.group("name")).strip()
            rank = int(match.group("rank"))
            points = int(match.group("points").replace(",", ""))
            key = name.lower()
            # Keep first occurrence to prefer the global ranking block and
            # avoid later regional duplicates overwriting points.
            if key not in rankings:
                rankings[key] = {
                    "team_name": name,
                    "vrs_rank": rank,
                    "points": points,
                }
        if rankings:
            break

    if not rankings:
        if "404" in html_l and "page not found" in html_l:
            raise RankingPageParseError("HLTV returned 404 page for VRS ranking URL.")
        if "cloudflare" in html_l or "just a moment" in html_l or "/cdn-cgi/challenge-platform" in html_l:
            raise HLTVRankingError(
                "HLTV page is behind a Cloudflare/interstitial challenge. "
                "Open once in visible browser mode and complete challenge, then retry."
            )
        raise RankingPageParseError("Could not parse team VRS ranking entries from HLTV page.")

    return rankings


def _fetch_html_with_uc_driver(url: str, timeout_ms: int = 45000, wait_text: str | None = "HLTV points") -> str:
    try:
        return fetch_hltv_html(url, timeout_ms=timeout_ms, wait_text=wait_text)
    except HLTVBrowserError as exc:
        raise HLTVRankingError(str(exc)) from exc


def _strip_html(text: str) -> str:
    if not text:
        return ""
    out = _TAG_RE.sub(" ", text)
    out = unescape(out)
    out = _WS_RE.sub(" ", out).strip()
    return out


def _canonical_hltv_map_name(value: str) -> str:
    raw = unescape(str(value or "")).strip()
    key = raw.lower().replace("_", " ")
    return _HLTV_MAP_NAMES.get(key, raw)


def _class_blocks(html: str, class_token: str) -> List[str]:
    if not html:
        return []
    starts = [
        m.start()
        for m in re.finditer(
            rf'<[^>]+class=["\'][^"\']*\b{re.escape(class_token)}\b[^"\']*["\'][^>]*>',
            html,
            flags=re.IGNORECASE,
        )
    ]
    blocks = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(html)
        blocks.append(html[start:end])
    return blocks


def _first_class_text(html: str, class_token: str) -> str:
    m = re.search(
        rf'<[^>]+class=["\'][^"\']*\b{re.escape(class_token)}\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _strip_html(m.group(1)) if m else ""


def _extract_match_map_details(html: str) -> List[Dict[str, object]]:
    maps: List[Dict[str, object]] = []
    for block in _class_blocks(html, "mapholder"):
        map_name = _canonical_hltv_map_name(_first_class_text(block, "mapname"))
        if not map_name or map_name.lower() not in _HLTV_MAP_NAMES:
            continue
        scores = [
            int(x)
            for x in re.findall(
                r'<[^>]+class=["\'][^"\']*\bresults-team-score\b[^"\']*["\'][^>]*>\s*(\d{1,2})\s*</',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
        ]
        if len(scores) < 2:
            continue
        s1, s2 = int(scores[0]), int(scores[1])
        if s1 < 0 or s2 < 0 or s1 == s2:
            continue
        maps.append({"map": map_name, "score1": s1, "score2": s2})
    if maps:
        return maps

    # Markup fallback: only parse mapname blocks that also contain two nearby score nodes.
    for match in re.finditer(
        r'class=["\'][^"\']*\bmapname\b[^"\']*["\'][^>]*>(?P<map>.*?)</[^>]+>(?P<tail>.{0,2500}?results-team-score.*?results-team-score.*?)',
        html or "",
        flags=re.IGNORECASE | re.DOTALL,
    ):
        map_name = _canonical_hltv_map_name(_strip_html(match.group("map")))
        if not map_name or map_name.lower() not in _HLTV_MAP_NAMES:
            continue
        scores = [int(x) for x in re.findall(r"results-team-score[^>]*>\s*(\d{1,2})\s*</", match.group("tail"), flags=re.IGNORECASE)]
        if len(scores) < 2:
            continue
        s1, s2 = scores[0], scores[1]
        if s1 == s2:
            continue
        row = {"map": map_name, "score1": s1, "score2": s2}
        if not any(existing == row for existing in maps):
            maps.append(row)
    return maps


_HALF_SIDE_SPAN_RE = re.compile(
    r'<span[^>]*class=["\'](?P<side>ct|t)["\'][^>]*>\s*(?P<score>\d{1,2})\s*</span>',
    re.IGNORECASE,
)


def _attach_half_scores_to_maps(html: str, maps: List[Dict[str, object]]) -> None:
    """Best-effort: add per-half breakdowns from each mapholder's half-score line.

    HLTV renders e.g. `(<span class=t>6</span><span>:</span><span class=ct>6</span>; ...) (2:4)`
    where overtime pairs have no side class, so sides come from the classed spans
    and the plain text keeps the full breakdown including overtime.
    """
    blocks = _class_blocks(html, "mapholder")
    by_name: Dict[str, Dict[str, object]] = {str(m.get("map", "")).lower(): m for m in maps}
    for block in blocks:
        map_name = _canonical_hltv_map_name(_first_class_text(block, "mapname"))
        target = by_name.get(str(map_name or "").lower())
        if not target or "halves" in target:
            continue
        half_block = re.search(
            r'class=["\'][^"\']*\bresults-center-half-score\b[^"\']*["\'][^>]*>(?P<body>.*?)</div>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not half_block:
            continue
        body = half_block.group("body")
        sides = _HALF_SIDE_SPAN_RE.findall(body)
        halves = [
            {
                "side1": sides[i][0].lower(),
                "score1": int(sides[i][1]),
                "side2": sides[i + 1][0].lower(),
                "score2": int(sides[i + 1][1]),
            }
            for i in range(0, len(sides) - 1, 2)
        ]
        if halves:
            target["halves"] = halves
        text = re.sub(r"\s+", "", _strip_html(body))
        if text:
            target["half_score_text"] = text


_VETO_MAP_ALT = r"(?:Mirage|Inferno|Nuke|Ancient|Anubis|Dust\s?2|Train|Overpass|Vertigo|Cache|Cobblestone)"
_VETO_LINE_RE = re.compile(
    rf"(?P<order>\d+)\.\s+(?:(?P<leftover_map>{_VETO_MAP_ALT})\s+was\s+left\s+over"
    rf"|(?P<team>.{{1,40}}?)\s+(?P<action>picked|removed)\s+(?P<map>{_VETO_MAP_ALT}))\b",
    re.IGNORECASE,
)


def _extract_match_veto(html: str) -> List[Dict[str, object]]:
    """Parse the map veto sequence from a match page's veto box.

    Blocks can over-extend into unrelated page content, so per candidate block
    only a strictly 1,2,3,... increasing sequence is kept; the longest such
    sequence across blocks wins.
    """
    best: List[Dict[str, object]] = []
    for block in _class_blocks(html or "", "veto-box"):
        text = _strip_html(block)
        if "picked" not in text.lower() and "removed" not in text.lower():
            continue
        candidate: List[Dict[str, object]] = []
        expected_order = 1
        for m in _VETO_LINE_RE.finditer(text):
            order = int(m.group("order"))
            if order != expected_order:
                break
            if m.group("leftover_map"):
                map_name = _canonical_hltv_map_name(m.group("leftover_map").strip())
                if not map_name or map_name.lower() not in _HLTV_MAP_NAMES:
                    break
                candidate.append({"order": order, "team": None, "action": "leftover", "map": map_name})
            else:
                map_name = _canonical_hltv_map_name(m.group("map").strip())
                if not map_name or map_name.lower() not in _HLTV_MAP_NAMES:
                    break
                candidate.append(
                    {
                        "order": order,
                        "team": m.group("team").strip(),
                        "action": m.group("action").lower(),
                        "map": map_name,
                    }
                )
            expected_order += 1
        if len(candidate) > len(best):
            best = candidate
    return best


_MATCH_TEAM_LINK_RE = re.compile(r'href=["\']/team/\d+/[^"\']*["\'][^>]*>(?P<name>[^<]{1,60})<', re.IGNORECASE)
_MATCH_PLAYER_LINK_RE = re.compile(r'href=["\'][^"\']*/player/(?P<pid>\d+)/(?P<slug>[^"\'/?#]+)', re.IGNORECASE)


def _extract_match_player_stats(html: str) -> List[Dict[str, object]]:
    """Parse the all-maps per-player scoreboard (K-D, +/-, ADR, KAST, rating)."""
    source = html or ""
    all_content = re.search(
        r'<div[^>]*id=["\']all-content["\'].*?(?=<div[^>]*id=["\'][^"\']*-content["\']|$)',
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if all_content:
        source = all_content.group(0)
    return _parse_scoreboard_tables(source)


# The scoreboard's map tabs: <div class="{statsid} dynamic-map-name-full"
# id="{statsid}">Dust2</div>, with the matching table set in <div
# id="{statsid}-content">. "all" (non-numeric id) is the all-maps tab.
_MAP_TAB_NAME_RE = re.compile(
    r'class=["\'][^"\']*dynamic-map-name-full[^"\']*["\'][^>]*id=["\'](?P<sid>\d+)["\'][^>]*>(?P<name>[^<]{1,30})<',
    re.IGNORECASE,
)


def _extract_match_map_player_stats(html: str) -> Dict[str, List[Dict[str, object]]]:
    """Per-map player scoreboards, keyed by map name."""
    source = html or ""
    out: Dict[str, List[Dict[str, object]]] = {}
    for tab in _MAP_TAB_NAME_RE.finditer(source):
        map_name = _strip_html(tab.group("name")).strip()
        if not map_name or map_name in out:
            continue
        content = re.search(
            rf'<div[^>]*id=["\']{tab.group("sid")}-content["\'].*?(?=<div[^>]*id=["\'][^"\']*-content["\']|$)',
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not content:
            continue
        entries = _parse_scoreboard_tables(content.group(0))
        if entries:
            out[map_name] = entries
    return out


def _parse_scoreboard_tables(source: str) -> List[Dict[str, object]]:
    """Parse every totalstats scoreboard table in an HTML fragment."""
    players: List[Dict[str, object]] = []
    seen_ids: set[int] = set()
    for table_match in re.finditer(
        r"<table[^>]*class=[\"'][^\"']*totalstats[^\"']*[\"'].*?</table>",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        table = table_match.group(0)
        team_link = _MATCH_TEAM_LINK_RE.search(table)
        team_name = _strip_html(team_link.group("name")).strip() if team_link else ""

        for row_html in re.split(r"<tr[^>]*>", table)[1:]:
            player_link = _MATCH_PLAYER_LINK_RE.search(row_html)
            if not player_link:
                continue
            player_id = int(player_link.group("pid"))
            if player_id in seen_ids:
                continue

            def cell(cls: str) -> str:
                m = re.search(
                    rf'class=["\'][^"\']*\b{cls}\b[^"\']*["\'][^>]*>(?P<body>.*?)</td>',
                    row_html,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                return _strip_html(m.group("body")).strip() if m else ""

            kd = re.search(r"(\d+)\s*-\s*(\d+)", cell("kd"))
            adr = re.search(r"(\d+(?:\.\d+)?)", cell("adr"))
            kast = re.search(r"(\d+(?:\.\d+)?)", cell("kast"))
            rating = re.search(r"(\d+(?:\.\d+)?)", cell("rating"))
            # HLTV's +/- column is not always present as its own cell; it is kills - deaths.
            plus_minus = re.search(r"([+-]?\d+)", cell("plus-minus"))
            if not plus_minus and kd:
                plus_minus_value = int(kd.group(1)) - int(kd.group(2))
            else:
                plus_minus_value = int(plus_minus.group(1)) if plus_minus else None
            nick_match = re.search(
                r'class=["\'][^"\']*player-nick[^"\']*["\'][^>]*>(?P<nick>[^<]{1,40})<',
                row_html,
                flags=re.IGNORECASE,
            )
            nick = (nick_match.group("nick").strip() if nick_match else player_link.group("slug")).strip()
            if not (kd or rating):
                continue
            seen_ids.add(player_id)
            players.append(
                {
                    "team": team_name,
                    "player_id": player_id,
                    "player": nick,
                    "kills": int(kd.group(1)) if kd else None,
                    "deaths": int(kd.group(2)) if kd else None,
                    "plus_minus": plus_minus_value,
                    "adr": float(adr.group(1)) if adr else None,
                    "kast": float(kast.group(1)) if kast else None,
                    "rating": float(rating.group(1)) if rating else None,
                }
            )
    return players


def _pct_from_block(block: str, label: str) -> float | None:
    m = re.search(rf"{re.escape(label)}\s*%\s*(\d+(?:\.\d+)?)%", block, flags=re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1)) / 100.0


def _int_from_block(block: str, label: str) -> int | None:
    m = re.search(rf"{re.escape(label)}\s+(\d+)", block, flags=re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def _parse_team_map_block(map_name: str, block: str) -> dict | None:
    wdl = re.search(
        r"Wins\s*/\s*draws\s*/\s*losses\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)",
        block,
        flags=re.IGNORECASE,
    )
    win_rate_match = re.search(r"Win\s+rate\s+(\d+(?:\.\d+)?)%", block, flags=re.IGNORECASE)
    rounds = _int_from_block(block, "Total rounds")
    pick_rate = _pct_from_block(block, "Pick")
    ban_rate = _pct_from_block(block, "Ban")
    if pick_rate is None or ban_rate is None:
        return None
    if not wdl or not win_rate_match or rounds is None:
        # A map the team never played in the window still carries veto rates
        # (a permaban is exactly the map with played=0 and a high ban rate).
        return {
            "map": map_name,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "played": 0,
            "win_rate": 0.0,
            "total_rounds": 0,
            "pick_rate": pick_rate,
            "ban_rate": ban_rate,
        }
    wins = int(wdl.group(1))
    draws = int(wdl.group(2))
    losses = int(wdl.group(3))
    return {
        "map": map_name,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "played": wins + draws + losses,
        "win_rate": float(win_rate_match.group(1)) / 100.0,
        "total_rounds": rounds,
        "pick_rate": pick_rate,
        "ban_rate": ban_rate,
    }


def _parse_team_map_stats_text(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    seen = set()
    for match in _TEAM_MAP_STATS_RE.finditer(text):
        map_name = str(match.group("map") or "").strip()
        key = map_name.lower()
        if not map_name or key in seen:
            continue
        wins = int(match.group("wins"))
        draws = int(match.group("draws"))
        losses = int(match.group("losses"))
        rows.append(
            {
                "map": map_name,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "played": wins + draws + losses,
                "win_rate": float(match.group("win_rate")) / 100.0,
                "total_rounds": int(match.group("rounds")),
                "pick_rate": float(match.group("pick")) / 100.0,
                "ban_rate": float(match.group("ban")) / 100.0,
            }
        )
        seen.add(key)

    # No early return when the primary pattern matched: it only captures maps
    # with full played-stats, so the block scan below must still run to pick up
    # never-played maps that carry only pick/ban rates.
    overview_start = text.lower().find("map overview")
    team_context_start = text.lower().find("team context")
    scan = text[overview_start if overview_start >= 0 else 0 : team_context_start if team_context_start > overview_start else len(text)]
    map_matches = list(_TEAM_MAP_NAME_RE.finditer(scan))
    for idx, map_match in enumerate(map_matches):
        map_name = map_match.group(1)
        key = map_name.lower()
        if key in seen:
            continue
        next_start = map_matches[idx + 1].start() if idx + 1 < len(map_matches) else len(scan)
        block = scan[map_match.start() : next_start]
        row = _parse_team_map_block(map_name, block)
        if row:
            rows.append(row)
            seen.add(key)
    return rows


def _extract_block_by_class(html: str, class_token: str) -> str:
    """
    Best-effort extraction for a single div block by class token.
    """
    m = re.search(
        rf'<div[^>]*class="[^"]*\b{re.escape(class_token)}\b[^"]*"[^>]*>(.*?)</div>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return m.group(1) if m else ""


def _extract_results_entries(html: str) -> List[Dict[str, object]]:
    """Result cards on a /results page. A match can appear twice: in the
    featured "big results" block at the top (no day heading or stamp near it,
    so no date) and again in its day's list (dated). The dated occurrence
    wins; a match seen only in the featured block keeps match_date None for
    the importer to fill from the match page."""
    rows: List[Dict[str, object]] = []
    by_href: Dict[str, Dict[str, object]] = {}
    skipped_not_played = 0

    for m in _RESULT_LINK_RE.finditer(html):
        href = m.group("href")
        body = m.group("body") or ""
        if "result-score" not in body.lower():
            continue

        team1_block = _extract_block_by_class(body, "team1")
        team2_block = _extract_block_by_class(body, "team2")
        team1 = _strip_html(team1_block)
        team2 = _strip_html(team2_block)
        if not team1 or not team2:
            continue

        body_text = _strip_html(body)
        score1 = None
        score2 = None

        # First try generic score pattern across the full card text.
        m_score = re.search(r"(?<!\d)(\d{1,2})\s*[-:]\s*(\d{1,2})(?!\d)", body_text)
        if m_score:
            score1 = int(m_score.group(1))
            score2 = int(m_score.group(2))
        else:
            score_block = _extract_block_by_class(body, "result-score")
            score_vals = re.findall(r"(\d+)", _strip_html(score_block))
            score1 = int(score_vals[0]) if len(score_vals) >= 1 else None
            score2 = int(score_vals[1]) if len(score_vals) >= 2 else None

        # On /results, upcoming/unplayed cards often appear as 0-0 or without winner markers.
        # We only keep completed matches for training/evaluation datasets.
        body_l = body.lower()
        has_winner_marker = ("team1 won" in body_l) or ("team2 won" in body_l) or ("won" in body_l and "team" in body_l)
        if (
            score1 is None
            or score2 is None
            or (score1 == 0 and score2 == 0 and not has_winner_marker)
        ):
            skipped_not_played += 1
            continue

        winner = None
        if score1 is not None and score2 is not None:
            if score1 > score2:
                winner = team1
            elif score2 > score1:
                winner = team2
            else:
                winner = "Draw"

        event_block = (
            _extract_block_by_class(body, "event-name")
            or _extract_block_by_class(body, "event")
            or _extract_block_by_class(body, "match-event")
            or _extract_block_by_class(body, "matchEvent")
        )
        event_name = _strip_html(event_block)
        if not event_name:
            m_event = _EVENT_LINK_RE.search(body)
            if m_event:
                event_name = _strip_html(m_event.group("name"))
        if not event_name:
            # On HLTV /results, event labels are often outside the match anchor.
            # Use nearest preceding /events/... link as a contextual fallback.
            lookback = html[max(0, m.start() - 5000):m.start()]
            event_matches = list(_EVENT_LINK_RE.finditer(lookback))
            if event_matches:
                event_name = _strip_html(event_matches[-1].group("name"))

        match_date = None
        # Prefer unix timestamp embedded near the result card.
        local_window = html[max(0, m.start() - 12000): min(len(html), m.end() + 1500)]
        unix_matches = list(_UNIX_ATTR_RE.finditer(local_window))
        if unix_matches:
            raw_ts = int(unix_matches[-1].group("ts"))
            if raw_ts > 10_000_000_000:
                raw_ts = raw_ts // 1000
            try:
                match_date = datetime.fromtimestamp(raw_ts, tz=timezone.utc).date().isoformat()
            except Exception:
                match_date = None

        # Fallback to nearby date heading text.
        if not match_date:
            lookback = html[max(0, m.start() - 24000):m.start()]
            lookback_text = _strip_html(lookback)
            date_matches = list(_DATE_HEADER_RE.finditer(lookback_text))
            if date_matches:
                date_text = date_matches[-1].group(0)
                date_text = re.sub(r"^Results\s+for\s+", "", date_text, flags=re.IGNORECASE).strip()
                date_text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", date_text, flags=re.IGNORECASE)
                date_text = date_text.replace(",", "")
                for fmt in ("%B %d %Y", "%b %d %Y"):
                    try:
                        match_date = datetime.strptime(date_text, fmt).date().isoformat()
                        break
                    except Exception:
                        continue
            if not match_date:
                month_matches = list(_MONTH_DAY_YEAR_RE.finditer(lookback_text))
                if month_matches:
                    date_text = month_matches[-1].group(0)
                    date_text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", date_text, flags=re.IGNORECASE).replace(",", "")
                    for fmt in ("%B %d %Y", "%b %d %Y"):
                        try:
                            match_date = datetime.strptime(date_text, fmt).date().isoformat()
                            break
                        except Exception:
                            continue

        existing = by_href.get(href)
        if existing is not None:
            if match_date and not existing.get("match_date"):
                existing["match_date"] = match_date
            if event_name and not existing.get("event"):
                existing["event"] = event_name
            continue
        row = {
            "team1": team1,
            "team2": team2,
            "score1": score1,
            "score2": score2,
            "winner": winner,
            "event": event_name or None,
            "match_date": match_date,
            "match_url": f"https://www.hltv.org{href}",
        }
        rows.append(row)
        by_href[href] = row

    with_match_date = sum(1 for r in rows if r.get("match_date"))
    logger.info(
        "HLTV results parse: kept=%d with_match_date=%d missing_match_date=%d skipped_not_played=%d",
        len(rows),
        with_match_date,
        max(0, len(rows) - with_match_date),
        skipped_not_played,
    )
    return rows


def get_recent_hltv_results(limit: int = 100, offsets: List[int] | None = None) -> Dict[str, object]:
    limit = max(1, min(100000, int(limit)))
    out: List[Dict[str, object]] = []
    last_error: Exception | None = None

    # Try a few offsets if needed to reach requested volume.
    use_offsets = [int(x) for x in (offsets or [0, 100, 200])]
    for offset in use_offsets:
        if len(out) >= limit:
            break
        url = _build_results_url(offset)
        logger.info("HLTV results fetch start: url=%s", url)

        html = None
        try:
            html = _fetch_html_with_uc_driver(url, wait_text=None)
            logger.info("HLTV SeleniumBase UC results fetch succeeded: url=%s", url)
        except Exception as exc:
            logger.warning("HLTV SeleniumBase UC results fetch failed: url=%s error=%s", url, exc)
            last_error = exc
            continue

        try:
            parsed = _extract_results_entries(html)
            for row in parsed:
                row["source_offset"] = int(offset)
                if row.get("match_id") is None:
                    mm = re.search(r"/matches/(\d+)/", str(row.get("match_url") or ""))
                    if mm:
                        row["match_id"] = int(mm.group(1))
            if not parsed and _is_cloudflare_challenge_html(html):
                raise HLTVRankingError(
                    "HLTV results page is behind a Cloudflare/interstitial challenge. "
                    "Open once in visible browser mode and complete challenge, then retry."
                )
            logger.info("HLTV results parse complete: url=%s matches=%d", url, len(parsed))
            if parsed:
                out.extend(parsed)
        except Exception as parse_exc:
            last_error = parse_exc
            logger.warning("HLTV results parse failed: url=%s error=%s", url, parse_exc)
            continue

    dedup = []
    seen_urls = set()
    for r in out:
        u = r.get("match_url")
        if u in seen_urls:
            continue
        dedup.append(r)
        seen_urls.add(u)
        if len(dedup) >= limit:
            break

    if not dedup and last_error is not None:
        raise HLTVRankingError(f"Failed to fetch HLTV recent results: {last_error}") from last_error

    return {
        "source": "https://www.hltv.org/results",
        "count": len(dedup),
        "requested_limit": limit,
        "offsets": use_offsets,
        "results": dedup,
    }


def get_hltv_match_details(match_url: str) -> Dict[str, object]:
    url = str(match_url or "").strip()
    if not url:
        raise HLTVRankingError("match_url is required")
    if url.startswith("/matches/"):
        url = f"https://www.hltv.org{url}"
    if "/matches/" not in url:
        raise HLTVRankingError("Invalid HLTV match URL")

    html = None
    try:
        html = _fetch_html_with_uc_driver(url, wait_text=None)
    except Exception as exc:
        raise HLTVRankingError(f"Failed to fetch HLTV match details with SeleniumBase UC: {exc}") from exc

    details = parse_hltv_match_details_html(html, url)
    logger.info(
        "HLTV match details parsed: url=%s maps=%d veto=%d players=%d",
        url,
        len(details["maps"]),
        len(details["veto"]),
        len(details["player_stats"]),
    )
    return details


def parse_hltv_match_details_html(html: str, match_url: str = "") -> Dict[str, object]:
    """Run the match-page parsers over already-fetched HTML (e.g. a stored
    page snapshot), with no scraping involved."""
    maps = _extract_match_map_details(html or "")
    _attach_half_scores_to_maps(html or "", maps)
    veto = _extract_match_veto(html or "")
    player_stats = _extract_match_player_stats(html or "")
    map_player_stats = _extract_match_map_player_stats(html or "")
    return {
        "match_url": str(match_url or ""),
        "maps": maps,
        "veto": veto,
        "player_stats": player_stats,
        "map_player_stats": map_player_stats,
    }


def get_hltv_team_map_stats_for_range(
    team_id: int,
    team_name: str | None = None,
    *,
    start_date: str,
    end_date: str,
) -> Dict[str, object]:
    hltv_team_id = int(team_id)
    if hltv_team_id <= 0:
        raise HLTVRankingError("team_id must be a positive HLTV team id")
    slug = slugify_hltv_team_name(team_name)
    url = _build_hltv_team_maps_url(hltv_team_id, slug, start_date, end_date)
    try:
        html = _fetch_html_with_uc_driver(url, wait_text="Map overview")
    except Exception as exc:
        raise HLTVRankingError(f"Failed to fetch HLTV team map stats with SeleniumBase UC: {exc}") from exc

    text = _strip_html(html)
    rows = _parse_team_map_stats_text(text)
    logger.info("HLTV team map stats parsed: team_id=%s rows=%d url=%s", hltv_team_id, len(rows), url)

    if not rows:
        if _is_cloudflare_challenge_html(html):
            raise HLTVRankingError(
                "HLTV team map stats page is behind a Cloudflare/interstitial challenge. "
                "Open once in visible browser mode and complete challenge, then retry."
            )
        # The stats page rendered but lists no maps: the team played nothing
        # in this window. That is a result (an empty one), not a parse failure.
        if "Map overview" not in text:
            raise RankingPageParseError("Could not parse team map stats from HLTV page.")
        logger.info("HLTV team map stats empty window: team_id=%s url=%s", hltv_team_id, url)

    return {
        "team_id": hltv_team_id,
        "team_name": team_name or "",
        "url": url,
        "startDate": start_date,
        "endDate": end_date,
        "maps": rows,
    }


def get_hltv_team_map_stats(team_id: int, team_name: str | None = None) -> Dict[str, object]:
    """The team's current map stats over the last six months: the same window
    the win model trains on (backend.routes.events._historical_map_stats_window),
    so live features mean what the fitted weights expect."""
    end_d = date.today()
    start_date, end_date = (end_d - relativedelta(months=6)).isoformat(), end_d.isoformat()
    return get_hltv_team_map_stats_for_range(team_id, team_name, start_date=start_date, end_date=end_date)


def _get_rankings_map_for_date(
    on_date: date, allow_undated_fallback: bool = True
) -> tuple[Dict[str, Dict[str, int | str]], str]:
    last_error: Exception | None = None

    urls = [_build_hltv_ranking_url(on_date)]
    if allow_undated_fallback:
        urls.append(_build_hltv_ranking_url_undated())

    for url in urls:
        logger.info("HLTV ranking page fetch start: date=%s url=%s", on_date.isoformat(), url)

        html = None
        try:
            html = _fetch_html_with_uc_driver(url)
            logger.info("HLTV SeleniumBase UC fetch succeeded: date=%s url=%s", on_date.isoformat(), url)
        except Exception as exc:
            logger.warning("HLTV SeleniumBase UC fetch failed: date=%s url=%s error=%s", on_date.isoformat(), url, exc)
            last_error = exc
            continue

        try:
            rankings = _extract_rankings(html)
            if "/ranking/teams/" in url:
                logger.info("HLTV used dated ranking URL for date=%s", on_date.isoformat())
            else:
                logger.info("HLTV fallback to undated ranking URL for date=%s", on_date.isoformat())
            return rankings, url
        except RankingPageParseError as parse_exc:
            last_error = parse_exc
            logger.warning("HLTV ranking parse failed for url=%s: %s", url, parse_exc)
            continue

    if last_error is not None:
        raise HLTVRankingError(f"Failed to fetch HLTV rankings page: {last_error}") from last_error
    raise HLTVRankingError("Failed to fetch HLTV rankings page: unknown error")


def _get_vrs_rankings_map_for_date(
    on_date: date, allow_undated_fallback: bool = True
) -> tuple[Dict[str, Dict[str, int | str]], str]:
    last_error: Exception | None = None
    urls = [_build_vrs_ranking_url(on_date)]
    if allow_undated_fallback:
        urls.append(_build_vrs_ranking_url_undated())

    for url in urls:
        logger.info("VRS ranking page fetch start: date=%s url=%s", on_date.isoformat(), url)
        html = None
        try:
            html = _fetch_html_with_uc_driver(url, wait_text="points")
            logger.info("VRS SeleniumBase UC fetch succeeded: date=%s url=%s", on_date.isoformat(), url)
        except Exception as exc:
            logger.warning("VRS SeleniumBase UC fetch failed: date=%s url=%s error=%s", on_date.isoformat(), url, exc)
            last_error = exc
            continue

        try:
            rankings = _extract_vrs_rankings(html)
            if "/valve-ranking/teams/" in url:
                logger.info("VRS used dated ranking URL for date=%s", on_date.isoformat())
            else:
                logger.info("VRS fallback to undated ranking URL for date=%s", on_date.isoformat())
            return rankings, url
        except RankingPageParseError as parse_exc:
            last_error = parse_exc
            logger.warning("VRS ranking parse failed for url=%s: %s", url, parse_exc)
            continue

    if last_error is not None:
        raise HLTVRankingError(f"Failed to fetch VRS rankings page: {last_error}") from last_error
    raise HLTVRankingError("Failed to fetch VRS rankings page: unknown error")


def get_all_hltv_rankings_on_or_before_date(
    on_or_before_date: date, max_days_back: int = 7
) -> Dict[str, object]:
    """
    Fetch one HLTV ranking snapshot on or before the requested date and return all teams.
    """
    if max_days_back < 0:
        raise ValueError("max_days_back must be >= 0")

    for probe_date, days_back in _iter_hltv_world_ranking_probe_dates(on_or_before_date, max_days_back):
        try:
            rankings, url = _get_rankings_map_for_date(probe_date, allow_undated_fallback=False)
            if rankings:
                return {
                    "requested_date": on_or_before_date.isoformat(),
                    "effective_date": probe_date.isoformat(),
                    "days_back": days_back,
                    "url": url,
                    "rankings_by_team": rankings,
                }
        except RankingPageParseError:
            continue

    raise RankingPageParseError(
        f"Could not parse HLTV ranking entries from {on_or_before_date.isoformat()} "
        f"back {max_days_back} day(s)."
    )


def get_all_vrs_rankings_on_or_before_date(
    on_or_before_date: date, max_days_back: int = 7
) -> Dict[str, object]:
    if max_days_back < 0:
        raise ValueError("max_days_back must be >= 0")

    for days_back in range(max_days_back + 1):
        probe_date = on_or_before_date.fromordinal(on_or_before_date.toordinal() - days_back)
        try:
            rankings, url = _get_vrs_rankings_map_for_date(probe_date, allow_undated_fallback=False)
            if rankings:
                return {
                    "requested_date": on_or_before_date.isoformat(),
                    "effective_date": probe_date.isoformat(),
                    "days_back": days_back,
                    "url": url,
                    "rankings_by_team": rankings,
                }
        except RankingPageParseError:
            continue

    raise RankingPageParseError(
        f"Could not parse VRS ranking entries from {on_or_before_date.isoformat()} "
        f"back {max_days_back} day(s)."
    )


def get_latest_hltv_rankings() -> Dict[str, object]:
    """
    Fetch the latest HLTV team ranking snapshot from the undated ranking URL.
    """
    url = _build_hltv_ranking_url_undated()
    today = date.today().isoformat()
    logger.info("HLTV latest ranking fetch start: url=%s", url)

    html = None
    try:
        html = _fetch_html_with_uc_driver(url)
        logger.info("HLTV SeleniumBase UC fetch succeeded for latest ranking")
    except Exception as exc:
        logger.warning("HLTV SeleniumBase UC fetch failed for latest ranking: %s", exc)
        raise HLTVRankingError(f"Failed to fetch HLTV rankings page: {exc}") from exc

    rankings = _extract_rankings(html)
    logger.info("HLTV latest ranking parse succeeded: teams=%d", len(rankings))
    return {
        "requested_date": today,
        "effective_date": "latest",
        "days_back": 0,
        "url": url,
        "rankings_by_team": rankings,
    }


def get_latest_vrs_rankings() -> Dict[str, object]:
    url = _build_vrs_ranking_url_undated()
    today = date.today().isoformat()
    logger.info("VRS latest ranking fetch start: url=%s", url)

    html = None
    try:
        html = _fetch_html_with_uc_driver(url, wait_text="points")
        logger.info("VRS SeleniumBase UC fetch succeeded for latest ranking")
    except Exception as exc:
        logger.warning("VRS SeleniumBase UC fetch failed for latest ranking: %s", exc)
        raise HLTVRankingError(f"Failed to fetch VRS rankings page: {exc}") from exc

    rankings = _extract_vrs_rankings(html)
    logger.info("VRS latest ranking parse succeeded: teams=%d", len(rankings))
    return {
        "requested_date": today,
        "effective_date": "latest",
        "days_back": 0,
        "url": url,
        "rankings_by_team": rankings,
    }


def get_team_hltv_rating_and_points_on_date(team_name: str, on_date: date) -> Dict[str, int | str]:
    normalized_team = team_name.strip().lower()
    if not normalized_team:
        raise ValueError("team_name is required")

    rankings, url = _get_rankings_map_for_date(on_date)
    data = rankings.get(normalized_team)
    if not data:
        raise TeamNotRankedError(f"Team '{team_name}' was not found in HLTV ranking for {on_date.isoformat()}.")

    return {
        "team_name": str(data["team_name"]),
        "date": on_date.isoformat(),
        "url": url,
        "hltv_rating": int(data["hltv_rating"]),
        "points": int(data["points"]),
        "hltv_team_id": int(data["hltv_team_id"]) if data.get("hltv_team_id") else None,
    }


def get_team_vrs_rank_and_points_on_date(team_name: str, on_date: date) -> Dict[str, int | str]:
    normalized_team = team_name.strip().lower()
    if not normalized_team:
        raise ValueError("team_name is required")

    rankings, url = _get_vrs_rankings_map_for_date(on_date)
    data = rankings.get(normalized_team)
    if not data:
        raise TeamNotRankedError(f"Team '{team_name}' was not found in VRS ranking for {on_date.isoformat()}.")

    return {
        "team_name": str(data["team_name"]),
        "date": on_date.isoformat(),
        "url": url,
        "vrs_rank": int(data["vrs_rank"]),
        "points": int(data["points"]),
    }
