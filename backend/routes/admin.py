import json
import logging
import os
import re
import math
import threading
import time
import uuid
from html import unescape
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from backend.data.db_admin import wipe_database
from backend.data.event_db import (
    get_event_detail,
    get_event_price_map,
    set_active_event,
    set_event_hltv_ref,
    upsert_event_snapshot,
)
from backend.data.player_db import add_or_update_player, get_player
from backend.data.team_db import add_or_update_team, get_team_by_name
from backend.services.hltv_browser import (
    HLTVBrowserError,
    _kill_orphan_chrome,
    close_hltv_browser,
    fetch_hltv_html,
    kill_own_uc_drivers,
    run_hltv_browser_session,
)
from backend.services import hltv_session
from backend.swiss_stage.fantasy_scoring import (
    compute_rating_points,
    compute_role_points,
    compute_win_points,
    compute_booster_points,
)
from backend.swiss_stage.swiss_models import PlayerState

router = APIRouter()

# Maps from our internal booster/role indices to HLTV IDs used in triggerRates JSON
BOOSTER_SOURCE_IDS = [
    2, 3, 5, 8, 9, 13, 16, 18, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30
]
ROLE_SOURCE_IDS = [
    0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13
]

HLTV_EVENT_URL_RE = re.compile(r"^https?://(?:www\.)?hltv\.org/events/\d+/[A-Za-z0-9_-]+(?:#simulator)?/?$")
HLTV_EVENT_LINK_RE = re.compile(r"(?:https?://(?:www\.)?hltv\.org)?(?P<path>/events/(?P<id>\d+)/(?:[A-Za-z0-9_-]+))", re.IGNORECASE)
HLTV_MATCH_LINE_RE = re.compile(
    r"(?P<date>\d{2}/\d{2}/\d{2})\s+"
    r"(?P<team_a>.+?)\s+"
    r"(?P<time>\d{1,2}:\d{2})\s+"
    r"(?P<team_b>.+?)\s+"
    r"(?P<format>bo\d)\b",
    re.IGNORECASE,
)
HLTV_SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"'](?P<src>[^\"']+\.js[^\"']*)[\"']", re.IGNORECASE)
HLTV_TEAM_RANK_RE = re.compile(r"^\s*(?P<team>.+?)\s+#\d+\s+#(?P<rank>\d+)\s*$")
HLTV_TEAM_LINK_RE = re.compile(r"<a\b[^>]*href=[\"']/team/\d+/[^\"']+[\"'][^>]*>(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL)
HLTV_IMAGE_NAME_RE = re.compile(r"<img\b[^>]*(?:alt|title)=[\"'](?P<name>[^\"']+)[\"'][^>]*>", re.IGNORECASE)

def _html_to_lines(html: str) -> List[str]:
    text = re.sub(r"(?is)<script\b.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style\b.*?</style>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(?:div|p|li|tr|td|th|h[1-6]|section|article|a)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def _normalise_hltv_event_url(path_or_url: str) -> str:
    raw = str(path_or_url or "").strip()
    if not raw:
        return ""
    match = HLTV_EVENT_LINK_RE.search(raw)
    if match:
        return f"https://www.hltv.org{match.group('path')}"
    return raw


def _extract_hltv_event_ref(value: Any) -> Dict[str, Any]:
    stack = [value]
    seen = set()
    while stack:
        current = stack.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(current, str):
            match = HLTV_EVENT_LINK_RE.search(current)
            if match:
                return {
                    "hltv_event_id": int(match.group("id")),
                    "hltv_event_url": _normalise_hltv_event_url(match.group("path")),
                }
        elif isinstance(current, dict):
            for key, child in current.items():
                key_text = str(key or "")
                if isinstance(child, str):
                    match = HLTV_EVENT_LINK_RE.search(child)
                    if match:
                        return {
                            "hltv_event_id": int(match.group("id")),
                            "hltv_event_url": _normalise_hltv_event_url(match.group("path")),
                        }
                if isinstance(child, (dict, list)):
                    stack.append(child)
                elif isinstance(child, (int, float)) and re.search(r"hltv.*event|event.*hltv", key_text, re.IGNORECASE):
                    return {"hltv_event_id": int(child), "hltv_event_url": ""}
        elif isinstance(current, list):
            stack.extend(current)
    return {}


# The fantasy overview's masthead names its event as a link INSIDE the header
# h1 — a deterministic element, unlike the nav dropdown's links to whatever
# events the page shell advertises (grabbing "the first /events/ link" once
# mislinked the Beijing qualifiers to BLAST Porto).
_FANTASY_MASTHEAD_EVENT_RE = re.compile(
    r'<h1 class="text-ellipsis">\s*<a[^>]*href="(/events/(\d+)/[^"]+)"'
)


def _extract_event_ref_from_fantasy_overview(html: str) -> Dict[str, Any]:
    m = _FANTASY_MASTHEAD_EVENT_RE.search(html or "")
    if not m:
        return {}
    return {
        "hltv_event_id": int(m.group(2)),
        "hltv_event_url": f"https://www.hltv.org{m.group(1)}",
    }


def _extract_hltv_event_ref_from_fantasy(fantasy_event_id: int, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """HLTV event ref: from the fantasy JSON when present, else from the
    fantasy overview page's masthead link (strict element match — never a
    scan for arbitrary /events/ links)."""
    if data:
        ref = _extract_hltv_event_ref(data)
        if ref.get("hltv_event_id"):
            return ref
    try:
        html = fetch_hltv_html(f"https://www.hltv.org/fantasy/{int(fantasy_event_id)}/overview")
    except HLTVBrowserError:
        return {}
    return _extract_event_ref_from_fantasy_overview(html)


def _ordered_unique(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _inspect_hltv_simulator_html(url: str, html: str) -> Dict[str, Any]:
    lines = _html_to_lines(html)
    line_text = "\n".join(lines)

    script_assets = []
    for match in HLTV_SCRIPT_SRC_RE.finditer(html or ""):
        src = match.group("src")
        if src.startswith("/"):
            src = f"https://www.hltv.org{src}"
        script_assets.append(src)

    format_lines = []
    for idx, line in enumerate(lines):
        lower = line.lower()
        if lower == "formats":
            format_lines.extend(lines[idx + 1 : idx + 4])
        elif "group stage swiss" in lower or "progression matches" in lower or "elimination matches" in lower:
            format_lines.append(line)
    format_lines = _ordered_unique(format_lines)

    buckets = _ordered_unique(re.findall(r"\b[0-3]:[0-3]\b", line_text))
    matchups = []
    seen_matchups = set()
    for line in lines:
        match = HLTV_MATCH_LINE_RE.search(line)
        if not match:
            continue
        team_a = match.group("team_a").strip()
        team_b = match.group("team_b").strip()
        key = tuple(sorted((team_a.lower(), team_b.lower()))) + (match.group("date"),)
        if key in seen_matchups:
            continue
        seen_matchups.add(key)
        matchups.append(
            {
                "date": match.group("date"),
                "time": match.group("time"),
                "team_a": team_a,
                "team_b": team_b,
                "format": match.group("format").lower(),
            }
        )

    return {
        "url": url,
        "simulator_visible": "Simulator" in lines and "0:0" in buckets,
        "format_lines": format_lines,
        "swiss_buckets": buckets,
        "initial_matchups": matchups,
        "script_assets": script_assets,
        "script_asset_count": len(script_assets),
        "notes": [
            "This inspects the rendered HLTV event page. It can confirm visible simulator data, format text, buckets, and initial pairings.",
            "If HLTV keeps pairing generation in client JS, inspect the listed script assets in browser devtools for the exact algorithm.",
            "If pairings are generated server-side, use this as a behavior-capture starting point and compare generated pairings after manual simulator clicks.",
        ],
    }


def _extract_team_ranks_from_html(html: str) -> Dict[str, int]:
    ranks: Dict[str, int] = {}
    for line in _html_to_lines(html):
        match = HLTV_TEAM_RANK_RE.match(line)
        if not match:
            continue
        team = re.sub(r"\s+", " ", match.group("team")).strip()
        if team and not team.lower().startswith(("player share", "club share")):
            ranks[team] = int(match.group("rank"))
    return ranks


def _strip_html(value: str) -> str:
    text = re.sub(r"(?is)<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _is_plausible_team_name(name: str) -> bool:
    clean = (name or "").strip()
    if len(clean) < 2 or len(clean) > 40:
        return False
    lower = clean.lower()
    blocked = {
        "overview",
        "matches",
        "fantasy",
        "news",
        "results",
        "ranking",
        "players",
        "events",
        "image",
    }
    return lower not in blocked and not lower.startswith(("image:", "logo", "flag"))


def _extract_event_team_names_from_html(html: str) -> List[str]:
    names: List[str] = []
    for match in HLTV_TEAM_LINK_RE.finditer(html or ""):
        clean = _strip_html(match.group("body"))
        if _is_plausible_team_name(clean):
            names.append(clean)
    for match in HLTV_IMAGE_NAME_RE.finditer(html or ""):
        clean = _strip_html(match.group("name"))
        if _is_plausible_team_name(clean):
            names.append(clean)
    return _ordered_unique(names)


def _extract_swiss_simulator_data_from_html(html: str) -> Dict[str, Any] | None:
    match = re.search(r'data-swiss-simulator-json="(?P<json>[^"]+)"', html or "")
    if not match:
        return None
    try:
        return json.loads(unescape(match.group("json")))
    except Exception:
        return None


def _team_slot_key(team_id: Dict[str, Any]) -> str:
    slot = (team_id or {}).get("eventTeamSlotId") or {}
    value = slot.get("eventTeamSlotId")
    if value is not None:
        return f"slot:{value}"
    team = (team_id or {}).get("teamId") or {}
    value = team.get("teamId")
    if value is not None:
        return f"team:{value}"
    return ""


def _swiss_team_name_lookup(simulator_data: Dict[str, Any]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for team in simulator_data.get("teams") or []:
        name = str(team.get("teamName") or "").strip()
        if not name:
            continue
        team_id = team.get("teamId") or {}
        slot_key = _team_slot_key(team_id)
        if slot_key:
            lookup[slot_key] = name
        raw_team_id = ((team_id.get("teamId") or {}).get("teamId"))
        if raw_team_id is not None:
            lookup[f"team:{raw_team_id}"] = name
    return lookup


def _swiss_team_logos(simulator_data: Dict[str, Any]) -> Dict[str, str]:
    logos: Dict[str, str] = {}
    for team in simulator_data.get("teams") or []:
        name = str(team.get("teamName") or "").strip()
        logo = team.get("teamLogo") or {}
        url = str(logo.get("nightLogoURL") or logo.get("dayLogoURL") or "").strip()
        if name and url:
            logos[name] = url
    return logos


def _swiss_team_name(team_id: Dict[str, Any], lookup: Dict[str, str]) -> str:
    slot_key = _team_slot_key(team_id)
    if slot_key and slot_key in lookup:
        return lookup[slot_key]
    raw_team_id = ((team_id or {}).get("teamId") or {}).get("teamId")
    if raw_team_id is not None:
        return lookup.get(f"team:{raw_team_id}", "")
    return ""


def _swiss_score_label(score: str) -> str:
    mapping = {
        "ZeroZero": "0:0",
        "OneZero": "1:0",
        "ZeroOne": "0:1",
        "TwoZero": "2:0",
        "OneOne": "1:1",
        "ZeroTwo": "0:2",
        "ThreeZero": "3:0",
        "TwoOne": "2:1",
        "OneTwo": "1:2",
        "ZeroThree": "0:3",
        "ThreeOne": "3:1",
        "ThreeTwo": "3:2",
        "TwoTwo": "2:2",
        "OneThree": "1:3",
        "TwoThree": "2:3",
    }
    return mapping.get(str(score or ""), str(score or ""))


def _swiss_matches_by_bucket(simulator_data: Dict[str, Any], lookup: Dict[str, str]) -> Dict[str, List[Dict[str, str]]]:
    buckets: Dict[str, List[Dict[str, str]]] = {}
    for raw_bucket, matches in (simulator_data.get("matches") or {}).items():
        label = _swiss_score_label(raw_bucket)
        bucket_pairs = []
        for match in matches or []:
            a = _swiss_team_name(match.get("team1") or {}, lookup)
            b = _swiss_team_name(match.get("team2") or {}, lookup)
            if a and b:
                bucket_pairs.append({"team_a": a, "team_b": b})
        buckets[label] = bucket_pairs
    return buckets


def _teams_from_pairs(pairs: List[Dict[str, str]]) -> List[str]:
    teams = []
    for pair in pairs:
        for key in ("team_a", "team_b"):
            name = str(pair.get(key) or "").strip()
            if name:
                teams.append(name)
    return _ordered_unique(teams)


def _bucket_teams_in_initial_order(
    initial_pairs: List[Dict[str, str]],
    bucket_pairs: List[Dict[str, str]],
) -> List[str]:
    bucket_team_set = {name.lower() for name in _teams_from_pairs(bucket_pairs)}
    ordered = []
    for pair in initial_pairs:
        for key in ("team_a", "team_b"):
            name = str(pair.get(key) or "").strip()
            if name and name.lower() in bucket_team_set:
                ordered.append(name)
    return _ordered_unique(ordered)


def _swiss_rule_label(simulator_data: Dict[str, Any]) -> str:
    successor = simulator_data.get("successorRoundRules")
    if isinstance(successor, dict):
        raw = str(successor.get("type") or "")
        if raw:
            return raw.rsplit(".", 1)[-1]
    if successor:
        return str(successor)
    return "unknown"


def _probe_from_swiss_simulator_data(simulator_data: Dict[str, Any], url: str) -> Dict[str, Any]:
    lookup = _swiss_team_name_lookup(simulator_data)
    logos = _swiss_team_logos(simulator_data)
    seedings = simulator_data.get("initialSeedings") or {}
    ranks: Dict[str, int] = {}
    for row in seedings.get("orderedSeedings") or []:
        name = _swiss_team_name(row.get("teamId") or {}, lookup)
        if name:
            ranks[name] = int(row.get("seedingOrdinal") or len(ranks) + 1)

    team_names = [
        name
        for name, _rank in sorted(ranks.items(), key=lambda item: (item[1], item[0].lower()))
    ]
    if not team_names:
        team_names = [str(team.get("teamName") or "").strip() for team in simulator_data.get("teams") or []]
        team_names = [name for name in team_names if name]

    matches_by_bucket = _swiss_matches_by_bucket(simulator_data, lookup)
    first_round = matches_by_bucket.get("0:0") or []
    recorded_after = {
        "1:0": matches_by_bucket.get("1:0") or [],
        "0:1": matches_by_bucket.get("0:1") or [],
    }
    winners = _bucket_teams_in_initial_order(first_round, recorded_after["1:0"])
    losers = _bucket_teams_in_initial_order(first_round, recorded_after["0:1"])
    successor_rule = _swiss_rule_label(simulator_data)
    first_round_rule = str(simulator_data.get("firstRoundRules") or "unknown")
    probe = {
        "ok": True,
        "url": url,
        "ranks": ranks,
        "rank_source": "hltv_simulator_initial_seedings",
        "parsed_rank_count": len(ranks),
        "extracted_team_count": len(team_names),
        "extracted_team_names": team_names,
        "team_names": team_names,
        "team_logos": logos,
        "event_name": str(simulator_data.get("eventShortName") or simulator_data.get("eventName") or "").strip(),
        "first_round_rule": first_round_rule,
        "successor_round_rule": successor_rule,
        "scenarios": [
            {
                "name": "hltv_recorded_round_2",
                "initial": first_round,
                "winners": winners,
                "losers": losers,
                "clicks": [],
                "clicks_succeeded": 0,
                "advance_result": {
                    "clicked": [],
                    "note": "Scored from HLTV's embedded Swiss simulator JSON instead of click probing, because this event already has played round-one results.",
                },
                "after": recorded_after,
                "source": "data-swiss-simulator-json",
            }
        ],
    }
    probe["inference"] = _infer_pairing_rule_from_probe(probe, ranks)
    if str(successor_rule).lower() == "buchholz":
        for candidate in probe["inference"].get("candidates") or []:
            if candidate.get("mode") == "rank_strong_weak":
                candidate["label"] = "Buchholz order, seed tiebreak, pair strongest vs weakest"
        best = probe["inference"].get("best")
        if best and best.get("mode") == "rank_strong_weak":
            best["label"] = "Buchholz order, seed tiebreak, pair strongest vs weakest"
    return probe


def _seed_ranks_from_initial_pairs(initial_pairs: List[Dict[str, str]]) -> Dict[str, int]:
    first_half = []
    second_half = []
    for pair in initial_pairs:
        a = str(pair.get("team_a") or "").strip()
        b = str(pair.get("team_b") or "").strip()
        if a and b:
            first_half.append(a)
            second_half.append(b)
    return {name: idx + 1 for idx, name in enumerate(first_half + second_half)}


def _pair_key(pair: Dict[str, str]) -> tuple[str, str]:
    a = str(pair.get("team_a") or "").strip().lower()
    b = str(pair.get("team_b") or "").strip().lower()
    return tuple(sorted((a, b)))


def _candidate_pairings(teams: List[str], ranks: Dict[str, int], mode: str) -> List[Dict[str, str]]:
    if len(teams) % 2 != 0:
        return []

    def seed_key(name: str) -> tuple[int, str]:
        return (int(ranks.get(name, 9999)), name.lower())

    ordered = list(teams)
    if mode in {"rank_adjacent", "rank_high_low", "rank_strong_weak"}:
        ordered = sorted(teams, key=seed_key)

    pairs = []
    if mode in {"rank_adjacent", "initial_order_adjacent"}:
        for idx in range(0, len(ordered), 2):
            pairs.append({"team_a": ordered[idx], "team_b": ordered[idx + 1]})
    elif mode in {"rank_high_low", "initial_order_high_low"}:
        half = len(ordered) // 2
        for idx in range(half):
            pairs.append({"team_a": ordered[idx], "team_b": ordered[idx + half]})
    elif mode == "rank_strong_weak":
        for idx in range(len(ordered) // 2):
            pairs.append({"team_a": ordered[idx], "team_b": ordered[len(ordered) - 1 - idx]})
    return pairs


def _score_candidate(observed: List[Dict[str, str]], predicted: List[Dict[str, str]]) -> Dict[str, Any]:
    observed_set = {_pair_key(pair) for pair in observed}
    predicted_set = {_pair_key(pair) for pair in predicted}
    matched = len(observed_set & predicted_set)
    total = max(len(observed_set), 1)
    return {
        "matched": matched,
        "total": len(observed_set),
        "score": matched / total,
        "missing": [
            {"team_a": a, "team_b": b}
            for a, b in sorted(observed_set - predicted_set)
            if a and b
        ],
        "extra": [
            {"team_a": a, "team_b": b}
            for a, b in sorted(predicted_set - observed_set)
            if a and b
        ],
    }


def _infer_pairing_rule_from_probe(probe: Dict[str, Any], ranks: Dict[str, int]) -> Dict[str, Any]:
    scenarios = probe.get("scenarios") or []
    modes = [
        ("rank_high_low", "Sort by rank, pair top half vs bottom half"),
        ("rank_adjacent", "Sort by rank, pair adjacent teams"),
        ("rank_strong_weak", "Sort by rank, pair strongest vs weakest"),
        ("initial_order_high_low", "Use previous match order, pair top half vs bottom half"),
        ("initial_order_adjacent", "Use previous match order, pair adjacent teams"),
    ]
    candidates = []
    for mode, label in modes:
        matched = 0
        total = 0
        details = []
        for scenario in scenarios:
            for bucket_key, teams_key in (("1:0", "winners"), ("0:1", "losers")):
                observed = scenario.get("after", {}).get(bucket_key) or []
                teams = scenario.get(teams_key) or []
                predicted = _candidate_pairings(teams, ranks, mode)
                score = _score_candidate(observed, predicted)
                matched += score["matched"]
                total += score["total"]
                details.append(
                    {
                        "scenario": scenario.get("name"),
                        "bucket": bucket_key,
                        "observed": observed,
                        "predicted": predicted,
                        **score,
                    }
                )
        candidates.append(
            {
                "mode": mode,
                "label": label,
                "matched": matched,
                "total": total,
                "score": (matched / total) if total else 0.0,
                "details": details,
            }
        )
    candidates.sort(key=lambda row: (-row["score"], -row["matched"], row["mode"]))
    return {
        "best": candidates[0] if candidates else None,
        "candidates": candidates,
    }


SIMULATOR_SNAPSHOT_JS = r"""
const teamNames = arguments[0] || [];
const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
const byNorm = new Map(teamNames.map((name) => [normalize(name), name]));
const isTeam = (value) => byNorm.has(normalize(value));
const teamName = (value) => byNorm.get(normalize(value));

function visible(el) {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
}

function teamsIn(el) {
  const names = [];
  el.querySelectorAll("img[alt], img[title], [aria-label], [title]").forEach((node) => {
    const values = [
      node.getAttribute("alt"),
      node.getAttribute("title"),
      node.getAttribute("aria-label"),
    ];
    values.forEach((value) => {
      if (isTeam(value)) names.push(teamName(value));
    });
  });
  (el.innerText || "").split(/\n+/).forEach((line) => {
    const clean = line.replace(/\s+/g, " ").trim();
    if (isTeam(clean)) names.push(teamName(clean));
  });
  return [...new Set(names)];
}

function simulatorRoot() {
  const roots = [...document.querySelectorAll("div, section, main")]
    .filter((el) => {
      const text = el.innerText || "";
      if (!scoreLabelInText(text, "0:0") || !scoreLabelInText(text, "3:2") || !scoreLabelInText(text, "2:3")) return false;
      return teamsIn(el).length >= 8;
    })
    .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
  return roots[0] || document.body;
}

function scoreLabelInText(text, label) {
  const [wins, losses] = label.split(":");
  const pattern = new RegExp(`(^|[^0-9])${wins}\\s*[:-]\\s*${losses}([^0-9]|$)`);
  return pattern.test(text || "");
}

function pairElements(root) {
  const candidates = [];
  [...root.querySelectorAll("div, a, button, span, li")].forEach((el, idx) => {
    if (!visible(el)) return;
    const found = teamsIn(el);
    if (found.length !== 2) return;
    const text = (el.innerText || "").replace(/\s+/g, " ").trim();
    const hasVs = /\bvs\b/i.test(text);
    const rect = el.getBoundingClientRect();
    if (!hasVs && rect.width > 260) return;
    if (text.length > 180) return;
    candidates.push({ el, idx, text, found, area: rect.width * rect.height, y: rect.top, x: rect.left });
  });

  const byPair = new Map();
  candidates
    .sort((a, b) => a.text.length - b.text.length || a.area - b.area || a.idx - b.idx)
    .forEach((item) => {
      const key = [...item.found].map(normalize).sort().join("||");
      if (!byPair.has(key)) byPair.set(key, item);
    });

  return [...byPair.values()].sort((a, b) => a.y - b.y || a.x - b.x);
}

function bucketElements(root) {
  const buckets = {};
  const bucketLabels = ["0:0", "1:0", "0:1", "2:0", "1:1", "0:2", "3:0", "2:1", "1:2", "0:3", "3:1", "3:2", "2:2", "1:3", "2:3"];
  [...root.querySelectorAll("div, section, article, li")].forEach((el, idx) => {
    if (!visible(el)) return;
    const text = (el.innerText || "").replace(/\s+/g, " ").trim();
    const found = teamsIn(el);
    if (!found.length) return;
    const labels = bucketLabels.filter((label) => scoreLabelInText(text, label));
    if (labels.length !== 1) return;
    const rect = el.getBoundingClientRect();
    if (text.length > 900 || rect.width > 700 || rect.height > 700) return;
    const label = labels[0];
    const current = buckets[label];
    const item = { label, teams: found, text, idx, area: rect.width * rect.height, x: rect.left, y: rect.top };
    if (!current || item.teams.length > current.teams.length || (item.teams.length === current.teams.length && item.area < current.area)) {
      buckets[label] = item;
    }
  });
  return buckets;
}

function pairFromBucketTeams(teams) {
  const pairs = [];
  for (let i = 0; i + 1 < teams.length; i += 2) {
    pairs.push({ team_a: teams[i], team_b: teams[i + 1] });
  }
  return pairs;
}

const root = simulatorRoot();
const buckets = bucketElements(root);
return {
  rootTextLength: (root.innerText || "").length,
  pairs: pairElements(root).map((item) => ({
    team_a: item.found[0],
    team_b: item.found[1],
    text: item.text,
    x: Math.round(item.x),
    y: Math.round(item.y),
  })),
  buckets: Object.fromEntries(Object.entries(buckets).map(([label, item]) => [
    label,
    {
      teams: item.teams,
      pairs: pairFromBucketTeams(item.teams),
      text: item.text,
      x: Math.round(item.x),
      y: Math.round(item.y),
    },
  ])),
};
"""


SIMULATOR_CLICK_TEAM_JS = r"""
const teamNames = arguments[0] || [];
const winner = arguments[1];
const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
const winnerNorm = normalize(winner);
const byNorm = new Map(teamNames.map((name) => [normalize(name), name]));
const isTeam = (value) => byNorm.has(normalize(value));
const teamName = (value) => byNorm.get(normalize(value));

function visible(el) {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
}

function teamsIn(el) {
  const names = [];
  el.querySelectorAll("img[alt], img[title], [aria-label], [title]").forEach((node) => {
    [node.getAttribute("alt"), node.getAttribute("title"), node.getAttribute("aria-label")].forEach((value) => {
      if (isTeam(value)) names.push(teamName(value));
    });
  });
  (el.innerText || "").split(/\n+/).forEach((line) => {
    const clean = line.replace(/\s+/g, " ").trim();
    if (isTeam(clean)) names.push(teamName(clean));
  });
  return [...new Set(names)];
}

function simulatorRoot() {
  const roots = [...document.querySelectorAll("div, section, main")]
    .filter((el) => {
      const text = el.innerText || "";
      if (!scoreLabelInText(text, "0:0") || !scoreLabelInText(text, "3:2") || !scoreLabelInText(text, "2:3")) return false;
      return teamsIn(el).length >= 8;
    })
    .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
  return roots[0] || document.body;
}

function scoreLabelInText(text, label) {
  const [wins, losses] = label.split(":");
  const pattern = new RegExp(`(^|[^0-9])${wins}\\s*[:-]\\s*${losses}([^0-9]|$)`);
  return pattern.test(text || "");
}

function clickableFor(node) {
  return node.closest("button, a, [role='button'], [onclick], .clickable") || node.parentElement || node;
}

function fireClick(el) {
  if (!el) return false;
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  ["pointerdown", "mousedown", "mouseup", "click"].forEach((type) => {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
  });
  return true;
}

const root = simulatorRoot();
const pairEls = [...root.querySelectorAll("div, a, button, span, li")]
  .filter((el) => visible(el) && teamsIn(el).length === 2)
  .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);

for (const pairEl of pairEls) {
  if (!teamsIn(pairEl).some((name) => normalize(name) === winnerNorm)) continue;
  const nodes = [...pairEl.querySelectorAll("img[alt], img[title], [aria-label], [title], span, div")];
  for (const node of nodes) {
    const values = [
      node.getAttribute && node.getAttribute("alt"),
      node.getAttribute && node.getAttribute("title"),
      node.getAttribute && node.getAttribute("aria-label"),
      node.innerText,
    ];
    if (!values.some((value) => normalize(value) === winnerNorm)) continue;
    const target = clickableFor(node);
    fireClick(target);
    return { clicked: true, winner, text: (pairEl.innerText || "").replace(/\s+/g, " ").trim() };
  }
  const pairTarget = clickableFor(pairEl);
  fireClick(pairTarget);
  return { clicked: true, winner, text: (pairEl.innerText || "").replace(/\s+/g, " ").trim(), fallback: "pair" };
}
return { clicked: false, winner };
"""


SIMULATOR_ADVANCE_JS = r"""
const labels = ["simulate", "next", "advance", "generate", "submit", "update", "apply"];
const blockedLabels = ["group play", "timeline play", "players", "fantasy", "overview", "matches", "results", "news"];
function visible(el) {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
}
function scoreLabelInText(text, label) {
  const [wins, losses] = label.split(":");
  const pattern = new RegExp(`(^|[^0-9])${wins}\\s*[:-]\\s*${losses}([^0-9]|$)`);
  return pattern.test(text || "");
}
function simulatorRoot() {
  const roots = [...document.querySelectorAll("div, section, main")]
    .filter((el) => {
      const text = el.innerText || "";
      return scoreLabelInText(text, "0:0") && scoreLabelInText(text, "3:2") && scoreLabelInText(text, "2:3");
    })
    .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
  return roots[0] || document.body;
}
function fireClick(el) {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  ["pointerdown", "mousedown", "mouseup", "click"].forEach((type) => {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
  });
}
const clicked = [];
const root = simulatorRoot();
[...root.querySelectorAll("button, a, [role='button'], input[type='button'], input[type='submit']")].forEach((el) => {
  if (!visible(el)) return;
  const text = ((el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || "") + "").replace(/\s+/g, " ").trim();
  if (!text) return;
  const lower = text.toLowerCase();
  if (blockedLabels.some((label) => lower === label || lower.includes(label))) return;
  if (!labels.some((label) => lower.includes(label))) return;
  fireClick(el);
  clicked.push(text);
});
return { clicked };
"""


def _choose_probe_winners(initial_pairs: List[Dict[str, str]], ranks: Dict[str, int], scenario_name: str) -> List[str]:
    winners = []
    for idx, pair in enumerate(initial_pairs):
        a = str(pair.get("team_a") or "").strip()
        b = str(pair.get("team_b") or "").strip()
        if not a or not b:
            continue
        if scenario_name == "left_wins":
            winners.append(a)
        elif scenario_name == "right_wins":
            winners.append(b)
        elif scenario_name == "alternating":
            winners.append(a if idx % 2 == 0 else b)
        else:
            winners.append(a if int(ranks.get(a, 9999)) <= int(ranks.get(b, 9999)) else b)
    return winners


def _classify_after_pairs(
    pairs: List[Dict[str, str]],
    winners: List[str],
    losers: List[str],
) -> Dict[str, List[Dict[str, str]]]:
    winner_set = {name.lower() for name in winners}
    loser_set = {name.lower() for name in losers}
    buckets = {"1:0": [], "0:1": []}
    seen = {"1:0": set(), "0:1": set()}
    for pair in pairs:
        a = str(pair.get("team_a") or "").strip()
        b = str(pair.get("team_b") or "").strip()
        if not a or not b:
            continue
        key = tuple(sorted((a.lower(), b.lower())))
        if a.lower() in winner_set and b.lower() in winner_set and key not in seen["1:0"]:
            buckets["1:0"].append({"team_a": a, "team_b": b})
            seen["1:0"].add(key)
        if a.lower() in loser_set and b.lower() in loser_set and key not in seen["0:1"]:
            buckets["0:1"].append({"team_a": a, "team_b": b})
            seen["0:1"].add(key)
    return buckets


def _after_pairings_from_snapshot(
    snapshot: Dict[str, Any],
    winners: List[str],
    losers: List[str],
) -> Dict[str, List[Dict[str, str]]]:
    buckets = snapshot.get("buckets") or {}
    after = {
        "1:0": list((buckets.get("1:0") or {}).get("pairs") or []),
        "0:1": list((buckets.get("0:1") or {}).get("pairs") or []),
    }
    if not after["1:0"] and not after["0:1"]:
        after = _classify_after_pairs(list(snapshot.get("pairs") or []), winners, losers)
    return after


def _run_hltv_pairing_probe(url: str, scenario_names: List[str] | None = None) -> Dict[str, Any]:
    scenario_names = scenario_names or ["higher_seed_wins", "left_wins", "right_wins", "alternating"]

    def callback(driver):
        html = driver.page_source or ""
        simulator_data = _extract_swiss_simulator_data_from_html(html)
        if simulator_data:
            return _probe_from_swiss_simulator_data(simulator_data, url)

        parsed_ranks = _extract_team_ranks_from_html(html)
        extracted_names = _extract_event_team_names_from_html(html)
        if len(parsed_ranks) >= 8:
            team_names = sorted(parsed_ranks.keys(), key=lambda name: (parsed_ranks.get(name, 9999), name.lower()))
        else:
            rank_names = list(parsed_ranks.keys())
            extra_names = [name for name in extracted_names if name not in parsed_ranks]
            team_names = _ordered_unique(rank_names + extra_names)[:24]

        if len(team_names) < 8:
            return {
                "ok": False,
                "error": "Could not extract enough teams from the HLTV event page.",
                "ranks": parsed_ranks,
                "extracted_team_names": extracted_names,
            }

        scenarios = []
        inferred_seed_ranks: Dict[str, int] | None = None
        for scenario_name in scenario_names:
            driver.get(url)
            time.sleep(1.0)
            initial_snapshot = driver.execute_script(SIMULATOR_SNAPSHOT_JS, team_names) or {}
            initial_pairs = list(initial_snapshot.get("pairs") or [])[:8]
            if len(initial_pairs) < 4:
                scenarios.append(
                    {
                        "name": scenario_name,
                        "error": "Could not capture enough initial simulator pairings.",
                        "initial_snapshot": initial_snapshot,
                    }
                )
                continue

            if inferred_seed_ranks is None:
                inferred_seed_ranks = _seed_ranks_from_initial_pairs(initial_pairs)
            ranks = parsed_ranks if len(parsed_ranks) >= 8 else inferred_seed_ranks
            winners = _choose_probe_winners(initial_pairs, ranks, scenario_name)
            winner_set = {name.lower() for name in winners}
            losers = [
                name
                for pair in initial_pairs
                for name in (pair.get("team_a"), pair.get("team_b"))
                if str(name or "").lower() not in winner_set
            ]
            clicks = []
            for winner in winners:
                click_result = driver.execute_script(SIMULATOR_CLICK_TEAM_JS, team_names, winner) or {}
                clicks.append(click_result)
                time.sleep(0.15)

            selected_snapshot = driver.execute_script(SIMULATOR_SNAPSHOT_JS, team_names) or {}
            advance_result = driver.execute_script(SIMULATOR_ADVANCE_JS) or {}
            time.sleep(0.8)
            after_snapshot = driver.execute_script(SIMULATOR_SNAPSHOT_JS, team_names) or {}
            selected_after = _after_pairings_from_snapshot(selected_snapshot, winners, losers)
            advanced_after = _after_pairings_from_snapshot(after_snapshot, winners, losers)
            after = advanced_after if advanced_after["1:0"] or advanced_after["0:1"] else selected_after
            scenarios.append(
                {
                    "name": scenario_name,
                    "initial": initial_pairs,
                    "winners": winners,
                    "losers": losers,
                    "clicks": clicks,
                    "clicks_succeeded": sum(1 for click in clicks if click.get("clicked")),
                    "advance_result": advance_result,
                    "after": after,
                    "selected_snapshot": selected_snapshot,
                    "after_snapshot": after_snapshot,
                }
            )

        probe = {
            "ok": True,
            "ranks": parsed_ranks if len(parsed_ranks) >= 8 else (inferred_seed_ranks or {}),
            "rank_source": "hltv_page" if len(parsed_ranks) >= 8 else "inferred_from_initial_pairings",
            "parsed_rank_count": len(parsed_ranks),
            "extracted_team_count": len(team_names),
            "team_names": team_names,
            "scenarios": scenarios,
        }
        probe["inference"] = _infer_pairing_rule_from_probe(probe, probe["ranks"])
        return probe

    return run_hltv_browser_session(url, callback, wait_text="Simulator", timeout_ms=90000)


def _import_money_draft_data(
    money: Dict[str, Any],
    event_id: Optional[int] = None,
    hltv_event_id: Optional[int] = None,
    hltv_event_url: Optional[str] = None,
) -> Dict[str, int]:
    teams = money.get("teams") or []
    imported_players = 0
    imported_teams = 0
    event_teams_snapshot: List[Dict[str, Any]] = []

    for team_block in teams:
        team_data = team_block.get("teamData", {}) or {}
        team_name = team_data.get("name", "").strip()
        if not team_name:
            continue
        hltv_team_id = (
            team_data.get("teamId")
            or team_data.get("id")
            or (team_data.get("fantasyTeamId", {}) or {}).get("teamId")
            or (team_data.get("team", {}) or {}).get("teamId")
        )
        try:
            hltv_team_id = int(hltv_team_id) if hltv_team_id is not None else None
        except Exception:
            hltv_team_id = None

        player_entries = team_block.get("players", []) or []
        player_ids = []
        prices_by_player: Dict[int, int] = {}

        for entry in player_entries:
            p_data = entry.get("playerData", {}) or {}
            pid_obj = p_data.get("fantasyPlayerId", {}) or {}
            pid = pid_obj.get("playerId")
            name = p_data.get("name", "").strip()
            cost = entry.get("cost", 0)

            if pid is None or not name:
                continue

            pid = int(pid)
            player_ids.append(pid)
            prices_by_player[pid] = int(cost)

            add_or_update_player(
                player_id=pid,
                name=name,
                # Do not import overall rating from event JSON.
                # Overall rating is sourced from player stats page fetch (Top-X flow).
                rating=None,
                price=int(cost),
                best_role="",
                major_win_pct=0.0,
                minor_win_pct=0.0,
                boosters_json=None,
                roles_json=None,
            )
            imported_players += 1

        ids = player_ids[:5] if len(player_ids) >= 5 else player_ids + [0] * (5 - len(player_ids))
        existing_team = get_team_by_name(team_name)
        hltv_rank = int((existing_team or {}).get("hltv_rank") or 999)
        vrs_rank = int((existing_team or {}).get("vrs_rank") or 999)
        win_rate = float((existing_team or {}).get("win_rate") or 0.5)
        add_or_update_team(
            name=team_name,
            hltv_rank=hltv_rank,
            hltv_points=int((existing_team or {}).get("hltv_points") or 0),
            vrs_rank=vrs_rank,
            vrs_points=int((existing_team or {}).get("vrs_points") or 0),
            win_rate=win_rate,
            player_ids=ids,
            hltv_team_id=hltv_team_id or (existing_team or {}).get("hltv_team_id"),
        )
        imported_teams += 1
        event_teams_snapshot.append(
            {
                "team_name": team_name,
                "player_ids": ids,
                "prices_by_player": prices_by_player,
            }
        )

    if event_id is not None:
        upsert_event_snapshot(
            int(event_id),
            event_teams_snapshot,
            hltv_event_id=hltv_event_id,
            hltv_event_url=hltv_event_url,
        )
        set_active_event(int(event_id))
    from backend.services import event_rosters

    event_rosters.invalidate()

    return {"imported_players": imported_players, "imported_teams": imported_teams}


@router.post("/wipe")
def wipe():
    """
    Deletes all players and teams but keeps the schema.
    """
    wipe_database()
    return {"status": "ok"}


@router.post("/field/{event_id}/read")
def start_field_read(event_id: int, payload: Dict[str, Any] | None = None):
    """Read every team of the event's fantasy league (services/fantasy_field):
    league_id and team_id default to a team the app already knows in the
    event (any connected account's); max_ids caps the id walk."""
    from backend.data import my_team_cache
    from backend.data.db import connect
    from backend.services import fantasy_field

    payload = payload or {}
    league_id, team_id = payload.get("league_id"), payload.get("team_id")
    if not league_id or not team_id:
        conn = connect()
        try:
            rows = conn.execute("SELECT team_json FROM my_team_cache WHERE event_id = ? AND team_json IS NOT NULL", (int(event_id),)).fetchall()
        finally:
            conn.close()
        for r in rows:
            t = my_team_cache._loads(r["team_json"]) or {}
            if t.get("team_id") and t.get("league_id"):
                league_id, team_id = league_id or t["league_id"], team_id or t["team_id"]
                break
    if not league_id or not team_id:
        raise HTTPException(status_code=400, detail="league_id and team_id are needed: no team of a connected account is known in this event")
    return fantasy_field.start_read(int(event_id), int(league_id), int(team_id), int(payload.get("max_ids") or fantasy_field.MAX_IDS))


@router.get("/field/{event_id}")
def get_field_status(event_id: int):
    from backend.services import fantasy_field

    return fantasy_field.status(int(event_id))


@router.post("/field/{event_id}/stop")
def stop_field_read(event_id: int):
    from backend.services import fantasy_field

    return {"status": "stopping" if fantasy_field.stop_read(int(event_id)) else "idle"}


@router.post("/fetch-page")
def fetch_page(payload: Dict[str, Any]):
    """Dev helper: fetch one HLTV page through the shared browser — which
    archives it as a page snapshot — so format-detection work can inspect the
    stored HTML without a second browser fighting over the Chrome profile."""
    url = str((payload or {}).get("url") or "").strip()
    if not url.startswith("https://www.hltv.org/"):
        raise HTTPException(status_code=400, detail="Only hltv.org URLs are allowed")
    try:
        html = fetch_hltv_html(url)
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {exc}")
    return {"status": "ok", "url": url, "length": len(html or "")}


@router.post("/import-trigger-rates")
def import_trigger_rates(payload: Dict[str, Any]):
    """
    Paste-in importer for triggerRates JSON (playerTriggerRates array).
    Updates boosters_json and roles_json for players.
    """
    raw = payload.get("trigger_json")
    if not raw:
        raise HTTPException(status_code=400, detail="trigger_json is required")

    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    ptr_list = data.get("playerTriggerRates") or []
    if not ptr_list:
        raise HTTPException(status_code=400, detail="playerTriggerRates missing or empty")
    return {"status": "ok", **_ingest_trigger_rates(ptr_list)}


def _ingest_trigger_rates(ptr_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    updated = 0
    updated_players_info: List[Dict[str, Any]] = []
    seen_player_ids = set()
    for entry in ptr_list:
        pid = entry.get("playerId", {}).get("playerId")
        if pid is None:
            continue
        booster_map_raw = entry.get("boosterIdToTriggerRate", {}) or {}
        role_map_raw = entry.get("roleIdToTriggerRate", {}) or {}

        # Preserve existing core info so NOT NULL cols are satisfied
        from backend.data.player_db import get_player  # local import to avoid circular

        existing = get_player(int(pid))
        name = (existing or {}).get("name") or f"Player {pid}"
        rating = (existing or {}).get("rating", 0.0)
        price = (existing or {}).get("price", 0)
        best_role = (existing or {}).get("best_role", "")
        major_win_pct = (existing or {}).get("major_win_pct", 0.0)
        minor_win_pct = (existing or {}).get("minor_win_pct", 0.0)

        # Start from existing booster/role JSON so we don't lose prior data
        existing_boosters = {}
        existing_roles = {}
        try:
            if existing and existing.get("boosters_json"):
                existing_boosters = json.loads(existing["boosters_json"])
        except Exception:
            existing_boosters = {}
        try:
            if existing and existing.get("roles_json"):
                existing_roles = json.loads(existing["roles_json"])
        except Exception:
            existing_roles = {}

        boosters_json = dict(existing_boosters)
        if booster_map_raw:
            boosters_json = {}
            for idx, src_id in enumerate(BOOSTER_SOURCE_IDS):
                obj = booster_map_raw.get(str(src_id), {}) or {}
                val = float(obj.get("value", 0.0))
                boosters_json[str(idx)] = val

        roles_json = dict(existing_roles)
        if role_map_raw:
            roles_json = {}
            for idx, src_id in enumerate(ROLE_SOURCE_IDS):
                rdata = role_map_raw.get(str(src_id), {}) or {}
                small = rdata.get("smallPoints", {}) or {}
                maxp = rdata.get("maxPoints", {}) or {}
                roles_json[str(idx)] = {
                    "major": float(maxp.get("value", 0.0)),
                    "minor": float(small.get("value", 0.0)),
                }

        # Pick best role by highest major trigger; fall back to existing values
        best_role_id = best_role
        best_major = major_win_pct
        best_minor = minor_win_pct
        if roles_json:
            best = max(roles_json.items(), key=lambda kv: kv[1].get("major", 0.0))
            best_role_id = str(best[0])
            best_major = float(best[1].get("major", 0.0))
            best_minor = float(best[1].get("minor", 0.0))

        add_or_update_player(
            player_id=int(pid),
            name=name,
            rating=rating,
            price=price,
            best_role=best_role_id,
            major_win_pct=best_major,
            minor_win_pct=best_minor,
            boosters_json=boosters_json,
            roles_json=roles_json,
        )
        updated += 1
        if int(pid) not in seen_player_ids:
            updated_players_info.append({"player_id": int(pid), "name": name})
            seen_player_ids.add(int(pid))

    updated_player_names = [p["name"] for p in updated_players_info]
    return {
        "updated_players": updated,
        "updated_players_info": updated_players_info,
        "updated_player_names": updated_player_names,
    }


# --- Automated trigger-rate acquisition -------------------------------------
# The fantasy team page fires an XHR whose response contains playerTriggerRates
# for the players in the current lineup. Discovery captures that request via
# CDP logging and stores it as a template; the auto-fetch job then replays it
# with substituted 5-player batches until every event player is covered.

from backend.data.db import ROOT_DIR as _ROOT_DIR

TRIGGER_TEMPLATE_PATH = _ROOT_DIR / "trigger_endpoint_template.json"
TRIGGER_JOBS: Dict[str, Dict[str, Any]] = {}
TRIGGER_JOBS_LOCK = threading.Lock()


def _load_trigger_template() -> Optional[Dict[str, Any]]:
    try:
        if TRIGGER_TEMPLATE_PATH.exists():
            return json.loads(TRIGGER_TEMPLATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _save_trigger_template(template: Dict[str, Any]) -> None:
    TRIGGER_TEMPLATE_PATH.write_text(json.dumps(template, indent=2), encoding="utf-8")


def _template_fantasy_id(template: Optional[Dict[str, Any]]) -> Optional[int]:
    """The fantasy (event) id baked into a captured trigger-rates endpoint.

    The captured URL embeds the fantasy page it was taken on, e.g.
    /fantasy/642/league/.../draft/.../triggerrates. That id is event-specific,
    so a template captured for one event must never be reused for another —
    doing so saves the lineup to, and reads rates from, the wrong event's team.
    """
    trig_url = str((template or {}).get("url") or "")
    m = re.search(r"/fantasy/(\d+)/league/(\d+)/overview/(\d+)/draft/(\d+)/triggerrates", trig_url)
    return int(m.group(1)) if m else None


def _template_matches_event(template: Optional[Dict[str, Any]], event_id: Optional[int]) -> bool:
    """True when the template's captured event matches the target event."""
    tpl_fid = _template_fantasy_id(template)
    if tpl_fid is None:
        return False
    target = int(event_id or 0)
    if not target:
        from backend.data.event_db import get_active_event_id

        target = int(get_active_event_id() or 0)
    return bool(target) and tpl_fid == target


HLTV_CREDENTIALS_PATH = _ROOT_DIR / "hltv_credentials.json"


def _load_hltv_credentials() -> Optional[Dict[str, str]]:
    try:
        if HLTV_CREDENTIALS_PATH.exists():
            data = json.loads(HLTV_CREDENTIALS_PATH.read_text(encoding="utf-8"))
            if data.get("username") and data.get("password"):
                return {"username": str(data["username"]), "password": str(data["password"])}
    except Exception:
        return None
    return None


@router.post("/hltv-credentials")
def save_hltv_credentials(payload: Dict[str, Any]):
    username = str((payload or {}).get("username") or "").strip()
    password = str((payload or {}).get("password") or "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")
    HLTV_CREDENTIALS_PATH.write_text(json.dumps({"username": username, "password": password}), encoding="utf-8")
    return {"status": "ok", "username": username}


@router.get("/hltv-credentials")
def get_hltv_credentials_status():
    creds = _load_hltv_credentials()
    return {"configured": bool(creds), "username": (creds or {}).get("username", "")}


def _is_hltv_logged_in(driver) -> bool:
    try:
        return bool(
            driver.execute_script(
                "return !!(document.querySelector('a[href*=\"logout\"]')"
                " || document.querySelector('.navaccount, .avatarNavItem, .nav-account, img.avatar'))"
            )
        )
    except Exception:
        return False


def _ensure_app_login(driver) -> None:
    """Require a persisted signed-in session (established once in the Capture
    window, which is where the user solves HLTV's captcha). No scripted login —
    it would hit the captcha and risk rate-limiting on every fetch."""
    driver.get("https://www.hltv.org")
    time.sleep(3)
    if _is_hltv_logged_in(driver):
        return
    raise RuntimeError(
        "Not signed in to HLTV. Run scripts\\hltv-login-handoff.ps1 from your desktop session "
        "(it signs this browser in with your session cookies), or click Capture and sign in there."
    )


@router.post("/hltv-login-session")
def hltv_login_session(payload: Dict[str, Any] | None = None):
    """Open hltv.org in the scraper's Chrome so the user can log in once; the
    profile keeps the session for later automated fantasy requests."""
    timeout_s = max(30, min(600, int((payload or {}).get("timeout_seconds") or 240)))

    def wait_for_login(driver):
        # Open the sign-in modal so the user only has to type + solve captcha.
        try:
            driver.execute_script(
                """
                if (!document.querySelector('input[type=password]')) {
                  var link = Array.from(document.querySelectorAll('a, button, .nav-link, [class*="sign" i]'))
                    .find(function(e){ return (e.textContent||'').trim().toLowerCase() === 'sign in'; });
                  if (link) link.click();
                }
                """
            )
        except Exception:
            pass
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if _is_hltv_logged_in(driver):
                return {"logged_in": True}
            time.sleep(3)
        return {"logged_in": False}

    try:
        result = run_hltv_browser_session(
            "https://www.hltv.org/", wait_for_login, timeout_ms=(timeout_s + 30) * 1000, wait_text=None
        )
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", **(result or {})}


# ---- HLTV session hand-off ---------------------------------------------------
# The 24/7 backend runs as the SYSTEM service: no desktop, and its own Chrome
# profile (SYSTEM cannot decrypt the interactive user's). HLTV's sign-in is
# captcha-protected, so the login is established in the USER's session and the
# session cookies are posted here (scripts\hltv-login-handoff.ps1). Verified:
# `autologin` + `PHPSESSID` alone move the login between profiles, and the
# autologin cookie lasts about a year.
_HLTV_HANDOFF_COOKIE_ALLOWLIST = ("autologin", "PHPSESSID", "cf_clearance", "_cfuvid", "__cflb", "__cf_bm")
_admin_logger = logging.getLogger(__name__)


def _hltv_session_snapshot(driver) -> Dict[str, Any]:
    # Login state + remember-me cookie expiry, graded ok/warning/error and
    # persisted so the Scheduling tab can show it (backend/services/hltv_session.py).
    snap = hltv_session.snapshot_from_driver(driver)
    hltv_session.save_snapshot(snap)
    return snap


@router.get("/hltv-session-status")
def hltv_session_status():
    """Is the scraper's browser profile signed in to HLTV, and for how long?
    Costs one page load; the result is also persisted for GET /schedule/status."""
    try:
        result = run_hltv_browser_session("https://www.hltv.org/", _hltv_session_snapshot, wait_text=None, timeout_ms=60000)
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", **(result or {})}


@router.post("/hltv-session-cookies")
def hltv_session_cookies(payload: Dict[str, Any]):
    """Receive an HLTV login from an interactive session (cookie hand-off) and
    add it to the scraper's browser profile. Requires an `autologin` cookie;
    `PHPSESSID` and the Cloudflare cookies are optional extras."""
    raw = (payload or {}).get("cookies")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="cookies must be a non-empty list")
    cookies: List[Dict[str, Any]] = []
    for item in raw:
        item = item or {}
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if name not in _HLTV_HANDOFF_COOKIE_ALLOWLIST or not value:
            continue
        cookie: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": str(item.get("domain") or ".hltv.org"),
            "path": str(item.get("path") or "/"),
        }
        try:
            if item.get("expiry"):
                cookie["expiry"] = int(item["expiry"])
        except Exception:
            pass
        if item.get("secure"):
            cookie["secure"] = True
        if item.get("httpOnly"):
            cookie["httpOnly"] = True
        cookies.append(cookie)
    names = sorted({c["name"] for c in cookies})
    if "autologin" not in names:
        raise HTTPException(status_code=400, detail="an 'autologin' cookie is required (PHPSESSID is optional)")

    def apply(driver):
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except Exception as exc:
                raise HLTVBrowserError(f"could not add cookie {cookie['name']}: {exc}") from exc
        return _hltv_session_snapshot(driver)

    try:
        result = run_hltv_browser_session("https://www.hltv.org/", apply, wait_text=None, timeout_ms=60000) or {}
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not result.get("logged_in"):
        raise HTTPException(
            status_code=409,
            detail="Cookies were added but hltv.org still shows this browser signed out; sign in again in your session and retry.",
        )
    _admin_logger.info("HLTV session hand-off applied (%s); signed in as %s", ", ".join(names), result.get("username") or "?")
    return {"status": "ok", "applied": names, **result}


def _handoff_cookies(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = (payload or {}).get("cookies")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="cookies must be a non-empty list")
    cookies: List[Dict[str, Any]] = []
    for item in raw:
        item = item or {}
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if name not in _HLTV_HANDOFF_COOKIE_ALLOWLIST or not value:
            continue
        cookie: Dict[str, Any] = {"name": name, "value": value, "domain": str(item.get("domain") or ".hltv.org"), "path": str(item.get("path") or "/")}
        try:
            if item.get("expiry"):
                cookie["expiry"] = int(item["expiry"])
        except Exception:
            pass
        if item.get("secure"):
            cookie["secure"] = True
        if item.get("httpOnly"):
            cookie["httpOnly"] = True
        cookies.append(cookie)
    if "autologin" not in {c["name"] for c in cookies}:
        raise HTTPException(status_code=400, detail="an 'autologin' cookie is required (PHPSESSID is optional)")
    return cookies


@router.post("/my-hltv-session-cookies")
def my_hltv_session_cookies(payload: Dict[str, Any]):
    """The PERSONAL HLTV login for the My Team page (hand-off with
    --account mine): verified by signing the shared browser in with it for a
    moment, then stored apart from the scraper account's session."""
    from backend.services import my_team

    cookies = _handoff_cookies(payload)
    try:
        username = my_team.with_personal_session(lambda _driver, name: name, cookies=cookies)
    except my_team.PersonalSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    data = my_team.save_account(my_team.OPERATOR_KEY, cookies, str(username or ""))
    _admin_logger.info("Operator's HLTV session handed over: %s", username or "?")
    return {"status": "ok", "logged_in": True, "username": username or "", "autologin_expires_at": data.get("autologin_expires_at")}


@router.get("/my-hltv-session")
def get_my_hltv_session():
    from backend.services import my_team

    return my_team.account_status(my_team.OPERATOR_KEY)


@router.delete("/my-hltv-session")
def delete_my_hltv_session():
    from backend.services import my_team

    my_team.forget_account(my_team.OPERATOR_KEY)
    return {"status": "ok", "connected": False}


@router.get("/team-aliases")
def get_team_aliases():
    """Team renames: an earlier name's results, rankings and map history count
    for the current team (see data/team_aliases.py)."""
    from backend.data import team_aliases

    return {"aliases": team_aliases.list_aliases()}


@router.post("/team-aliases")
def add_team_alias(payload: Dict[str, Any]):
    from backend.data import team_aliases

    body = payload or {}
    try:
        record = team_aliases.add_alias(int(body.get("old_team_id") or 0), int(body.get("new_team_id") or 0))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _after_alias_change()
    return {"status": "ok", "alias": record}


@router.delete("/team-aliases/{old_key}")
def delete_team_alias(old_key: str):
    from backend.data import team_aliases

    removed = team_aliases.remove_alias(old_key)
    _after_alias_change()
    return {"status": "ok", "removed": removed}


def _after_alias_change() -> None:
    """A rename changes which results belong to a team: drop the per-team
    caches and retrain the win model in the background."""
    from backend.routes import events as events_routes
    from backend.services import event_rosters, team_strength

    team_strength.clear_caches()
    event_rosters.invalidate()

    def _retrain() -> None:
        try:
            events_routes.ensure_production_map_model()
        except Exception:  # noqa: BLE001
            _admin_logger.warning("Model retrain after a team rename failed", exc_info=True)

    threading.Thread(target=_retrain, name="alias-retrain", daemon=True).start()


@router.get("/users")
def list_accounts() -> dict:
    """Every signed-in account with activity (operator only)."""
    from backend.data import auth_db

    users = auth_db.list_users()
    now = time.time()
    return {
        "users": users,
        "total": len(users),
        "online_now": sum(1 for u in users if (u.get("last_seen_at") or 0) > now - 10 * 60),
        "active_7d": sum(1 for u in users if (u.get("last_seen_at") or u.get("last_login_at") or 0) > now - 7 * 86400),
        "admins": sum(1 for u in users if u.get("is_admin")),
    }


@router.post("/users/{user_id}/admin")
def set_account_admin(user_id: int, payload: dict | None = None) -> dict:
    from backend.data import auth_db

    is_admin = bool((payload or {}).get("is_admin"))
    if not auth_db.set_admin(int(user_id), is_admin):
        raise HTTPException(status_code=404, detail="No such user")
    return {"status": "ok", "user": auth_db.public_user(auth_db.get_user(int(user_id)))}


@router.post("/restart")
def restart_backend():
    """Exit the process shortly after responding; the watchdog relaunches it
    with the current code. Exists because the backend may run as SYSTEM,
    which an unelevated shell cannot kill (see scripts\\restart-backend.ps1)."""
    _admin_logger.warning("Restart requested via API; exiting in 0.5s (the watchdog relaunches the backend)")

    def _exit() -> None:
        # A hard exit would orphan the shared Chrome and leave it holding the
        # profile; close it first, then sweep anything still using the profile.
        try:
            close_hltv_browser()
        except Exception:
            pass
        try:
            kill_own_uc_drivers()
        except Exception:
            pass
        try:
            _kill_orphan_chrome()
        except Exception:
            pass
        os._exit(0)

    threading.Timer(0.5, _exit).start()
    return {"status": "restarting", "pid": os.getpid()}


TRIGGER_CAPTURE_DEBUG_PATH = _ROOT_DIR / "trigger_capture_debug.json"


def _collect_trigger_requests(driver, settle_seconds: float = 20.0) -> List[Dict[str, Any]]:
    time.sleep(max(3.0, settle_seconds))
    try:
        entries = driver.get_log("performance")
    except Exception as exc:
        raise HLTVBrowserError(f"CDP performance logging unavailable: {exc}") from exc
    requests_by_id: Dict[str, Dict[str, Any]] = {}
    responses: List[tuple] = []
    fantasy_reqs: List[Dict[str, Any]] = []
    for entry in entries:
        try:
            msg = json.loads(entry.get("message") or "{}").get("message") or {}
        except Exception:
            continue
        method = msg.get("method")
        params = msg.get("params") or {}
        rid = params.get("requestId")
        if method == "Network.requestWillBeSent" and rid:
            req = params.get("request") or {}
            url = str(req.get("url") or "")
            requests_by_id[rid] = {
                "url": url,
                "method": str(req.get("method") or "GET"),
                "post_data": req.get("postData"),
            }
            # Log every non-GET fantasy request so the lineup-save endpoint is
            # discoverable from a capture where the user saves a lineup.
            if "/fantasy/" in url and str(req.get("method") or "GET").upper() != "GET":
                fantasy_reqs.append(requests_by_id[rid])
        elif method == "Network.responseReceived" and rid:
            resp = params.get("response") or {}
            responses.append((rid, str(resp.get("url") or "")))
    if fantasy_reqs:
        try:
            existing = []
            if TRIGGER_CAPTURE_DEBUG_PATH.exists():
                existing = json.loads(TRIGGER_CAPTURE_DEBUG_PATH.read_text(encoding="utf-8"))
            TRIGGER_CAPTURE_DEBUG_PATH.write_text(
                json.dumps((existing + fantasy_reqs)[-100:], indent=2), encoding="utf-8"
            )
        except Exception:
            pass
    found: List[Dict[str, Any]] = []
    for rid, url in responses:
        try:
            body = (driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid}) or {}).get("body") or ""
        except Exception:
            continue
        if "playerTriggerRates" not in body:
            continue
        req = requests_by_id.get(rid) or {"url": url, "method": "GET", "post_data": None}
        found.append({**req, "response_body": body})
    return found


@router.post("/fantasy-recon")
def fantasy_recon(payload: Dict[str, Any] | None = None):
    """Recon for trigger-rates automation: log in as the app account, open the
    event's gameredirect, and report where it lands (does the account already
    have a team? do league/team/draft ids appear without human interaction?).
    Read-only — navigates and inspects, saves nothing."""
    from backend.data.event_db import get_active_event_id

    fantasy_id = int((payload or {}).get("event_id") or get_active_event_id() or 0)
    if not fantasy_id:
        raise HTTPException(status_code=400, detail="No event id")

    recon_path = str((payload or {}).get("path") or "").strip()

    def recon(driver):
        _ensure_app_login(driver)
        target = f"https://www.hltv.org{recon_path}" if recon_path else f"https://www.hltv.org/fantasy/{fantasy_id}/gameredirect"
        driver.get(target)
        time.sleep(6)
        current = str(getattr(driver, "current_url", "") or "")
        buttons = driver.execute_script(
            "return Array.from(document.querySelectorAll('button, a.button, [class*=join], [class*=create]'))"
            ".map(function(b){return (b.tagName + ':' + (b.textContent||'').trim().slice(0,40) + ':' + (b.getAttribute('href')||''));})"
            ".filter(function(s){return s.length > 8;}).slice(0, 30);"
        ) or []
        hrefs = driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href*=\"/fantasy/\"]'))"
            ".map(function(a){return a.getAttribute('href');}).slice(0, 40);"
        ) or []
        body_ids = driver.execute_script(
            "var m = document.documentElement.outerHTML.match(/league\\/(\\d+)[^\"']*?(?:team|overview)\\/(\\d+)/);"
            "return m ? m.slice(1) : null;"
        )
        draft_hits = driver.execute_script(
            "var mm = document.documentElement.outerHTML.match(/draft\\/(\\d+)/g);"
            "return mm ? mm.slice(0, 5) : null;"
        )
        return {
            "landed_url": current,
            "buttons": buttons,
            "fantasy_links": [h for h in hrefs if h],
            "league_team_in_dom": body_ids,
            "draft_ids_in_dom": draft_hits,
        }

    try:
        result = run_hltv_browser_session(
            "https://www.hltv.org", recon, timeout_ms=180000, wait_text=None
        )
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", "event_id": fantasy_id, **(result or {})}


@router.post("/capture-trigger-endpoint")
def capture_trigger_endpoint(payload: Dict[str, Any] | None = None):
    """One-time interactive capture: open the draft builder in a visible window,
    log in with the app account if needed, and watch the network while the user
    adds a few players. Saves the reusable request template on success."""
    from backend.data.event_db import get_active_event_id

    fantasy_id = int((payload or {}).get("event_id") or get_active_event_id() or 0)
    if not fantasy_id:
        raise HTTPException(status_code=400, detail="No active event. Import an event first.")

    def capture(driver):
        return _auto_discover_template(driver, fantasy_id, wait_seconds=220)

    try:
        template = run_hltv_browser_session(
            "https://www.hltv.org", capture, timeout_ms=260000, wait_text=None
        )
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "template_url": template.get("url"),
        "template_method": template.get("method"),
        "players_in_capture": len(template.get("captured_player_ids") or []),
    }


@router.post("/discover-trigger-rates")
def discover_trigger_rates(payload: Dict[str, Any]):
    """Load the user's fantasy team page, capture the XHR that carries
    playerTriggerRates, import what it contains, and store the request as a
    template for automated batch fetching.

    Without an explicit URL, opens the fantasy overview for the active event
    and follows the logged-in user's own league/team link automatically."""
    url = str((payload or {}).get("url") or "").strip()
    auto_detect = not url.startswith("http")
    if auto_detect:
        from backend.data.event_db import get_active_event_id

        fantasy_id = (payload or {}).get("event_id") or get_active_event_id()
        if not fantasy_id:
            raise HTTPException(status_code=400, detail="No active event; import an event or provide a fantasy team URL.")
        url = f"https://www.hltv.org/fantasy/{int(fantasy_id)}/overview"

    settle = float((payload or {}).get("settle_seconds") or 20)
    detected_url: Dict[str, Any] = {}

    def capture(driver):
        if auto_detect:
            href = driver.execute_script(
                "var a = document.querySelector('a[href*=\"/league/\"][href*=\"/team/\"]')"
                " || document.querySelector('a[href*=\"/league/\"]');"
                "return a ? a.getAttribute('href') : null;"
            )
            if not href:
                raise HLTVBrowserError(
                    "Could not find your league/team link on the fantasy overview — are you logged in and joined to a league for this event?"
                )
            target = href if str(href).startswith("http") else f"https://www.hltv.org{href}"
            detected_url["url"] = target
            driver.get(target)
            time.sleep(3)
        return _collect_trigger_requests(driver, settle_seconds=settle)

    try:
        found = run_hltv_browser_session(url, capture, timeout_ms=120000, wait_text=None)
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if auto_detect and detected_url.get("url"):
        url = detected_url["url"]

    if not found:
        return {
            "status": "not_found",
            "detail": "No response containing playerTriggerRates was captured. Make sure you are logged in (use the login window) and the URL is your fantasy team page.",
        }

    total = {"updated_players": 0, "updated_player_names": []}
    seen_ids: List[int] = []
    for item in found:
        try:
            data = json.loads(item["response_body"])
        except Exception:
            continue
        ptr = data.get("playerTriggerRates") or []
        if not ptr:
            continue
        for entry in ptr:
            pid = (entry.get("playerId") or {}).get("playerId")
            if pid is not None:
                seen_ids.append(int(pid))
        counts = _ingest_trigger_rates(ptr)
        total["updated_players"] += counts["updated_players"]
        total["updated_player_names"].extend(counts["updated_player_names"])

    template = {
        "url": found[0]["url"],
        "method": found[0]["method"],
        "post_data": found[0].get("post_data"),
        "captured_player_ids": seen_ids[:10],
        "page_url": url,
        "captured_at": time.time(),
    }
    _save_trigger_template(template)
    return {
        "status": "ok",
        "requests_found": len(found),
        "template_url": template["url"],
        "template_method": template["method"],
        "players_in_capture": len(set(seen_ids)),
        **total,
    }


def _build_trigger_batches(
    player_prices: Dict[int, int],
    player_teams: Dict[int, int],
    budget: int,
    cover: Optional[set] = None,
) -> List[List[int]]:
    """Greedy set cover: 5-player batches within budget and max 2 per team,
    preferring uncovered players so each batch covers ~5 new ones. `cover`
    limits which players must be covered; fillers may come from the rest."""
    min_price = min(player_prices.values()) if player_prices else 0
    all_ids = set(player_prices)
    remaining = set(cover) & all_ids if cover is not None else set(all_ids)
    batches: List[List[int]] = []
    while remaining:
        seed = max(remaining, key=lambda p: player_prices[p])
        batch = [seed]
        cost = player_prices[seed]
        counts = {player_teams.get(seed, 0): 1}
        pools = (
            sorted(remaining - {seed}, key=lambda p: player_prices[p]),
            sorted(all_ids - remaining - {seed}, key=lambda p: player_prices[p]),
        )
        for pool in pools:
            for cand in pool:
                if len(batch) == 5:
                    break
                if cand in batch:
                    continue
                tid = player_teams.get(cand, 0)
                if counts.get(tid, 0) >= 2:
                    continue
                slots_after = 5 - len(batch) - 1
                if cost + player_prices[cand] + slots_after * min_price > budget:
                    continue
                batch.append(cand)
                counts[tid] = counts.get(tid, 0) + 1
                cost += player_prices[cand]
            if len(batch) == 5:
                break
        if len(batch) == 5 and cost <= budget:
            batches.append(batch)
            remaining -= set(batch)
        else:
            # Unfillable seed (pathological prices/teams); skip it rather than loop.
            remaining.discard(seed)
    return batches


def ensure_trigger_backfill_schema() -> None:
    from backend.data.db import connect as _db_connect

    conn = _db_connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trigger_rates_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                processed_items INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                current_item TEXT NOT NULL DEFAULT '',
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _save_trigger_job(job: Dict[str, Any]) -> None:
    from backend.data.db import connect as _db_connect

    now = time.time()
    job["updated_at"] = now
    conn = _db_connect()
    try:
        conn.execute(
            """
            INSERT INTO trigger_rates_jobs (
                job_id, status, error, last_error, progress, processed_items, total_items,
                ok, failed, current_item, pause_requested, cancel_requested,
                created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                last_error = excluded.last_error,
                progress = excluded.progress,
                processed_items = excluded.processed_items,
                total_items = excluded.total_items,
                ok = excluded.ok,
                failed = excluded.failed,
                current_item = excluded.current_item,
                pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested,
                updated_at = excluded.updated_at,
                started_at = COALESCE(excluded.started_at, trigger_rates_jobs.started_at),
                finished_at = excluded.finished_at
            """,
            (
                str(job["job_id"]),
                str(job.get("status") or "queued"),
                str(job.get("error") or ""),
                str(job.get("last_error") or ""),
                float(job.get("progress") or 0.0),
                int(job.get("processed_items") or 0),
                int(job.get("total_items") or 0),
                int(job.get("ok") or 0),
                int(job.get("failed") or 0),
                str(job.get("current_item") or ""),
                1 if job.get("pause_requested") else 0,
                1 if job.get("cancel_requested") else 0,
                float(job.get("created_at") or now),
                now,
                job.get("started_at"),
                job.get("finished_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _publish_trigger_job(job: Dict[str, Any]) -> None:
    with TRIGGER_JOBS_LOCK:
        TRIGGER_JOBS[str(job["job_id"])] = dict(job)
    _save_trigger_job(job)


def _get_stored_trigger_job(job_id: str) -> Optional[Dict[str, Any]]:
    from backend.data.db import connect as _db_connect

    conn = _db_connect()
    try:
        row = conn.execute("SELECT * FROM trigger_rates_jobs WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    job = dict(row)
    job["pause_requested"] = bool(job.get("pause_requested"))
    job["cancel_requested"] = bool(job.get("cancel_requested"))
    return job


TRIGGER_WORKERS: Dict[str, threading.Thread] = {}
_TRIGGER_INTERRUPTION_BOILERPLATE = {"Job was interrupted before completion. Resume to continue.", "Paused", "Canceled"}


def _trigger_worker_is_active(job_id: str) -> bool:
    with TRIGGER_JOBS_LOCK:
        worker = TRIGGER_WORKERS.get(job_id)
        return bool(worker and worker.is_alive())


def _trigger_job_response(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": str(job["job_id"]),
        "status": job.get("status", "queued"),
        "error": job.get("error", ""),
        "last_error": job.get("last_error", ""),
        "progress": job.get("progress", 0.0),
        "processed_items": job.get("processed_items", 0),
        "total_items": job.get("total_items", 0),
        "ok": job.get("ok", 0),
        "failed": job.get("failed", 0),
        "current_item": job.get("current_item", ""),
        "pause_requested": bool(job.get("pause_requested")),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _get_trigger_job_for_response(job_id: str) -> Dict[str, Any]:
    with TRIGGER_JOBS_LOCK:
        cached = TRIGGER_JOBS.get(job_id)
        job = dict(cached) if cached else None
    if not job:
        job = _get_stored_trigger_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.get("status") in {"queued", "running", "pausing", "canceling"} and not _trigger_worker_is_active(job_id):
        job["status"] = "canceled" if job.get("cancel_requested") else "paused"
        job["last_error"] = job.get("last_error") or "Job was interrupted before completion. Resume to continue."
        job["error"] = ""
        job["pause_requested"] = False
        job["cancel_requested"] = False
        _publish_trigger_job(job)
    return job


def _player_teams_map() -> Dict[int, int]:
    from backend.data.team_db import get_all_teams

    player_teams: Dict[int, int] = {}
    for t in get_all_teams():
        for key in ("player1_id", "player2_id", "player3_id", "player4_id", "player5_id"):
            pid = int(t.get(key) or 0)
            if pid > 0:
                player_teams[pid] = int(t.get("team_id") or 0)
    return player_teams


def _missing_trigger_players(event_id: Optional[int] = None) -> tuple:
    price_map = {int(k): int(v) for k, v in (get_event_price_map(event_id) or {}).items()}
    missing = []
    for pid in price_map:
        row = get_player(pid) or {}
        if not row.get("boosters_json") or not row.get("roles_json"):
            missing.append(pid)
    template = _load_trigger_template()
    coverage = {
        "event_players": len(price_map),
        "with_data": len(price_map) - len(missing),
        "missing": len(missing),
        # Only "ready" when the captured endpoint belongs to THIS event; a
        # template from a prior event must be re-captured before it can be used.
        "template_ready": _template_matches_event(template, event_id),
        "template_event_id": _template_fantasy_id(template),
    }
    return price_map, missing, coverage


@router.get("/trigger-rates-backfill/coverage")
def get_trigger_backfill_coverage(event_id: Optional[int] = None):
    _prices, _missing, coverage = _missing_trigger_players(event_id)
    return {"status": "ok", **coverage}


@router.post("/trigger-rates-backfill/start")
def start_trigger_backfill(payload: Dict[str, Any] | None = None):
    template = _load_trigger_template()
    event_id = (payload or {}).get("event_id")
    # A template for another event is fine when credentials exist — the worker
    # auto-provisions a team + endpoint for the target event itself.
    if not _template_matches_event(template, event_id) and not _load_hltv_credentials():
        raise HTTPException(
            status_code=400,
            detail=(
                "No captured endpoint for this event and no saved HLTV login to auto-provision one. "
                "Save the HLTV app login (or run Capture on this event) first."
            ),
        )
    latest = _get_latest_trigger_job()
    if latest and latest.get("status") in {"queued", "running", "pausing", "canceling"}:
        return {"job_id": latest["job_id"], "status": latest.get("status"), "reused": True}
    price_map, missing, _cov = _missing_trigger_players((payload or {}).get("event_id"))
    if not price_map:
        raise HTTPException(status_code=400, detail="No event player prices found. Import the event first.")
    now = time.time()
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "error": "",
        "last_error": "",
        "progress": 0.0,
        "processed_items": 0,
        "total_items": 0,
        "ok": 0,
        "failed": 0,
        "current_item": "",
        "pause_requested": False,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    _publish_trigger_job(job)
    _start_trigger_worker(job_id, (payload or {}).get("event_id"), bool((payload or {}).get("refresh_all")))
    return {"job_id": job_id, "status": "queued"}


def run_trigger_backfill_blocking(event_id: int, refresh_all: bool = True, timeout_seconds: int = 2400) -> str:
    """Start a trigger backfill for one event and wait for its terminal state —
    the nightly events task fetches rates for EACH new event in sequence."""
    started = start_trigger_backfill({"event_id": int(event_id), "refresh_all": bool(refresh_all)})
    job_id = str(started.get("job_id") or "")
    deadline = time.time() + int(timeout_seconds)
    while time.time() < deadline:
        job = _get_trigger_job_for_response(job_id)
        status = str(job.get("status") or "")
        if status in ("completed", "failed", "canceled", "paused"):
            err = str(job.get("error") or job.get("last_error") or "")
            if status == "completed":
                return f"completed ({job.get('ok')} players)"
            return f"{status}: {err[:140]}"
        time.sleep(5)
    return "timeout"


def _get_latest_trigger_job() -> Optional[Dict[str, Any]]:
    from backend.data.db import connect as _db_connect

    conn = _db_connect()
    try:
        row = conn.execute("SELECT job_id FROM trigger_rates_jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return _get_trigger_job_for_response(str(row["job_id"])) if row else None


@router.get("/trigger-rates-backfill/latest")
def get_latest_trigger_backfill_job():
    job = _get_latest_trigger_job()
    if not job:
        return {"exists": False}
    return {"exists": True, **_trigger_job_response(job)}


@router.get("/trigger-rates-backfill/job/{job_id}")
def get_trigger_backfill_job(job_id: str):
    return _trigger_job_response(_get_trigger_job_for_response(job_id))


@router.post("/trigger-rates-backfill/job/{job_id}/pause")
def pause_trigger_backfill_job(job_id: str):
    job = _get_trigger_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed", "paused"}:
        return _trigger_job_response(job)
    job["pause_requested"] = True
    job["status"] = "pausing" if _trigger_worker_is_active(job_id) else "paused"
    _publish_trigger_job(job)
    return _trigger_job_response(job)


@router.post("/trigger-rates-backfill/job/{job_id}/cancel")
def cancel_trigger_backfill_job(job_id: str):
    job = _get_trigger_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled", "failed"}:
        return _trigger_job_response(job)
    job["cancel_requested"] = True
    job["status"] = "canceling" if _trigger_worker_is_active(job_id) else "canceled"
    if job["status"] == "canceled":
        job["cancel_requested"] = False
        job["finished_at"] = time.time()
    _publish_trigger_job(job)
    return _trigger_job_response(job)


@router.post("/trigger-rates-backfill/job/{job_id}/resume")
def resume_trigger_backfill_job(job_id: str):
    job = _get_trigger_job_for_response(job_id)
    if job.get("status") in {"completed", "canceled"}:
        return _trigger_job_response(job)
    if job.get("status") == "running" and _trigger_worker_is_active(job_id):
        return _trigger_job_response(job)
    job["status"] = "queued"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _TRIGGER_INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["finished_at"] = None
    _publish_trigger_job(job)
    _start_trigger_worker(job_id, None)
    return _trigger_job_response(_get_trigger_job_for_response(job_id))


def _start_trigger_worker(job_id: str, event_id, refresh_all: bool = False) -> None:
    with TRIGGER_JOBS_LOCK:
        worker = TRIGGER_WORKERS.get(job_id)
        if worker and worker.is_alive():
            return
        worker = threading.Thread(
            target=_run_trigger_backfill_job, args=(job_id, event_id, refresh_all), daemon=True
        )
        TRIGGER_WORKERS[job_id] = worker
        worker.start()


def _cheapest_event_players(fantasy_id: int, count: int = 5) -> List[int]:
    """Cheapest valid lineup: HLTV rejects lineups with more than 2 players
    from the same team, so cap picks per team while walking prices upward."""
    from backend.data.event_db import get_event_detail

    detail = get_event_detail(int(fantasy_id)) or {}
    players = sorted(
        (p for p in (detail.get("players") or []) if p.get("player_id")),
        key=lambda p: int(p.get("price") or 0),
    )
    picked: List[int] = []
    per_team: Dict[str, int] = {}
    for p in players:
        team = str(p.get("team_name") or "")
        if per_team.get(team, 0) >= 2:
            continue
        picked.append(int(p["player_id"]))
        per_team[team] = per_team.get(team, 0) + 1
        if len(picked) >= count:
            break
    return picked


def _auto_provision_template(driver, fantasy_id: int) -> Dict[str, Any]:
    """Create the app account's fantasy team for an event with no human in the
    loop, replaying the SPA's own join flow (recorded from real capture
    sessions in trigger_capture_debug.json): POST leagues/join into the
    event's featured public league with a minimal 5-player lineup, resolve the
    created team via gameredirect, and build the trigger-rates URL — the
    draft id equals the fantasy event id (verified on events 642/644).
    Raises loudly at any deviation from the recorded flow."""
    fantasy_id = int(fantasy_id)
    base = f"https://www.hltv.org/fantasy/{fantasy_id}"

    def _team_from_gameredirect():
        driver.get(f"{base}/gameredirect")
        time.sleep(5)
        landed = str(getattr(driver, "current_url", "") or "")
        m = re.search(rf"/fantasy/{fantasy_id}/league/(\d+)/team/(\d+)", landed)
        return (int(m.group(1)), int(m.group(2))) if m else (None, landed)

    league_id, team_or_landed = _team_from_gameredirect()
    if league_id is None:
        # No team yet: join the featured public league (first league link on
        # the overview page — gameredirect lands there when teamless).
        league_href = driver.execute_script(
            f"var a = document.querySelector('a[href^=\"/fantasy/{fantasy_id}/league/\"]');"
            "return a ? a.getAttribute('href') : null;"
        )
        lm = re.search(rf"/fantasy/{fantasy_id}/league/(\d+)", str(league_href or ""))
        if not lm:
            raise RuntimeError(f"No public league link on the fantasy overview for event {fantasy_id}")
        join_league_id = int(lm.group(1))
        lineup = _cheapest_event_players(fantasy_id, 5)
        if len(lineup) < 5:
            raise RuntimeError(f"Event {fantasy_id} has fewer than 5 priced players stored — import it first")
        join_body = json.dumps(
            {
                "leagueId": {"id": join_league_id},
                "leagueSecret": None,
                "teamName": "wrenchtest",
                "lineup": [{"playerId": pid} for pid in lineup],
            }
        )
        join_script = """
            var url = arguments[0], body = arguments[1];
            var done = arguments[arguments.length - 1];
            (async function(){
                try {
                    var r = await fetch(url, {method:"POST", headers:{"content-type":"application/json"}, credentials:"include", body: body});
                    var t = await r.text();
                    done({ok: r.ok, status: r.status, text: t.slice(0, 400)});
                } catch (e) { done({ok:false, error:String(e)}); }
            })();
        """
        res = driver.execute_async_script(join_script, f"{base}/leagues/join", join_body)
        if not (res or {}).get("ok"):
            raise RuntimeError(f"leagues/join failed for event {fantasy_id}: {json.dumps(res)[:300]}")
        try:
            join_data = json.loads((res or {}).get("text") or "{}")
        except Exception:
            join_data = {}
        if join_data.get("error"):
            # HLTV's own refusal (e.g. "Game for league has already started") —
            # surface it verbatim; there is nothing to retry.
            raise RuntimeError(f"HLTV refused the league join for event {fantasy_id}: {join_data['error']}")
        created = join_data.get("createdTeamId")
        created_id = created.get("id") if isinstance(created, dict) else created
        if created_id:
            league_id, team_or_landed = join_league_id, int(created_id)
        else:
            league_id, team_or_landed = _team_from_gameredirect()
        if league_id is None:
            # gameredirect can land on the overview even with a team; the nav
            # dropdown lists the account's own teams — check there too.
            own_href = driver.execute_script(
                f"var a = document.querySelector('a.dropdown-link[href*=\"/fantasy/{fantasy_id}/league/\"][href*=\"/team/\"]');"
                "return a ? a.getAttribute('href') : null;"
            )
            m2 = re.search(rf"/fantasy/{fantasy_id}/league/(\d+)/team/(\d+)", str(own_href or ""))
            if m2:
                league_id, team_or_landed = int(m2.group(1)), int(m2.group(2))
            else:
                raise RuntimeError(
                    f"Joined league {join_league_id} for event {fantasy_id} (response: "
                    f"{json.dumps(res)[:300]}) but no team page found — gameredirect landed on "
                    f"{team_or_landed!r} and the nav dropdown has no own-team link"
                )
    team_id = int(team_or_landed)

    trig_url = (
        f"https://www.hltv.org/fantasy/{fantasy_id}/league/{league_id}"
        f"/overview/{team_id}/draft/{fantasy_id}/triggerrates/json"
    )
    verify = driver.execute_async_script(
        """
        var url = arguments[0]; var done = arguments[arguments.length - 1];
        fetch(url, {credentials:"include", headers:{accept:"application/json"}})
          .then(function(r){ return r.text(); })
          .then(function(t){ done({ok: t.indexOf("playerTriggerRates") !== -1, text: t.slice(0, 200)}); })
          .catch(function(e){ done({ok:false, error:String(e)}); });
        """,
        trig_url,
    )
    if not (verify or {}).get("ok"):
        raise RuntimeError(
            f"Auto-provisioned trigger endpoint returned no playerTriggerRates: {json.dumps(verify)[:300]}"
        )
    template = {
        "url": trig_url,
        "method": "GET",
        "post_data": None,
        "captured_player_ids": [],
        "page_url": f"https://www.hltv.org/fantasy/{fantasy_id}/league/{league_id}/team/{team_id}",
        "captured_at": time.time(),
        "auto_provisioned": True,
    }
    _save_trigger_template(template)
    return template


def _auto_discover_template(driver, event_id, wait_seconds: int = 200) -> Dict[str, Any]:
    """Capture the playerTriggerRates request template.

    The rates are served by an XHR that only fires when a lineup is edited in
    the fantasy SPA, and the endpoint is not present in any static HTML/JSON, so
    a passive page load cannot capture it. This opens the draft builder and
    watches the network for up to the capture window while the user adds a few
    players; the first matching request becomes the reusable template.
    """
    from backend.data.event_db import get_active_event_id

    fantasy_id = int(event_id or get_active_event_id() or 0)
    if not fantasy_id:
        raise RuntimeError("No active event to discover against.")

    # Land on the fantasy area and simply watch the network. The user signs in
    # (if needed) and adds players in this same window; we capture whenever the
    # request fires, wherever they navigate — no pre-login/team resolution.
    driver.get(f"https://www.hltv.org/fantasy/{fantasy_id}/gameredirect")
    time.sleep(4)
    deadline = time.time() + int(wait_seconds)
    while time.time() < deadline:
        found = _collect_trigger_requests(driver, settle_seconds=5)
        if found:
            seen_ids: List[int] = []
            for item in found:
                try:
                    data = json.loads(item["response_body"])
                except Exception:
                    continue
                ptr = data.get("playerTriggerRates") or []
                for entry in ptr:
                    pid = (entry.get("playerId") or {}).get("playerId")
                    if pid is not None:
                        seen_ids.append(int(pid))
                if ptr:
                    _ingest_trigger_rates(ptr)
            if seen_ids:
                template = {
                    "url": found[0]["url"],
                    "method": found[0]["method"],
                    "post_data": found[0].get("post_data"),
                    "captured_player_ids": seen_ids[:10],
                    "page_url": str(getattr(driver, "current_url", "") or ""),
                    "captured_at": time.time(),
                }
                _save_trigger_template(template)
                return template

    raise RuntimeError(
        "No trigger-rates request captured. In the window, sign in and add any 5 players to your fantasy team."
    )


def _run_trigger_backfill_job(job_id: str, event_id, refresh_all: bool = False) -> None:
    job = _get_stored_trigger_job(job_id)
    if not job:
        return

    def stop_requested() -> str:
        with TRIGGER_JOBS_LOCK:
            live = TRIGGER_JOBS.get(job_id) or {}
        if live.get("cancel_requested"):
            return "canceled"
        if live.get("pause_requested"):
            return "paused"
        return ""

    job["status"] = "running"
    job["pause_requested"] = False
    job["cancel_requested"] = False
    job["error"] = ""
    if job.get("last_error") in _TRIGGER_INTERRUPTION_BOILERPLATE:
        job["last_error"] = ""
    job["started_at"] = job.get("started_at") or time.time()
    job["finished_at"] = None
    job["current_item"] = "Scanning event players for missing booster/role data"
    _publish_trigger_job(job)

    try:
        # Recomputed on every (re)start, so already-enriched players are
        # skipped automatically and resume continues where it left off.
        price_map, missing, _cov = _missing_trigger_players(event_id)
        if not price_map:
            raise RuntimeError("No event player prices found. Import the event first.")
        # refresh_all re-reads every priced player (rates drift over time —
        # a new event wants everyone current, not just the never-fetched).
        cover = set(price_map.keys()) if refresh_all else set(missing)
        batches = _build_trigger_batches(price_map, _player_teams_map(), 1_000_000, cover=cover)
        total = len(batches)
        job["total_items"] = total
        job["processed_items"] = 0
        job["progress"] = 0.0 if total else 1.0
        _publish_trigger_job(job)

        if not batches:
            job["status"] = "completed"
            job["current_item"] = ""
            job["finished_at"] = time.time()
            _publish_trigger_job(job)
            return

        ok = int(job.get("ok") or 0)
        failed = int(job.get("failed") or 0)

        def run_batches(driver):
            nonlocal ok, failed
            # Stage 1: guarantee the session is the app's own account (not any
            # personal login that happens to be persisted in the profile).
            job["current_item"] = "Signing in as the app account"
            _publish_trigger_job(job)
            _ensure_app_login(driver)

            # Stage 2: need the trigger-rates GET endpoint. The endpoint reads
            # whatever lineup is saved on the team, so covering every player
            # means saving each batch as the lineup, then reading its rates.
            # When no endpoint matches this event (new event, no capture),
            # auto-provision one: join a public league as the app account,
            # which creates the team the endpoint needs.
            from backend.data.event_db import get_active_event_id

            target_fid = int(event_id or get_active_event_id() or 0)
            template = _load_trigger_template()
            if not _template_matches_event(template, target_fid):
                if not target_fid:
                    raise RuntimeError("No target event for the trigger backfill")
                job["current_item"] = f"Creating fantasy team for event {target_fid}"
                _publish_trigger_job(job)
                template = _auto_provision_template(driver, target_fid)
            trig_url = str((template or {}).get("url") or "")
            m = re.search(r"/fantasy/(\d+)/league/(\d+)/overview/(\d+)/draft/(\d+)/triggerrates", trig_url)
            if not m:
                raise RuntimeError(f"Trigger endpoint URL has an unexpected shape: {trig_url!r}")
            fantasy_id, league_id, team_id, draft_id = (int(x) for x in m.groups())
            save_url = f"https://www.hltv.org/fantasy/{fantasy_id}/teams/edit"
            try:
                driver.get(str((template or {}).get("page_url") or f"https://www.hltv.org/fantasy/{fantasy_id}/overview"))
                time.sleep(3)
            except Exception:
                pass

            # Save the batch as the team lineup, then read the rates it produces.
            script = """
                var saveUrl = arguments[0], saveBody = arguments[1], trigUrl = arguments[2];
                var done = arguments[arguments.length - 1];
                (async function(){
                    try {
                        var sr = await fetch(saveUrl, {method:"POST", headers:{"content-type":"application/json"}, credentials:"include", body: saveBody});
                        if (!sr.ok) { done({ok:false, error:"save HTTP " + sr.status}); return; }
                        await new Promise(function(r){ setTimeout(r, 500); });
                        var rr = await fetch(trigUrl, {credentials:"include", headers:{accept:"application/json"}});
                        var tt = await rr.text();
                        done({ok:true, text: tt});
                    } catch (e) { done({ok:false, error:String(e)}); }
                })();
            """
            updated_ids: set = set()
            for idx, batch in enumerate(batches):
                stopped = stop_requested()
                if stopped:
                    return stopped
                job["current_item"] = f"Batch {idx + 1} / {total} ({len(batch)} players)"
                save_body = json.dumps(
                    {
                        "leagueId": {"id": league_id},
                        "teamId": {"id": team_id},
                        "draft": {"id": draft_id},
                        "updatedPlayers": [{"playerId": int(p)} for p in batch],
                    }
                )
                try:
                    res = driver.execute_async_script(script, save_url, save_body, trig_url)
                except Exception as exc:
                    res = {"ok": False, "error": str(exc)}
                new_ids = 0
                if res and res.get("ok"):
                    try:
                        data = json.loads(res.get("text") or "{}")
                        ptr = data.get("playerTriggerRates") or []
                        if ptr:
                            before = len(updated_ids)
                            for entry in ptr:
                                pid = (entry.get("playerId") or {}).get("playerId")
                                if pid is not None:
                                    updated_ids.add(int(pid))
                            _ingest_trigger_rates(ptr)
                            new_ids = len(updated_ids) - before
                    except Exception as exc:
                        job["last_error"] = f"Batch {idx + 1}: {exc}"
                else:
                    job["last_error"] = f"Batch {idx + 1}: {(res or {}).get('error') or 'request failed'}"
                if new_ids <= 0:
                    failed += 1
                job["processed_items"] = idx + 1
                # ok = unique players actually covered, so it can never exceed
                # the event's player count (no double-counting across batches).
                job["ok"] = len(updated_ids)
                ok = len(updated_ids)
                job["failed"] = failed
                job["progress"] = (idx + 1) / float(total)
                # Merge live pause/cancel requests before publishing.
                with TRIGGER_JOBS_LOCK:
                    live = TRIGGER_JOBS.get(job_id) or {}
                job["pause_requested"] = bool(live.get("pause_requested"))
                job["cancel_requested"] = bool(live.get("cancel_requested"))
                if job["cancel_requested"]:
                    job["status"] = "canceling"
                elif job["pause_requested"]:
                    job["status"] = "pausing"
                else:
                    job["status"] = "running"
                _publish_trigger_job(job)
                time.sleep(1.0)
            return ""

        stopped = run_hltv_browser_session("https://www.hltv.org", run_batches, timeout_ms=120000, wait_text=None)
        if stopped:
            job["status"] = stopped
            job["pause_requested"] = False
            job["cancel_requested"] = False
            job["last_error"] = "Canceled" if stopped == "canceled" else "Paused"
            job["finished_at"] = time.time()
            _publish_trigger_job(job)
            return

        job["status"] = "completed"
        job["current_item"] = ""
        job["finished_at"] = time.time()
        _publish_trigger_job(job)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = time.time()
        _publish_trigger_job(job)
    finally:
        with TRIGGER_JOBS_LOCK:
            TRIGGER_WORKERS.pop(job_id, None)


@router.post("/inspect-hltv-simulator")
def inspect_hltv_simulator(payload: Dict[str, Any]):
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if url.startswith("/events/"):
        url = f"https://www.hltv.org{url}"
    if "#simulator" not in url:
        url = url.rstrip("/") + "#simulator"
    if not HLTV_EVENT_URL_RE.match(url):
        raise HTTPException(status_code=400, detail="Use a valid HLTV event URL, e.g. https://www.hltv.org/events/8914/xse-pro-league-2026#simulator")

    try:
        html = fetch_hltv_html(url, wait_text="Simulator", timeout_ms=60000)
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch HLTV event page with SeleniumBase UC: {exc}") from exc

    return _inspect_hltv_simulator_html(url, html)


def _hltv_simulator_url_from_payload(payload: Dict[str, Any]) -> str:
    url = str(payload.get("url") or "").strip()
    hltv_event_url = str(payload.get("hltv_event_url") or "").strip()
    hltv_event_id = str(payload.get("hltv_event_id") or "").strip()
    fantasy_event_id = str(payload.get("fantasy_event_id") or payload.get("event_id") or "").strip()
    if not url and hltv_event_url:
        url = hltv_event_url
    if not url and hltv_event_id:
        if not hltv_event_id.isdigit():
            raise HTTPException(status_code=400, detail="hltv_event_id must be numeric")
        url = f"https://www.hltv.org/events/{hltv_event_id}"
    if not url and fantasy_event_id:
        if not fantasy_event_id.isdigit():
            raise HTTPException(status_code=400, detail="fantasy_event_id must be numeric")
        event = get_event_detail(int(fantasy_event_id)) or {}
        stored_hltv_event_id = event.get("hltv_event_id")
        stored_hltv_event_url = str(event.get("hltv_event_url") or "").strip()
        if stored_hltv_event_url:
            url = stored_hltv_event_url
        elif stored_hltv_event_id:
            url = f"https://www.hltv.org/events/{stored_hltv_event_id}"
        else:
            ref = _extract_hltv_event_ref_from_fantasy(int(fantasy_event_id))
            if ref.get("hltv_event_id"):
                set_event_hltv_ref(int(fantasy_event_id), ref.get("hltv_event_id"), ref.get("hltv_event_url"))
                url = ref.get("hltv_event_url") or f"https://www.hltv.org/events/{ref['hltv_event_id']}"
    if not url:
        raise HTTPException(status_code=400, detail="url, hltv_event_id, or fantasy_event_id is required")
    if url.startswith("/events/"):
        url = f"https://www.hltv.org{url}"
    if "#simulator" not in url:
        url = url.rstrip("/") + "#simulator"
    if not re.match(r"^https?://(?:www\.)?hltv\.org/events/\d+(?:/[A-Za-z0-9_-]+)?(?:#simulator)?/?$", url):
        raise HTTPException(status_code=400, detail="Use a valid HLTV event URL or numeric event_id")
    return url


@router.post("/infer-hltv-simulator-pairing")
def infer_hltv_simulator_pairing(payload: Dict[str, Any]):
    url = _hltv_simulator_url_from_payload(payload)

    scenario_names = payload.get("scenarios")
    if not isinstance(scenario_names, list):
        scenario_names = None
    else:
        scenario_names = [str(name) for name in scenario_names if str(name) in {"higher_seed_wins", "left_wins", "right_wins", "alternating"}]

    try:
        result = _run_hltv_pairing_probe(url, scenario_names=scenario_names)
    except HLTVBrowserError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to probe HLTV simulator with SeleniumBase UC: {exc}") from exc

    result["url"] = url
    ref = _extract_hltv_event_ref(url)
    if ref.get("hltv_event_id"):
        result.update(ref)
    return result


