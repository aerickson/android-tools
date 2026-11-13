"""Fixed-input, bounded replay batches using the pool controller's safeguards."""

import json
import logging
import re

import requests
from replay_tc_task import ROOT_URL, source_queue

from rich.console import Group
from rich.table import Table
from rich.text import Text

from worker_health.replay_controller import TERMINAL, ReplayController, workload_key
from worker_health.replay_picker import Discovery, ReplayJob, level_one_problem
from worker_health.replay_sessions import write_json

logger = logging.getLogger(__name__)
LOG_ARTIFACT = "public/logs/live_backing.log"
LOG_LIMIT = 4 * 1024 * 1024


def configure_targets(args, session, existing):
    """Keep selection and limits immutable across restarts and offline views."""
    config = session.configuration()
    targets = config.get("targets")
    if targets:
        if args.successes_per_type is not None or args.exit_on_full_coverage or not args.stop_on_full_coverage:
            raise ValueError("Targeted sessions do not accept coverage controls")
        args.task_ids = targets["task_ids"]
        args.attempts_per_target = targets["attempts_per_target"]
        args.stop_after_success = targets["stop_after_success"]
    elif args.task_ids:
        if existing:
            raise ValueError("Fixed task selection requires a new session")
        targets = {
            "task_ids": args.task_ids,
            "attempts_per_target": args.attempts_per_target,
            "stop_after_success": bool(args.stop_after_success),
        }
        write_json(session.path / "session.json", {**config, "targets": targets})
    if targets:
        # Coverage hotkeys cannot override the fixed batch policy.
        args.stop_on_full_coverage = False
        args.exit_on_full_coverage = False


def execution_evidence(task_id, status):
    return {
        "status_source": f"{ROOT_URL}/api/queue/v1/task/{task_id}/status",
        "runs": [
            {
                "run_id": run.get("runId", "unknown"),
                "worker_id": run.get("workerId", "unknown"),
                "worker_group": run.get("workerGroup", "unknown"),
                "state": run.get("state", "unknown"),
                "reason_resolved": run.get("reasonResolved", "unknown"),
                "started": run.get("started"),
                "resolved": run.get("resolved"),
                "expected_worker_version": "unknown",
                "verified_worker_version": "unknown",
                "worker_version_source": "unknown",
                "failure_signature": "unknown",
                "failure_signature_source": "unknown",
            }
            for run in status.get("runs", [])
        ],
    }


def collect_log_evidence(queue, task_id, run):
    """Read a bounded public execution log; absence never implies a clean run."""
    run_id = run["run_id"]
    if run_id == "unknown":
        return
    url = queue.buildUrl("getArtifact", task_id, str(run_id), LOG_ARTIFACT)
    try:
        data = bytearray()
        with requests.get(url, stream=True, timeout=10) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=65536):
                data.extend(chunk[: LOG_LIMIT - len(data)])
                if len(data) >= LOG_LIMIT:
                    break
        run["log_truncated"] = len(data) >= LOG_LIMIT
        run["log_source"] = url
        lines = data.decode("utf-8", errors="replace").splitlines()
        # Accept only an explicit image-version assertion from the execution log.
        # A destination name (e.g. -v115) or generic-worker binary version is not evidence.
        versions = [
            match.group(1)
            for line in lines
            if (match := re.fullmatch(r"\[taskcluster\] Worker Image Version: ([A-Za-z0-9._+\-]+)", line.strip()))
        ]
        if versions and len(set(versions)) == 1:
            run["verified_worker_version"] = versions[0]
            run["worker_version_source"] = {
                "artifact": url,
                "line": f"[taskcluster] Worker Image Version: {versions[0]}",
            }
        if run["state"] in ("failed", "exception"):
            errors = [
                line[:500]
                for line in lines
                if re.search(r"TEST-UNEXPECTED|\b(?:Error|Exception):|error loading image", line, re.I)
            ]
            if errors:
                run["failure_signature"] = errors[:10]
                run["failure_signature_source"] = url
        run["log_evidence_status"] = "sampled"
    except requests.RequestException:
        # Request exceptions can contain signed redirect URLs; do not persist them.
        run["log_evidence_status"] = "unavailable"


