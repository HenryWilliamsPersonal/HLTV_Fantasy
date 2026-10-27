import atexit
import json
import logging
import os
from pathlib import Path
import contextlib
import random
import threading
import time
from typing import Any

from backend.data.page_snapshots import save_page_snapshot

logger = logging.getLogger(__name__)

# Slower default throttle: rapid successive fetches drop Cloudflare's trust
# score and trigger interactive challenges, so a calmer pace avoids them.
_DEFAULT_RECONNECT_TIME = float(os.getenv("HLTV_UC_RECONNECT_TIME", "2"))
_FETCH_DELAY_MIN = float(os.getenv("HLTV_FETCH_DELAY_MIN", "2"))
_FETCH_DELAY_MAX = float(os.getenv("HLTV_FETCH_DELAY_MAX", "3.5"))
_WAIT_AFTER_LOAD_MIN = float(os.getenv("HLTV_UC_WAIT_AFTER_LOAD_MIN", os.getenv("HLTV_UC_WAIT_AFTER_LOAD", "0.5")))
_WAIT_AFTER_LOAD_MAX = float(os.getenv("HLTV_UC_WAIT_AFTER_LOAD_MAX", "1.0"))
_DEFAULT_WINDOW_SIZE = (1400, 900)
_FETCH_LOCK = threading.Lock()
# Personal reads (a user's My Team / Boosters page) wait on the lock with
# priority: while one is waiting, background live-page reads that offered to
# yield are refused (HLTVBrowserBusy) instead of queuing ahead of it.
_PRIORITY_LOCK = threading.Lock()
_PRIORITY_WAITING = 0


class HLTVBrowserBusy(Exception):
    """A personal read is waiting for the browser; try again later."""


def _priority_waiting() -> bool:
    with _PRIORITY_LOCK:
        return _PRIORITY_WAITING > 0


@contextlib.contextmanager
def _browser_turn(priority: bool):
    global _PRIORITY_WAITING
    if priority:
        with _PRIORITY_LOCK:
            _PRIORITY_WAITING += 1
    try:
        _FETCH_LOCK.acquire()
    finally:
        if priority:
            with _PRIORITY_LOCK:
                _PRIORITY_WAITING -= 1
    try:
        yield
    finally:
        _FETCH_LOCK.release()
_LAST_FETCH_TIME = 0.0

# Persistent shared browser: launching and quitting Chrome per fetch costs
# ~15-20s of cold-start + teardown, which dominates batch imports. Instead we
# keep ONE warm UC Chrome alive across fetches (all serialized by _FETCH_LOCK)
# and just navigate it to each URL, then quit it once idle. A warm session with
# a stable Cloudflare trust score also tends to hit fewer challenges.
_SHARED_DRIVER: Any = None
_SHARED_PROFILE_DIR: Any = None
_SHARED_HEADLESS = False
_IDLE_TIMER: "threading.Timer | None" = None
_DRIVER_IDLE_TIMEOUT = float(os.getenv("HLTV_DRIVER_IDLE_TIMEOUT", "90"))


class HLTVBrowserError(RuntimeError):
    """Raised when SeleniumBase UC cannot load or extract an HLTV page."""


def _resolve_profile_dir() -> Path:
    configured = os.getenv("HLTV_PROFILE_DIR", "").strip()
    if configured:
        configured_path = Path(configured).expanduser().resolve()
        legacy_names = {"hltv_profile", "hltv_profile_playwright"}
        if configured_path.name.lower() in legacy_names:
            migrated = configured_path.parent / "hltv_profile_seleniumbase"
            logger.warning(
                "HLTV_PROFILE_DIR points to legacy browser profile '%s'; using SeleniumBase profile '%s'.",
                configured_path,
                migrated,
            )
            return migrated.resolve()
        return configured_path
    return (Path(__file__).resolve().parents[2] / "hltv_profile_seleniumbase").resolve()


_STALE_UC_MAX_AGE = float(os.getenv("HLTV_STALE_UC_MAX_AGE", "600"))


def _is_orphaned(proc) -> bool:
    """True when the process that launched `proc` is gone (psutil guards
    against pid reuse via create time)."""
    try:
        parent = proc.parent()
        return parent is None or not parent.is_running()
    except Exception:
        return False


