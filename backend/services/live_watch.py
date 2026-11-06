"""Keeps the live events' HLTV pages fresh on the server's own clock, so the
scores move whether or not an app is asking: every tick walks the current
events (live_state.live_events), which schedules a background read of any
event whose pages are due — every LIVE_MATCH_REFRESH seconds while a match
is in play, LIVE_REFRESH otherwise (services/live_pages). A changed read
bumps live_pages' version, which the live stream pushes to open apps."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

TICK_SECONDS = 15.0
FIELD_START_EVERY = 600.0  # how often to look for a live event whose field was never read
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    from backend.services import live_state

    field_checked = 0.0
    while not _stop.wait(TICK_SECONDS):
        try:
            entries = live_state.live_events()
        except Exception:  # noqa: BLE001
            logger.exception("Live watch tick failed")
            continue
        try:
            from backend.services import fantasy_field

            fantasy_field.resume_interrupted()  # a full read a restart cut off carries on
            fantasy_field.after_matches(entries)  # re-read the teams a finished match needs
            if time.time() - field_checked >= FIELD_START_EVERY:
                field_checked = time.time()
                fantasy_field.auto_reads()  # an event gone live since the nightly batch
        except Exception:  # noqa: BLE001
            logger.exception("Field match refresh tick failed")
        try:
            from backend.services import event_finish

            event_finish.tick(entries)  # a finished event brought up to date once
        except Exception:  # noqa: BLE001
            logger.exception("Event finish tick failed")


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="live-watch", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
