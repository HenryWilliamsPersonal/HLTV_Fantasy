"""Structured tournament-format detection from HLTV event pages.

Modern HLTV event pages embed their full structure as typed JSON:

- ``data-swiss-simulator-json`` — ``SwissSimulatorGroup.<Variant>`` (e.g.
  ``SixteenEight`` = 16 teams, top 8) with the full team list, initial
  seedings, pairing rules (round 1 + Buchholz) and per-record matchups.
- ``data-slotted-bracket-json`` — ``Bracket.SingleElimination`` /
  ``Bracket.DoubleElimination4`` / ``Bracket.DoubleElimination8`` (October
  2026 pages: ``singleElimination`` / ``doubleElimination4`` / ``doubleElimination8``) with named
  brackets ("Playoffs", "Group A", "3rd Place Decider Match") and
  ``FixedTeam`` slots carrying team ids + names.
- ``<table class="formats table">`` — per-stage format text
  ("Group stage" -> "Swiss Bo3").

Detection reads ONLY these structured markers; the old scrape heuristics
(bracket-shape scans + team-count guessing) are gone. Pages without the
embedded JSON (pre-2026 archives) simply come back empty.
"""

import html as _htmllib
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SWISS_JSON_RE = re.compile(r'data-swiss-simulator-json="([^"]+)"')
# Majors embed one blob for the whole tournament: swissStages[] + playoffs.
_MULTI_JSON_RE = re.compile(r'data-multi-stage-simulator-json="([^"]+)"')
_BRACKET_JSON_RE = re.compile(r'data-slotted-bracket-json="([^"]+)"')
_FORMATS_TABLE_RE = re.compile(r'<table class="formats table">.*?</table>', re.DOTALL)
_FORMAT_ROW_RE = re.compile(r'format-header">([^<]+)</th>\s*<td class="format-data">([^<]*)</td>')

# CamelCase word-number tokens used in swiss variant names ("SixteenEight").
_WORD_NUMBERS = {
    "One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5, "Six": 6,
    "Seven": 7, "Eight": 8, "Nine": 9, "Ten": 10, "Twelve": 12,
    "Fourteen": 14, "Sixteen": 16, "Twenty": 20, "TwentyFour": 24,
    "ThirtyTwo": 32,
}


