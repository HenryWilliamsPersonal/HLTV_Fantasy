# db_prune_dead_best_team_tables.py — reclaim the space the deleted best-team
# router left in fantasy_players.db.
#
# Found 2026-10-27: the database file was 70.7 GB, of which
#   best_team_combos   14.2 M rows, 65.9 GB (+ seven indexes) — every roster
#                      of an exhaustive 80-player run from 2026-09-30, written
#                      by backend/routes/best_team.py, deleted in the v0.1.81
#                      dead-code sweep; nothing reads it any more
#   best_team_runs     the three runs that owned those rows
#   bounty_best_team_state key 1   a 791 MB single-row-era bounty roster set
#                      from 2026-07-30 that cannot be migrated (over SQLite's
#                      1 GB string limit once migrated) and would be re-keyed
#                      onto the next bounty event opened
# Everything else together is about 2 GB.
#
# Run with the backend STOPPED (VACUUM rewrites the file and needs the
# database to itself), from an elevated PowerShell:
#   Stop-ScheduledTask -TaskName HLTVFantasyBackend
#   python scripts\migrations\db_prune_dead_best_team_tables.py        (--dry-run to only report)
#   Start-ScheduledTask -TaskName HLTVFantasyBackend
# The script refuses to change anything while the backend answers on its
# memorized port (override with --force). VACUUM needs free disk for a copy
# of the remaining data (a few GB) and takes a few minutes.
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "fantasy_players.db"
PORT_FILE = ROOT / ".runtime" / "backend-port.json"
DEAD_TABLES = ("best_team_combos", "best_team_runs")
LEGACY_BOUNTY_KEY = 1


def backend_alive() -> bool:
    try:
        info = json.loads(PORT_FILE.read_text(encoding="utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - no port file, nothing listening
        return False


def mb(n: float) -> str:
    return f"{n / 1e6:,.0f} MB"


def main() -> int:
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}")
        return 1
    print(f"{DB_PATH}: {mb(DB_PATH.stat().st_size)}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for t in DEAD_TABLES:
            if t in tables:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  {t}: {n:,} rows (to drop)")
            else:
                print(f"  {t}: already gone")
        legacy = None
        if "bounty_best_team_state" in tables:
            legacy = conn.execute(
                "SELECT length(result_json) AS size, payload_json FROM bounty_best_team_state WHERE singleton_id = ?", (LEGACY_BOUNTY_KEY,)
            ).fetchone()
            if legacy:
                print(f"  bounty_best_team_state key {LEGACY_BOUNTY_KEY}: {mb(legacy['size'] or 0)} legacy row (to delete)")
            else:
                print(f"  bounty_best_team_state key {LEGACY_BOUNTY_KEY}: already gone")
        if dry:
            print("Dry run: nothing changed.")
            return 0
        if backend_alive() and not force:
            print("The backend is running: stop it first (Stop-ScheduledTask -TaskName HLTVFantasyBackend), or pass --force.")
            return 2
        for t in DEAD_TABLES:
            if t in tables:
                conn.execute(f"DROP TABLE {t}")
                print(f"dropped {t}")
        if legacy:
            conn.execute("DELETE FROM bounty_best_team_state WHERE singleton_id = ?", (LEGACY_BOUNTY_KEY,))
            print(f"deleted bounty_best_team_state key {LEGACY_BOUNTY_KEY}")
        conn.commit()
        print("VACUUM (rewrites the file; a few minutes)…", flush=True)
        t0 = time.time()
        conn.execute("VACUUM")
        print(f"VACUUM done in {time.time() - t0:.0f}s")
    finally:
        conn.close()
    print(f"{DB_PATH}: {mb(DB_PATH.stat().st_size)} now")
    return 0


if __name__ == "__main__":
    sys.exit(main())
