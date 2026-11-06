#!/usr/bin/env python3
"""Clone a completed Firefox CI task as an independent diagnostic task."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import getpass
import json
import logging
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

import taskcluster
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text
from rich.prompt import Confirm

from create_tc_task import TaskMonitor, format_elapsed, gen_task_id, wait_for_task_results
from worker_health.replay_picker import RefreshPicker, discover_jobs, level_one_problem, missing_scopes, pick_job
from worker_health.replay_cache import load_discovery, save_discovery
from worker_health.monitor_keys import MonitoringStopped
from worker_health.replay_coverage import remember_results, remember_submission, target_coverage


ROOT_URL = "https://firefox-ci-tc.services.mozilla.com"
TASK_ID_PATTERN = r"[A-Za-z0-9_-]{22}"
QUEUE_PATTERN = r"[a-zA-Z0-9_-]{1,38}/[a-z]([-a-z0-9]{0,36}[a-z0-9])?"
ARTIFACT_URL_PATTERN = re.compile(
    rf"https?://firefox-ci-tc\.services\.mozilla\.com/api/queue/v1/task/({TASK_ID_PATTERN})"
    r"(?:/runs/(\d+))?/artifacts/([^\s\"'\\<>]+)",
)


def task_id_argument(value):
    if value.startswith(("https://", "http://")):
        url = urlsplit(value)
        if url.hostname != urlsplit(ROOT_URL).hostname:
            raise argparse.ArgumentTypeError("Source URL must be a Firefox CI task URL")
        match = re.fullmatch(rf"/tasks/({TASK_ID_PATTERN})(?:/runs/\d+)?/?", url.path)
        if not match:
            raise argparse.ArgumentTypeError("Expected a task URL, optionally ending in /runs/NUMBER")
        value = match.group(1)
    if not re.fullmatch(TASK_ID_PATTERN, value):
        raise argparse.ArgumentTypeError("Expected a 22-character task ID or Firefox CI task URL")
    return value


def queue_argument(value):
    if value.startswith(("https://", "http://")):
        url = urlsplit(value)
        match = re.fullmatch(r"/provisioners/([^/]+)/worker-types/([^/]+)/?", url.path)
        if url.hostname != urlsplit(ROOT_URL).hostname or not match:
            raise argparse.ArgumentTypeError("Expected a Firefox CI worker pool URL")
        value = f"{match.group(1)}/{match.group(2)}"
    if not re.fullmatch(QUEUE_PATTERN, value):
        raise argparse.ArgumentTypeError("Expected PROVISIONER/WORKER_TYPE or a Firefox CI worker pool URL")
    return value


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be greater than zero")
    return number


def picker_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("Must be zero or greater")
    return number


def parse_args():
    parser = argparse.ArgumentParser(
        description="Replay a completed Firefox CI task using a new task ID and diagnostic task group.",
        epilog=(
            "The source payload and scopes are preserved; destination compatibility must be checked by the operator. "
            "Original routes, tags, and reporting metadata are removed. Dependencies are reused, not rerun. "
            "A source URL ending in /runs/N selects the task definition, not a historical run's inputs. "
            "Submission uses ~/.tc_token and requires the source payload's scopes plus destination creation permissions. "
            "Only level-1 sources are allowed; see README.md, Replay client scopes. "
            "A successful dry run does not validate submission permissions."
        ),
    )
    parser.add_argument(
        "source_task",
        nargs="?",
        type=task_id_argument,
        help="Completed level-1 task ID or Firefox CI task/run URL",
    )
    parser.add_argument(
        "--from-pool",
        type=queue_argument,
        help="Open an interactive picker using this source pool's worker history",
    )
    parser.add_argument("--filter", default="", help="Initial picker search (requires --from-pool)")
    parser.add_argument(
        "--cache-minutes",
        type=nonnegative_int,
        default=10,
        help="Reuse picker history for this many minutes; 0 disables caching (default: 10)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh picker history and client scopes (requires --from-pool)",
    )
    parser.add_argument("-q", "--queue", type=queue_argument, help="Destination pool ID or URL (default: source pool)")
    parser.add_argument("-c", "--count", type=positive_int, default=1, help="Clones per selected job (default: 1)")
    parser.add_argument(
        "-D",
        "--dry-run",
        action="store_true",
        help="Check inputs and print full replay plan as JSON without submitting",
    )
    wait_options = parser.add_mutually_exclusive_group()
    wait_options.add_argument(
        "--wait",
        "--wait-for-completion",
        action="store_true",
        default=None,
        help="Wait for submitted tasks to finish (default with --from-pool)",
    )
    wait_options.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Return after submission without monitoring",
    )
    parser.add_argument(
        "--output",
        choices=("table", "log", "json"),
        default="table",
        help="Result format (default: table); with --dry-run, the plan is always JSON",
    )
    parser.add_argument(
        "--poll-interval",
        type=positive_int,
        default=5,
        help="Status polling interval in seconds (default: 5)",
    )
    args = parser.parse_args()
    if args.wait is None:
        args.wait = bool(args.from_pool)
    if bool(args.source_task) == bool(args.from_pool):
        parser.error("Provide either a source task or --from-pool")
    if (args.filter or args.refresh or args.cache_minutes != 10) and not args.from_pool:
        parser.error("--filter, --refresh, and --cache-minutes require --from-pool")
    if args.from_pool and not picker_terminal():
        parser.error("--from-pool requires an interactive terminal; use a task ID for scripted replays")
    return args


def queue_client(authenticated):
    options = {"rootUrl": ROOT_URL, "timeout": 60}
    token_file = Path.home() / ".tc_token"
    if authenticated or token_file.exists():
        try:
            credentials = json.loads(token_file.read_text())
            options["credentials"] = {key: credentials[key] for key in ("clientId", "accessToken")}
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError("Cannot read Taskcluster credentials from ~/.tc_token") from exc
    return taskcluster.Queue(options)


def timestamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def source_queue(task):
    return task.get("taskQueueId") or f"{task['provisionerId']}/{task['workerType']}"


def clone_task(source, source_id, task_group_id, destination, now):
    """Copy execution inputs without carrying over the original reporting identity."""
    expires = timestamp(now + timedelta(days=7))
    payload = deepcopy(source["payload"])
    artifacts = payload.get("artifacts", [])
    for artifact in artifacts.values() if isinstance(artifacts, dict) else artifacts:
        artifact["expires"] = expires

    metadata = source["metadata"]
    clone = {
        "taskQueueId": destination,
        "schedulerId": "-",
        "projectId": "none",
        "taskGroupId": task_group_id,
        "dependencies": deepcopy(source.get("dependencies", [])),
        "requires": source.get("requires", "all-completed"),
        "priority": "lowest",
        "retries": 0,
        "routes": [],
        "scopes": deepcopy(source.get("scopes", [])),
        "created": timestamp(now),
        "deadline": timestamp(now + timedelta(days=1)),
        "expires": expires,
        "payload": payload,
        "metadata": {
            "name": f"Replay: {metadata['name']}",
            "description": f"Diagnostic replay of `{metadata['name']}` ([{source_id}]({ROOT_URL}/tasks/{source_id})) from `{source_queue(source)}`.",
            "owner": f"{getpass.getuser()}@mozilla.com",
            "source": metadata["source"],
        },
        "extra": {"replay": {"sourceTaskId": source_id, "sourceTaskQueueId": source_queue(source)}},
    }
    return clone


def artifact_references(payload):
    """Find structured fetches, mounts, task images, and explicit Firefox CI URLs."""
    references = set()

    def visit(value):
        if isinstance(value, dict):
            if "taskId" in value:
                name = value.get("artifact")
                if value.get("type") == "task-image":
                    name = name or value.get("path")
                if name:
                    references.add((value["taskId"], None, name))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            for match in ARTIFACT_URL_PATTERN.finditer(value):
                task_id, run_id, name = match.groups()
                references.add((task_id, int(run_id) if run_id is not None else None, unquote(name.split("?", 1)[0])))

    visit(payload)
    fetches = payload.get("env", {}).get("MOZ_FETCHES")
    if fetches:
        for fetch in json.loads(fetches):
            references.add((fetch["task"], None, fetch["artifact"]))
    return references


def source_runtime(status):
    last_run = next((run for run in reversed(status.get("runs", [])) if run.get("state") == "completed"), {})
    if last_run.get("started") and last_run.get("resolved"):
        return max(0, (parse_timestamp(last_run["resolved"]) - parse_timestamp(last_run["started"])).total_seconds())
    return None


def check_inputs(queue, source, source_id, now, status=None, progress=lambda message: None):
    problem = level_one_problem(source)
    if problem:
        raise ValueError(f"Replay requires a level-1 source: {problem}")
    status = status if status is not None else queue.status(source_id)["status"]
    if status["state"] != "completed":
        raise ValueError(f"Source task must be completed; it is {status['state']}")

    dependencies = source.get("dependencies", [])
    for index, dependency in enumerate(dependencies, 1):
        progress(f"dependency {index}/{len(dependencies)}: {dependency}")
        state = queue.status(dependency)["status"]["state"]
        allowed = {"completed", "failed", "exception"} if source.get("requires") == "all-resolved" else {"completed"}
        if state not in allowed:
            raise ValueError(f"Dependency {dependency} is {state}; cannot reuse it")

    references = artifact_references(source["payload"])
    inventories = {}
    for index, (task_id, run_id, name) in enumerate(
        sorted(references, key=lambda ref: (ref[0], -1 if ref[1] is None else ref[1], ref[2])),
        1,
    ):
        progress(f"artifact {index}/{len(references)}: {task_id}/{name}")
        key = (task_id, run_id)
        if key not in inventories:
            artifacts = []
            options = {}
            while True:
                page = (
                    queue.listLatestArtifacts(task_id, **options)
                    if run_id is None
                    else queue.listArtifacts(task_id, run_id, **options)
                )
                artifacts.extend(page["artifacts"])
                if not page.get("continuationToken"):
                    break
                options["continuationToken"] = page["continuationToken"]
            inventories[key] = {artifact["name"]: artifact for artifact in artifacts}
        artifact = inventories[key].get(name)
        if not artifact or artifact.get("storageType") == "error" or parse_timestamp(artifact["expires"]) <= now:
            raise ValueError(f"Required artifact is missing, expired, or an error: {task_id}/{name}")
    return len(references)


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=Console(stderr=True), show_time=False, show_path=False, highlighter=None)],
    )
    if args.dry_run:
        logging.info(
            "[bold yellow]DRY RUN: checking inputs and preparing a preview; no tasks will be submitted[/]",
            extra={"markup": True},
        )
    submitted = []
    try:
        queue = queue_client(authenticated=not args.dry_run)
        if args.from_pool:
            console = Console(stderr=True)
            refresh = args.refresh
            query = args.filter
            selected_ids = ()
            while True:
                discovery = None if refresh else load_discovery(args.from_pool, args.cache_minutes)
                if discovery is None:
                    with console.status("Reading recent worker history") as status:
                        discovery = discover_jobs(
                            queue,
                            taskcluster.WorkerManager(queue.options),
                            args.from_pool,
                            progress=lambda message: status.update(Text(message)),
                        )
                    if args.cache_minutes:
                        save_discovery(args.from_pool, discovery)
                else:
                    logging.info(
                        "Using cached worker history (maximum age %s minutes); Ctrl+R refreshes",
                        args.cache_minutes,
                    )
                available_ids = {job.task_id for job in discovery.jobs}
                for task_id in selected_ids:
                    if task_id not in available_ids:
                        logging.warning(
                            "Selected task disappeared from refreshed history; removed: %s/tasks/%s",
                            ROOT_URL,
                            task_id,
                        )
                selected_ids = tuple(task_id for task_id in selected_ids if task_id in available_ids)
                if not discovery.jobs:
                    raise ValueError(f"No completed level-1 jobs found in recent worker history for {args.from_pool}")
                granted = (
                    taskcluster.Auth(queue.options).currentScopes()["scopes"]
                    if queue.options.get("credentials")
                    else None
                )
                coverage = None
                if args.queue and args.queue != args.from_pool:
                    with console.status("Reading target coverage") as status:
                        coverage = target_coverage(
                            queue,
                            taskcluster.WorkerManager(queue.options),
                            args.queue,
                            minutes=args.cache_minutes,
                            refresh=refresh,
                            progress=lambda message: status.update(Text("Target: " + message)),
                        )
                selected = pick_job(
                    discovery,
                    args.from_pool,
                    args.queue or args.from_pool,
                    query,
                    granted,
                    preview=args.dry_run,
                    coverage=coverage,
                    selected_ids=selected_ids,
                )
                if isinstance(selected, RefreshPicker):
                    refresh = True
                    query = selected.query
                    selected_ids = selected.selected_ids
                    continue
                break
            if selected is None:
                logging.info("Picker cancelled; no tasks submitted")
                return 0
            source_ids = [job.task_id for job in selected]
            for job in selected:
                logging.info("Selected job: %s", job.name)
                logging.info("Selected task: %s/tasks/%s", ROOT_URL, job.task_id)
                logging.info("Selected worker: %s | Completed: %s", job.worker, job.completed)
        else:
            source_ids = [args.source_task]
        now = datetime.now(timezone.utc)
        with Console(stderr=True).status(Text("Checking replay permissions and inputs")) as preflight:
            granted = (
                taskcluster.Auth(queue.options).currentScopes()["scopes"]
                if args.from_pool and not args.dry_run
                else None
            )
            prepared = []
            for index, source_id in enumerate(source_ids, 1):
                source = None
                preflight.update(Text(f"Checking job {index}/{len(source_ids)}: fetching {source_id}"))
                try:
                    source = queue.task(source_id)
                    if args.from_pool and source_queue(source) != args.from_pool:
                        raise ValueError("Selected task does not belong to the requested source pool")
                    label = f"Checking job {index}/{len(source_ids)}: {source['metadata']['name']}"

                    def progress(message):
                        preflight.update(Text(f"{label} | {message}"))

                    progress("source status")
                    source_status = queue.status(source_id)["status"]
                    artifact_count = check_inputs(
                        queue,
                        source,
                        source_id,
                        now,
                        status=source_status,
                        progress=progress,
                    )
                    destination = args.queue or source_queue(source)
                    if granted is not None:
                        missing = missing_scopes(source, destination, granted)
                        if missing:
                            raise ValueError("Client lacks required replay scopes: " + ", ".join(missing))
                except (ValueError, KeyError, taskcluster.exceptions.TaskclusterFailure) as exc:
                    name = source.get("metadata", {}).get("name", "unknown") if source else "unknown"
                    raise ValueError(f"{name} ({source_id}): {exc}") from exc
                prepared.append(
                    {
                        "source_task_id": source_id,
                        "source": source,
                        "source_queue": source_queue(source),
                        "destination_queue": destination,
                        "artifact_references_checked": artifact_count,
                        "expected_runtime": source_runtime(source_status),
                    },
                )
        # Build every definition before confirmation or submission.
        now = datetime.now(timezone.utc)
        tasks = []
        origins = {}
        group_id = None
        for item in prepared:
            for _ in range(args.count):
                task_id = gen_task_id()
                group_id = group_id or task_id
                try:
                    definition = clone_task(
                        item["source"],
                        item["source_task_id"],
                        group_id,
                        item["destination_queue"],
                        now,
                    )
                except (ValueError, KeyError) as exc:
                    raise ValueError(f"{item['source']['metadata']['name']} ({item['source_task_id']}): {exc}") from exc
                tasks.append(
                    {
                        "task_id": task_id,
                        "task": definition,
                    },
                )
                origins[task_id] = item
        if args.from_pool and not args.dry_run:
            for item in prepared:
                console.print(
                    Text(
                        f"Source: {item['source']['metadata']['name']}\nTask: {item['source_task_id']}\n"
                        f"From: {item['source_queue']}\nTo: {item['destination_queue']}\nCopies: {args.count}\n"
                        f"Artifact references checked: {item['artifact_references_checked']}",
                    ),
                )
            console.print(
                Text(f"Total: {len(prepared)} workload(s), {len(tasks)} replay task(s)"),
            )
            if not Confirm.ask("Submit these replay tasks?", default=False, console=console):
                logging.info("Replay cancelled; no tasks submitted")
                return 0
        logging.info("Replay batch: %s workload(s), %s task(s)", len(prepared), len(tasks))
        logging.info(
            "Preserving payload and scopes; using a new group, no reporting routes, lowest priority, and no retries",
        )
        if args.dry_run:
            Console().print(
                Text(
                    json.dumps(
                        {
                            "dry_run": True,
                            "tasks_submitted": 0,
                            # Preserve the single-source preview fields for existing callers.
                            **(
                                {
                                    key: prepared[0][key]
                                    for key in (
                                        "source_task_id",
                                        "source_queue",
                                        "destination_queue",
                                        "artifact_references_checked",
                                    )
                                }
                                if len(prepared) == 1
                                else {}
                            ),
                            "sources": [
                                {key: value for key, value in item.items() if key != "source"} for item in prepared
                            ],
                            "tasks": tasks,
                        },
                        indent=2,
                    ),
                    style="dim",
                ),
                soft_wrap=True,
            )
            logging.info(
                "[bold yellow]DRY RUN complete: no tasks submitted; listed task IDs are proposed IDs only[/]",
                extra={"markup": True},
            )
            return 0

        submission_failed = False
        for entry in tasks:
            try:
                queue.createTask(entry["task_id"], entry["task"])
                submitted.append(entry["task_id"])
                item = origins[entry["task_id"]]
                if item["destination_queue"] != item["source_queue"]:
                    remember_submission(item["destination_queue"], entry["task_id"], item["source"])
                logging.info("Task created: %s/tasks/%s", ROOT_URL, entry["task_id"])
            except Exception as exc:
                logging.error("Could not create %s: %s", entry["task_id"], exc)
                submission_failed = True

        if args.wait and submitted:
            runtimes = {task_id: origins[task_id]["expected_runtime"] for task_id in submitted}
            expected_runtime = prepared[0]["expected_runtime"] if len(prepared) == 1 else runtimes
            for item in prepared:
                if item["expected_runtime"] is not None:
                    logging.info(
                        "%s: last source run took %s; ETA estimates exclude queue time",
                        item["source"]["metadata"]["name"],
                        format_elapsed(item["expected_runtime"]),
                    )
            results = wait_for_task_results(
                TaskMonitor(queue),
                submitted,
                args.poll_interval,
                args.output,
                expected_runtime=expected_runtime,
            )
            for destination in {
                item["destination_queue"] for item in prepared if item["destination_queue"] != item["source_queue"]
            }:
                remember_results(destination, results)
            return int(submission_failed or any(result["state"] != "completed" for result in results.values()))
        if args.output == "json":
            print(
                json.dumps(
                    {"tasks": [{"task_id": task_id, "url": f"{ROOT_URL}/tasks/{task_id}"} for task_id in submitted]},
                    indent=2,
                ),
            )
        else:
            for task_id in submitted:
                print(f"{ROOT_URL}/tasks/{task_id}")
        return int(submission_failed)
    except (ValueError, KeyError, taskcluster.exceptions.TaskclusterFailure) as exc:
        logging.error("Replay failed: %s", exc)
        if args.output == "json":
            print(json.dumps({"tasks": [], "error": str(exc)}))
        return 1
    except MonitoringStopped:
        logging.info(
            "Stopped watching; tasks continue running: %s",
            ", ".join(f"{ROOT_URL}/tasks/{task_id}" for task_id in submitted),
        )
        return 0
    except KeyboardInterrupt:
        logging.info("Stopped monitoring; submitted tasks continue running: %s", ", ".join(submitted) or "none")
        return 130


if __name__ == "__main__":
    sys.exit(main())
