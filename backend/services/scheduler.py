"""Nightly data-ingestion scheduler.

A single daemon thread wakes periodically and, once per day at the configured
local time, runs the ingestion jobs in sequence — team rankings, team data,
new matches, then the map and rating models. They run sequentially because they all
serialize on the one shared HLTV browser anyway. Manual "run now" triggers reuse
the same code path. State + history live in schedule_db.

No external scheduler dependency: this is plain threading, started from the
FastAPI lifespan so it only runs while the (always-on) backend is up.
"""

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.data import schedule_db


# ---------------------------------------------------------------------------
# Heartbeat: a healthchecks.io-style URL pinged when the nightly batch starts,
# succeeds or fails (the service alerts when the success ping is late or a
# fail ping arrives). The URL comes from HLTV_HEARTBEAT_URL or
# .runtime/heartbeat.json {"url": "https://hc-ping.com/<uuid>"}; nothing is
# pinged when neither is set. Failures to ping never affect the batch.
def _backup_status() -> Dict[str, Any]:
    from backend.services import backup

    try:
        cfg = backup.backup_config()
        return {"dir": cfg.get("dir"), "keep_days": cfg.get("keep_days"), "latest": backup.latest_backups()}
    except Exception as exc:  # noqa: BLE001
        return {"dir": None, "error": str(exc)}


