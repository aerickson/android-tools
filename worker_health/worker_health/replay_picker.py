"""Discover recent replay workloads and select them in a terminal."""

import curses
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

import taskcluster

logger = logging.getLogger(__name__)


LEVEL_PATTERN = re.compile(r"level-(\d+)(?=[/\-]|$)")
PRIORITIES = ("highest", "very-high", "high", "medium", "low", "very-low", "lowest")


def level_one_problem(task):
    """Require positive level-1 evidence and reject conflicting resource levels."""
    env_level = str(task.get("payload", {}).get("env", {}).get("MOZ_SCM_LEVEL", ""))
    if env_level and env_level != "1":
        return f"source has MOZ_SCM_LEVEL={env_level}"
    scopes = task.get("scopes", [])
    if any(
        scope_covered(scope, scopes)
        for scope in (
            "secrets:get:project/perftest/gecko/level-3/perftest-login",
            "generic-worker:cache:gecko-level-3-pip",
        )
    ):
        return "source requests level-3 access or a wildcard covering it"
    resources = [task.get("schedulerId", ""), *task.get("scopes", [])]
    payload = task.get("payload", {})
    resources.extend(mount.get("cacheName", "") for mount in payload.get("mounts", []))
    resources.extend(payload.get("cache", {}))
    levels = {match.group(1) for value in resources for match in LEVEL_PATTERN.finditer(value)}
    if levels - {"1"}:
        return "source references resources outside level 1"
    if env_level != "1" and "1" not in levels:
        return "source has no explicit level-1 marker"
    return None


def scope_covered(scope, granted):
    return any(scope == item or (item.endswith("*") and scope.startswith(item[:-1])) for item in granted)


def missing_scopes(source, destination, granted):
    required = [*source.get("scopes", []), "queue:create-task:project:none", "queue:scheduler-id:-"]
    missing = [scope for scope in required if not scope_covered(scope, granted)]
    if not any(scope_covered(f"queue:create-task:{priority}:{destination}", granted) for priority in PRIORITIES):
        missing.append(f"queue:create-task:lowest:{destination}")
    return sorted(set(missing))


@dataclass
class ReplayJob:
    task_id: str
    task: dict
    completed: str
    seconds: float | None
    worker: str
    observed: int = 1

    @property
    def name(self):
        return self.task["metadata"]["name"]

    @property
    def secrets(self):
        return [scope for scope in self.task.get("scopes", []) if scope.startswith("secrets:")]


@dataclass
class Discovery:
    jobs: list
    workers: int
    task_ids: int
    eligible_tasks: int
    unavailable_workers: int
    cached_at: float | None = None
    complete: bool = True


@dataclass(frozen=True)
class RefreshPicker:
    query: str
    selected_ids: tuple[str, ...] = ()


def batch_results(method, ids, key):
    for start in range(0, len(ids), 100):
        options = {}
        while True:
            page = method({"taskIds": ids[start : start + 100]}, **options)
            yield from page[key]
            if not page.get("continuationToken"):
                break
            options["continuationToken"] = page["continuationToken"]


def pool_task_ids(manager, pool, progress=lambda message: None):
    """Return task IDs observed in every worker's bounded history."""
    # TODO: Add Pool Classifier history as another source of task IDs.
    provisioner, worker_type = pool.split("/", 1)
    workers = {}
    options = {}
    while True:
        page = manager.listWorkers(provisioner, worker_type, **options)
        for worker in page["workers"]:
            workers[(worker["workerGroup"], worker["workerId"])] = worker
        if not page.get("continuationToken"):
            break
        options["continuationToken"] = page["continuationToken"]

    ids = {worker["latestTask"]["taskId"] for worker in workers.values() if worker.get("latestTask")}
    unavailable = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(manager.getWorker, provisioner, worker_type, group, worker_id): (group, worker_id)
            for group, worker_id in workers
        }
        for index, future in enumerate(as_completed(futures), 1):
            try:
                ids.update(task["taskId"] for task in future.result().get("recentTasks", []))
            except taskcluster.exceptions.TaskclusterFailure as exc:
                unavailable += 1
                code = getattr(exc, "status_code", None)
                reason = (
                    "worker no longer available" if code == 404 else (str(exc).partition("\n")[0] or type(exc).__name__)
                )
                http = f" (HTTP {code})" if code else ""
                logger.warning("Skipping history for %s/%s: %s%s", *futures[future], reason, http)
            progress(f"Reading worker history: {index}/{len(workers)} workers; {len(ids)} unique task IDs")
    return ids, len(workers), unavailable


