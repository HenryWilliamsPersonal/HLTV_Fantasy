"""Which HLTV fantasy games are over, as HLTV's fantasy front page says.

The front page (https://www.hltv.org/fantasy, fetched by the nightly event
sweep and archived as a page snapshot) lists the season's games month by
month; a finished game's card carries the `finished-event` class. That is
the fantasy game's own state — a stage-scoped game (a group stage) is over
while its HLTV event is still being played."""
from __future__ import annotations

import re
import threading
import time
from typing import Optional, Set

FRONT_PAGE = "https://www.hltv.org/fantasy"
_CACHE_SECONDS = 60.0
_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "ids": set()}

_FINISHED = re.compile(
    r'<div class="game-container[^"]*\bfinished-event\b[^"]*">.{0,400}?href="/fantasy/(\d+)/gameredirect"', re.S
)


def parse_finished(html: str) -> Set[int]:
    return {int(m) for m in _FINISHED.findall(html or "")}


def finished_ids() -> Set[int]:
    """Fantasy event ids the archived front page shows as finished."""
    now = time.time()
    with _LOCK:
        if now - _CACHE["at"] < _CACHE_SECONDS:
            return set(_CACHE["ids"])
    ids: Set[int] = set()
    try:
        from backend.data.page_snapshots import get_page_snapshot

        ids = parse_finished((get_page_snapshot(FRONT_PAGE) or {}).get("html") or "")
    except Exception:  # noqa: BLE001 - an unreadable page leaves the date rule in charge
        ids = set()
    with _LOCK:
        _CACHE.update(at=now, ids=ids)
    return set(ids)


def is_finished(event_id: Optional[int]) -> bool:
    return bool(event_id) and int(event_id) in finished_ids()
