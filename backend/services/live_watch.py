"""Keeps the live events' HLTV pages fresh on the server's own clock, so the
scores move whether or not an app is asking: every tick walks the current
events (live_state.live_events), which schedules a background read of any
event whose pages are due — every LIVE_MATCH_REFRESH seconds while a match
is in play, LIVE_REFRESH otherwise (services/live_pages). A changed read
bumps live_pages' version, which the live stream pushes to open apps."""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

TICK_SECONDS = 15.0
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    from backend.services import live_state

    while not _stop.wait(TICK_SECONDS):
        try:
            live_state.live_events()
        except Exception:  # noqa: BLE001
            logger.exception("Live watch tick failed")


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="live-watch", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