def heartbeat_url() -> str:
    import json
    import os
    from pathlib import Path

    url = str(os.getenv("HLTV_HEARTBEAT_URL") or "").strip()
    if url:
        return url
    path = Path(__file__).resolve().parents[2] / ".runtime" / "heartbeat.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("url") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def heartbeat(kind: str) -> bool:
    """kind: "start" | "success" | "fail". Returns True when a ping was sent."""
    import urllib.request

    base = heartbeat_url()
    if not base:
        return False
    url = base.rstrip("/") + {"start": "/start", "fail": "/fail"}.get(kind, "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="POST", data=b""), timeout=10) as resp:
            ok = 200 <= int(resp.status) < 300
        logger.info("Heartbeat %s -> %s", kind, "ok" if ok else "rejected")
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("Heartbeat %s failed: %s", kind, exc)
        return False

logger = logging.getLogger(__name__)

RESUME_WINDOW_SECONDS = 20 * 3600  # an interrupted batch older than this is left to the next slot
RESUME_DELAY_SECONDS = 120  # let the process warm up before the resumed batch starts
_TICK_SECONDS = 30


class DataScheduler:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._run_lock = threading.Lock()  # only one ingestion batch at a time
        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "running": False,
            "current_task": None,
            "trigger": None,
            "started_at": None,
            "processed": 0,
            "total": 0,
            "message": "",
        }

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_resurrect = 0.0
        self._keep_awake = False
        self._resume: Optional[Dict[str, Any]] = None
        try:
            orphans = schedule_db.fail_orphan_runs()
        except Exception:
            logger.exception("Could not clean orphaned schedule runs")
            orphans = []
        # A batch the previous process died in (restart, update, reboot)
        # resumes from the interrupted task once this process has warmed
        # up, so a restart during the nightly run no longer costs the
        # night's results import and valuations.
        latest = max(orphans, key=lambda o: o["started_at"]) if orphans else None
        if latest and time.time() - latest["started_at"] < RESUME_WINDOW_SECONDS:
            self._resume = {"task": latest["task"], "trigger": latest["trigger"], "due": time.time() + RESUME_DELAY_SECONDS}
            logger.info("Scheduler will resume the %s batch from task %s in %ss", latest["trigger"], latest["task"], RESUME_DELAY_SECONDS)
        self._thread = threading.Thread(target=self._loop, name="data-scheduler", daemon=True)
        self._thread.start()
        logger.info("Data scheduler started")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(_TICK_SECONDS):
            try:
                self._maybe_resume()
            except Exception:
                logger.exception("Scheduler resume failed")
            try:
                self._maybe_run_morning()
            except Exception:
                logger.exception("Scheduler morning tick failed")
            try:
                self._maybe_run_scheduled()
            except Exception:
                logger.exception("Scheduler tick failed")
            try:
                self._maybe_resurrect_jobs()
            except Exception:
                logger.exception("Job resurrection tick failed")
            try:
                self._update_keep_awake()
            except Exception:
                logger.exception("Keep-awake tick failed")

    # ---- overnight autonomy --------------------------------------------------
    # Long scraping jobs get marked "paused" whenever the backend dies mid-run
    # (reboot, restart-backend.ps1, crash). Nothing used to resume them, so an
    # unattended machine made no progress for days. The scheduler thread now
    # resurrects interruption-paused jobs itself — a pause the USER requested
    # (pause_requested=True) is respected and left alone.
    def _job_families(self):
        from backend.routes import admin, events, teams

        return [
            ("trigger rates", admin._get_latest_trigger_job, admin.resume_trigger_backfill_job),
            ("veto backfill", events._get_latest_veto_job, events.resume_veto_backfill_job),
            ("map scoreboards", events._get_latest_map_sb_job, events.resume_map_scoreboards_job),
            ("historical map-stats", events._get_latest_historical_job, events.resume_historical_map_stats_job),
            ("results import", events.get_latest_hltv_results_import_job, events.resume_hltv_results_import_job),
            ("rankings refresh", teams._get_latest_rankings_job, teams.resume_rankings_refresh_job),
            ("team rosters", teams._get_latest_roster_job, teams.resume_roster_import_job),
            ("map-stats import", lambda: teams._get_latest_map_stats_job(include_completed=False),
             teams.resume_map_stats_import_job),
        ]

    def _maybe_resurrect_jobs(self) -> None:
        now = time.time()
        if now - self._last_resurrect < 120:
            return
        self._last_resurrect = now
        for name, getter, resume in self._job_families():
            try:
                job = getter() or {}
                if not isinstance(job, dict) or job.get("exists") is False:
                    continue
                if str(job.get("status") or "") != "paused" or job.get("pause_requested"):
                    continue
                # A pause the worker honoured on request (the user's Pause button
                # or the map_model task's time budget) is recorded as last_error
                # "Paused"; an interruption (backend died mid-run) carries the
                # "interrupted before completion" text. Only the latter resumes.
                if str(job.get("last_error") or "").strip() == "Paused":
                    continue
                resume(str(job.get("job_id")))
                logger.info("Auto-resumed interrupted %s job %s", name, job.get("job_id"))
            except Exception:
                logger.info("Could not auto-resume %s job", name, exc_info=True)

    def _any_work_active(self) -> bool:
        with self._state_lock:
            if self._state.get("running"):
                return True
        active = {"queued", "running", "pausing", "canceling"}
        for _name, getter, _resume in self._job_families():
            try:
                job = getter() or {}
                if isinstance(job, dict) and str(job.get("status") or "") in active:
                    return True
            except Exception:
                continue
        return False

    def _update_keep_awake(self) -> None:
        """Block system sleep while any scrape job or scheduled batch runs —
        an overnight run is pointless if Windows dozes off 20 minutes in.
        SetThreadExecutionState is per-thread, and this scheduler thread lives
        for the whole process, so the flag persists until released here."""
        busy = self._any_work_active()
        if busy == self._keep_awake:
            return
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if busy else 0)
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self._keep_awake = busy
            logger.info("Keep-awake %s (jobs %s)", "engaged" if busy else "released", "active" if busy else "idle")
        except Exception:
            logger.exception("SetThreadExecutionState failed")

    # A catch-up run this close before the next slot also counts for that slot,
    # so a machine that comes back late in the afternoon does not run twice.
    _CATCH_UP_COVERS_NEXT = timedelta(hours=6)

    def _due_slot(self, cfg: Dict[str, Any], now: datetime, key: str = "run_time") -> datetime:
        """The most recent daily slot that has passed: today's, or yesterday's
        while today's is still ahead."""
        hh, mm = (cfg.get(key) or "00:00").split(":")
        today_slot = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return today_slot if now >= today_slot else today_slot - timedelta(days=1)

    def _pending_morning_slot(self, cfg: Dict[str, Any], now: datetime) -> Optional[datetime]:
        """The morning slot still owed, or None: covered once a morning run
        started at or after it, or once the nightly batch has (the nightly
        imports the same results, so a morning missed until then is moot)."""
        if not cfg.get("do_morning"):
            return None
        due = self._due_slot(cfg, now, key="morning_time")
        if schedule_db.last_morning_run_ts() >= due.timestamp():
            return None
        if schedule_db.last_scheduled_run_ts() >= due.timestamp():
            return None
        return due

    def _pending_slot(self, cfg: Dict[str, Any], now: datetime) -> Optional[datetime]:
        """The slot the batch still owes, or None. A slot is covered once a
        scheduled or catch-up run started at or after it (survives restarts
        via the DB), or when a catch-up ran within a few hours before it."""
        due = self._due_slot(cfg, now)
        last = schedule_db.last_scheduled_run_ts()
        if last >= due.timestamp():
            return None
        if last > 0 and due.timestamp() - last < self._CATCH_UP_COVERS_NEXT.total_seconds():
            return None
        return due

    def _maybe_run_scheduled(self) -> None:
        cfg = schedule_db.get_schedule_config()
        if not cfg.get("enabled"):
            return
        now = datetime.now()
        due = self._pending_slot(cfg, now)
        if due is None:
            return
        if self._run_lock.locked():
            return  # a manual run is in progress; retry next tick
        # Fired at the slot itself: a normal scheduled run. Anything later is a
        # catch-up: the machine or the backend was down at the slot, so the
        # batch runs now instead of skipping the day.
        trigger = "scheduled" if now - due < timedelta(minutes=5) else "catch-up"
        if trigger == "catch-up":
            logger.info("Catching up the %s batch missed at %s", cfg.get("run_time"), due.strftime("%Y-%m-%d %H:%M"))
        self._run_batch(self._enabled_tasks(cfg), trigger=trigger, cfg=cfg)

    def _maybe_run_morning(self) -> None:
        """The light morning run: the results import, then the season read
        (an event that ended overnight is scored by then)."""
        cfg = schedule_db.get_schedule_config()
        if not cfg.get("enabled") or not cfg.get("do_matches"):
            return
        now = datetime.now()
        due = self._pending_morning_slot(cfg, now)
        if due is None or self._run_lock.locked():
            return
        trigger = "morning" if now - due < timedelta(minutes=5) else "morning-catch-up"
        self._run_batch(["matches"], trigger=trigger, cfg=cfg, tail=("season", "field"))

    # ---- running -------------------------------------------------------------
    @staticmethod
    def _enabled_tasks(cfg: Dict[str, Any]) -> List[str]:
        tasks = []
        if cfg.get("do_events"):
            tasks.append("events")
        if cfg.get("do_rankings"):
            tasks.append("rankings")
        # Rosters + team map stats for the top-N teams; after rankings so the
        # scope uses tonight's ranks.
        if cfg.get("do_team_data"):
            tasks.append("team_data")
        if cfg.get("do_matches"):
            tasks.append("matches")
        # After the results import, so tonight's new matches get their
        # vetoes, scoreboards and map-stat windows too.
        if cfg.get("do_map_model"):
            tasks.append("map_model")
        return tasks

    def run_now(self, task: str = "all") -> Dict[str, Any]:
        """Trigger a run in a background thread; returns immediately."""
        if self._run_lock.locked():
            return {"status": "busy", "detail": "An ingestion run is already in progress."}
        cfg = schedule_db.get_schedule_config()
        if task == "all":
            tasks = self._enabled_tasks(cfg) or list(schedule_db.TASK_KEYS)
        elif task in schedule_db.TASK_KEYS:
            tasks = [task]
        elif "," in task and all(t.strip() in schedule_db.TASK_KEYS for t in task.split(",")):
            wanted = {t.strip() for t in task.split(",")}
            tasks = [t for t in (self._enabled_tasks(cfg) or list(schedule_db.TASK_KEYS)) if t in wanted] or sorted(wanted)
        elif task == "hltv_session":
            threading.Thread(
                target=self._run_session_check_only, args=("manual",), name="scheduler-session-check", daemon=True
            ).start()
            return {"status": "started", "tasks": ["hltv_session"]}
        elif task == "valuations":
            threading.Thread(target=self._run_bake_only, args=("manual",), name="scheduler-bake", daemon=True).start()
            return {"status": "started", "tasks": ["valuations"]}
        elif task == "backup":
            threading.Thread(target=self._run_backup_only, args=("manual",), name="scheduler-backup", daemon=True).start()
            return {"status": "started", "tasks": ["backup"]}
        elif task == "season":
            threading.Thread(target=self._run_season_only, args=("manual",), name="scheduler-season", daemon=True).start()
            return {"status": "started", "tasks": ["season"]}
        else:
            return {"status": "error", "detail": f"Unknown task '{task}'."}
        threading.Thread(
            target=self._run_batch, args=(tasks, "manual", cfg), name="scheduler-manual", daemon=True
        ).start()
        return {"status": "started", "tasks": tasks}

    def _run_batch(self, tasks: List[str], trigger: str, cfg: Dict[str, Any], tail: Optional[tuple] = None) -> None:
        """The tasks in order, then the tail (all of it, or only the steps
        named in `tail`)."""
        if not tasks:
            return
        if not self._run_lock.acquire(blocking=False):
            return
        batch_started = time.time()
        outcome = "fail"
        heartbeat("start")
        try:
            for task in tasks:
                self._run_task(task, trigger, cfg)
            self._run_tail(trigger, only=tail)
            statuses = [str(r.get("status")) for r in schedule_db.list_runs(limit=40) if float(r.get("started_at") or 0) >= batch_started]
            outcome = "fail" if "error" in statuses else "success"
        finally:
            self._run_lock.release()
            self._set_state(running=False, current_task=None, trigger=None, processed=0, total=0)
            heartbeat(outcome)

    def _run_tail(self, trigger: str, start: str = "valuations", only: Optional[tuple] = None) -> None:
        """What every batch ends with, from `start` on: the event behaviour
        contract for every unfinished event (bake what is missing, refresh
        what has not started, leave the rest), the HLTV login-health check
        (a dying remember-me cookie flagged weeks ahead in the run history),
        and a backup of both databases (backend/services/backup.py)."""
        steps = [
            ("valuations", lambda: self._bake_valuations(trigger, only_if_missing=True)),
            ("season", lambda: self._refresh_season(trigger)),
            ("field", lambda: self._start_field_reads(trigger)),
            ("hltv_session", lambda: self._check_hltv_session(trigger)),
            ("backup", lambda: self._run_backup(trigger)),
        ]
        names = [n for n, _ in steps]
        for name, fn in steps[names.index(start) if start in names else 0:]:
            if only is not None and name not in only:
                continue
            fn()

    def _maybe_resume(self) -> None:
        """Run the batch the previous process was interrupted in, from the
        interrupted task: that task again (its own job may have resumed by
        itself, which is harmless) and every task after it, then the tail."""
        pending = self._resume
        if not pending or time.time() < float(pending.get("due") or 0):
            return
        self._resume = None
        cfg = schedule_db.get_schedule_config()
        if not cfg.get("enabled"):
            return
        task = str(pending.get("task") or "")
        order = self._enabled_tasks(cfg)
        tail = ["valuations", "season", "field", "hltv_session", "backup"]
        if task in order:
            logger.info("Resuming the interrupted %s batch from task %s", pending.get("trigger"), task)
            self._run_batch(order[order.index(task):], "resume", cfg)
        elif task in tail:
            if not self._run_lock.acquire(blocking=False):
                return
            try:
                logger.info("Resuming the interrupted %s batch from %s", pending.get("trigger"), task)
                self._run_tail("resume", start=task)
            finally:
                self._run_lock.release()
                self._set_state(running=False, current_task=None, trigger=None, processed=0, total=0)

    def _refresh_season(self, trigger: str) -> None:
        """Every connected account's fantasy season (placement, season points
        per game), re-read once the night's events are scored; its own run
        row so the Scheduling history shows it (services/fantasy_season)."""
        from backend.services import fantasy_season

        run_id = schedule_db.start_run("season", trigger)
        self._set_state(running=True, current_task="season", trigger=trigger, started_at=time.time(),
                        processed=0, total=0, message="Reading the fantasy season...")
        try:
            schedule_db.finish_run(run_id, "success", fantasy_season.refresh_all())
        except Exception as exc:  # noqa: BLE001 — never let it break a batch
            schedule_db.finish_run(run_id, "error", f"season read failed: {exc}")
            logger.exception("Season refresh failed")

    def _start_field_reads(self, trigger: str) -> None:
        """Start the field read a live event needs (services/fantasy_field:
        every team of its league, for the leaderboard's roles, boosters and
        best team); the read runs on in the background for hours."""
        from backend.services import fantasy_field

        run_id = schedule_db.start_run("field", trigger)
        try:
            schedule_db.finish_run(run_id, "success", fantasy_field.auto_reads())
        except Exception as exc:  # noqa: BLE001
            schedule_db.finish_run(run_id, "error", f"field read not started: {exc}")
            logger.exception("Field read start failed")

    def _run_season_only(self, trigger: str) -> None:
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            self._refresh_season(trigger)
        finally:
            self._run_lock.release()
            self._set_state(running=False, current_task=None, trigger=None, processed=0, total=0)

    def _check_hltv_session(self, trigger: str) -> None:
        """Is the scraper browser still signed in to HLTV, and for how long?
        One page load, recorded as its own run row (status success / warning /
        error) so the Scheduling tab shows it. See backend/services/hltv_session.py."""
        from backend.services import hltv_session

        run_id = schedule_db.start_run("hltv_session", trigger)
        self._set_state(running=True, current_task="hltv_session", trigger=trigger, started_at=time.time(),
                        processed=0, total=0, message="Checking HLTV login...")
        try:
            snap = hltv_session.check_session_health()
            status = {"ok": "success", "warning": "warning"}.get(str(snap.get("level")), "error")
            schedule_db.finish_run(run_id, status, str(snap.get("message") or ""))
        except Exception as exc:  # noqa: BLE001 — never let the check break a batch
            schedule_db.finish_run(run_id, "error", f"check failed: {exc}")
            logger.exception("HLTV session check failed")

    def _run_backup(self, trigger: str) -> None:
        """Consistent, compressed copies of both databases into the configured
        folder (OneDrive), keeping the last N days. Its own run row: warning
        when no folder is configured, error when the copy fails."""
        from backend.services import backup

        run_id = schedule_db.start_run("backup", trigger)
        self._set_state(running=True, current_task="backup", trigger=trigger, started_at=time.time(),
                        processed=0, total=0, message="Backing up databases...")
        try:
            outcome = backup.run_backup(progress=lambda msg: self._set_state(message=msg))
            status = "success" if outcome.get("status") == "ok" else "warning"
            schedule_db.finish_run(run_id, status, str(outcome.get("summary") or outcome.get("reason") or ""))
            logger.info("Backup (%s): %s", trigger, outcome.get("summary") or outcome.get("reason"))
        except Exception as exc:  # noqa: BLE001 - never let the backup break a batch
            schedule_db.finish_run(run_id, "error", f"backup failed: {exc}")
            logger.exception("Backup failed")

    def _run_backup_only(self, trigger: str) -> None:
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            self._run_backup(trigger)
        finally:
            self._run_lock.release()
            self._set_state(running=False, current_task=None, trigger=None, processed=0, total=0)

    def _run_session_check_only(self, trigger: str) -> None:
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            self._check_hltv_session(trigger)
        finally:
            self._run_lock.release()
            self._set_state(running=False, current_task=None, trigger=None, processed=0, total=0)

    def _bake_valuations(self, trigger: str, only_if_missing: bool = False) -> None:
        """Apply the event behaviour contract to every unfinished event (see
        backend.services.event_pipeline): bake what is missing, refresh with
        tonight's inputs what has not started, leave started events alone,
        never touch finished ones. One run row summarises all of them.
        `only_if_missing` is kept for callers; the contract decides."""
        from backend.services import event_pipeline as pipeline

        run_id = schedule_db.start_run("valuations", trigger)
        self._set_state(running=True, current_task="valuations", trigger=trigger, started_at=time.time(),
                        processed=0, total=0, message="Baking event valuations...")
        try:
            outcomes = pipeline.refresh_live_events(trigger)
            if not outcomes:
                schedule_db.finish_run(run_id, "warning", "no unfinished events")
                return
            rank = {"error": 2, "skipped": 1, "pending": 1, "manual": 1, "unsupported": 1, "missing": 1}
            worst = max(rank.get(str(o.get("status")), 0) for o in outcomes)
            status = {0: "success", 1: "warning", 2: "error"}[worst]
            parts = []
            for o in outcomes:
                extra = o.get("reason") or (f"{o.get('seconds')}s" if o.get("seconds") is not None else "")
                parts.append(f"{o.get('event_id')}: {o.get('status')}{' ' + str(extra) if extra else ''}")
            summary = "; ".join(parts)
            schedule_db.finish_run(run_id, status, summary[:2000])
            logger.info("Valuation pass (%s): %s", trigger, summary[:500])
        except Exception as exc:  # noqa: BLE001 — never let the bake break a batch
            schedule_db.finish_run(run_id, "error", f"bake failed: {exc}")
            logger.exception("Valuation pass failed")

    def _run_bake_only(self, trigger: str) -> None:
        # Manual run-now: the same pass as the nightly one, over every unfinished event.
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            self._bake_valuations(trigger, only_if_missing=False)
        finally:
            self._run_lock.release()
            self._set_state(running=False, current_task=None, trigger=None, processed=0, total=0)

    def _run_task(self, task: str, trigger: str, cfg: Dict[str, Any]) -> None:
        run_id = schedule_db.start_run(task, trigger)
        self._set_state(running=True, current_task=task, trigger=trigger, started_at=time.time(),
                        processed=0, total=0, message=f"Running {task}...")
        try:
            if task == "events":
                msg = self._task_events()
            elif task == "rankings":
                msg = self._task_rankings()
            elif task == "matches":
                msg = self._task_matches(cfg)
            elif task == "map_model":
                msg = self._task_map_model(cfg)
            elif task == "team_data":
                msg = self._task_team_data(cfg)
            else:
                raise ValueError(f"Unknown task '{task}'")
            schedule_db.finish_run(run_id, "success", msg)
            logger.info("Scheduler task %s (%s) succeeded: %s", task, trigger, msg)
        except Exception as exc:  # noqa: BLE001 — record and continue to next task
            schedule_db.finish_run(run_id, "error", str(exc))
            logger.exception("Scheduler task %s (%s) failed", task, trigger)

    # ---- individual tasks (lazy imports avoid import cycles) -----------------
    def _task_events(self) -> str:
        from backend.routes import admin, events

        result = events.discover_and_import_new_fantasy_events() or {}
        # Booster/role data rides along: when active-event players are missing
        # trigger rates, start the backfill (it auto-provisions the fantasy
        # team for events with no captured endpoint yet).
        try:
            if result.get("imported"):
                # EVERY new event gets a full rates refresh, one at a time —
                # data drifts between events, and per-stage events (playoffs +
                # two qualifiers in one night) each need their own fetch.
                notes = []
                for fid in sorted(result["imported"]):
                    outcome = admin.run_trigger_backfill_blocking(int(fid), refresh_all=True)
                    notes.append(f"{fid}: {outcome}")
                result["trigger_backfill"] = "; ".join(notes)
                # Bake each new event's valuation straight away (stored per
                # event), so it is ready the moment it is made active; the
                # nightly pass refreshes it until the event starts.
                from backend.services import event_pipeline

                baked = [event_pipeline.bake_event(int(fid), trigger="import") for fid in sorted(result["imported"])]
                result["valuations"] = "; ".join(f"{b.get('event_id')}: {b.get('status')} {b.get('reason') or b.get('seconds', '')}" for b in baked)
            else:
                _prices, missing, _cov = admin._missing_trigger_players(None)
                if missing:
                    started = admin.start_trigger_backfill({})
                    result["trigger_backfill"] = f"started for active event ({len(missing)} players missing)"
                    result["trigger_job"] = started.get("job_id")
                else:
                    result["trigger_backfill"] = "coverage complete"
        except Exception as exc:  # noqa: BLE001 — report, don't fail the import
            result["trigger_backfill"] = f"not started: {exc}"
        return _short(result)

    def _task_rankings(self) -> str:
        from backend.routes import teams

        result = teams.refresh_all_rankings_for_all_teams_today() or {}
        return _short(result)

    def _task_matches(self, cfg: Dict[str, Any]) -> str:
        from backend.routes import events

        lookback = int(cfg.get("matches_lookback_days") or 3)
        until = (date.today() - timedelta(days=lookback)).isoformat()
        result = events.import_hltv_results({"import_mode": "until_date", "until_date": until}) or {}
        return _short(result)

    def _drive_job(self, name, unit, total_hint, latest_fn, start_fn, resume_fn, pause_fn, deadline,
                   processed_key="processed_items", total_key="total_items", final_fn=None):
        """Run one pausable job family to completion (or to the deadline):
        resume a paused/failed latest job, else start one; poll it, mirror
        progress into the scheduler state; at the deadline ask it to pause
        and wait for it to settle. Returns (note, failed)."""
        active = {"queued", "running", "pausing", "canceling"}
        latest = latest_fn() or {}
        status = str(latest.get("status") or "")
        if status in active:
            job_id = str(latest.get("job_id") or "")
        elif status in {"paused", "failed"} and latest.get("job_id"):
            job_id = str(latest["job_id"])
            resume_fn(job_id)
        else:
            job_id = str((start_fn() or {}).get("job_id") or "")
        if not job_id:
            return f"{name}: could not start a job", True
        self._set_state(processed=0, total=int(total_hint or 0), message=f"{name.capitalize()}: starting ({total_hint} {unit})")
        cut_short = False
        while True:
            time.sleep(2)
            job = latest_fn() or {}
            st = str(job.get("status") or "")
            processed = int(job.get(processed_key) or 0)
            total = int(job.get(total_key) or 0)
            self._set_state(processed=processed, total=total,
                            message=f"{name.capitalize()}: {processed}/{total} {unit}" + (" (pausing, time budget)" if cut_short else ""))
            if st not in active:
                break
            if not cut_short and time.time() >= deadline:
                pause_fn(job_id)
                cut_short = True
        # The finished job, from a lookup that includes completed jobs: a
        # latest_fn limited to live jobs answers nothing once it is done.
        final = (final_fn or latest_fn)() or {}
        st = str(final.get("status") or "")
        ok = int(final.get("ok") or 0)
        failed = int(final.get("failed") or 0)
        tail = " (paused: time budget)" if cut_short else f" ({st})"
        return f"{name}: {ok} done, {failed} failed{tail}", st == "failed"

    def _task_team_data(self, cfg: Dict[str, Any]) -> str:
        """Refresh the top-N ranked teams: current lineups from their HLTV team
        pages (players created by HLTV id, roster slots filled), then their
        six-month map stats through the existing map-stats import job."""
        from backend.routes import teams

        top_n = max(10, min(500, int(cfg.get("team_data_top_n") or 200)))
        deadline = time.time() + 6 * 3600  # bounded by N teams, not by time
        notes: List[str] = []
        failed_any = False

        cov = teams._roster_coverage(top_n)
        note, failed = self._drive_job(
            "team rosters", "teams", cov.get("teams_in_scope", 0),
            teams._get_latest_roster_job, lambda: teams.start_roster_import_job({"top_n": top_n}),
            teams.resume_roster_import_job, teams.pause_roster_import_job, deadline,
        )
        after = teams._roster_coverage(top_n)
        notes.append(
            f"{note}, {after.get('checked', 0)}/{after.get('teams_in_scope', 0)} top-{top_n} teams checked this week, "
            f"{after.get('full_lineups', 0)} with a full lineup, {after.get('short_lineups', 0)} short"
        )
        failed_any = failed_any or failed

        scope_ids = [int(t["team_id"]) for t in teams._roster_scope_teams(top_n)]
        note, failed = self._drive_job(
            "team map stats", "teams", len(scope_ids),
            lambda: teams._get_latest_map_stats_job(include_completed=False),
            lambda: teams.start_map_stats_import_job({"team_ids": scope_ids}),
            teams.resume_map_stats_import_job, teams.pause_map_stats_import_job, deadline,
            processed_key="processed_teams", total_key="total_teams",
            final_fn=lambda: teams._get_latest_map_stats_job(include_completed=True),
        )
        notes.append(note)
        failed_any = failed_any or failed

        message = "; ".join(notes)
        if failed_any:
            raise RuntimeError(message)
        return message

    def _task_map_model(self, cfg: Dict[str, Any]) -> str:
        """Fetch what the map-data model is missing: match vetoes, per-map
        scoreboards, then historical map-stat windows (the quick ones first;
        the windows are the long tail and take whatever time is left). Drives
        the same pausable jobs the Model Lab runs by hand (so the lab shows
        the progress and can pause them), within map_model_minutes per night;
        a job cut short is paused and resumed by the next night's task."""
        from backend.routes import events

        budget = max(60.0, float(cfg.get("map_model_minutes") or 120) * 60.0)
        deadline = time.time() + budget
        active = {"queued", "running", "pausing", "canceling"}
        families = [
            ("vetoes", "matches", events.get_veto_backfill_coverage, "missing_veto",
             events._get_latest_veto_job, events.start_veto_backfill_job,
             events.resume_veto_backfill_job, events.pause_veto_backfill_job),
            ("map scoreboards", "matches", events.get_map_scoreboards_coverage, "missing_map_scoreboards",
             events._get_latest_map_sb_job, events.start_map_scoreboards_job,
             events.resume_map_scoreboards_job, events.pause_map_scoreboards_job),
            ("historical map stats", "windows", events.get_historical_map_stats_coverage, "missing_windows",
             events._get_latest_historical_job, events.start_historical_map_stats_job,
             events.resume_historical_map_stats_job, events.pause_historical_map_stats_job),
        ]
        notes: List[str] = []
        failed_any = False
        for name, unit, coverage_fn, missing_key, latest_fn, start_fn, resume_fn, pause_fn in families:
            missing = int((coverage_fn() or {}).get(missing_key) or 0)
            if missing <= 0:
                notes.append(f"{name}: complete")
                continue
            if time.time() >= deadline:
                notes.append(f"{name}: {missing} {unit} missing, left for the next night (time budget)")
                continue
            note, failed = self._drive_job(name, unit, missing, latest_fn, start_fn, resume_fn, pause_fn, deadline)
            after = int((coverage_fn() or {}).get(missing_key) or 0)
            notes.append(f"{note}, {after} {unit} left")
            failed_any = failed_any or failed
        # The rating model the valuations use: retrain on tonight's scoreboards.
        try:
            from backend.services import rating_model

            self._set_state(processed=0, total=0, message="Rating model: refreshing the app model")
            info = rating_model.ensure()
            notes.append(f"rating model {'retrained' if info.get('trained') else 'already current'} ({int(info.get('rows') or 0)} maps)")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"rating model refresh failed: {exc}")
        # The model the simulators use: retrain on everything when the data changed.
        try:
            self._set_state(processed=0, total=0, message="Map model: refreshing the app model")
            info = events.ensure_production_map_model()
            notes.append(
                f"app model {'retrained' if info.get('trained') else 'already current'} "
                f"({int(info.get('maps') or 0)} maps, {int(info.get('teams_rated') or 0)} teams rated)"
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"app model refresh failed: {exc}")
        message = "; ".join(notes)
        if failed_any:
            raise RuntimeError(message)
        return message

    # ---- status --------------------------------------------------------------
    def _set_state(self, **kwargs: Any) -> None:
        with self._state_lock:
            self._state.update(kwargs)

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            state = dict(self._state)
        cfg = schedule_db.get_schedule_config()
        now = datetime.now()
        hh, mm = (cfg.get("run_time") or "00:00").split(":")
        scheduled_today = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        next_run = scheduled_today if now < scheduled_today else scheduled_today + timedelta(days=1)
        pending = self._pending_slot(cfg, now) if cfg.get("enabled") else None
        if pending is not None:
            next_run = now  # the missed slot is caught up on the next tick
        mh, mm2 = (cfg.get("morning_time") or "09:00").split(":")
        morning_today = now.replace(hour=int(mh), minute=int(mm2), second=0, microsecond=0)
        next_morning = morning_today if now < morning_today else morning_today + timedelta(days=1)
        if cfg.get("enabled") and self._pending_morning_slot(cfg, now) is not None:
            next_morning = now
        from backend.services import hltv_session

        return {
            "config": cfg,
            "state": state,
            "next_run_at": next_run.timestamp(),
            "next_morning_at": next_morning.timestamp() if cfg.get("do_morning") else None,
            "catch_up_pending": pending is not None,
            "missed_slot_at": pending.timestamp() if pending is not None else None,
            "last_success_by_task": schedule_db.last_success_by_task(),
            # Last persisted login-health snapshot (no page load here).
            "hltv_session": hltv_session.load_snapshot(),
            "backup": _backup_status(),
        }


scheduler = DataScheduler()


def _short(result: Any, limit: int = 400) -> str:
    try:
        text = ", ".join(f"{k}={result[k]}" for k in list(result)[:8]) if isinstance(result, dict) else str(result)
    except Exception:
        text = str(result)
    return text[:limit]