def discover_jobs(queue, manager, pool, progress=lambda message: None):
    """Collect every worker's bounded history, deduplicate IDs, and group workloads."""
    ids, workers, unavailable = pool_task_ids(manager, pool, progress)

    progress(f"Checking status of {len(ids)} observed tasks")
    observed_statuses = {
        entry["taskId"]: entry["status"] for entry in batch_results(queue.statuses, sorted(ids), "statuses")
    }
    statuses = {task_id: status for task_id, status in observed_statuses.items() if status["state"] == "completed"}
    progress(f"Reading {len(statuses)} completed task definitions")
    grouped = {}
    eligible = 0
    definitions = list(batch_results(queue.tasks, sorted(statuses), "tasks"))
    for entry in definitions:
        task = entry["task"]
        task_pool = task.get("taskQueueId") or f"{task.get('provisionerId')}/{task.get('workerType')}"
        if task_pool != pool or level_one_problem(task):
            continue
        status = statuses[entry["taskId"]]
        run = next((run for run in reversed(status.get("runs", [])) if run.get("state") == "completed"), {})
        seconds = None
        if run.get("resolved") and run.get("started"):
            seconds = max(
                0,
                (
                    datetime.fromisoformat(run["resolved"].replace("Z", "+00:00"))
                    - datetime.fromisoformat(run["started"].replace("Z", "+00:00"))
                ).total_seconds(),
            )
        job = ReplayJob(entry["taskId"], task, run.get("resolved", task["created"]), seconds, run.get("workerId", "-"))
        eligible += 1
        # Keep scope variants separate so a new secret requirement does not hide a usable workload.
        key = (job.name, tuple(sorted(task.get("scopes", []))))
        previous = grouped.get(key)
        if previous:
            if job.completed > previous.completed:
                job.observed = previous.observed + 1
                grouped[key] = job
            else:
                previous.observed += 1
        else:
            grouped[key] = job
    return Discovery(
        sorted(grouped.values(), key=lambda job: job.completed, reverse=True),
        workers,
        len(ids),
        eligible,
        unavailable,
        complete=not unavailable
        and set(observed_statuses) == ids
        and {entry["taskId"] for entry in definitions} == set(statuses),
    )


def filtered_jobs(jobs, query, coverage=None):
    words = query.casefold().split()

    def searchable(job):
        target = coverage.search_terms(job.task) if coverage is not None else ""
        return f"{job.name} {job.task_id} {job.worker} {target}".casefold()

    return [job for job in jobs if all(word in searchable(job) for word in words)]


def runtime_label(seconds):
    return "-" if seconds is None else f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"


def pick_job(
    discovery,
    source_pool,
    destination,
    query="",
    granted=None,
    preview=False,
    coverage=None,
    selected_ids=(),
):
    """Return selected jobs, a refresh request, or None on cancellation."""
    try:
        return curses.wrapper(
            _picker_screen,
            discovery,
            source_pool,
            destination,
            query,
            granted,
            preview,
            coverage,
            selected_ids,
        )
    except curses.error as exc:
        raise ValueError("Cannot open terminal picker; use a task ID or a supported terminal") from exc


