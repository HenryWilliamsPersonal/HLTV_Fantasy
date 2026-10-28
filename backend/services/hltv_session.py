"""HLTV login-session health for the scraper browser.

The trigger-rates backfill needs the scraper's Chrome profile to be signed in
to HLTV. That login rides on the `autologin` remember-me cookie, which lasts
about 400 days from the last real sign-in and is NOT renewed when HLTV
re-authenticates from it (verified 2026-09-07), so it dies on a fixed date.
Renewal needs a human (the sign-in modal is reCAPTCHA-gated):
    scripts\\hltv-login-handoff.ps1 --headed

This module takes a snapshot of that state from the browser, persists it to
.runtime/hltv-session.json so the Scheduling tab can show it without a page
load, and grades it: ok / warning (expiry within WARN_DAYS, or no remember-me
cookie) / error (signed out).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
SESSION_FILE = ROOT_DIR / ".runtime" / "hltv-session.json"
WARN_DAYS = int(os.getenv("HLTV_SESSION_WARN_DAYS", "30"))
RENEW_HINT = "Renew from your desktop: scripts\\hltv-login-handoff.ps1 --headed"
SCRAPER_KEY = "scraper"  # the scraper browser's own login in data/hltv_accounts (stored by the hand-off)
SESSION_COOKIE_NAMES = ("autologin", "PHPSESSID")

LOGGED_IN_JS = (
    "return !!(document.querySelector('a[href*=\"logout\"]')"
    " || document.querySelector('.navaccount, .avatarNavItem, .nav-account, img.avatar'))"
)
USERNAME_JS = (
    "const el = document.querySelector('a[href*=\"/profile/\"]') || document.querySelector('.navusername');"
    "return el ? el.textContent.trim() : '';"
)


def is_logged_in(driver) -> bool:
    try:
        return bool(driver.execute_script(LOGGED_IN_JS))
    except Exception:
        return False


def nav_username(driver) -> str:
    try:
        return str(driver.execute_script(USERNAME_JS) or "")
    except Exception:
        return ""


def _autologin_expiry(driver) -> float | None:
    try:
        for cookie in driver.get_cookies():
            if cookie.get("name") == "autologin" and cookie.get("value"):
                expiry = cookie.get("expiry")
                return float(expiry) if expiry else None
    except Exception:
        pass
    return None


def evaluate(snap: dict[str, Any]) -> dict[str, Any]:
    """Attach `level` (ok | warning | error) and a human `message`."""
    user = snap.get("username") or "the app account"
    expires_at = snap.get("autologin_expires_at")
    days_left = snap.get("days_left")
    until = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else None

    if not snap.get("logged_in"):
        level, message = "error", f"Signed out of HLTV: the trigger-rates backfill will fail. {RENEW_HINT}"
    elif expires_at is None:
        level, message = "warning", (
            f"Signed in as {user}, but there is no remember-me cookie, so the login will not survive a browser restart. {RENEW_HINT}"
        )
    elif days_left is not None and days_left <= 0:
        level, message = "error", f"Signed in as {user}, but the login cookie expired on {until}. {RENEW_HINT}"
    elif days_left is not None and days_left <= WARN_DAYS:
        level, message = "warning", f"Signed in as {user}; login cookie expires in {int(days_left)} days ({until}). {RENEW_HINT}"
    else:
        level, message = "ok", f"Signed in as {user}; login cookie valid for {int(days_left)} days (until {until})."
    snap["level"] = level
    snap["message"] = message
    return snap


def snapshot_from_driver(driver) -> dict[str, Any]:
    """Load hltv.org in the given driver and report the login state. Used by
    the status endpoint, the cookie hand-off, and the nightly check."""
    driver.get("https://www.hltv.org/")
    time.sleep(2)
    now = time.time()
    expires_at = _autologin_expiry(driver)
    snap = {
        "logged_in": is_logged_in(driver),
        "username": nav_username(driver),
        "autologin_expires_at": expires_at,
        "days_left": round((expires_at - now) / 86400, 1) if expires_at else None,
        "checked_at": now,
    }
    return evaluate(snap)


def save_snapshot(snap: dict[str, Any]) -> None:
    # the account the browser was last seen signed in as survives a
    # signed-out snapshot: restore_login() uses it to pick a stored session
    if snap.get("logged_in") and snap.get("username"):
        snap["last_ok_username"] = snap["username"]
    elif not snap.get("last_ok_username"):
        previous = load_snapshot() or {}
        if previous.get("last_ok_username"):
            snap["last_ok_username"] = previous["last_ok_username"]
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        os.replace(tmp, SESSION_FILE)
    except Exception:
        logger.exception("Could not persist HLTV session snapshot")


def load_snapshot() -> dict[str, Any] | None:
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _last_ok_username() -> str:
    """The account the scraper browser was last signed in as: the snapshot
    file, else the last successful login check in the run history."""
    import re

    last = load_snapshot() or {}
    if last.get("last_ok_username"):
        return str(last["last_ok_username"])
    if last.get("logged_in") and last.get("username"):
        return str(last["username"])
    try:
        from backend.data import schedule_db

        for run in schedule_db.list_runs(limit=400):
            if str(run.get("task")) == "hltv_session" and str(run.get("status")) == "success":
                m = re.search(r"Signed in as ([^;,]+)", str(run.get("message") or ""))
                if m:
                    return m.group(1).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def stored_login() -> dict[str, Any] | None:
    """The stored HLTV session to sign the scraper browser in with: its own
    (SCRAPER_KEY, stored by the hand-off), else the stored session of the
    account it was last signed in as (a My Team connection of the same
    account). None when nothing usable is stored."""
    from backend.data import hltv_accounts

    def usable(row):
        return bool(row and any(c.get("name") == "autologin" and c.get("value") for c in (row.get("cookies") or [])))

    row = hltv_accounts.get(SCRAPER_KEY)
    if usable(row):
        return row
    name = _last_ok_username().strip().lower()
    if not name:
        return None
    for meta in sorted(hltv_accounts.list_accounts(), key=lambda m: -float(m.get("saved_at") or 0.0)):
        if str(meta.get("username") or "").strip().lower() == name:
            row = hltv_accounts.get(str(meta["user_key"]))
            if usable(row):
                return row
    return None


def apply_login(driver, cookies: list) -> dict[str, Any]:
    """Replace the browser's HLTV session cookies with `cookies` and report
    the login state the front page then shows."""
    for name in SESSION_COOKIE_NAMES:
        try:
            driver.delete_cookie(name)
        except Exception:  # noqa: BLE001
            pass
    for c in cookies:
        if c.get("name") not in SESSION_COOKIE_NAMES or not c.get("value"):
            continue
        cookie = {"name": c["name"], "value": c["value"], "domain": c.get("domain") or ".hltv.org", "path": c.get("path") or "/"}
        if c.get("expiry"):
            cookie["expiry"] = int(c["expiry"])
        if c.get("secure"):
            cookie["secure"] = True
        if c.get("httpOnly"):
            cookie["httpOnly"] = True
        driver.add_cookie(cookie)
    return snapshot_from_driver(driver)


def restore_login() -> dict[str, Any] | None:
    """Sign the scraper browser back in from the stored session (see
    stored_login); on success the session is kept under SCRAPER_KEY so the
    next restore needs no guesswork. None when nothing is stored."""
    from backend.data import hltv_accounts
    from backend.services.hltv_browser import run_hltv_browser_session

    row = stored_login()
    if not row:
        return None
    cookies = list(row.get("cookies") or [])
    snap = run_hltv_browser_session("https://www.hltv.org/", lambda d: apply_login(d, cookies), wait_text=None, timeout_ms=60000) or {}
    if snap.get("logged_in"):
        if str(row.get("user_key")) != SCRAPER_KEY:
            hltv_accounts.save(SCRAPER_KEY, cookies, str(snap.get("username") or row.get("username") or ""))
        logger.info("HLTV scraper login restored from the stored session of %s", snap.get("username") or row.get("username") or "?")
    else:
        logger.warning("HLTV scraper login could not be restored: the stored session of %s is no longer accepted", row.get("username") or "?")
    return snap


def check_session_health() -> dict[str, Any]:
    """One page load in the shared scraper browser; persists and logs the
    result. A signed-out browser is first signed back in from the stored
    session when there is one (a hard browser kill can lose the profile's
    cookies), and only a failed restore counts as signed out."""
    from backend.services.hltv_browser import run_hltv_browser_session

    snap = run_hltv_browser_session("https://www.hltv.org/", snapshot_from_driver, wait_text=None, timeout_ms=60000) or {}
    if not snap.get("logged_in"):
        try:
            restored = restore_login()
        except Exception:  # noqa: BLE001
            logger.warning("HLTV scraper login restore failed", exc_info=True)
            restored = None
        if restored and restored.get("logged_in"):
            snap = restored
            snap["message"] = f"Restored from the stored login. {snap.get('message') or ''}".strip()
    save_snapshot(snap)
    level = snap.get("level")
    if level == "ok":
        logger.info("HLTV session check: %s", snap.get("message"))
    else:
        logger.warning("HLTV session check (%s): %s", level, snap.get("message"))
    return snap
