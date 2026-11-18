"""Per player per map: the stats every HLTV fantasy role and booster is judged
on, rebuilt from a CS2 demo (demoparser2). Rules decoded against HLTV's own
tables on 10 IEM Beijing maps (2026-11-04) and HLTV's booster descriptions:

  round        = (previous round_officially_ended, this one]: exit frags and
                 post-round deaths stay in the round just played
  damage assist = player_death assister with assistedflash False, on the
                 killer's side (CS2 also credits the victim's teammates; HLTV drops them)
  traded death = the killer is killed by a teammate of the victim within 5 s
                 (whole seconds of tick time); each trade kill credits the
                 EARLIEST such teammate death
  KAST round   = kill or damage assist or survived or traded (flash assists don't count)
  Support round = no kill and (damage assist or survived)
  best pistol  = rounds 1 and 13: most kills, then most damage assists, then fewer
                 deaths (HLTV's own description); every player still tied
  clutch       = last alive on the side with 2+ enemies alive, and the side wins the round
  weapons      SMG = mp9/mac10/mp7/mp5sd/ump45/p90/bizon; shotgun = nova/xm1014/mag7/sawedoff;
               grenade = hegrenade/inferno (molotov, incendiary); knife*/taser = Rambo

Output: {steamid: {...counts...}} plus round count and the player names (for
matching to HLTV player ids).
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List

SMG = {"mp9", "mac10", "mp7", "mp5sd", "ump45", "p90", "bizon"}
SHOTGUN = {"nova", "xm1014", "mag7", "sawedoff"}
GRENADE = {"hegrenade", "inferno", "molotov", "incgrenade"}
TICKRATE = 64
TRADE_WINDOW_S = 5


def _int(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else int(f)


def _sid(v) -> str:
    s = str(v if v is not None else "")
    return "" if s in ("None", "nan", "0") else s


def _clean(r: dict) -> dict:
    """steamids as strings ('' when absent), team numbers as int or None"""
    for k in ("user_steamid", "attacker_steamid", "assister_steamid"):
        r[k] = _sid(r.get(k))
    for k in ("user_team_num", "attacker_team_num", "assister_team_num"):
        r[k] = _int(r.get(k))
    return r


def _melee(weapon: str) -> bool:
    w = weapon.lower()
    return w.startswith("knife") or w in ("taser", "bayonet") or "knife" in w


def parse_map(path: str, trade_phase: int = 0) -> Dict[str, Any]:
    return compute_map(load_map(path), trade_phase)


def compute_map(raw: tuple, trade_phase: int = 0) -> Dict[str, Any]:
    """The per-player stats of a loaded map (load_map) for one trade-window
    phase: the 5-second window counts whole seconds of tick time from an
    offset HLTV's logs fix per demo, calibrated on its traded-deaths column."""
    deaths, ends, official, freeze, p = raw
    return _compute(deaths, ends, official, freeze, p, trade_phase)