def _picker_screen(
    screen,
    discovery,
    source_pool,
    destination,
    query,
    granted=None,
    preview=False,
    coverage=None,
    selected_ids=(),
):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    target_colors = {}
    try:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for index, color in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
                curses.init_pair(index, color, -1)
                target_colors[index] = curses.color_pair(index)
    except curses.error:
        pass
    selected = 0
    marked = {task_id: None for task_id in selected_ids if any(job.task_id == task_id for job in discovery.jobs)}
    blocked = False
    permissions = (
        {job.task_id: missing_scopes(job.task, destination, granted) for job in discovery.jobs}
        if granted is not None
        else {}
    )

    def line(row, text, style=0):
        height, width = screen.getmaxyx()
        if row < height:
            try:
                screen.addnstr(row, 0, text, max(0, width - 1), style)
            except curses.error:
                pass

    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 16 or width < 60:
            line(0, "Resize terminal to at least 60 columns x 16 rows; Esc to cancel")
            screen.refresh()
            if screen.getch() in (27, 3):
                return None
            continue
        jobs = filtered_jobs(discovery.jobs, query, coverage)
        selected = min(selected, max(0, len(jobs) - 1))
        missing = permissions.get(jobs[selected].task_id, []) if jobs else []
        details = []
        list_start = 7 if coverage is not None else 6
        if jobs:
            job = jobs[selected]
            details = [job.name, f"Task: {job.task_id}  Worker: {job.worker}"]
            details.extend("Missing: " + scope for scope in missing)
            if coverage is not None:
                details.extend(coverage.details(job.task))
            details.append("Secrets: " + (", ".join(job.secrets) or "none"))
        details_height = min(10, len(details), max(3, height - list_start - 3))
        page_size = max(1, height - list_start - 3 - details_height)
        top = (selected // page_size) * page_size
        line(0, f"Replay completed level-1 jobs | {len(marked)} selected", curses.A_BOLD)
        line(1, f"From: {source_pool}")
        line(2, f"To:   {destination}")
        line(
            3,
            f"{'Cached' if discovery.cached_at is not None else 'Recent'} worker history (~20/worker): {discovery.workers} workers, {discovery.task_ids} IDs, {discovery.eligible_tasks} eligible runs",
            curses.A_DIM,
        )
        if coverage is not None:
            line(4, coverage.summary(discovery.jobs), curses.A_BOLD)
        line(list_start - 2, f"Filter: {query}   ({len(jobs)}/{len(discovery.jobs)} workloads)", curses.A_BOLD)
        line(
            list_start - 1,
            "    Runtime  Access    Runs  "
            + ("Target     " if coverage is not None else "")
            + "Completed UTC         Workload",
            curses.A_UNDERLINE,
        )
        for row, job in enumerate(jobs[top : top + page_size], list_start):
            index = top + row - list_start
            denied = permissions.get(job.task_id, [])
            access = f"need {len(denied)}" if denied else "ready" if granted is not None else "unknown"
            target = f"{coverage.badge(job.task):<10} " if coverage is not None else ""
            check = "[x]" if job.task_id in marked else "[ ]"
            label = f"{check} {runtime_label(job.seconds):>7}  {access:>8} {job.observed:>4}  {target}{job.completed[:19].replace('T', ' ')}  {job.name}"
            style = curses.A_REVERSE if index == selected else 0
            if coverage is not None:
                badge = coverage.badge(job.task)
                color = (
                    1
                    if coverage.passed(job.task)
                    else 3
                    if badge in {"failed", "exception"}
                    else 2
                    if badge in {"running", "queued"}
                    else 0
                )
                style |= target_colors.get(color, 0)
            if denied and not preview:
                style |= curses.A_DIM
            line(row, label, style)
        if not jobs:
            line(list_start, "No matching jobs. Backspace to change the filter.")
        if jobs:
            job = jobs[selected]
            detail_start = height - details_height - 2
            for index, text in enumerate(details[:details_height], detail_start):
                line(
                    index,
                    text,
                    curses.A_BOLD if text.startswith("Missing:") or index == detail_start else curses.A_DIM,
                )
        line(
            height - 2,
            "Arrows / PgUp / PgDn: move | Type: filter"
            + (" (unseen / passed)" if coverage is not None else "")
            + " | Tab: toggle | Backspace: erase",
        )
        footer = (
            "Selection disabled: client lacks scopes. Uncheck affected jobs or Ctrl+R to refresh."
            if blocked
            else "Enter: preview"
            if preview
            else "Enter: select and check inputs"
        )
        line(height - 1, footer + " | Ctrl+R: refresh | Esc: cancel", curses.A_DIM)
        screen.refresh()
        key = screen.get_wch()
        if key in ("\x1b", "\x03"):
            return None
        if key == "\x12":
            return RefreshPicker(query, tuple(marked))
        if key in ("\n", "\r", curses.KEY_ENTER) and (jobs or marked):
            choices = [job for job in discovery.jobs if job.task_id in marked] if marked else [jobs[selected]]
            if not preview and any(permissions.get(job.task_id) for job in choices):
                blocked = True
                continue
            return choices
        if key == "\t" and jobs:
            task_id = jobs[selected].task_id
            if task_id in marked:
                del marked[task_id]
            elif missing and not preview:
                blocked = True
                continue
            else:
                marked[task_id] = None
        blocked = False
        if key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(max(0, len(jobs) - 1), selected + 1)
        elif key == curses.KEY_PPAGE:
            selected = max(0, selected - page_size)
        elif key == curses.KEY_NPAGE:
            selected = min(max(0, len(jobs) - 1), selected + page_size)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            query = query[:-1]
            selected = 0
        elif isinstance(key, str) and key.isprintable():
            query += key
            selected = 0
