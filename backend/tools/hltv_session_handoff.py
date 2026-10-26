"""Hand the HLTV login from THIS desktop session to the always-on backend.

Why: the 24/7 backend runs as the SYSTEM service with no desktop, on its own
Chrome profile. HLTV's sign-in is captcha-protected, so nobody can log the
service browser in directly. Instead you sign in once here, as yourself, and
this tool posts the session cookies (`autologin`, `PHPSESSID`) to the backend,
which adds them to the service profile. The `autologin` cookie lasts about a
year, so this is a rare chore. Verified: the two cookies alone move the login
between profiles.

Usage (from the repo root, as your normal user):
    python -m backend.tools.hltv_session_handoff [--backend-url URL]
        [--include-cloudflare] [--timeout-minutes N] [--headed]

--include-cloudflare also hands over the cf_clearance cookie, for the case
where the headless service browser is stuck on an interactive challenge.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

SESSION_COOKIES = ("autologin", "PHPSESSID")
CLOUDFLARE_COOKIES = ("cf_clearance", "_cfuvid", "__cflb")

LOGGED_IN_JS = (
    "return !!(document.querySelector('a[href*=\"logout\"]')"
    " || document.querySelector('.navaccount, .avatarNavItem, .nav-account, img.avatar'))"
)
USERNAME_JS = (
    "const el = document.querySelector('a[href*=\"/profile/\"]') || document.querySelector('.navusername');"
    "return el ? el.textContent.trim() : '';"
)
OPEN_SIGN_IN_JS = """
if (!document.querySelector('input[type=password]')) {
  var link = Array.from(document.querySelectorAll('a, button, .nav-link, [class*="sign" i]'))
    .find(function(e){ return (e.textContent||'').trim().toLowerCase() === 'sign in'; });
  if (link) link.click();
}
"""


def _backend_url(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    from backend.services.backend_port import DEFAULT_PORT, HOST, read_port_info

    info = read_port_info()
    return info["url"] if info else f"http://{HOST}:{DEFAULT_PORT}"


def _request_json(url: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail") or body
        except Exception:
            detail = body
        raise RuntimeError(f"{url} -> HTTP {exc.code}: {detail}") from exc


def _logged_in(driver) -> bool:
    try:
        return bool(driver.execute_script(LOGGED_IN_JS))
    except Exception:
        return False


def _username(driver) -> str:
    try:
        return str(driver.execute_script(USERNAME_JS) or "")
    except Exception:
        return ""


def _export_cookies(driver, names: tuple[str, ...]) -> list[dict]:
    wanted = set(names)
    out = []
    for c in driver.get_cookies():
        if c.get("name") in wanted and c.get("value"):
            item = {"name": c["name"], "value": c["value"], "domain": c.get("domain") or ".hltv.org", "path": c.get("path") or "/"}
            if c.get("expiry"):
                item["expiry"] = int(c["expiry"])
            if c.get("secure"):
                item["secure"] = True
            if c.get("httpOnly"):
                item["httpOnly"] = True
            out.append(item)
    return out


def _collect_from_profile(headless: bool, names: tuple[str, ...], timeout_minutes: float) -> tuple[list[dict], str]:
    """Open the interactive user's own Chrome profile (headless first, or a
    visible window when a sign-in is needed) and export the session cookies."""
    from backend.services.hltv_browser import close_hltv_browser, run_hltv_browser_session

    os.environ["HLTV_HEADLESS"] = "1" if headless else "0"
    close_hltv_browser()  # a previous phase may have left a driver in the other mode

    def session(driver):
        if not _logged_in(driver):
            if headless:
                return None, ""
            try:
                driver.execute_script(OPEN_SIGN_IN_JS)
            except Exception:
                pass
            print(
                f"A Chrome window is open on hltv.org: sign in there as {'YOUR OWN account' if os.getenv('HLTV_HANDOFF_ACCOUNT') == 'mine' else 'the APP account'} "
                f"(solve the captcha if asked). Waiting up to {timeout_minutes:g} minutes...",
                flush=True,
            )
            deadline = time.time() + timeout_minutes * 60
            while time.time() < deadline and not _logged_in(driver):
                time.sleep(3)
            if not _logged_in(driver):
                return None, ""
        user = _username(driver)
        # A fresh navigation makes sure the PHPSESSID we export is the live one.
        driver.get("https://www.hltv.org/")
        time.sleep(1.5)
        return _export_cookies(driver, names), user

    result = run_hltv_browser_session("https://www.hltv.org/", session, wait_text=None, timeout_ms=int(timeout_minutes * 60000) + 60000)
    close_hltv_browser()
    cookies, user = result if result else (None, "")
    return (cookies or []), user


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hand this session's HLTV login to the always-on backend.")
    ap.add_argument("--backend-url", help="override the backend URL (default: read .runtime/backend-port.json)")
    ap.add_argument("--include-cloudflare", action="store_true", help="also hand over the Cloudflare clearance cookies")
    ap.add_argument("--timeout-minutes", type=float, default=5.0, help="how long to wait for a manual sign-in (default 5)")
    ap.add_argument("--headed", action="store_true", help="open the visible window immediately instead of trying headless first")
    ap.add_argument("--account", choices=("scraper", "mine"), default="scraper", help="scraper = the app's data account (default); mine = YOUR account for the My Team page")
    ap.add_argument("--profile-dir", help="browser profile to read the login from (default: the hand-off profile; '-mine' sibling for --account mine)")
    args = ap.parse_args(argv)
    personal = args.account == "mine"
    os.environ["HLTV_HANDOFF_ACCOUNT"] = args.account
    if args.profile_dir:
        os.environ["HLTV_PROFILE_DIR"] = args.profile_dir
    elif personal:
        from backend.services.hltv_browser import _resolve_profile_dir

        base = _resolve_profile_dir()
        os.environ["HLTV_PROFILE_DIR"] = str(base.parent / f"{base.name}-mine")

    names = SESSION_COOKIES + (CLOUDFLARE_COOKIES if args.include_cloudflare else ())
    backend = _backend_url(args.backend_url)
    print(f"Backend: {backend}", flush=True)

    cookies, user = ([], "")
    if not args.headed:
        print("Checking your own Chrome profile for an existing HLTV login (headless)...", flush=True)
        cookies, user = _collect_from_profile(headless=True, names=names, timeout_minutes=args.timeout_minutes)
    if not any(c["name"] == "autologin" for c in cookies):
        print("No usable login in the profile yet; opening a visible browser window for you to sign in.", flush=True)
        cookies, user = _collect_from_profile(headless=False, names=names, timeout_minutes=args.timeout_minutes)
    if not any(c["name"] == "autologin" for c in cookies):
        print("ERROR: still not signed in to HLTV (no autologin cookie); nothing handed over.", file=sys.stderr)
        return 1
    print(f"Signed in here as {user or '(unknown)'}; handing over {sorted(c['name'] for c in cookies)}", flush=True)

    route = "/admin/my-hltv-session-cookies" if personal else "/admin/hltv-session-cookies"
    try:
        result = _request_json(f"{backend}{route}", {"cookies": cookies})
    except Exception as exc:
        print(f"ERROR: backend rejected the hand-off: {exc}", file=sys.stderr)
        return 1
    if not result.get("logged_in"):
        print(f"ERROR: backend applied the cookies but is still signed out: {result}", file=sys.stderr)
        return 1
    if personal:
        print(f"OK: the My Team page now reads the HLTV account {result.get('username') or '(unknown)'}.", flush=True)
    else:
        print(f"OK: the backend's browser is now signed in to HLTV as {result.get('username') or '(unknown)'}.", flush=True)
    if result.get("message"):
        print(f"    {result['message']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