def _decode_attr_json(raw: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(_htmllib.unescape(raw))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _variant_numbers(token: str) -> List[int]:
    """'SixteenEight' -> [16, 8] (longest word-number match first)."""
    numbers: List[int] = []
    rest = token
    while rest:
        match = None
        for word in sorted(_WORD_NUMBERS, key=len, reverse=True):
            if rest.startswith(word):
                match = word
                break
        if not match:
            break
        numbers.append(_WORD_NUMBERS[match])
        rest = rest[len(match):]
    return numbers if not rest else numbers  # partial parses keep what matched


def _put_team(out: Dict[int, str], tid: Any, name: Any) -> None:
    try:
        tid_int = int(tid)
    except (TypeError, ValueError):
        return
    label = str(name or "").strip()
    if tid_int > 0 and label and label.upper() != "TBD":
        out[tid_int] = label


def _collect_teams(node: Any, out: Dict[int, str]) -> None:
    """Walk any of the embedded JSON shapes collecting team id -> name."""
    if isinstance(node, dict):
        # Bracket FixedTeam: {"id": N, "name": "X", "profileURL": "/team/N/x"}
        if str(node.get("profileURL") or "").startswith("/team/") and node.get("name"):
            _put_team(out, node.get("id"), node.get("name"))
        # Swiss team entries: {"teamId": {..., "teamId": {"teamId": N}}, "teamName": "X"}
        if node.get("teamName") and isinstance(node.get("teamId"), dict):
            inner = node["teamId"].get("teamId")
            tid = inner.get("teamId") if isinstance(inner, dict) else inner
            _put_team(out, tid, node.get("teamName"))
        # SlotTeam.Known: {"id": {"teamId": N}, "name": "X", "logo": {...}} (multi-stage
        # blobs) or {"type": "...SlotTeam.Known", "id": {"teamId": N}, "name": "X"} (2026 brackets)
        if node.get("name") and isinstance(node.get("id"), dict) and (
            node.get("logo") or str(node.get("type") or "").endswith("SlotTeam.Known") or node.get("type") == "known"
        ):
            _put_team(out, node["id"].get("teamId"), node.get("name"))
        for value in node.values():
            _collect_teams(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_teams(value, out)


def parse_formats_table(html: str) -> List[Dict[str, str]]:
    """The event page's own per-stage format table."""
    table = _FORMATS_TABLE_RE.search(html or "")
    if not table:
        return []
    return [
        {"stage": stage.strip(), "format": _htmllib.unescape(text).strip()}
        for stage, text in _FORMAT_ROW_RE.findall(table.group(0))
    ]


def _swiss_stage_from_group(group: Dict[str, Any], variant: str, stage_name: str, event_name: str) -> Dict[str, Any]:
    numbers = _variant_numbers(variant)
    teams: Dict[int, str] = {}
    _collect_teams(group.get("teams"), teams)
    successor = group.get("successorRoundRules") or {}
    # Official seed order: initialSeedings.orderedSeedings[].seedingOrdinal.
    seed_pairs = []
    for entry in (group.get("initialSeedings") or {}).get("orderedSeedings") or []:
        tid_node = (entry.get("teamId") or {}).get("teamId")
        tid = tid_node.get("teamId") if isinstance(tid_node, dict) else tid_node
        try:
            seed_pairs.append((int(entry.get("seedingOrdinal")), int(tid)))
        except (TypeError, ValueError):
            continue
    seedings = [tid for _, tid in sorted(seed_pairs) if tid > 0]
    return {
        "seedings": seedings,
        "variant": variant,
        "stage_name": stage_name,
        "team_count": numbers[0] if numbers else len(teams),
        "advance_count": numbers[1] if len(numbers) > 1 else 0,
        "teams": teams,
        "first_round_rule": str(group.get("firstRoundRules") or ""),
        "successor_rule": str(successor.get("type") or "").rsplit(".", 1)[-1],
        "event_name": event_name or str(group.get("eventName") or ""),
    }


def parse_swiss_stages(html: str) -> List[Dict[str, Any]]:
    stages: List[Dict[str, Any]] = []
    for raw in _SWISS_JSON_RE.findall(html or ""):
        data = _decode_attr_json(raw)
        if not data:
            continue
        type_name = str(data.get("type") or "")
        if ".swiss." not in type_name.lower():
            continue
        stages.append(
            _swiss_stage_from_group(data, type_name.rsplit(".", 1)[-1], "", str(data.get("eventName") or ""))
        )
    # Major-style multi-stage blob: one JSON with every swiss stage.
    for raw in _MULTI_JSON_RE.findall(html or ""):
        data = _decode_attr_json(raw)
        if not data:
            continue
        for st in data.get("swissStages") or []:
            group = st.get("group") or {}
            if not isinstance(group, dict) or not group.get("teams"):
                continue
            stages.append(
                _swiss_stage_from_group(group, "", str(st.get("name") or ""), str(data.get("name") or ""))
            )
    return stages


# Bracket names that are tie-break side shows, not stages of their own.
_AUX_BRACKET_RE = re.compile(r"3rd|third|decider", re.IGNORECASE)


def _de_slots(section: Any) -> List[Dict[str, Any]]:
    """A DE bracket section's slots in order. HLTV embeds them either as a
    `slots` list (older pages) or as `slot1`, `slot2`, ... keys (2026 pages,
    e.g. StarLadder StarSeries Fall 2026)."""
    if not isinstance(section, dict):
        return []
    slots = section.get("slots")
    if isinstance(slots, list):
        return [s for s in slots if isinstance(s, dict)]
    keyed = []
    for key, value in section.items():
        m = re.fullmatch(r"slot(\d+)", str(key))
        if m and isinstance(value, dict):
            keyed.append((int(m.group(1)), value))
    keyed.sort(key=lambda kv: kv[0])
    return [v for _n, v in keyed]


# How HLTV routes a whole-event 8-team double-elimination bracket
# (section, slot number) -> the two feeders (slotEntry type, source slot id).
# Lower semis are crossed (each upper-semi loser meets the OTHER side's
# lower-round-1 winner); the consolidation final is the upper-final loser v
# the lower-final winner. The simulator plays exactly this layout, so a page
# routed any other way is refused rather than mis-simulated.
_DE8_FULL_ROUTING = {
    ("upperRound2", 1): (("WinnerOf", "UpperQuarters1"), ("WinnerOf", "UpperQuarters2")),
    ("upperRound2", 2): (("WinnerOf", "UpperQuarters3"), ("WinnerOf", "UpperQuarters4")),
    ("upperFinal", 1): (("WinnerOf", "UpperSemis1"), ("WinnerOf", "UpperSemis2")),
    ("lowerRound1", 1): (("LoserOf", "UpperQuarters1"), ("LoserOf", "UpperQuarters2")),
    ("lowerRound1", 2): (("LoserOf", "UpperQuarters3"), ("LoserOf", "UpperQuarters4")),
    ("lowerDropdown1", 1): (("LoserOf", "UpperSemis2"), ("WinnerOf", "LowerRound1Match1")),
    ("lowerDropdown1", 2): (("LoserOf", "UpperSemis1"), ("WinnerOf", "LowerRound1Match2")),
    ("lowerRound2", 1): (("WinnerOf", "LowerSemis1"), ("WinnerOf", "LowerSemis2")),
    ("consolidationFinal", 1): (("LoserOf", "UpperFinal"), ("WinnerOf", "LowerFinal")),
    ("grandFinal", 1): (("WinnerOf", "UpperFinal"), ("WinnerOf", "ConsolidationFinal")),
}


def _slot_side_team(slot: Dict[str, Any], side: str) -> Optional[Dict[str, Any]]:
    """{id, name} of a known team on one side of a slot (2026 schema keeps the
    sides under `matchup`; older pages at the slot level), else None."""
    matchup = slot.get("matchup") if isinstance(slot.get("matchup"), dict) else {}
    t = matchup.get(side) or slot.get(side) or {}
    if not isinstance(t, dict):
        return None
    raw_id = t.get("id")
    tid = raw_id.get("teamId") if isinstance(raw_id, dict) else raw_id
    if isinstance(t.get("team"), dict):  # older FixedTeam: {"team": {"id": N, "name": ...}}
        tid = t["team"].get("id", tid)
        name = t["team"].get("name")
    else:
        name = t.get("name")
    try:
        tid_int = int(tid)
    except (TypeError, ValueError):
        return None
    if tid_int <= 0 or not name:
        return None
    return {"id": tid_int, "name": str(name)}


def parse_double8_full(data: Dict[str, Any]) -> Dict[str, Any]:
    """Seeds (upper-round-1 slot order) and the grand-final Bo of a whole-event
    8-team double-elimination bracket, after checking the page routes every
    match the way the simulator plays it (_DE8_FULL_ROUTING). Raises
    ValueError on any other routing."""

    def entry(slot: Dict[str, Any], n: int) -> tuple:
        e = slot.get(f"slotEntry{n}") or {}
        return (str(e.get("type") or ""), str(((e.get("slotId") or {}).get("id")) or ""))

    def slot_id(slot: Dict[str, Any]) -> str:
        return str(((slot.get("slotId") or {}).get("id")) or "")

    def side_ids(slot: Dict[str, Any], side: str) -> set:
        """Team ids a side can hold: the known team, or its oneOf candidates."""
        t = ((slot.get("matchup") or {}).get(side)) or {}
        if not isinstance(t, dict):
            return set()
        if t.get("type") == "oneOf":
            out = set()
            for cand in t.get("candidates") or []:
                raw = (cand or {}).get("id")
                tid = raw.get("teamId") if isinstance(raw, dict) else raw
                if tid:
                    out.add(int(tid))
            return out
        known = _slot_side_team(slot, side)
        return {known["id"]} if known else set()

    explicit = any("slotEntry1" in slot for section in data.values() for slot in _de_slots(section))
    if explicit:
        for (section, idx), expected in _DE8_FULL_ROUTING.items():
            slots = _de_slots(data.get(section))
            if len(slots) < idx:
                raise ValueError(f"DE8 bracket has no {section} slot {idx}")
            got = (entry(slots[idx - 1], 1), entry(slots[idx - 1], 2))
            if got != expected:
                raise ValueError(f"DE8 routing at {section}[{idx}] is {got}, expected {expected}")
    else:
        # October-2026 schema: no source slot ids. The page must carry exactly
        # the simulator's sections and slot ids, and wherever it lists the
        # candidate teams of a slot they must be the feeders the simulator
        # routes there (the upper bracket and lower round 1); the lower
        # bracket's crossing is only described in prose, so it is taken as
        # HLTV's standard layout, which every DE8 page so far has used.
        expected_ids = {
            "upperRound1": ["UpperQuarters1", "UpperQuarters2", "UpperQuarters3", "UpperQuarters4"],
            "upperRound2": ["UpperSemis1", "UpperSemis2"],
            "upperFinal": ["UpperFinal"],
            "lowerRound1": ["LowerRound1Match1", "LowerRound1Match2"],
            "lowerDropdown1": ["LowerSemis1", "LowerSemis2"],
            "lowerRound2": ["LowerFinal"],
            "consolidationFinal": ["ConsolidationFinal"],
            "grandFinal": ["GrandFinal"],
        }
        for section, ids in expected_ids.items():
            got = [slot_id(sl) for sl in _de_slots(data.get(section))]
            if got != ids:
                raise ValueError(f"DE8 section {section} has slots {got}, expected {ids}")
        quarters = _de_slots(data.get("upperRound1"))
        q_teams = [side_ids(q, "team1") | side_ids(q, "team2") for q in quarters]
        feeders = {
            ("upperRound2", 0): (0, 1),
            ("upperRound2", 1): (2, 3),
            ("lowerRound1", 0): (0, 1),
            ("lowerRound1", 1): (2, 3),
        }
        for (section, idx), (qa, qb) in feeders.items():
            slot = _de_slots(data.get(section))[idx]
            for side, q in (("team1", qa), ("team2", qb)):
                cands = side_ids(slot, side)
                if cands and q_teams[q] and cands != q_teams[q]:
                    raise ValueError(f"DE8 {section}[{idx + 1}] {side} candidates {sorted(cands)} are not upper quarter {q + 1}'s teams {sorted(q_teams[q])}")
    seeds: List[Optional[Dict[str, Any]]] = []
    for slot in _de_slots(data.get("upperRound1"))[:4]:
        seeds.append(_slot_side_team(slot, "team1"))
        seeds.append(_slot_side_team(slot, "team2"))
    seeds = (seeds + [None] * 8)[:8]
    gf_slots = _de_slots(data.get("grandFinal"))
    gf_match = ((gf_slots[0].get("matchup") or {}).get("match") or {}) if gf_slots else {}
    try:
        gf_bo = int(gf_match.get("numberOfMaps") or 0)
    except (TypeError, ValueError):
        gf_bo = 0
    return {"seeds": seeds, "grand_final_bo": gf_bo}


def _de_section_state(data: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    """A DE bracket section's visibility + winner/loser routing, or None when
    the section has no slots at all."""
    slots = _de_slots(data.get(key))
    if not slots:
        return None
    return {
        "visible": any(not s.get("hidden") for s in slots),
        "winner_types": [(s.get("winnerType") or {}).get("type") for s in slots],
        "loser_types": [(s.get("loserType") or {}).get("type") for s in slots],
    }


def _classify_de_group(data: Dict[str, Any], type_name: str, name: str, size: int):
    """Which double-elim group variation this bracket plays, from the slot
    routing HLTV encodes (winnerType/loserType + hidden finals). Unexpected
    shapes raise so a new variation fails loudly instead of mislabeling.

    de8_top3 (Porto/Cologne): upperFinal played; its winner goes straight to
        the playoff semis, its loser + the lower-bracket winner to quarters.
    de8_top4 (EWC): bracket truncated — upperFinal and lowerRound2 hidden;
        upper-semi winners + lower-bracket survivors qualify (4).
    gsl4_top2 (GSL): grandFinal hidden; winners' match and decider winners
        qualify (2).
    """
    upper_final = _de_section_state(data, "upperFinal")
    grand_final = _de_section_state(data, "grandFinal")
    lower_round2 = _de_section_state(data, "lowerRound2")
    # Qualifier brackets route their deciding matches with an explicit
    # 'Qualifies' winner type (e.g. DE16 closed qualifier: upper final +
    # consolidation final qualify, grand final never played).
    qualify_count = 0
    for section in data.values():
        for slot in _de_slots(section):
            if slot.get("hidden"):
                continue
            if (slot.get("winnerType") or {}).get("type") == "Qualifies":
                qualify_count += 1
            if (slot.get("loserType") or {}).get("type") == "Qualifies":
                qualify_count += 1
    if qualify_count:
        return f"de{size}_qual{qualify_count}", {"count": qualify_count, "winner_to_semis": False}
    # A visible grand final = a FULL double-elimination bracket (a whole
    # qualifier/event played to its end), not a truncated group stage.
    if grand_final and grand_final["visible"]:
        return f"de{size}_full", {"count": 1, "winner_to_semis": False}
    if size == 8:
        if upper_final and upper_final["visible"]:
            if upper_final["winner_types"] != ["ToPlayoffsSemis"] or upper_final["loser_types"] != ["ToPlayoffsQuarters"]:
                raise ValueError(
                    f"Unrecognized DE8 upper-final routing in {name!r}: "
                    f"{upper_final['winner_types']}/{upper_final['loser_types']}"
                )
            return "de8_top3", {"count": 3, "winner_to_semis": True}
        if upper_final and not upper_final["visible"] and lower_round2 and not lower_round2["visible"]:
            return "de8_top4", {"count": 4, "winner_to_semis": False}
        raise ValueError(f"Unrecognized DoubleElimination8 shape in {name!r}")
    if size == 4:
        if grand_final and not grand_final["visible"]:
            return "gsl4_top2", {"count": 2, "winner_to_semis": False}
        raise ValueError(f"Unrecognized DoubleElimination4 shape in {name!r}")
    raise ValueError(f"Unrecognized double-elimination size in {name!r}: {type_name}")


def parse_bracket_stages(html: str) -> List[Dict[str, Any]]:
    brackets: List[Dict[str, Any]] = []
    for raw in _BRACKET_JSON_RE.findall(html or ""):
        data = _decode_attr_json(raw)
        if not data:
            continue
        type_name = str(data.get("type") or "").rsplit(".", 1)[-1]
        type_key = type_name.lower()  # "DoubleElimination8" (2025) / "doubleElimination8" (Oct 2026)
        name = str(data.get("name") or "")
        teams: Dict[int, str] = {}
        _collect_teams(data, teams)
        if type_key.startswith("doubleelimination"):
            try:
                size = int(re.sub(r"\D", "", type_name) or 0)
            except ValueError:
                size = 0
            variant, advance = _classify_de_group(data, type_name, name, size)
            entry = {
                "bracket": "double_elim", "size": size, "name": name, "teams": teams,
                "aux": False, "variant": variant, "advance": advance, "source": "slotted",
            }
            if variant == "de8_full":
                # a whole-event bracket the playoff simulator can play: seeds in
                # slot order, grand-final Bo, or the reason its routing is refused
                try:
                    full = parse_double8_full(data)
                    entry.update({"bracket_kind": "double", "seeds": full["seeds"], "grand_final_bo": full["grand_final_bo"]})
                except ValueError as exc:
                    entry.update({"bracket_kind": "double", "seeds": None, "routing_error": str(exc)})
            brackets.append(entry)
        elif type_key.startswith("singleelimination"):
            rounds = data.get("rounds") or []
            first = _de_slots(rounds[0]) if rounds else []
            byes = sum(1 for s in first if s.get("hidden"))
            visible = len(first) - byes
            brackets.append(
                {
                    "bracket": "single_elim",
                    "size": visible * 2 + byes,
                    "rounds": len(rounds),
                    "byes": byes,
                    "name": name,
                    "teams": teams,
                    "aux": bool(_AUX_BRACKET_RE.search(name)),
                    "source": "slotted",
                }
            )
    # Major-style multi-stage blob carries its playoff bracket inline.
    for raw in _MULTI_JSON_RE.findall(html or ""):
        data = _decode_attr_json(raw)
        if not data:
            continue
        playoff = data.get("playoffs") or {}
        bracket = playoff.get("bracket") if isinstance(playoff, dict) else None
        rounds = (bracket or {}).get("rounds") or []
        if not rounds:
            continue
        first = _de_slots(rounds[0])
        byes = sum(1 for s in first if s.get("hidden"))
        teams = {}
        _collect_teams(bracket, teams)
        name = str(playoff.get("name") or (bracket or {}).get("name") or "Playoffs")
        brackets.append(
            {
                "bracket": "single_elim",
                "size": (len(first) - byes) * 2 + byes,
                "rounds": len(rounds),
                "byes": byes,
                "name": name,
                "teams": teams,
                "aux": bool(_AUX_BRACKET_RE.search(name)),
                "source": "multi",
            }
        )
    return brackets


# Strict grammar for the swiss formats-table text. Exactly these line shapes
# are recognized; anything else on a swiss row raises so an HLTV wording
# change fails loudly instead of silently mis-assigning Bo1/Bo3.
_SWISS_MAIN_RE = re.compile(r"^Swiss Bo(\d)$")
_SWISS_SUB_RES = {
    "progression": re.compile(r"^- Progression matches Bo(\d)$"),
    "elimination": re.compile(r"^- Elimination matches Bo(\d)$"),
}


def parse_swiss_bo_rules(format_text: str) -> Optional[Dict[str, int]]:
    """Best-of rules from the formats-table text for a swiss stage.

    'Swiss Bo1\\n- Progression matches Bo3\\n- Elimination matches Bo3'
    -> {"default": 1, "progression": 3, "elimination": 3}   (the Major system)
    'Swiss Bo3' -> {"default": 3, "progression": 3, "elimination": 3}

    Returns None for a non-swiss format row; raises ValueError when the row
    is swiss but any line deviates from the known grammar.
    """
    lines = [line.strip() for line in str(format_text or "").splitlines() if line.strip()]
    if not lines:
        return None
    main = _SWISS_MAIN_RE.match(lines[0])
    if not main:
        if any("Swiss" in line for line in lines):
            raise ValueError(f"Unrecognized swiss format text: {format_text!r}")
        return None
    rules: Dict[str, int] = {"default": int(main.group(1))}
    for line in lines[1:]:
        for key, pattern in _SWISS_SUB_RES.items():
            sub = pattern.match(line)
            if sub:
                rules[key] = int(sub.group(1))
                break
        else:
            raise ValueError(f"Unrecognized swiss format line: {line!r}")
    rules.setdefault("progression", rules["default"])
    rules.setdefault("elimination", rules["default"])
    return rules


# Group-stage format grammar. Main line names the group style; Bo-bearing
# sub-lines must match exactly (loud failure on new wording); sub-lines with
# no Bo token are qualification prose and carry no Bo information.
_GROUP_MAIN_RE = re.compile(r"^(?:Double elimination|GSL) Bo(\d)$")
_GROUP_SUB_RES = {
    "upperRound1": re.compile(r"^- Upper bracket quarter-finals Bo(\d)$"),
    # Double-elimination brackets played as the whole event (closed qualifiers,
    # e.g. PGL Masters Bucharest 2026 Europe Closed Qualifier) end in a longer
    # grand final.
    "grandFinal": re.compile(r"^- Grand final Bo(\d)$"),
}
_ANY_BO_RE = re.compile(r"\bBo\d\b")


def parse_group_bo_rules(format_text: str) -> Optional[Dict[str, int]]:
    """Best-of rules for a double-elim/GSL group stage.

    'Double elimination Bo3\\n- Upper bracket quarter-finals Bo1' (EWC)
    -> {"default": 3, "upperRound1": 1}
    'GSL Bo3' -> {"default": 3}

    Returns None for non-group rows; raises ValueError when a group row has a
    Bo-bearing line outside the known grammar.
    """
    lines = [line.strip() for line in str(format_text or "").splitlines() if line.strip()]
    if not lines:
        return None
    main = _GROUP_MAIN_RE.match(lines[0])
    if not main:
        return None
    rules: Dict[str, int] = {"default": int(main.group(1))}
    for line in lines[1:]:
        for key, pattern in _GROUP_SUB_RES.items():
            sub = pattern.match(line)
            if sub:
                rules[key] = int(sub.group(1))
                break
        else:
            if _ANY_BO_RE.search(line):
                raise ValueError(f"Unrecognized group format line: {line!r}")
            # No Bo token: qualification prose ("Group winners advance to...").
    return rules


# Playoff format grammar (also used by Bounty stages and play-ins).
_PLAYOFF_MAIN_RE = re.compile(r"^(?:Play-in: )?Single elimination Bo(\d)$")
_PLAYOFF_SUB_RES = {
    "grandFinal": re.compile(r"^- Grand final Bo(\d)$"),
    "thirdPlace": re.compile(r"^(?:- )?3rd place decider match Bo(\d)$"),
}


def parse_playoff_bo_rules(format_text: str) -> Optional[Dict[str, int]]:
    """Best-of rules for a single-elimination playoff stage.

    'Single elimination Bo3\\n- Grand final Bo5\\n3rd place decider match Bo3'
    -> {"default": 3, "grandFinal": 5, "thirdPlace": 3}

    Returns None for non-playoff rows; raises ValueError when a playoff row
    has a Bo-bearing line outside the known grammar.
    """
    lines = [line.strip() for line in str(format_text or "").splitlines() if line.strip()]
    if not lines:
        return None
    main = _PLAYOFF_MAIN_RE.match(lines[0])
    if not main:
        return None
    rules: Dict[str, int] = {"default": int(main.group(1))}
    for line in lines[1:]:
        for key, pattern in _PLAYOFF_SUB_RES.items():
            sub = pattern.match(line)
            if sub:
                rules[key] = int(sub.group(1))
                break
        else:
            if _ANY_BO_RE.search(line):
                raise ValueError(f"Unrecognized playoff format line: {line!r}")
    return rules


def detect_event_structure(html: str) -> Dict[str, Any]:
    """Everything the page's structured markers say about the tournament."""
    formats_table = parse_formats_table(html)
    swiss = parse_swiss_stages(html)
    brackets = parse_bracket_stages(html)
    swiss_bo_rules = None
    group_bo_rules = None
    playoff_bo_rules = None
    for row in formats_table:
        text = row.get("format") or ""
        swiss_bo_rules = swiss_bo_rules or parse_swiss_bo_rules(text)
        group_bo_rules = group_bo_rules or parse_group_bo_rules(text)
        playoff_bo_rules = playoff_bo_rules or parse_playoff_bo_rules(text)
    # Loud contract: a stage that exists must have a recognized format row
    # (when the page has a formats table at all) — silence hides mislabeling.
    # Multi-stage (Major) playoff brackets are exempt: a per-stage page lists
    # only its own stage's format row.
    if formats_table:
        if swiss and not swiss_bo_rules:
            raise ValueError("Swiss stage present but no swiss row recognized in the formats table")
        if any(b["bracket"] == "double_elim" for b in brackets) and not group_bo_rules:
            raise ValueError("Group brackets present but no group row recognized in the formats table")
        if (
            any(b["bracket"] == "single_elim" and not b["aux"] and b.get("source") == "slotted" for b in brackets)
            and not playoff_bo_rules
        ):
            raise ValueError("Playoff bracket present but no playoff row recognized in the formats table")
    for stage in swiss:
        stage["bo_rules"] = swiss_bo_rules
    return {
        "formats_table": formats_table,
        "swiss": swiss,
        "brackets": brackets,
        "swiss_bo_rules": swiss_bo_rules,
        "group_bo_rules": group_bo_rules,
        "playoff_bo_rules": playoff_bo_rules,
    }