def load_map(path: str) -> tuple:
    """The demo's events, warmup and match-end noise removed (parsed once)."""
    from demoparser2 import DemoParser

    p = DemoParser(str(path))
    deaths = p.parse_event("player_death", player=["team_num"], other=["is_warmup_period"])
    ends = p.parse_event("round_end", other=["total_rounds_played"])
    official = p.parse_event("round_officially_ended")
    freeze = p.parse_event("round_freeze_end")
    # newer demos carry the warmup (and any restarts): keep only what follows
    # the LAST match start, and no warmup kill
    try:
        starts = [int(t) for t in p.parse_event("round_announce_match_start")["tick"]]
    except Exception:  # noqa: BLE001 - event absent in trimmed demos
        starts = []
    if starts:
        t0 = max(starts)  # round 1's freeze end fires on this same tick
        deaths, ends, official, freeze = (df[df["tick"] >= t0] for df in (deaths, ends, official, freeze))
    # backup restores (a technical issue): total_rounds_played drops back to R,
    # and the rounds played after round R up to the restore are replayed, so
    # HLTV drops them; so does this
    if "total_rounds_played" in ends.columns and len(ends):
        cuts = []
        seen = []  # (tick, total) of rounds ended so far
        for r in ends.sort_values("tick").itertuples():
            total = int(r.total_rounds_played) if r.total_rounds_played == r.total_rounds_played else None
            if total is None:
                continue
            if seen and total < max(t for _, t in seen):
                back_to = max((tk for tk, t in seen if t == total), default=0)
                cuts.append((back_to, int(r.tick)))
                seen = [(tk, t) for tk, t in seen if t <= total]
            else:
                seen.append((int(r.tick), total))
        for lo, hi in cuts:
            # round R itself runs on to its own official end (exit frags)
            off_after = [int(t) for t in official["tick"] if lo <= int(t) < hi]
            lo = min(off_after) if off_after else lo
            deaths, ends, official, freeze = (df[(df["tick"] <= lo) | (df["tick"] > hi)] for df in (deaths, ends, official, freeze))
    if "is_warmup_period" in deaths.columns:
        deaths = deaths[deaths["is_warmup_period"] != True]  # noqa: E712
    # 'world' suicides are not deaths HLTV counts: the server killing the
    # players left when the match ends, and disconnects / reconnects mid-match
    world_suicide = (deaths["weapon"].astype(str) == "world") & (deaths["attacker_steamid"].astype(str) == deaths["user_steamid"].astype(str))
    deaths = deaths[~world_suicide]
    valid_ends = ends[ends["winner"].notna()]
    if len(valid_ends):
        last = int(valid_ends["tick"].max())
        # HLTV's record of a map stops at its final round end: nothing after
        # it counts (a bomb death a few ticks later, exit frags, server kills)
        deaths = deaths[deaths["tick"] <= last]
    return deaths, ends, official, freeze, p


