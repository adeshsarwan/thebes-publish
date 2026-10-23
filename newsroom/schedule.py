"""Daily pulls at noon New York, London and Sydney time.

macOS cron and launchd only know the Mac's own time zone, so instead of fixed
clock times a launchd agent runs `python -m newsroom tick` every 10 minutes.
Each tick checks config/schedule.yaml: a region is due when its local time is
past its scheduled time (in that region's own zone, so daylight saving is
handled) and it hasn't run yet that local day. The run is claimed in
pipeline.scheduled_runs first, so a region never runs twice in a day.

    python -m newsroom schedule install     # start the launchd agent
    python -m newsroom schedule status
    python -m newsroom schedule uninstall
    python -m newsroom tick [--dry-run]     # what the agent runs
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from pipeline.db import Database, connect, utcnow
from pipeline.settings import CONFIG_DIR, ROOT

from .pull import LOG_DIR, PullBusy, run_pull, summarise

LABEL = "com.financehub.newsroom.schedule"
INTERVAL_S = 600


def load_config() -> dict:
    with open(Path(os.getenv("FINANCE_CONFIG_DIR", CONFIG_DIR)) / "schedule.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _at(local_day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return local_day.replace(hour=h, minute=m, second=0, microsecond=0)


def plan(now: datetime | None = None, cfg: dict | None = None) -> list[dict]:
    """For each region: its local time now, today's slot, whether it's in the run window, and the next slot."""
    now = now or datetime.now(timezone.utc)
    cfg = cfg or load_config()
    window = timedelta(hours=float(cfg.get("catch_up_hours", 6)))
    out = []
    for r in cfg.get("regions", []):
        tz = ZoneInfo(r["timezone"])
        local = now.astimezone(tz)
        slot = _at(local, str(r.get("time", "12:00")))
        next_slot = slot if local < slot else _at(local + timedelta(days=1), str(r.get("time", "12:00")))
        out.append({
            "id": r["id"], "timezone": r["timezone"], "countries": [c.upper() for c in r.get("countries", [])],
            "local_now": local, "local_date": local.date().isoformat(), "slot": slot,
            "in_window": slot <= local < slot + window, "next_slot": next_slot,
            "next_slot_utc": next_slot.astimezone(timezone.utc),
        })
    return out


def _claim(db: Database, region: dict, trigger: str = "schedule") -> int | None:
    rid = db.insert(
        """insert into pipeline.scheduled_runs (region, local_date, countries, trigger, started_at)
           values (?,?,?,?,?) on conflict (region, local_date) do nothing returning id""",
        (region["id"], region["local_date"], db.j(region["countries"]), trigger, utcnow()))
    db.commit()
    return rid


def tick(now: datetime | None = None, *, runner=run_pull, dry_run: bool = False) -> list[dict]:
    """Run every region that is due. Safe to call as often as you like."""
    results = []
    db = connect("pipeline")
    try:
        for region in plan(now):
            if not region["in_window"]:
                continue
            done = db.fetchone("select id from pipeline.scheduled_runs where region = ? and local_date = ?",
                               (region["id"], region["local_date"]))
            if done:
                continue
            if dry_run:
                results.append({"region": region["id"], "would_run": region["countries"]})
                continue
            run_id = _claim(db, region)
            if run_id is None:                          # another tick claimed it a moment ago
                continue
            try:
                result = runner(region["countries"], trigger=f"schedule:{region['id']}")
                db.execute("update pipeline.scheduled_runs set finished_at = ?, stats = ? where id = ?",
                           (utcnow(), db.j(summarise(result)), run_id))
                results.append({"region": region["id"], "ok": True, "summary": summarise(result)})
            except PullBusy:
                # a manual pull is running: release the claim so the next tick tries again
                db.execute("delete from pipeline.scheduled_runs where id = ?", (run_id,))
                results.append({"region": region["id"], "ok": False, "error": "busy, will retry"})
            except Exception as exc:
                db.execute("update pipeline.scheduled_runs set finished_at = ?, error = ? where id = ?",
                           (utcnow(), f"{type(exc).__name__}: {exc}"[:500], run_id))
                results.append({"region": region["id"], "ok": False, "error": str(exc)})
            db.commit()
    finally:
        db.close()
    return results


def recent_runs(db: Database, limit: int = 12) -> list[dict]:
    rows = db.fetchall("select * from pipeline.scheduled_runs order by id desc limit ?", (limit,))
    for r in rows:
        r["countries"] = Database.unj(r.get("countries"), [])
        r["stats"] = Database.unj(r.get("stats"), {})
    return rows


# --------------------------------------------------------------------------- launchd (macOS)
def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_bytes(python: str | None = None) -> bytes:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path_env = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, "-m", "newsroom", "tick"],
        "WorkingDirectory": str(ROOT),
        "StartInterval": INTERVAL_S,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "scheduler.log"),
        "StandardErrorPath": str(LOG_DIR / "scheduler.log"),
        "EnvironmentVariables": {"PATH": path_env, "PYTHONUNBUFFERED": "1"},
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install() -> str:
    if sys.platform != "darwin":
        raise SystemExit("The launchd schedule is for macOS. On a Linux VPS, run `python -m newsroom tick` "
                         "every 10 minutes from cron or a systemd timer.")
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(p))            # replace any older version
    p.write_bytes(plist_bytes())
    res = _launchctl("bootstrap", domain, str(p))
    if res.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {res.stderr.strip() or res.stdout.strip()}")
    return str(p)


def uninstall() -> None:
    p = plist_path()
    if sys.platform == "darwin":
        _launchctl("bootout", f"gui/{os.getuid()}", str(p))
    if p.exists():
        p.unlink()


CRON_FILE = Path("/etc/cron.d/finance-hub")   # written by deploy/setup_vps.sh on a Linux server


def installed() -> bool:
    if sys.platform != "darwin":
        return CRON_FILE.exists()
    return plist_path().exists()


def scheduler_name() -> str:
    return "launchd agent on this Mac" if sys.platform == "darwin" else f"cron job ({CRON_FILE})"
