"""Capacity-limited, persistent scheduling of diagnostic replays."""

import fcntl
import hashlib
import json
import logging
import os
import random
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import taskcluster
from create_tc_task import format_elapsed
from replay_tc_task import (
    ExpiredReplayInputs,
    check_inputs,
    clone_task,
    gen_task_id,
    parse_timestamp,
    source_queue,
)
from rich.console import Group
from rich.table import Table
from rich.text import Text

from worker_health.replay_cache import load_discovery, save_discovery
from worker_health.replay_coverage import remember_results, remember_submission
from worker_health.replay_picker import ReplayJob, discover_jobs, missing_scopes

ACTIVE = ("reserved", "pending", "running", "unscheduled")
TERMINAL = ("completed", "failed", "exception")
logger = logging.getLogger(__name__)


class SchedulingStopped(Exception):
    """Stop before further API work when the fullscreen UI exits."""


def workload_key(source_pool, job):
    return json.dumps([source_pool, job.name, sorted(set(job.task.get("scopes", [])))])


def state_path(destination):
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return (
        root
        / "worker-health"
        / "replay-controller"
        / f"{hashlib.sha256(destination.encode()).hexdigest()[:24]}.sqlite3"
    )


def active_worker_count(manager, pool, now, minutes):
    """Standalone worker capacity is one worker, independent of its capacity field."""
    provisioner, worker_type = pool.split("/", 1)
    active = set()
    options = {}
    while True:
        page = manager.listWorkers(provisioner, worker_type, **options)
        for worker in page["workers"]:
            try:
                last_active = parse_timestamp(worker["lastDateActive"])
                quarantine = parse_timestamp(worker["quarantineUntil"]) if worker.get("quarantineUntil") else None
                if now - timedelta(minutes=minutes) <= last_active <= now and (quarantine is None or quarantine <= now):
                    active.add((worker["workerGroup"], worker["workerId"]))
            except (KeyError, TypeError, ValueError):
                # Missing or malformed activity is not evidence of usable capacity.
                continue
        if not page.get("continuationToken"):
            return len(active)
        options["continuationToken"] = page["continuationToken"]