def _kill_stale_uc_processes(profile_dir: Path) -> None:
    """Kill uc_driver/UC-Chrome leftovers from sessions that died mid-fetch.

    They keep the profile poisoned so every new launch fails with
    'session not created: cannot connect to chrome'. _FETCH_LOCK serializes
    fetches, so nothing of ours should be alive when a driver is created; the
    age guard only protects a second app instance scraping in parallel.
    Only ORPHANED uc_driver.exe (launcher gone) are touched: when the backend
    runs as the SYSTEM service it can see every user's processes, and a live
    interactive scraping/login session must never be killed from here.
    """
    try:
        import psutil
    except Exception:
        return
    now = time.time()
    profile_marker = str(profile_dir).lower()
    for proc in psutil.process_iter(["name", "create_time", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            age = now - float(proc.info.get("create_time") or now)
            if name == "uc_driver.exe":
                # An orphaned driver is never legitimate (its launcher is
                # gone), so no age guard: every restart/crash would otherwise
                # leave one behind for ten minutes.
                if not _is_orphaned(proc):
                    continue
                logger.warning("Killing orphaned uc_driver.exe (pid %s, age %.0fs)", proc.pid, age)
                proc.kill()
                continue
            if name not in ("chrome.exe", "chrome"):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if not (profile_marker and profile_marker in cmdline):
                continue
            # SeleniumBase launches the browser process directly from this
            # python, so a browser process (no --type=) whose parent is gone is
            # a leftover from a dead backend holding our profile: kill its tree
            # now instead of paying a 45s+ dead-driver retry on the next fetch.
            is_browser_process = "--type=" not in cmdline
            if is_browser_process and _is_orphaned(proc):
                logger.warning("Killing orphaned UC Chrome (pid %s, age %.0fs) holding profile %s", proc.pid, age, profile_dir)
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except Exception:
                        pass
                proc.kill()
                continue
            if age < _STALE_UC_MAX_AGE:
                continue
            logger.warning("Killing stale UC Chrome (pid %s, age %.0fs)", proc.pid, age)
            proc.kill()
        except Exception:
            continue


def _cleanup_profile_locks(profile_dir: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = profile_dir / name
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception as exc:
            logger.debug("Unable to remove %s: %s", lock_path, exc)


def _import_seleniumbase_driver():
    try:
        from seleniumbase import Driver
    except Exception as exc:
        raise HLTVBrowserError(
            f"SeleniumBase is not available for HLTV scraping. Install requirements first: {exc}"
        ) from exc
    return Driver


def _make_driver():
    Driver = _import_seleniumbase_driver()
    profile_dir = _resolve_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    _kill_stale_uc_processes(profile_dir)
    _cleanup_profile_locks(profile_dir)

    headless = os.getenv("HLTV_HEADLESS", "0") == "1"
    if headless:
        logger.warning("HLTV_HEADLESS=1 is set, but SeleniumBase UC mode is more detectable in headless mode.")

    kwargs = {
        "uc": True,
        "headless": headless,
        "user_data_dir": str(profile_dir),
        "page_load_strategy": "eager",
        "browser": "chrome",
        # CDP performance logging so sessions can inspect network responses
        # (used to discover the fantasy trigger-rates endpoint).
        "log_cdp_events": True,
    }
    driver = None
    for drop in ("log_cdp_events", "page_load_strategy", "browser", None):
        try:
            driver = Driver(**kwargs)
            break
        except TypeError:
            if drop is None:
                raise
            kwargs.pop(drop, None)
    try:
        driver.set_page_load_timeout(45)
        driver.set_script_timeout(45)
    except Exception:
        pass
    try:
        driver.set_window_size(*_DEFAULT_WINDOW_SIZE)
    except Exception:
        pass
    return driver, profile_dir, headless


def _open_hltv_url(driver, url: str, *, reconnect_time: float | None = None) -> None:
    delay = _DEFAULT_RECONNECT_TIME if reconnect_time is None else reconnect_time
    if hasattr(driver, "uc_open_with_reconnect"):
        driver.uc_open_with_reconnect(url, reconnect_time=delay)
    else:
        driver.get(url)


def _accept_cookies(driver) -> None:
    # The banner only appears until it's accepted once; after that the profile
    # remembers it. Cheaply check the DOM for the button text first so we don't
    # pay uc_click's multi-second element-wait timeout on every subsequent page.
    try:
        if "allow all cookies" not in (driver.page_source or "").lower():
            return
    except Exception:
        return
    try:
        driver.uc_click("button:contains('Allow all cookies')", reconnect_time=1)
        return
    except Exception:
        pass
    try:
        from selenium.webdriver.common.by import By

        for button in driver.find_elements(By.TAG_NAME, "button"):
            if "allow all cookies" in (button.text or "").strip().lower():
                button.click()
                return
    except Exception:
        pass


def _looks_like_challenge(driver) -> bool:
    try:
        src = (driver.page_source or "").lower()
    except Exception:
        return False
    # The passive challenge-platform script is on every page; only genuine
    # interstitial markers indicate an actual challenge.
    if len(src) > 120000:
        return False  # a full page loaded, not a lightweight challenge shell
    return (
        "just a moment" in src
        or "challenge-running" in src
        or 'id="challenge-form"' in src
        or "checking your browser before accessing" in src
    )


def _try_gui_captcha(driver, timeout: float = 18.0) -> None:
    """Attempt UC mode's Turnstile click, but in a watchdog thread — the click
    drives the real mouse (PyAutoGUI) and works when Chrome is visible on a
    desktop, yet blocks forever without one, so it must never join the caller."""
    def run():
        for name in ("uc_gui_click_captcha", "uc_gui_handle_captcha"):
            fn = getattr(driver, name, None)
            if not fn:
                continue
            try:
                fn()
                return
            except Exception:
                pass

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)


def _clear_cloudflare(driver, url: str, wait_text: str | None) -> None:
    """Clear a Cloudflare interstitial: first wait it out with longer reconnect
    windows (managed challenges auto-solve), then, if an interactive Turnstile
    persists, try the mouse-click solver guarded by a watchdog timeout."""
    for reconnect in (8.0, 12.0):
        if not _looks_like_challenge(driver):
            return
        try:
            if hasattr(driver, "uc_open_with_reconnect"):
                driver.uc_open_with_reconnect(url, reconnect_time=reconnect)
            else:
                driver.get(url)
        except Exception:
            pass
        if wait_text:
            _wait_for_text(driver, wait_text, 12.0)
    if _looks_like_challenge(driver):
        if _SHARED_HEADLESS:
            # No desktop to click a Turnstile on (headless, possibly running as
            # the SYSTEM service). An interactive session can hand over a
            # fresh clearance instead: scripts\hltv-login-handoff.ps1 --include-cloudflare
            logger.warning(
                "Cloudflare challenge persists for %s and the browser is headless; "
                "run scripts\\hltv-login-handoff.ps1 --include-cloudflare from a desktop session to hand over a clearance.",
                url,
            )
        else:
            _try_gui_captcha(driver)
            time.sleep(3.0)
        if _looks_like_challenge(driver) and wait_text:
            _wait_for_text(driver, wait_text, 10.0)


def _wait_for_text(driver, text: str, timeout: float) -> bool:
    if not text:
        return True
    deadline = time.monotonic() + max(0.5, timeout)
    needle = text.lower()
    while time.monotonic() < deadline:
        try:
            if needle in (driver.page_source or "").lower():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _random_range(min_value: float, max_value: float) -> float:
    low = min(float(min_value), float(max_value))
    high = max(float(min_value), float(max_value))
    return random.uniform(low, high)


def _wait_for_rate_limit() -> None:
    global _LAST_FETCH_TIME
    # Enforce a MINIMUM start-to-start interval, not an added delay. The anchor
    # is the previous request's *start*, so a page that already took longer than
    # the throttle incurs no extra wait — the load time itself provided the gap.
    # Only an unusually fast page waits out the remainder.
    now = time.monotonic()
    elapsed = now - _LAST_FETCH_TIME
    delay = _random_range(_FETCH_DELAY_MIN, _FETCH_DELAY_MAX)
    if elapsed < delay:
        sleep_for = delay - elapsed
        logger.info("HLTV SeleniumBase UC throttle sleeping %.2fs before next request", sleep_for)
        time.sleep(sleep_for)
    _LAST_FETCH_TIME = time.monotonic()


def _mark_fetch_complete() -> None:
    # No-op: the throttle anchor is now set at request START in
    # _wait_for_rate_limit, so the inter-request gap is measured start-to-start
    # (a minimum interval) rather than added after each page finishes.
    return


def _wait_after_load() -> None:
    time.sleep(_random_range(_WAIT_AFTER_LOAD_MIN, _WAIT_AFTER_LOAD_MAX))


def _is_dead_webdriver_error(exc: Exception) -> bool:
    text = str(exc).lower()
    needles = (
        "connection refused",
        "actively refused",
        "max retries exceeded",
        "newconnectionerror",
        "invalid session id",
        "disconnected",
        "chrome not reachable",
        "target window already closed",
        "/window/handles",
    )
    return any(needle in text for needle in needles)


def _quit_driver(driver: Any) -> None:
    try:
        driver.quit()
    except Exception:
        pass


def _kill_orphan_chrome() -> None:
    """Force-kill Chrome processes orphaned by a dead driver session.

    When the webdriver connection dies, driver.quit() cannot reach Chrome and
    the orphaned process tree keeps the UC profile locked — every subsequent
    launch then fails with 'session not created'. Only processes whose command
    line references this app's profile directory are touched, never the
    user's own browser."""
    try:
        import subprocess

        profile = str(_resolve_profile_dir())
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$m = [regex]::Escape('" + profile.replace("'", "''") + "'); "
                "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | "
                "Where-Object { $_.CommandLine -match $m } | "
                "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }",
            ],
            timeout=30,
            capture_output=True,
        )
        logger.info("Killed orphaned Chrome processes for profile %s", profile)
    except Exception:
        logger.exception("Could not clean up orphaned Chrome processes")


def kill_own_uc_drivers() -> int:
    """Kill uc_driver.exe processes launched by THIS process. Used right before
    a deliberate hard exit (POST /admin/restart): if a fetch still holds the
    lock, driver.quit() cannot run and the driver would be orphaned."""
    try:
        import psutil
    except Exception:
        return 0
    killed = 0
    me = os.getpid()
    for proc in psutil.process_iter(["name", "ppid"]):
        try:
            if (proc.info.get("name") or "").lower() == "uc_driver.exe" and proc.info.get("ppid") == me:
                proc.kill()
                killed += 1
        except Exception:
            continue
    return killed


def _shared_driver_alive(driver: Any) -> bool:
    if driver is None:
        return False
    try:
        _ = driver.title  # cheap round-trip that fails if the session is dead
        return True
    except Exception:
        return False


def _close_shared_driver_locked() -> None:
    """Quit and forget the shared driver. Caller must hold _FETCH_LOCK."""
    global _SHARED_DRIVER, _SHARED_PROFILE_DIR
    if _SHARED_DRIVER is not None:
        _quit_driver(_SHARED_DRIVER)
    _SHARED_DRIVER = None
    _SHARED_PROFILE_DIR = None


def _acquire_shared_driver(force_new: bool = False):
    """Return the warm shared driver, (re)creating it if missing or dead.
    Caller must hold _FETCH_LOCK."""
    global _SHARED_DRIVER, _SHARED_PROFILE_DIR, _SHARED_HEADLESS
    if force_new or not _shared_driver_alive(_SHARED_DRIVER):
        _close_shared_driver_locked()
        _SHARED_DRIVER, _SHARED_PROFILE_DIR, _SHARED_HEADLESS = _make_driver()
    return _SHARED_DRIVER, _SHARED_PROFILE_DIR, _SHARED_HEADLESS


def _cancel_idle_timer() -> None:
    global _IDLE_TIMER
    if _IDLE_TIMER is not None:
        _IDLE_TIMER.cancel()
        _IDLE_TIMER = None


def _idle_close() -> None:
    # Fires from a background timer. A running fetch holds _FETCH_LOCK, so if we
    # can't take it a fetch is in progress and will reschedule us on completion.
    if _FETCH_LOCK.acquire(blocking=False):
        try:
            _close_shared_driver_locked()
        finally:
            _FETCH_LOCK.release()


def _schedule_idle_close() -> None:
    """Keep the driver warm between fetches but quit it once idle so we never
    leave Chrome running indefinitely. Caller must hold _FETCH_LOCK."""
    global _IDLE_TIMER
    _cancel_idle_timer()
    if _SHARED_DRIVER is None or _DRIVER_IDLE_TIMEOUT <= 0:
        return
    _IDLE_TIMER = threading.Timer(_DRIVER_IDLE_TIMEOUT, _idle_close)
    _IDLE_TIMER.daemon = True
    _IDLE_TIMER.start()


def close_hltv_browser() -> None:
    """Close the shared browser (e.g. at shutdown). Best-effort."""
    got = _FETCH_LOCK.acquire(timeout=5)
    try:
        _cancel_idle_timer()
        _close_shared_driver_locked()
    finally:
        if got:
            _FETCH_LOCK.release()


atexit.register(close_hltv_browser)


def fetch_hltv_html(
    url: str,
    *,
    timeout_ms: int = 45000,
    wait_text: str | None = None,
    reconnect_time: float | None = None,
    yield_to_priority: bool = False,
) -> str:
    if yield_to_priority and _priority_waiting():
        raise HLTVBrowserBusy("a personal read is waiting for the browser")
    with _FETCH_LOCK:
        # checked again once the lock is ours: a read that queued before the
        # personal one arrived steps aside too (raising releases the lock)
        if yield_to_priority and _priority_waiting():
            raise HLTVBrowserBusy("a personal read is waiting for the browser")
        _cancel_idle_timer()
        _wait_for_rate_limit()
        last_error: Exception | None = None
        try:
            for attempt in range(2):
                try:
                    driver, profile_dir, headless = _acquire_shared_driver(force_new=(attempt == 1))
                    _open_hltv_url(driver, url, reconnect_time=reconnect_time)
                    _accept_cookies(driver)
                    if wait_text:
                        _wait_for_text(driver, wait_text, min(12.0, max(1.0, timeout_ms / 1000.0)))
                    # If the wait_text never appeared and the page is a Cloudflare
                    # challenge, wait it out (reconnect-based, no GUI clicks).
                    if _looks_like_challenge(driver):
                        _clear_cloudflare(driver, url, wait_text)
                    _wait_after_load()
                    html = driver.page_source or ""
                    # Still challenged after clearing? On the first attempt, retry
                    # with a brand-new driver and a long reconnect window.
                    if attempt == 0 and _looks_like_challenge(driver):
                        logger.warning("HLTV Cloudflare challenge persisted for %s; retrying with a fresh driver.", url)
                        time.sleep(2.0)
                        driver, profile_dir, headless = _acquire_shared_driver(force_new=True)
                        _open_hltv_url(driver, url, reconnect_time=12.0)
                        _accept_cookies(driver)
                        if wait_text:
                            _wait_for_text(driver, wait_text, min(15.0, max(1.0, timeout_ms / 1000.0)))
                        if _looks_like_challenge(driver):
                            _clear_cloudflare(driver, url, wait_text)
                        _wait_after_load()
                        html = driver.page_source or ""
                    _mark_fetch_complete()
                    save_page_snapshot(url, html)
                    logger.info(
                        "HLTV SeleniumBase UC page loaded: title='%s' final_url=%s headless=%s profile_dir=%s",
                        getattr(driver, "title", ""),
                        getattr(driver, "current_url", url),
                        headless,
                        profile_dir,
                    )
                    return html
                except Exception as exc:
                    last_error = exc
                    # A crashed session must be dropped so the next attempt (or
                    # call) starts a fresh warm driver.
                    if _is_dead_webdriver_error(exc):
                        _close_shared_driver_locked()
                        _kill_orphan_chrome()
                    if attempt == 0 and _is_dead_webdriver_error(exc):
                        logger.warning("HLTV UC driver died while fetching %s; retrying with a fresh driver: %s", url, exc)
                        time.sleep(1.0)
                        continue
                    raise HLTVBrowserError(f"Failed to fetch HLTV page with SeleniumBase UC: {exc}") from exc
            raise HLTVBrowserError(f"Failed to fetch HLTV page with SeleniumBase UC: {last_error}")
        finally:
            _schedule_idle_close()


def run_hltv_browser_session(
    url: str,
    callback,
    *,
    timeout_ms: int = 45000,
    wait_text: str | None = None,
    reconnect_time: float | None = None,
    priority: bool = False,
):
    with _browser_turn(priority):
        _cancel_idle_timer()
        _wait_for_rate_limit()
        last_error: Exception | None = None
        try:
            for attempt in range(2):
                try:
                    driver, profile_dir, _ = _acquire_shared_driver(force_new=(attempt == 1))
                    _open_hltv_url(driver, url, reconnect_time=reconnect_time)
                    _accept_cookies(driver)
                    if wait_text:
                        _wait_for_text(driver, wait_text, min(12.0, max(1.0, timeout_ms / 1000.0)))
                    _wait_after_load()
                    result = callback(driver)
                    _mark_fetch_complete()
                    try:
                        # Archive whatever page the session ended on, keyed by its
                        # actual URL (the callback may have navigated).
                        save_page_snapshot(getattr(driver, "current_url", url) or url, driver.page_source or "")
                    except Exception:
                        pass
                    return result
                except Exception as exc:
                    last_error = exc
                    if _is_dead_webdriver_error(exc):
                        _close_shared_driver_locked()
                        _kill_orphan_chrome()
                    if attempt == 0 and _is_dead_webdriver_error(exc):
                        logger.warning("HLTV UC driver died while running session for %s; retrying: %s", url, exc)
                        time.sleep(1.0)
                        continue
                    raise HLTVBrowserError(f"Failed to run HLTV browser session with SeleniumBase UC: {exc}") from exc
            raise HLTVBrowserError(f"Failed to run HLTV browser session with SeleniumBase UC: {last_error}")
        finally:
            _schedule_idle_close()


def fetch_hltv_json(url: str, *, timeout_ms: int = 45000) -> dict[str, Any]:
    with _FETCH_LOCK:
        _cancel_idle_timer()
        _wait_for_rate_limit()
        last_error: Exception | None = None
        try:
            for attempt in range(2):
                try:
                    driver, profile_dir, _ = _acquire_shared_driver(force_new=(attempt == 1))
                    _open_hltv_url(driver, url)
                    _wait_after_load()

                    body_text = ""
                    try:
                        from selenium.webdriver.common.by import By

                        body_text = driver.find_element(By.TAG_NAME, "body").text
                    except Exception:
                        body_text = ""

                    if body_text.strip():
                        try:
                            data = json.loads(body_text)
                            _mark_fetch_complete()
                            return data
                        except Exception:
                            pass

                    script = """
                        const url = arguments[0];
                        const done = arguments[arguments.length - 1];
                        fetch(url, {headers: {accept: "application/json"}})
                            .then((response) => response.text())
                            .then((text) => done({ok: true, text}))
                            .catch((error) => done({ok: false, error: String(error)}));
                    """
                    result = driver.execute_async_script(script, url)
                    if not result or not result.get("ok"):
                        raise HLTVBrowserError((result or {}).get("error") or "browser fetch failed")
                    data = json.loads(result.get("text") or "{}")
                    _mark_fetch_complete()
                    return data
                except Exception as exc:
                    last_error = exc
                    if _is_dead_webdriver_error(exc):
                        _close_shared_driver_locked()
                        _kill_orphan_chrome()
                    if attempt == 0 and _is_dead_webdriver_error(exc):
                        logger.warning("HLTV UC driver died while fetching JSON %s; retrying with a fresh driver: %s", url, exc)
                        time.sleep(1.0)
                        continue
                    if isinstance(exc, HLTVBrowserError):
                        raise
                    raise HLTVBrowserError(f"Failed to fetch HLTV JSON with SeleniumBase UC: {exc}") from exc
            raise HLTVBrowserError(f"Failed to fetch HLTV JSON with SeleniumBase UC: {last_error}")
        finally:
            _schedule_idle_close()
