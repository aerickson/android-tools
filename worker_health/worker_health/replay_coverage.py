"""Persist observed target-pool task outcomes for workload coverage badges."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import time

import taskcluster

from worker_health.replay_picker import batch_results, level_one_problem, pool_task_ids


def workload_name(task):
    name = task["metadata"]["name"]
    if task.get("extra", {}).get("replay"):
        while name.startswith("Replay: "):
            name = name[len("Replay: ") :]
    return name


def coverage_path():
    directory = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "worker-health"
    return directory / "replay-coverage.sqlite3"


def database():
    path = coverage_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, pool TEXT NOT NULL, name TEXT NOT NULL,
            state TEXT NOT NULL, passed INTEGER NOT NULL, worker TEXT NOT NULL,
            observed TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS tasks_pool ON tasks(pool);
        CREATE TABLE IF NOT EXISTS scans (pool TEXT PRIMARY KEY, scanned_at REAL NOT NULL, complete INTEGER NOT NULL);
    """)
    return db


def store_task(db, pool, task_id, name, status):
    runs = status.get("runs", [])
    latest = runs[-1] if runs else {}
    passed = status["state"] == "completed" or any(run.get("state") == "completed" for run in runs)
    db.execute(
        """
        INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            state=excluded.state, passed=MAX(tasks.passed, excluded.passed),
            worker=excluded.worker, observed=excluded.observed
    """,
        (
            task_id,
            pool,
            name,
            status["state"],
            int(passed),
            latest.get("workerId", "-"),
            latest.get("resolved") or latest.get("started") or datetime.now(timezone.utc).isoformat(),
        ),
    )


@dataclass
class TargetCoverage:
    entries: dict
    complete: bool = True
    cached: bool = False

    def records(self, task):
        return self.entries.get(workload_name(task), [])

    def passed(self, task):
        return sum(bool(record["passed"]) for record in self.records(task))

    def search_terms(self, task):
        states = " ".join(record["state"] for record in self.records(task))
        return f"{'passed ' if self.passed(task) else ''}{self.badge(task)} {states}"

    def badge(self, task):
        records = self.records(task)
        passed = self.passed(task)
        if passed:
            return f"★ {passed}"
        states = {record["state"] for record in records}
        for state in ("running", "pending", "failed", "exception", "unscheduled"):
            if state in states:
                return "queued" if state in {"pending", "unscheduled"} else state
        return "unseen" if self.complete else "unknown"

    def details(self, task):
        records = self.records(task)
        if not records:
            return [
                "Target: no matching jobs in observed history"
                if self.complete
                else "Target: history unavailable or partial; coverage unknown",
            ]
        counts = {
            state: sum(record["state"] == state for record in records)
            for state in {record["state"] for record in records}
        }
        latest = max(records, key=lambda record: record["observed"])
        return [
            f"Target: {self.passed(task)} succeeded; "
            + ", ".join(f"{count} {state}" for state, count in sorted(counts.items())),
            f"Last target: {latest['task_id']} ({latest['state']}) on {latest['worker']}",
        ]

    def summary(self, jobs):
        names = {workload_name(job.task) for job in jobs}
        passed = sum(any(record["passed"] for record in self.entries.get(name, [])) for name in names)
        qualifiers = ["local + recent history"]
        if self.cached:
            qualifiers.append("cached")
        if not self.complete:
            qualifiers.append("partial")
        return f"Target: {passed}/{len(names)} job types succeeded ({', '.join(qualifiers)})"


def snapshot(db, pool, complete, cached=False):
    entries = {}
    for row in db.execute("SELECT * FROM tasks WHERE pool=?", (pool,)):
        entries.setdefault(row["name"], []).append(dict(row))
    return TargetCoverage(entries, complete, cached)


def target_coverage(queue, manager, pool, minutes=10, refresh=False, progress=lambda message: None):
    try:
        with closing(database()) as db:
            scan = db.execute("SELECT * FROM scans WHERE pool=?", (pool,)).fetchone()
            if not refresh and scan and 0 <= time.time() - scan["scanned_at"] < minutes * 60:
                return snapshot(db, pool, bool(scan["complete"]), cached=True)
            try:
                ids, workers, unavailable = pool_task_ids(manager, pool, progress)
                ids.update(
                    row[0]
                    for row in db.execute(
                        "SELECT task_id FROM tasks WHERE pool=? AND state IN ('pending', 'running', 'unscheduled')",
                        (pool,),
                    )
                )
                statuses = {
                    entry["taskId"]: entry["status"] for entry in batch_results(queue.statuses, sorted(ids), "statuses")
                }
                progress(f"Reading {len(statuses)} target task definitions")
                definitions = {
                    entry["taskId"]: entry["task"] for entry in batch_results(queue.tasks, sorted(statuses), "tasks")
                }
                with db:
                    for task_id, task in definitions.items():
                        task_pool = task.get("taskQueueId") or f"{task.get('provisionerId')}/{task.get('workerType')}"
                        if task_pool == pool and not level_one_problem(task):
                            store_task(db, pool, task_id, workload_name(task), statuses[task_id])
                    complete = not unavailable and len(statuses) == len(ids) and len(definitions) == len(statuses)
                    db.execute("INSERT OR REPLACE INTO scans VALUES (?, ?, ?)", (pool, time.time(), int(complete)))
                return snapshot(db, pool, complete)
            except taskcluster.exceptions.TaskclusterFailure as exc:
                logging.warning("Target coverage unavailable: %s", exc)
                return snapshot(db, pool, complete=False)
    except (OSError, sqlite3.Error) as exc:
        logging.warning("Target coverage unavailable: %s", exc)
        return TargetCoverage({}, complete=False)


def remember_submission(pool, task_id, source):
    try:
        with closing(database()) as db, db:
            store_task(db, pool, task_id, workload_name(source), {"state": "pending"})
            db.execute("DELETE FROM scans WHERE pool=?", (pool,))
    except (OSError, sqlite3.Error) as exc:
        logging.warning("Could not record target replay: %s", exc)


def remember_results(pool, results):
    try:
        with closing(database()) as db, db:
            for task_id, result in results.items():
                row = db.execute("SELECT name FROM tasks WHERE pool=? AND task_id=?", (pool, task_id)).fetchone()
                if row:
                    store_task(
                        db,
                        pool,
                        task_id,
                        row["name"],
                        {"state": result["state"], "runs": [{"workerId": result.get("worker_id", "-")}]},
                    )
            db.execute("DELETE FROM scans WHERE pool=?", (pool,))
    except (OSError, sqlite3.Error) as exc:
        logging.warning("Could not record target replay results: %s", exc)