class ControllerState:
    """One local controller per destination; commit submission intent before the API call."""

    def __init__(self, path, lock_path=None):
        self.path = Path(path)
        self.lock_path = Path(lock_path) if lock_path else Path(str(self.path) + ".lock")
        self.db = None
        self.lock = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.lock = os.fdopen(os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600), "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise ValueError("Another replay controller is using this destination's state") from exc
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(self.path)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS workloads (
                    key TEXT PRIMARY KEY, name TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, workload TEXT NOT NULL, definition TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL NOT NULL);
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(workloads)")}
            if "source_task_id" not in columns:
                self.db.execute("ALTER TABLE workloads ADD COLUMN source_task_id TEXT")
            if "last_seen" not in columns:
                self.db.execute("ALTER TABLE workloads ADD COLUMN last_seen TEXT")
            if "source_job" not in columns:
                self.db.execute("ALTER TABLE workloads ADD COLUMN source_job TEXT")
            if "expired_source_id" not in columns:
                self.db.execute("ALTER TABLE workloads ADD COLUMN expired_source_id TEXT")
            self.db.commit()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        if self.db:
            self.db.close()
        if self.lock:
            self.lock.close()

    def ensure_job(self, key, name, source_task_id=None, last_seen=None):
        with self.db:
            self.db.execute(
                """INSERT INTO workloads (key, name, source_task_id, last_seen) VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    source_task_id=COALESCE(excluded.source_task_id, workloads.source_task_id),
                    last_seen=COALESCE(excluded.last_seen, workloads.last_seen)""",
                (key, name, source_task_id, last_seen),
            )

    def history(self, key):
        return self.db.execute("SELECT * FROM workloads WHERE key=?", (key,)).fetchone()

    def remember_source(self, key, job):
        """Discovery supplies completed sources; retain that evidence and definition."""
        previous = self.history(key)
        saved = json.loads(previous["source_job"]) if previous["source_job"] else None
        if saved and saved["completed"] > job.completed:
            return
        changed = not saved or saved["task_id"] != job.task_id
        with self.db:
            self.db.execute(
                """UPDATE workloads SET source_job=?, source_task_id=?,
                expired_source_id=CASE WHEN ? THEN NULL ELSE expired_source_id END,
                retry_after=CASE WHEN ? THEN 0 ELSE retry_after END WHERE key=?""",
                (json.dumps(asdict(job)), job.task_id, changed, changed and bool(saved), key),
            )

    def active(self):
        return self.db.execute(
            "SELECT * FROM tasks WHERE state IN ('reserved','pending','running','unscheduled') ORDER BY created",
        ).fetchall()

    def rate_ready(self, now, interval):
        row = self.db.execute("SELECT value FROM settings WHERE key='last_submission'").fetchone()
        return row is None or now - row[0] >= interval

    def mark_submission(self, now):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_submission', ?)", (now,))

    def reject(self, key, error, now, backoff, attempted=True):
        failures = self.history(key)["failures"] + 1
        delay = min(3600, backoff * 2 ** min(failures - 1, 10))
        self.db.execute(
            "UPDATE workloads SET attempts=attempts+?, failures=?, last_attempt=?, retry_after=?, error=? WHERE key=?",
            (int(attempted), failures, now, now + delay, str(error), key),
        )

    def reserve(self, key, task_id, definition, now):
        with self.db:
            self.db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, 'reserved', ?)",
                (task_id, key, json.dumps(definition), now),
            )
            self.db.execute("UPDATE workloads SET attempts=attempts+1, last_attempt=? WHERE key=?", (now, key))

    def set_status(self, row, state, now, backoff):
        with self.db:
            self.db.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, row["task_id"]))
            if state == "completed":
                self.db.execute(
                    "UPDATE workloads SET successes=successes+1, failures=0, retry_after=0, error='' WHERE key=?",
                    (row["workload"],),
                )
            elif state in ("failed", "exception", "rejected"):
                self.reject(row["workload"], f"Replay {row['task_id']}: {state}", now, backoff, attempted=False)


