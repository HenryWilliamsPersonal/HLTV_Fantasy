"""Download a match's demo archive through the app's HLTV browser.

HLTV's /download/demo/{id} sits behind the same Cloudflare check as every page
the app reads (a plain HTTP request gets `Cf-Mitigated: challenge`), so the
download runs in the shared browser like any page load: the browser is told to
save downloads into DEMO_DIR, opens the demo link, and the file is complete
once Chrome has renamed it from .crdownload. Demo ids come from the match page
(stored snapshot first, else a page read).
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from backend.data.db import DB_PATH

logger = logging.getLogger(__name__)

DEMO_DIR = Path(DB_PATH).parent / "demos"
DOWNLOAD_TIMEOUT_S = 1800.0
STALL_S = 90.0  # a download that stops growing this long is dropped (retried another night)
_DEMO_RE = re.compile(r"/download/demo/(\d+)")


def match_url(match_id: int) -> Optional[str]:
    from backend.data.db import connect

    conn = connect()
    try:
        row = conn.execute("SELECT match_url FROM hltv_results WHERE match_id = ?", (str(int(match_id)),)).fetchone()
    finally:
        conn.close()
    return str(row["match_url"]) if row and row["match_url"] else None


def demo_id(match_id: int) -> Optional[int]:
    """The match's demo id from its match page (stored snapshot, else read)."""
    from backend.data.page_snapshots import get_page_snapshot

    url = match_url(match_id)
    if not url:
        return None
    snap = get_page_snapshot(url) or {}
    m = _DEMO_RE.search(str(snap.get("html") or ""))
    if m:
        return int(m.group(1))
    from backend.services.hltv_browser import run_hltv_browser_session

    html = run_hltv_browser_session(url, lambda d: d.page_source or "", wait_text=None, timeout_ms=60000) or ""
    m = _DEMO_RE.search(html)
    return int(m.group(1)) if m else None


def download(match_id: int) -> Dict[str, Any]:
    """Download the match's demo archive into DEMO_DIR/<match_id>/; returns
    {match_id, demo_id, path, bytes, seconds}."""
    from backend.services.hltv_browser import run_hltv_browser_session

    did = demo_id(int(match_id))
    if not did:
        raise RuntimeError(f"No demo link on the match page of {match_id}")
    target = DEMO_DIR / str(int(match_id))
    target.mkdir(parents=True, exist_ok=True)
    for f in target.glob("*.crdownload"):
        f.unlink(missing_ok=True)
    before = {f.name for f in target.iterdir()}
    started = time.time()

    def session(driver):
        driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(target), "eventsEnabled": True})
        logger.info("Demo %s of match %s: download folder set, opening the link", did, match_id)
        # a download never finishes loading as a page, so driver.get() would
        # wait for its page-load timeout: navigate from script, which returns at once
        driver.execute_script("window.location.href = arguments[0];", f"https://www.hltv.org/download/demo/{did}")
        deadline = time.time() + DOWNLOAD_TIMEOUT_S
        seen_partial = False
        last_size, last_growth = -1, time.time()
        while time.time() < deadline:
            files = [f for f in target.iterdir() if f.name not in before]
            partial = [f for f in files if f.suffix == ".crdownload"]
            done = [f for f in files if f.suffix != ".crdownload" and f.stat().st_size > 0]
            if done and not partial:
                return str(done[0])
            size = sum(f.stat().st_size for f in partial if f.exists())
            if size != last_size:
                last_size, last_growth = size, time.time()
            elif partial and time.time() - last_growth > STALL_S:
                raise RuntimeError(f"Demo download stalled at {size / 1e6:.0f} MB")
            if partial and not seen_partial:
                logger.info("Demo %s of match %s: download started", did, match_id)
            seen_partial = seen_partial or bool(partial)
            if not seen_partial and time.time() - started > 120:
                title = ""
                try:
                    title = driver.title
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(f"Demo download did not start within 2 minutes (page: {title!r}, url: {driver.current_url})")
            time.sleep(2.0)
        raise RuntimeError("Demo download timed out")

    path = None
    for attempt in (1, 2):  # a stalled CDN transfer usually goes through on a second try
        try:
            path = run_hltv_browser_session("https://www.hltv.org/", session, wait_text=None, timeout_ms=60000)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2 or "stalled" not in str(exc):
                raise
            logger.info("Demo %s of match %s: %s; trying once more", did, match_id, exc)
            for f in target.glob("*.crdownload"):
                f.unlink(missing_ok=True)
            started = time.time()
    p = Path(path)
    out = {"match_id": int(match_id), "demo_id": did, "path": str(p), "bytes": p.stat().st_size, "seconds": round(time.time() - started, 1)}
    logger.info("Demo of match %s downloaded: %s (%.0f MB in %.0f s)", match_id, p.name, out["bytes"] / 1e6, out["seconds"])
    return out