class TargetedReplayController(ReplayController):
    def job_key(self, job):
        return json.dumps([*json.loads(workload_key(self.args.from_pool, job)), job.task_id])

    def refresh_sources(self, now):
        if self.last_discovery is not None:
            return
        jobs = []
        # Resolve all sources before persisting any: an invalid selection fails closed.
        for task_id in self.args.task_ids:
            saved = next(
                (
                    json.loads(row["source_job"])
                    for row in self.state.db.execute(
                        "SELECT source_job FROM workloads WHERE source_task_id=?",
                        (task_id,),
                    )
                    if row["source_job"]
                ),
                None,
            )
            if saved:
                job = ReplayJob(**saved)
            else:
                task = self.queue.task(task_id)
                status = self.queue.status(task_id)["status"]
                if source_queue(task) != self.args.from_pool:
                    raise ValueError(f"Source {task_id} does not belong to {self.args.from_pool}")
                problem = level_one_problem(task)
                if problem or status["state"] != "completed":
                    raise ValueError(f"Source {task_id} is not a completed level-1 task: {problem or status['state']}")
                job = ReplayJob(task_id, task, now.isoformat(), None, "unknown")
            jobs.append(job)
        for job in jobs:
            key = self.job_key(job)
            self.state.ensure_job(key, job.name, last_seen=now.isoformat())
            self.state.remember_source(key, job)
        self.jobs = jobs
        self.discovery = Discovery(jobs, 0, len(jobs), len(jobs), 0)
        self.last_discovery = now
        logger.info("Targeted replay preview: %s → %s", self.args.from_pool, self.args.queue)
        for job in jobs:
            logger.info("  %s: %s", job.task_id, job.name)
        logger.info(
            "Maximum submissions: %s (%s total per target); stop after success: %s",
            len(jobs) * self.args.attempts_per_target,
            self.args.attempts_per_target,
            bool(self.args.stop_after_success),
        )

    def submission_count(self, key):
        return self.state.db.execute("SELECT COUNT(*) FROM tasks WHERE workload=?", (key,)).fetchone()[0]

    def candidate_allowed(self, key, history):
        return self.submission_count(key) < self.args.attempts_per_target and not (
            self.args.stop_after_success and history["successes"] > 0
        )

    def update_coverage(self):
        self.full_coverage = self.coverage_state()[2]
        done = bool(self.known_jobs) and all(
            not self.candidate_allowed(key, self.state.history(key)) for key in self.known_jobs
        )
        self.exit_ready = done and not self.state.active()
        self.paused = done
        return done and not any(row["state"] == "reserved" for row in self.state.active())

    def record_observation(self, row, status):
        evidence = execution_evidence(row["task_id"], status)
        previous = self.state.db.execute(
            "SELECT evidence FROM observations WHERE task_id=?",
            (row["task_id"],),
        ).fetchone()
        previous_runs = {run["run_id"]: run for run in json.loads(previous[0])["runs"]} if previous else {}
        for run in evidence["runs"]:
            saved = previous_runs.get(run["run_id"], {})
            if saved.get("log_evidence_status"):
                run.update(
                    {key: value for key, value in saved.items() if key not in ("state", "reason_resolved", "resolved")},
                )
            elif run["state"] in TERMINAL:
                collect_log_evidence(self.queue, row["task_id"], run)
        with self.state.db:
            self.state.db.execute(
                "INSERT OR REPLACE INTO observations VALUES (?, ?)",
                (row["task_id"], json.dumps(evidence)),
            )

    def tick(self, now=None):
        super().tick(now)
        if self.exit_ready:
            self.message = "Targeted batch finished; all submitted tasks resolved"
        elif self.paused:
            self.message = "Target limits reached; waiting for submitted tasks"
        elif "waiting for newer job" in self.message:
            self.message = "Fixed source inputs expired; start a new batch with valid source tasks"

    def display_rows(self):
        passed, eligible, rows = super().display_rows()
        for index, row in enumerate(rows):
            key = row[1]
            if key not in self.histories:
                continue
            cells = list(row)
            cells[5] = str(self.submission_count(key))
            if self.histories[key].get("expired_source_id"):
                cells[6] = Text("fixed source inputs expired")
            elif key not in self.active_types and not self.candidate_allowed(key, self.histories[key]):
                cells[6] = Text("target finished")
            rows[index] = tuple(cells)
        return passed, eligible, rows

    def snapshot(self):
        data = super().snapshot()
        data["targets"] = {
            "task_ids": self.args.task_ids,
            "attempts_per_target": self.args.attempts_per_target,
            "stop_after_success": bool(self.args.stop_after_success),
            "maximum_submissions": len(self.args.task_ids) * self.args.attempts_per_target,
        }
        data["mode"] = "targeted"
        data["batch_finished"] = self.exit_ready
        return data

    def report(self):
        data = super().report()
        data["coverage_definition"] = (
            "Results for fixed source task IDs in this batch only; no pool-wide coverage claim."
        )
        observations = {row[0]: json.loads(row[1]) for row in self.state.db.execute("SELECT * FROM observations")}
        for row in data.get("rows", []):
            row["attempts"] = self.submission_count(row["key"])
            for task in row.get("tasks", []):
                task["source_task_id"] = row["source_task_id"]
                task.update(observations.get(task["task_id"], execution_evidence(task["task_id"], {})))
        return data

    def render(self, max_rows=None):
        table = Table(title=f"Targeted replay: {self.args.from_pool} → {self.args.queue}")
        for column in ("Source task", "Workload", "Passed", "Submitted", "Status"):
            table.add_column(column)
        _, _, rows = self.display_rows()
        for _, key, name, _, successes, attempts, status, _ in rows[:max_rows]:
            table.add_row(
                self.known_jobs[key].task_id,
                name,
                successes,
                f"{attempts}/{self.args.attempts_per_target}",
                status,
            )
        return Group(table, Text(self.message), Text(f"Report: {getattr(self.args, 'report', '-')}", style="dim"))