class ReplayController:
    def __init__(self, queue, manager, auth, state, args, clock=None, stop_requested=None):
        self.queue, self.manager, self.auth, self.state, self.args = queue, manager, auth, state, args
        self.jobs = []
        self.known_jobs = {}
        self.granted = []
        self.last_discovery = None
        self.workers = self.outstanding = self.limit = 0
        self.message = "Starting"
        self.clock = clock or (lambda: datetime.now(UTC))
        self.histories = {}
        self.active_types = set()
        self.active_tasks = {}
        self.observed_statuses = {}
        self.stop_requested = stop_requested or (lambda: False)
        self.discovery = None
        self.coverage_confirmed = False
        self.full_coverage = False
        self.paused = False
        self.exit_ready = False
        self.initial_task_ids = {row[0] for row in self.state.db.execute("SELECT task_id FROM tasks")}
        session = getattr(args, "session_info", None)
        self.saved_snapshot = session.report() if session else {}
        self.view_ready = False
        self.refresh_view()

    def coverage_state(self):
        passed, eligible, _ = self.display_rows()
        full = bool(self.coverage_confirmed and eligible and passed == eligible)
        return passed, eligible, full

    def update_coverage(self):
        passed, eligible, full = self.coverage_state()
        if full and not self.full_coverage:
            logger.info("★ Full coverage: %s/%s eligible types", passed, eligible)
        elif self.full_coverage and not full:
            logger.info("Coverage changed: %s/%s eligible types; checking uncovered workloads", passed, eligible)
        self.full_coverage = full
        self.exit_ready = full and getattr(self.args, "exit_on_full_coverage", False)
        self.paused = full and getattr(self.args, "stop_on_full_coverage", True)
        return self.paused or self.exit_ready

    def refresh_view(self):
        # Rich refreshes in another thread; render only snapshots, never SQLite queries.
        self.histories = {row["key"]: dict(row) for row in self.state.db.execute("SELECT * FROM workloads")}
        self.active_tasks = {
            row["workload"]: {
                "state": row["state"],
                "created": row["created"],
                "started": self.observed_statuses.get(row["task_id"], {}).get("started"),
            }
            for row in self.state.active()
        }
        self.active_types = set(self.active_tasks)
        current = {workload_key(self.args.from_pool, job): job for job in self.jobs}
        self.known_jobs = {}
        for key, history in self.histories.items():
            source, name, scopes = json.loads(key)
            if source == self.args.from_pool:
                saved = ReplayJob(**json.loads(history["source_job"])) if history.get("source_job") else None
                self.known_jobs[key] = (
                    saved
                    or current.get(key)
                    or ReplayJob(
                        history.get("source_task_id"),
                        {"metadata": {"name": name}, "scopes": scopes},
                        "",
                        None,
                        "-",
                    )
                )

    def progress(self, message):
        if self.stop_requested():
            raise SchedulingStopped
        self.message = message

    def capacity(self, now):
        self.workers = active_worker_count(self.manager, self.args.queue, now, self.args.worker_active_minutes)
        self.limit = self.workers * self.args.tasks_per_worker
        counts = self.queue.taskQueueCounts(self.args.queue)
        remote = counts["pendingTasks"] + counts["claimedTasks"]
        local = self.state.active()
        # A reservation may already exist remotely after a lost createTask response.
        # Double-count it conservatively until status confirms where it stands.
        self.outstanding = max(remote + sum(row["state"] == "reserved" for row in local), len(local))

    def reconcile(self, now):
        for row in self.state.active():
            try:
                status = self.queue.status(row["task_id"])["status"]
            except taskcluster.exceptions.TaskclusterRestFailure as exc:
                if exc.status_code == 404:
                    definition = json.loads(row["definition"])
                    if parse_timestamp(definition["deadline"]) <= now:
                        self.state.set_status(row, "exception", now.timestamp(), self.args.failure_backoff)
                        continue
                    if row["state"] == "reserved":
                        continue
                raise
            state = status["state"]
            if state not in (*ACTIVE, *TERMINAL):
                raise ValueError(f"Unknown task state: {state}")
            self.state.set_status(row, state, now.timestamp(), self.args.failure_backoff)
            latest_run = status.get("runs", [])[-1] if status.get("runs") else {}
            self.observed_statuses[row["task_id"]] = {"started": latest_run.get("started")}
            if state in TERMINAL:
                self.observed_statuses.pop(row["task_id"], None)
                remember_results(self.args.queue, {row["task_id"]: {"state": state}})

    def submit(self, row, now):
        if self.stop_requested() or not getattr(self.args, "submissions_enabled", False):
            self.message = "Submissions paused; submitted tasks keep running"
            return
        definition = json.loads(row["definition"])
        self.state.mark_submission(now.timestamp())
        try:
            self.queue.createTask(row["task_id"], definition)
        except taskcluster.exceptions.TaskclusterRestFailure as exc:
            if exc.status_code in (400, 401, 403):
                self.state.set_status(row, "rejected", now.timestamp(), self.args.failure_backoff)
            # Other failures can leave a created task behind. Retain the exact ID/definition.
            raise
        self.state.set_status(row, "pending", now.timestamp(), self.args.failure_backoff)
        remember_submission(self.args.queue, row["task_id"], definition)
        self.message = f"Submitted {row['task_id']}: {definition['metadata']['name']}"
        logger.info("%s", self.message)

    def tick(self, now=None):
        self.coverage_confirmed = False
        self.exit_ready = self.paused = False
        if self.stop_requested():
            return
        now = now or self.clock()
        self.progress("Checking submitted tasks")
        self.reconcile(now)
        self.progress("Reading current client scopes")
        self.granted = self.auth.currentScopes()["scopes"]
        if (
            self.last_discovery is None
            or (now - self.last_discovery).total_seconds() >= self.args.source_refresh_minutes * 60
        ):
            discovery = load_discovery(self.args.from_pool, self.args.source_refresh_minutes)
            if discovery is None:
                discovery = discover_jobs(self.queue, self.manager, self.args.from_pool, progress=self.progress)
                save_discovery(self.args.from_pool, discovery)
            for job in discovery.jobs:
                key = workload_key(self.args.from_pool, job)
                self.state.ensure_job(key, job.name, last_seen=now.isoformat())
                self.state.remember_source(key, job)
            self.jobs = discovery.jobs
            self.discovery = discovery
            self.last_discovery = now
        self.refresh_view()
        self.view_ready = True
        self.coverage_confirmed = (
            self.discovery is not None and self.discovery.complete and not self.discovery.unavailable_workers
        )
        if self.update_coverage():
            self.message = (
                "Full coverage reached; exiting"
                if self.exit_ready
                else "Paused while covered; monitoring for new workload types"
            )
            self.progress(self.message)
            self.capacity(now)
            return
        self.progress("Reading destination activity and queue counts")
        self.capacity(now)
        if not getattr(self.args, "submissions_enabled", False) and not self.args.dry_run:
            self.message = "Monitoring only; submissions paused (S to enable)"
            return
        if not self.state.rate_ready(now.timestamp(), self.args.submit_interval):
            self.message = "Waiting for submission rate limit"
            return

        # Resolve uncertain submissions first, reusing the persisted ID and definition.
        reserved = [row for row in self.state.active() if row["state"] == "reserved"]
        if reserved:
            row = reserved[0]
            if self.outstanding - len(reserved) >= self.limit:
                self.message = "Waiting for capacity to retry an uncertain submission"
                return
            definition = json.loads(row["definition"])
            if missing_scopes(definition, self.args.queue, self.granted):
                self.message = "Uncertain submission lacks scopes; waiting for permissions"
                return
            source_id = definition["extra"]["replay"]["sourceTaskId"]
            history = self.state.history(row["workload"])
            saved = json.loads(history["source_job"]) if history["source_job"] else None
            status = {"state": "completed"} if saved and saved["task_id"] == source_id else None
            try:
                check_inputs(self.queue, definition, source_id, now, status=status, progress=self.progress)
            except ValueError as exc:
                if not self.args.dry_run:
                    self.state.set_status(row, "rejected", now.timestamp(), self.args.failure_backoff)
                self.message = f"Uncertain submission inputs no longer valid: {exc}"
                return
            self.granted = self.auth.currentScopes()["scopes"]
            if missing_scopes(definition, self.args.queue, self.granted):
                self.message = "Permissions changed during input validation; no task submitted"
                return
            now = self.clock()
            self.capacity(now)
            if self.outstanding - len(reserved) >= self.limit:
                self.message = "Capacity changed during input validation; no task submitted"
                return
            if self.args.dry_run:
                self.message = f"DRY RUN: would retry {row['task_id']}"
            else:
                self.submit(row, now)
            return
        if self.outstanding >= self.limit:
            self.message = "Waiting for destination capacity"
            return

        active_types = {row["workload"] for row in self.state.active()}
        candidates = []
        for job in self.known_jobs.values():
            key = workload_key(self.args.from_pool, job)
            history = self.state.history(key)
            if (
                key not in active_types
                and "payload" in job.task
                and history["expired_source_id"] != job.task_id
                and not (getattr(self.args, "stop_on_full_coverage", True) and history["successes"])
                and history["retry_after"] <= now.timestamp()
                and not missing_scopes(job.task, self.args.queue, self.granted)
            ):
                candidates.append((history["successes"] > 0, history["attempts"], history["last_attempt"], key, job))
        # Stable sorting preserves shuffled order among equally ranked workloads.
        random.shuffle(candidates)
        for _, _, _, key, job in sorted(candidates, key=lambda item: item[:3]):
            self.progress(f"Checking inputs: {job.name}")
            try:
                source = job.task
                if (
                    source_queue(source) != self.args.from_pool
                    or workload_key(self.args.from_pool, replace(job, task=source)) != key
                ):
                    raise ValueError("Source pool or workload identity changed")
                denied = missing_scopes(source, self.args.queue, self.granted)
                if denied:
                    raise ValueError("Client lacks replay scopes: " + ", ".join(denied))
                check_inputs(
                    self.queue,
                    source,
                    job.task_id,
                    now,
                    status={"state": "completed"},
                    progress=self.progress,
                )
                task_id = gen_task_id()
                definition = clone_task(source, job.task_id, task_id, self.args.queue, now)
            except (ValueError, KeyError, taskcluster.exceptions.TaskclusterFailure) as exc:
                if not self.args.dry_run:
                    with self.state.db:
                        self.state.reject(key, exc, now.timestamp(), self.args.failure_backoff)
                        if isinstance(exc, ExpiredReplayInputs) or (
                            isinstance(exc, taskcluster.exceptions.TaskclusterRestFailure) and exc.status_code == 404
                        ):
                            self.state.db.execute(
                                "UPDATE workloads SET expired_source_id=? WHERE key=?",
                                (job.task_id, key),
                            )
                logger.warning("Skipping %s: %s", job.name, exc)
                continue
            # Preflight can take time: refresh scopes and capacity just before submission.
            self.granted = self.auth.currentScopes()["scopes"]
            if missing_scopes(source, self.args.queue, self.granted):
                self.message = "Permissions changed during input validation; no task submitted"
                return
            now = self.clock()
            self.capacity(now)
            if self.outstanding >= self.limit:
                self.message = "Capacity changed during input validation; no task submitted"
                return
            if self.stop_requested() or (
                not getattr(self.args, "submissions_enabled", False) and not self.args.dry_run
            ):
                self.message = "Submissions paused; submitted tasks keep running"
                return
            if self.args.dry_run:
                self.message = f"DRY RUN: would submit {job.name}"
                logger.info("%s", self.message)
                return
            self.state.reserve(key, task_id, definition, now.timestamp())
            row = next(row for row in self.state.active() if row["task_id"] == task_id)
            self.submit(row, now)
            self.outstanding += 1
            return  # At most one submission each cycle, even after long preflight checks.
        self.refresh_view()
        self.message = (
            "Inputs expired; waiting for newer job"
            if any(row.get("expired_source_id") for row in self.histories.values())
            else "No eligible workload ready (permissions, in-flight work, or backoff)"
        )

    def display_rows(self):
        if not self.view_ready and self.saved_snapshot.get("rows"):
            return (
                self.saved_snapshot.get("passed", 0),
                self.saved_snapshot.get("eligible", 0),
                [
                    (
                        0,
                        row["key"],
                        Text(row["name"]),
                        Text(row["coverage"]),
                        str(row["successes"]),
                        str(row["attempts"]),
                        Text(row["status"]),
                        Text(row["elapsed"]),
                    )
                    for row in self.saved_snapshot["rows"]
                ],
            )
        passed = eligible = 0
        now = self.clock().timestamp()
        rows = []
        current_keys = {workload_key(self.args.from_pool, job) for job in self.jobs}
        for key, job in self.known_jobs.items():
            history = self.histories.get(key, {"successes": 0, "attempts": 0, "retry_after": 0})
            denied = missing_scopes(job.task, self.args.queue, self.granted)
            eligible += not denied
            passed += bool(history["successes"]) and not denied
            task = self.active_tasks.get(key)
            task_state = task["state"] if task else None
            elapsed = "-"
            if task:
                try:
                    started = (
                        parse_timestamp(task["started"]).timestamp() if task_state == "running" else task["created"]
                    )
                    elapsed = format_elapsed(max(0, now - started))
                except (TypeError, ValueError, AttributeError):
                    elapsed = "unknown"
            status = (
                "missing scopes"
                if denied
                else "Inputs expired; waiting for newer job"
                if history.get("expired_source_id") == job.task_id
                else {"running": "running", "pending": "queued", "unscheduled": "waiting", "reserved": "uncertain"}.get(
                    task_state,
                    "in flight",
                )
                if key in self.active_types
                else "saved source available"
                if key not in current_keys and "payload" in job.task
                else "history only"
                if key not in current_keys
                else f"retry in {max(0, int(history['retry_after'] - now))}s"
                if history["retry_after"] > now
                else "ready"
            )
            covered = bool(history["successes"])
            failed = bool(history.get("failures", 0))
            active = key in self.active_types
            coverage = (
                Text("passed", style="bold green")
                if covered
                else Text("unsuccessful", style="bold red")
                if failed
                else Text("awaiting result", style="cyan")
                if active
                else Text("untested", style="dim")
            )
            status_style = (
                "red" if denied else "bold cyan" if active else "yellow" if history["retry_after"] > now else "dim"
            )
            priority = 4 if denied else 0 if active else 1 if failed else 2 if covered else 3
            rows.append(
                (
                    priority,
                    key,
                    Text(job.name),
                    coverage,
                    str(history["successes"]),
                    str(history["attempts"]),
                    Text(status, style=status_style),
                    Text(elapsed, style="cyan" if active else "dim"),
                ),
            )
        rows.sort(key=lambda row: row[0])
        return passed, eligible, rows

    def snapshot(self):
        if not self.view_ready and self.saved_snapshot.get("rows"):
            return {
                **self.saved_snapshot,
                "message": f"{self.message} — showing saved results until refresh completes",
                "refreshing": True,
                "coverage_confirmed": False,
                "full_coverage": False,
                "paused": not getattr(self.args, "submissions_enabled", False),
                "submissions_enabled": getattr(self.args, "submissions_enabled", False),
                "stop_on_full_coverage": getattr(self.args, "stop_on_full_coverage", True),
                "exit_on_full_coverage": getattr(self.args, "exit_on_full_coverage", False),
                "report_path": getattr(self.args, "report", None),
            }
        passed, eligible, rows = self.display_rows()
        return {
            "workers": self.workers,
            "outstanding": self.outstanding,
            "limit": self.limit,
            "passed": passed,
            "eligible": eligible,
            "excluded": len(self.known_jobs) - eligible,
            "full_coverage": self.coverage_state()[2],
            "coverage_confirmed": self.coverage_confirmed,
            "paused": self.paused,
            "submissions_enabled": getattr(self.args, "submissions_enabled", False),
            "stop_on_full_coverage": getattr(self.args, "stop_on_full_coverage", True),
            "exit_on_full_coverage": getattr(self.args, "exit_on_full_coverage", False),
            "report_path": getattr(self.args, "report", None),
            "message": self.message,
            "rows": [
                {
                    "key": key,
                    "name": name.plain,
                    "coverage": coverage.plain,
                    "successes": successes,
                    "attempts": attempts,
                    "status": status.plain,
                    "elapsed": elapsed.plain,
                    "observed_at": self.clock().timestamp(),
                    "source_present": key in {workload_key(self.args.from_pool, job) for job in self.jobs},
                    "last_seen": self.histories.get(key, {}).get("last_seen"),
                }
                for _, key, name, coverage, successes, attempts, status, elapsed in rows
            ],
        }

    def report(self):
        if not self.view_ready and self.saved_snapshot.get("rows"):
            return self.snapshot()
        data = self.snapshot()
        data["coverage_definition"] = (
            "At least one observed successful replay per eligible name/scope variant observed during this session; "
            "includes session successes across restarts, not worker-image or per-worker coverage. "
            "Discovery uses bounded recent source history."
        )
        data["discovery"] = {
            "checked_at": self.last_discovery.isoformat() if self.last_discovery else None,
            "cached_at": self.discovery.cached_at if self.discovery else None,
            "unavailable_workers": self.discovery.unavailable_workers if self.discovery else None,
            "complete": self.discovery.complete if self.discovery else False,
            "workers": self.discovery.workers if self.discovery else 0,
            "observed_task_ids": self.discovery.task_ids if self.discovery else 0,
            "confirmed": self.coverage_confirmed,
        }
        tasks = {}
        for record in self.state.db.execute("SELECT task_id, workload, state, created FROM tasks ORDER BY created"):
            task = dict(record)
            task["url"] = f"https://firefox-ci-tc.services.mozilla.com/tasks/{task['task_id']}"
            task["this_invocation"] = task["task_id"] not in getattr(self, "initial_task_ids", set())
            tasks.setdefault(task["workload"], []).append(task)
        jobs = self.known_jobs
        for row in data["rows"]:
            history = self.histories.get(row["key"], {})
            denied = missing_scopes(jobs[row["key"]].task, self.args.queue, self.granted)
            row.update(
                source_task_id=jobs[row["key"]].task_id,
                source_url=(
                    f"https://firefox-ci-tc.services.mozilla.com/tasks/{jobs[row['key']].task_id}"
                    if jobs[row["key"]].task_id
                    else None
                ),
                eligible=not denied,
                missing_scopes=denied,
                attempts=int(row["attempts"]),
                successes=int(row["successes"]),
                consecutive_failures=history.get("failures", 0),
                last_error=history.get("error", ""),
                tasks=tasks.get(row["key"], []),
            )
            row["failures"] = sum(t["state"] in ("failed", "exception", "rejected") for t in row["tasks"])
            row["latest_outcome"] = row["tasks"][-1]["state"] if row["tasks"] else None
        data["outstanding_tasks"] = [t for group in tasks.values() for t in group if t["state"] in ACTIVE]
        return data

    def render(self, max_rows=None):
        table = Table(title=f"Continuous replay: {self.args.from_pool} → {self.args.queue}")
        table.add_column("Workload", no_wrap=True, overflow="ellipsis", ratio=1)
        table.add_column("Coverage", no_wrap=True)
        table.add_column("Succeeded", justify="right", style="green")
        table.add_column("Attempts", justify="right")
        table.add_column("Status", no_wrap=True)
        table.add_column("Elapsed", no_wrap=True, justify="right")
        passed, eligible, rows = self.display_rows()
        visible = rows if max_rows is None else rows[:max_rows]
        for _, _key, *cells in visible:
            table.add_row(*cells)
        if len(visible) < len(rows):
            table.caption = f"Showing {len(visible)}/{len(rows)} types; active, unsuccessful, and covered types first"
        full = self.coverage_state()[2]
        return Group(
            Text(
                f"Active workers: {self.workers} | Outstanding: {self.outstanding}/{self.limit} | "
                f"{'★ Full coverage' if full else 'Coverage'}: {passed}/{eligible} eligible types | "
                f"{len(self.known_jobs) - eligible} excluded",
                style="bold green" if full else "",
            ),
            Text(
                f"Pause when covered: {'on' if getattr(self.args, 'stop_on_full_coverage', True) else 'off'} | "
                f"Exit when covered: {'on' if getattr(self.args, 'exit_on_full_coverage', False) else 'off'} | "
                f"Report: {getattr(self.args, 'report', '-')}",
                style="dim",
            ),
            table,
            Text(self.message, style="cyan"),
            Text("Ctrl+C: stop scheduling; submitted tasks keep running", style="dim"),
        )