def _compute(deaths, ends, official, freeze, parser, trade_phase: int = 0) -> Dict[str, Any]:
    ends = ends[ends["winner"].notna()].sort_values("tick")
    end_ticks = [int(t) for t in ends["tick"]]
    winners = [str(w) for w in ends["winner"]]  # 'T' / 'CT'
    off = sorted(int(t) for t in official["tick"]) if len(official) else []
    # round i closes at the first officially_ended after its round_end (else the next round's start)
    bounds = []
    for i, et in enumerate(end_ticks):
        nxt = next((t for t in off if t >= et), None)
        if nxt is None:
            nxt = end_ticks[i + 1] - 1 if i + 1 < len(end_ticks) else 10**12
        bounds.append(nxt)
    n_rounds = len(bounds)

    def round_of(tick: int) -> int:
        for i, b in enumerate(bounds):
            if tick <= b:
                return i
        return n_rounds - 1

    # side of every player in every round, read at each freeze end
    fz = sorted(int(t) for t in freeze["tick"])
    starts = []
    for i in range(n_rounds):
        lo = bounds[i - 1] if i else 0
        starts.append(next((t for t in fz if lo < t <= bounds[i]), None))
    ticks = [t for t in starts if t is not None]
    side: Dict[int, Dict[str, int]] = defaultdict(dict)
    names: Dict[str, str] = {}
    if ticks:
        df = parser.parse_ticks(["team_num", "name"], ticks=ticks)
        for r in df.itertuples():
            sid = str(r.steamid)
            names[sid] = str(r.name)
            rnd = starts.index(int(r.tick)) if int(r.tick) in starts else None
            if rnd is not None and int(r.team_num) in (2, 3):
                side[rnd][sid] = int(r.team_num)

    st: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    kills_in = defaultdict(lambda: defaultdict(int))
    hs_in = defaultdict(int)
    dmg_assist_in = defaultdict(lambda: defaultdict(int))
    died_in = defaultdict(set)
    traded_in = defaultdict(set)
    first_kill = {}
    rows = [_clean(r) for r in deaths.sort_values("tick").to_dict("records")]
    for r in rows:
        tick = int(r["tick"])
        rnd = round_of(tick)
        vic, att = r["user_steamid"], r["attacker_steamid"]
        if vic:
            died_in[rnd].add(vic)
            if r.get("user_name"):
                names.setdefault(vic, str(r["user_name"]))
        vt, at = r.get("user_team_num"), r.get("attacker_team_num")
        enemy_kill = bool(att) and att != vic and at is not None and vt is not None and at != vt
        if enemy_kill:
            if r.get("attacker_name"):
                names.setdefault(att, str(r["attacker_name"]))
            kills_in[rnd][att] += 1
            if rnd not in first_kill:
                first_kill[rnd] = att
            w = str(r.get("weapon") or "").lower()
            s = st[att]
            s["kills"] += 1
            if r.get("headshot"):
                s["hs_kills"] += 1
            if w == "awp":
                s["awp_kills"] += 1
            if w in SMG:
                s["smg_kills"] += 1
            if w in SHOTGUN:
                s["shotgun_kills"] += 1
            if w in GRENADE:
                s["grenade_kills"] += 1
            if w in SHOTGUN or _melee(w):
                s["rambo_kills"] += 1
            ass = r["assister_steamid"]
            if ass and r["assister_team_num"] == at:
                if r.get("assistedflash"):
                    st[ass]["flash_assists_cs2"] += 1
                else:
                    st[ass]["damage_assists"] += 1
                    dmg_assist_in[rnd][ass] += 1
        elif vic and att and att != vic and at is not None and at == vt:
            # a teamkill: the enemy CS2 names as assister (who damaged the
            # victim) gets a damage assist from HLTV, and the KAST round with it
            ass = r["assister_steamid"]
            if ass and r["assister_team_num"] is not None and r["assister_team_num"] != vt and not r.get("assistedflash"):
                st[ass]["damage_assists"] += 1
                dmg_assist_in[rnd][ass] += 1
        if vic:
            st[vic]["deaths"] += 1

    # trades: a kill of K (who killed teammate V of the trader) within 5 whole seconds
    sec = lambda t: math.floor((t + trade_phase) / TICKRATE)  # noqa: E731
    credited = set()
    for j, r in enumerate(rows):
        killer_of_k, k = r["attacker_steamid"], r["user_steamid"]
        if not killer_of_k or killer_of_k == k:
            continue
        if r["attacker_team_num"] is None or r["user_team_num"] is None or r["attacker_team_num"] == r["user_team_num"]:
            continue
        t2 = int(r["tick"])
        for i in range(j):
            q = rows[i]
            if i in credited or q["attacker_steamid"] != k:
                continue
            if q["user_team_num"] != r["attacker_team_num"]:
                continue
            if sec(t2) - sec(int(q["tick"])) <= TRADE_WINDOW_S and round_of(int(q["tick"])) == round_of(t2):
                credited.add(i)
                v = q["user_steamid"]
                st[v]["traded_deaths"] += 1
                traded_in[round_of(int(q["tick"]))].add(v)
                st[killer_of_k]["trade_kills"] += 1
                break

    players = set(names) | set(st)
    for sid in players:
        s = st[sid]
        for rnd in range(n_rounds):
            if sid not in side.get(rnd, {}) and sid not in kills_in[rnd] and sid not in died_in[rnd]:
                continue
            s["rounds"] += 1
            k = kills_in[rnd].get(sid, 0)
            a = dmg_assist_in[rnd].get(sid, 0)
            alive = sid not in died_in[rnd]
            if k or a or alive or sid in traded_in[rnd]:
                s["kast_rounds"] += 1
            if not k and (a or alive):
                s["support_rounds"] += 1
            if k >= 2:
                s["multi_kill_rounds"] += 1
            if k >= 4:
                s["quad_rounds"] += 1
            if first_kill.get(rnd) == sid:
                s["first_kills"] += 1
            my_side = side.get(rnd, {}).get(sid)
            if my_side is not None and alive:
                won = (winners[rnd] == "CT") == (my_side == 3)
                if not won:
                    s["saved_lost_rounds"] += 1
    # clutches: the moment a side is down to one player during the round
    # (deaths after the round ended don't count), that player faces N
    # enemies; a clutch 1vN is won when the side wins the round. HLTV's 1vsX
    # column counts every N; the fantasy booster needs N >= 2.
    for rnd in range(n_rounds):
        sides = side.get(rnd, {})
        if not sides:
            continue
        alive = set(sides)
        done = set()
        end_tick = end_ticks[rnd]
        in_round = [x for x in rows if round_of(int(x["tick"])) == rnd and int(x["tick"]) <= end_tick]
        for j, r in enumerate(in_round):
            alive.discard(r["user_steamid"])
            for team in (2, 3):
                mine = [s_ for s_ in alive if sides.get(s_) == team]
                theirs = [s_ for s_ in alive if sides.get(s_) not in (team, None)]
                if len(mine) == 1 and theirs and mine[0] not in done:
                    done.add(mine[0])
                    if (winners[rnd] == "CT") == (team == 3):
                        # enemies a teammate who is already dead still kills
                        # afterwards (a grenade or molotov thrown before dying)
                        # are not the clutcher's to beat: HLTV sizes the clutch without them
                        by_dead = sum(
                            1 for q in in_round[j + 1:]
                            if q["user_steamid"] in theirs and q["attacker_steamid"] and q["attacker_steamid"] != mine[0]
                            and sides.get(q["attacker_steamid"]) == team
                        )
                        size = len(theirs) - by_dead
                        st[mine[0]]["clutches_won_any"] += 1
                        if size >= 2:
                            st[mine[0]]["clutches_won"] += 1
    # best pistol round: rounds 1 and 13, most kills then most damage assists, ties all
    for rnd in (0, 12):
        if rnd >= n_rounds:
            continue
        # HLTV: "best based on kills, then assists, and then deaths" (fewer is better)
        cand = {sid: (kills_in[rnd].get(sid, 0), dmg_assist_in[rnd].get(sid, 0), -int(sid in died_in[rnd])) for sid in side.get(rnd, {})}
        if cand:
            best = max(cand.values())
            for sid, v in cand.items():
                if v == best:
                    st[sid]["best_pistol"] = 1
    return {"rounds": n_rounds, "names": names, "players": {sid: dict(v) for sid, v in st.items() if sid in names}}


