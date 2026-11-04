# swiss_stage/fantasy_scoring.py

from backend.swiss_stage.swiss_models import PlayerState


def compute_rating_points(player: PlayerState) -> float:
    """
    Rating component.

    Previously: (rating - 1) / 2  -> values like 0.05–0.15
    Now scaled up by 100x so rating has a meaningful impact:

        rating_points = ((rating - 1) / 2) * 100
    """
    return ((player.rating - 1.0) / 2.0) * 100.0


# HLTV scales a match's rating points by its series length: a Bo1 counts
# half, a Bo5 a quarter more (user-confirmed rule, 2026-11-04).
RATING_SERIES_FACTOR = {1: 0.5, 2: 1.0, 3: 1.0, 5: 1.25}


def rating_series_factor(bo) -> float:
    try:
        return RATING_SERIES_FACTOR.get(int(bo), 1.0)
    except (TypeError, ValueError):
        return 1.0


def compute_role_points(player: PlayerState) -> float:
    """
    Role component:
      5 * major + 2 * minor - 2 * (1 - major - minor)
    Where major/minor are trigger rates in [0,1].
    """
    major = player.major_pct
    minor = player.minor_pct
    return 5.0 * major + 2.0 * minor - 2.0 * (1.0 - major - minor)


def compute_win_points(win_prob: float, did_win: bool) -> float:
    """
    Win component (deterministic by match outcome):
      - win  -> +6
      - loss -> -3

    win_prob is retained in the signature for compatibility with existing callers.
    """
    return 6.0 if did_win else -3.0


def compute_booster_points(player: PlayerState, match_number: int) -> float:
    """
    Booster component for match N:
      5 * booster_value_for_that_match

    We expect player.boosters to be a list of float trigger rates;
    we use match_number - 1 as index into that list.
    """
    idx = match_number - 1
    if 0 <= idx < len(player.boosters):
        return 5.0 * float(player.boosters[idx])
    return 0.0


def compute_padding_components(player: PlayerState) -> dict:
    """
    For qualified teams that don't play all 5 matches:
    We give padding per missing match:
      rating_points + 6 (win) + role_points, no booster.
    Returns per-component contributions for a *single* missing match.
    """
    rating_pts = compute_rating_points(player)
    role_pts = compute_role_points(player)
    win_pts = 6.0
    return {
        "rating": rating_pts,
        "role": role_pts,
        "win": win_pts,
        "booster": 0.0,
    }


def compute_elimination_penalty_components(missing_games: int) -> dict:
    """
    For eliminated teams that don't play all 5 matches:
      each missing match gives -3 points (treated as a 'win' component penalty).
    Returns a dict of per-component contributions for ALL missing matches.
    """
    penalty = -3.0 * missing_games
    return {
        "rating": 0.0,
        "role": 0.0,
        "win": penalty,
        "booster": 0.0,
    }
