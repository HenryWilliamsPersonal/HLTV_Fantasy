"""Players: the pool, a player's recent form, the card's rating curve, and
the player CRUD the imports use."""
from fastapi import APIRouter, HTTPException

from backend.data.db import connect as _connect
from backend.data.player_db import add_or_update_player, delete_player, get_all_players, get_player


router = APIRouter()
# Status boilerplate that should disappear once a scraping job is resumed
# (the teams and events job families read it); real fetch errors are kept so
# the user still sees what last went wrong.
_INTERRUPTION_BOILERPLATE = {
    "Job was interrupted before completion. Resume to continue.",
    "Paused",
    "Canceled",
}


@router.get("/")
def list_players():
    return get_all_players()


_FORM_CACHE: dict = {"key": None, "data": None}


@router.get("/recent-form")
def players_recent_form(limit: int = 10):
    """Every player's ratings in their last `limit` stored matches (newest
    first) and the average, from the per-match scoreboards on the result
    rows. Cached until a result is imported or changed."""
    import json as _json

    limit = max(1, min(20, int(limit)))
    conn = _connect()
    try:
        key = tuple(conn.execute("SELECT COUNT(*), MAX(imported_at), MAX(last_seen_at) FROM hltv_results").fetchone()) + (limit,)
        if _FORM_CACHE["key"] == key and _FORM_CACHE["data"] is not None:
            return _FORM_CACHE["data"]
        rows = conn.execute(
            "SELECT match_date, player_stats_json FROM hltv_results "
            "WHERE match_date IS NOT NULL AND player_stats_json IS NOT NULL AND player_stats_json != '' "
            "ORDER BY match_date DESC, match_id DESC"
        ).fetchall()
    finally:
        conn.close()
    form: dict = {}
    for row in rows:
        try:
            stats = _json.loads(row[1])
        except Exception:  # noqa: BLE001
            continue
        for s in stats or []:
            try:
                pid = int(s.get("player_id") or 0)
                rating = float(s.get("rating"))
            except (TypeError, ValueError):
                continue
            if not pid:
                continue
            lst = form.setdefault(pid, [])
            if len(lst) < limit:
                lst.append(round(rating, 2))
    data = {
        str(pid): {"ratings": r, "avg": round(sum(r) / len(r), 3)}
        for pid, r in form.items()
        if r
    }
    _FORM_CACHE.update(key=key, data=data)
    return data


@router.get("/{player_id}/recent-form")
def player_recent_form(player_id: int, limit: int = 5):
    """The player's recent matches with their per-match rating. Sources, in
    order: the stored per-player scoreboard (player_stats_json on the result
    row, present for ~80% of matches), then the archived match-page HTML.
    The list is the team's TRUE last matches in order — an unrated match stays
    in place with rating=null (run the veto/details backfill to fill stored
    scoreboards) — skipping only matches where the team verifiably played
    without this player."""
    import html as _htmlmod
    import json as _json
    import re

    from backend.data.event_db import get_player_latest_teams, list_hltv_results_for_team
    from backend.data.page_snapshots import get_page_snapshot

    pid = int(player_id)
    latest = get_player_latest_teams().get(pid)
    if not latest:
        return {"team_name": None, "matches": []}
    team_name = str(latest["team_name"]).strip().lower()
    limit = max(1, min(10, int(limit)))
    rating_re = re.compile(r'class="rating[^"]*"[^>]*>\s*([0-9]\.[0-9]{2})')

    matches = []
    for r in list_hltv_results_for_team(latest["team_name"], limit=60):
        if len(matches) >= limit:
            break
        t1 = str(r.get("team1") or "").strip().lower()
        is_t1 = t1 == team_name
        rating = None
        played = None  # unknown
        raw_stats = r.get("player_stats_json")
        if raw_stats:
            try:
                stats = _json.loads(raw_stats)
            except Exception:
                stats = []
            mine = next((s for s in stats if int(s.get("player_id") or 0) == pid), None)
            if mine is None:
                played = False
            else:
                played = True
                val = mine.get("rating")
                rating = float(val) if isinstance(val, (int, float)) else None
        if played is None or (played and rating is None):
            snap = get_page_snapshot(str(r.get("match_url") or ""))
            if snap and snap.get("html"):
                un = _htmlmod.unescape(snap["html"])
                idx = un.find(f"/player/{pid}/")
                if idx < 0:
                    played = False
                else:
                    played = True
                    m = rating_re.search(un, idx, idx + 2500)
                    if m:
                        rating = float(m.group(1))
        if played is False:
            continue  # stand-in / roster change — not this player's match
        s_own = r.get("score1") if is_t1 else r.get("score2")
        s_opp = r.get("score2") if is_t1 else r.get("score1")
        matches.append(
            {
                "match_url": r.get("match_url"),
                "date": r.get("match_date"),
                "opponent": r.get("team2") if is_t1 else r.get("team1"),
                "won": str(r.get("winner") or "").strip().lower() == team_name,
                "score": f"{s_own}-{s_opp}" if s_own is not None and s_opp is not None else "",
                "rating": rating,
            }
        )
    return {"team_name": latest["team_name"], "matches": matches}


@router.get("/{player_id}/rating-curve")
def fetch_player_rating_curve(player_id: int):
    """The player card's Top X tab: the learned model's expected rating
    against a typical team of each HLTV rank (from the player's own team's
    rank), and the player's last ten stored maps (services/rating_model)."""
    from backend.services import rating_model, team_strength

    player = get_player(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    model_rows: list = []
    try:
        conn = _connect()
        try:
            team = conn.execute(
                "SELECT team_id FROM teams WHERE ? IN (player1_id, player2_id, player3_id, player4_id, player5_id) ORDER BY team_id DESC LIMIT 1",
                (int(player_id),),
            ).fetchone()
        finally:
            conn.close()
        own_rank = team_strength.get_hltv_rank(int(team["team_id"])) if team else 50
        model_rows = rating_model.rank_curve(int(player_id), own_rank)
    except Exception:  # noqa: BLE001 - the card shows what it can
        model_rows = []
    try:
        recent = rating_model.recent_maps(int(player_id), 10)
    except Exception:  # noqa: BLE001
        recent = []
    return {
        "player_id": player_id,
        "player_name": player.get("name"),
        "model_rows": model_rows,
        "recent_maps": recent,
    }


@router.post("/")
def upsert_player(payload: dict):
    if "player_id" not in payload:
        raise HTTPException(status_code=400, detail="player_id is required")
    add_or_update_player(**payload)
    return {"status": "ok"}


@router.get("/{player_id}")
def fetch_player(player_id: int):
    player = get_player(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    return player


@router.delete("/{player_id}")
def remove_player(player_id: int):
    delete_player(player_id)
    return {"status": "ok"}