def _norm_name(s: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def match_page_rows(rows: List[Dict[str, Any]], names: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """steamid -> the player's row on HLTV's map stats page, by name (exact
    after normalising, else a unique prefix either way: 'Techno' / 'Techno4K')."""
    by = {_norm_name(r["name"]): r for r in rows}
    out = {}
    for sid, name in names.items():
        n = _norm_name(name)
        row = by.get(n)
        if row is None:
            hits = [r for k, r in by.items() if k and n and (n.startswith(k) or k.startswith(n))]
            row = hits[0] if len(hits) == 1 else None
        if row is not None:
            out[sid] = row
    return out


def with_page(raw: tuple, page: Dict[str, Any]) -> Dict[str, Any]:
    """The map's stats with HLTV's own map stats page folded in:
      - the trade window phase that reproduces the page's traded-deaths
        column (Bait, Avenger, KAST and Support follow it)
      - page figures where HLTV publishes them: opening kills (Entry
        Fragger), flash assists (Flash), damage assists = A - f (Assist),
        clutches won (1vsX; the demo's 1v2+ clutches are capped by it: HLTV
        does not count every situation the demo shows), per-side ratings
      - each player's HLTV id from the page row."""
    best = None
    for phase in range(0, TICKRATE, 2):
        r = compute_map(raw, phase)
        pr = match_page_rows(page.get("total") or [], r["names"])
        err = sum(abs(int(row.get("traded_deaths") or 0) - int(r["players"].get(sid, {}).get("traded_deaths", 0))) for sid, row in pr.items())
        if best is None or err < best[0]:
            best = (err, phase, r, pr)
        if err == 0:
            break
    err, phase, r, pr = best
    side_rows = {k: match_page_rows(page.get(k) or [], r["names"]) for k in ("ct", "t")}
    for sid, s in r["players"].items():
        row = pr.get(sid)
        if not row:
            continue
        s["hltv_player_id"] = row["player_id"]
        s["page_opening_kills"] = row.get("opening_kills")
        s["page_flash_assists"] = row.get("flash_assists")
        s["page_damage_assists"] = (row.get("assists") or 0) - (row.get("flash_assists") or 0)
        s["page_clutches"] = row.get("clutches")
        s["page_kast"] = row.get("kast")
        s["page_rating"] = row.get("rating")
        s["clutches_won_hltv"] = min(int(s.get("clutches_won", 0)), int(row.get("clutches") or 0))
        for k in ("ct", "t"):
            srow = side_rows[k].get(sid)
            if srow:
                s[f"page_{k}_rating"] = srow.get("rating")
    r["trade_phase"] = phase
    r["trade_error"] = err
    r["page_matched"] = len(pr)
    return r
