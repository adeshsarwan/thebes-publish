"""Run the whole pull: fetch feeds -> cluster -> score -> AI drafts -> auto-publish.

Used by the "Pull now" button, the daily schedule and the CLI:

    python -m newsroom pull                    # all countries
    python -m newsroom pull --countries US,CA

Each step uses its own database login: the pipeline steps connect as the
pipeline role, and only auto-publishing connects as the newsroom role. A file
lock makes sure two pulls (button + schedule) never run at the same time.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pipeline import cluster as cluster_stage
from pipeline import desk, feeds, writer
from pipeline.db import connect, utcnow
from pipeline.llm import get_client
from pipeline.settings import ROOT

from . import workflow

DATA = Path(os.getenv("FINANCE_DATA_DIR") or ROOT / "data")
LOCK_PATH = DATA / "pull.lock"
STATUS_PATH = DATA / "pull-status.json"
LOG_DIR = DATA / "logs"
COUNTRIES = ("US", "UK", "CA", "AU")


class PullBusy(Exception):
    pass


@contextmanager
def _lock():
    DATA.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PullBusy("A pull is already running.")
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def is_running() -> bool:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def _write_status(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(STATUS_PATH)


def status() -> dict:
    try:
        data = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        data = {}
    data["running"] = is_running()
    if data.get("state") == "running" and not data["running"]:
        data["state"] = "interrupted"            # the process died without finishing
    return data


def normalise_countries(countries) -> list[str] | None:
    if not countries:
        return None
    if isinstance(countries, str):
        countries = [c for c in countries.replace(" ", "").split(",") if c]
    out = [c.upper() for c in countries]
    bad = [c for c in out if c not in COUNTRIES]
    if bad:
        raise ValueError(f"unknown country code(s): {', '.join(bad)}")
    return out


def run_pull(countries=None, *, trigger: str = "manual", client=None, fetch_text: bool = True,
             started_by: str | None = None) -> dict:
    """Run every stage for the given countries (None = all). Raises PullBusy if one is running."""
    countries = normalise_countries(countries)
    with _lock():
        info = {"state": "running", "trigger": trigger, "countries": countries or list(COUNTRIES),
                "started_by": started_by, "started_at": utcnow(), "pid": os.getpid(), "stage": "starting"}
        _write_status(info)
        result: dict = {}
        try:
            client = client or get_client()
            pdb = connect("pipeline")
            try:
                for stage, fn in (
                    ("ingest", lambda: feeds.ingest(pdb, countries=countries)),
                    ("cluster", lambda: cluster_stage.cluster(pdb, countries)),
                    ("evaluate", lambda: desk.evaluate(pdb, client, countries=countries)),
                    ("write", lambda: writer.write(pdb, client, fetch_text=fetch_text, countries=countries)),
                ):
                    info["stage"] = stage
                    _write_status(info)
                    result[stage] = fn()
            finally:
                pdb.close()
            info["stage"] = "autopublish"
            _write_status(info)
            ndb = connect("newsroom")
            try:
                result["autopublish"] = workflow.autopublish(ndb, countries)
            finally:
                ndb.close()
            info.update(state="finished", finished_at=utcnow(), summary=summarise(result))
        except Exception as exc:  # recorded for the desk; re-raised for the CLI/scheduler
            info.update(state="failed", finished_at=utcnow(), error=f"{type(exc).__name__}: {exc}",
                        trace=traceback.format_exc()[-2000:], summary=summarise(result))
            _write_status(info)
            raise
        _write_status(info)
        return result


def summarise(result: dict) -> dict:
    ing = result.get("ingest") or {}
    cl = result.get("cluster") or {}
    ev = result.get("evaluate") or {}
    wr = result.get("write") or {}
    ap = result.get("autopublish") or {}
    return {
        "new_items": ing.get("new", 0), "feed_errors": len(ing.get("errors") or []),
        "prefiltered": ing.get("filtered", 0), "too_old": cl.get("too_old", 0),
        "new_stories": cl.get("new_clusters", 0), "joined_stories": cl.get("joined", 0),
        "scored": ev.get("evaluated", 0), "run": ev.get("run", 0),
        "drafted": wr.get("drafted", 0), "auto_published": ap.get("published", 0),
        "autopublish_note": ap.get("error") or ("disabled" if ap.get("disabled") else None),
    }


def start_background(countries=None, started_by: str | None = None) -> None:
    """Launch a pull in a separate process (used by the desk's Pull now button)."""
    countries = normalise_countries(countries)
    if is_running():
        raise PullBusy("A pull is already running.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"pull-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log", "ab")
    args = [sys.executable, "-m", "newsroom", "pull", "--trigger", "button"]
    if countries:
        args += ["--countries", ",".join(countries)]
    if started_by:
        args += ["--started-by", started_by]
    subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    log.close()
