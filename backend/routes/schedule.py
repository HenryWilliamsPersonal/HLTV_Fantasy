"""REST surface for the nightly data-ingestion scheduler."""

from fastapi import APIRouter

from backend.data import schedule_db
from backend.services.scheduler import scheduler

router = APIRouter()


@router.get("/status")
def get_schedule_status() -> dict:
    return scheduler.status()


@router.get("/runs")
def get_schedule_runs(limit: int = 50) -> dict:
    return {"runs": schedule_db.list_runs(limit)}


@router.post("/config")
def update_config(payload: dict | None = None) -> dict:
    schedule_db.update_schedule_config(payload or {})
    return scheduler.status()


@router.post("/heartbeat-test")
def heartbeat_test() -> dict:
    """Send one success ping to the configured heartbeat URL (see
    docs/PUBLIC_RELEASE.md, Uptime alerts) and report what happened."""
    from backend.services.scheduler import heartbeat, heartbeat_url

    url = heartbeat_url()
    if not url:
        return {"configured": False, "sent": False, "detail": "No heartbeat URL: set HLTV_HEARTBEAT_URL or .runtime/heartbeat.json"}
    return {"configured": True, "url": url, "sent": heartbeat("success")}


@router.post("/run-now")
def run_now(payload: dict | None = None) -> dict:
    task = str((payload or {}).get("task") or "all").strip().lower()
    return scheduler.run_now(task)
