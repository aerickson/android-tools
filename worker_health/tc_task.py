#!/usr/bin/env python
"""Inspect Taskcluster tasks and their text logs."""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
import taskcluster

TC_ROOT_URL = "https://firefox-ci-tc.services.mozilla.com"
DEFAULT_LOG = "public/logs/live_backing.log"
TASK_ID_PATTERN = r"[A-Za-z0-9_-]{22}"


def get_queue(authenticated=False):
    options = {"rootUrl": TC_ROOT_URL, "timeout": 60}
    if authenticated:
        try:
            data = json.loads((Path.home() / ".tc_token").read_text())
            credentials = {key: data[key] for key in ("clientId", "accessToken")}
            if not all(isinstance(value, str) and value.strip() for value in credentials.values()):
                raise ValueError("Empty credentials")
            options["credentials"] = credentials
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("Cannot read valid Taskcluster credentials from ~/.tc_token") from exc
    return taskcluster.Queue(options)


def task_reference(value):
    """Return a task ID and optional run ID from a slug or Firefox CI task URL."""
    if re.fullmatch(TASK_ID_PATTERN, value):
        return value, None
    url = urlsplit(value)
    match = re.fullmatch(
        rf"/tasks/({TASK_ID_PATTERN})(?:/runs/(\d+))?(?:/(?:details|artifacts|logs))?/?",
        url.path,
    )
    if url.scheme != "https" or url.netloc != urlsplit(TC_ROOT_URL).netloc or not match:
        raise argparse.ArgumentTypeError("Expected a task ID or Firefox CI /tasks/TASK_ID[/runs/N] URL")
    return match[1], int(match[2]) if match[2] is not None else None


def duration_str(started, resolved):
    if not started or not resolved:
        return "-"
    try:
        # Python 3.10 requires an explicit UTC offset instead of Z.
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))  # noqa: FURB162
        end = datetime.fromisoformat(resolved.replace("Z", "+00:00"))  # noqa: FURB162
        return f"{int((end - start).total_seconds())}s"
    except (ValueError, TypeError):
        return "-"


def fetch_log(queue, task_id, run_id, artifact=DEFAULT_LOG):
    build_url = queue.buildSignedUrl if queue.options.get("credentials") else queue.buildUrl
    url = build_url("getArtifact", task_id, str(run_id), artifact)
    # Request errors can contain signed URLs; never expose those URLs in diagnostics.
    try:
        with requests.get(url, allow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            return response.text
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else None
        message = f"HTTP {status}" if status is not None else type(exc).__name__
        raise RuntimeError(f"Cannot fetch artifact {artifact}: {message}") from None


def summarize_task(task_id, status, run_id=None):
    task_status = status["status"]
    runs = task_status.get("runs", [])
    if run_id is None:
        run = max(runs, key=lambda item: item["runId"]) if runs else {}
    else:
        run = next((item for item in runs if item["runId"] == run_id), None)
        if run is None:
            available = ", ".join(str(item["runId"]) for item in runs) or "none"
            raise ValueError(f"Run {run_id} does not exist (available runs: {available})")
    return {
        "taskId": task_id,
        "state": run.get("state", task_status.get("state", "-")),
        "reasonResolved": run.get("reasonResolved", "-"),
        "workerType": task_status.get("workerType", "-"),
        "workerId": run.get("workerId", "-"),
        "workerGroup": run.get("workerGroup", "-"),
        "runId": run.get("runId"),
        "scheduled": run.get("scheduled", "-"),
        "started": run.get("started", "-"),
        "resolved": run.get("resolved", "-"),
        "duration": duration_str(run.get("started"), run.get("resolved")),
        "totalRuns": len(runs),
    }


def print_summary(summary):
    print(f"Task:        {summary['taskId']}")
    print(f"State:       {summary['state']} ({summary['reasonResolved']})")
    print(f"Worker type: {summary['workerType']}")
    print(f"Worker:      {summary['workerGroup']}/{summary['workerId']}")
    run = summary["runId"]
    print(f"Run ID:      {run if run is not None else '-'} ({summary['totalRuns']} total runs)")
    print(f"Scheduled:   {summary['scheduled']}")
    print(f"Started:     {summary['started']}")
    print(f"Resolved:    {summary['resolved']}")
    print(f"Duration:    {summary['duration']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_ids", nargs="+", type=task_reference, metavar="TASK_ID_OR_URL")
    parser.add_argument("--logs", action="store_true", help="Fetch and print task logs")
    parser.add_argument("--grep", metavar="PATTERN", help="Filter log lines by regex (implies --logs)")
    parser.add_argument("--artifact", metavar="PATH", help="Text artifact to fetch (implies --logs)")
    parser.add_argument("--auth", action="store_true", help="Read credentials from ~/.tc_token")
    parser.add_argument("--run", type=int, metavar="N", help="Zero-based run ID (overrides URL run; default: latest)")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output a JSON array")
    args = parser.parse_args(argv)
    if args.run is not None and args.run < 0:
        parser.error("--run must be nonnegative")
    pattern = None
    if args.grep is not None:
        try:
            pattern = re.compile(args.grep)
        except re.error as exc:
            parser.error(f"Invalid --grep regex: {exc}")
    args.logs = args.logs or args.grep is not None or args.artifact is not None

    results = []
    failed = False
    try:
        queue = get_queue(args.auth)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.as_json:
            print("[]")
        return 1

    for task_id, url_run in args.task_ids:
        try:
            run_id = args.run if args.run is not None else url_run
            summary = summarize_task(task_id, queue.status(task_id), run_id)
        except (
            taskcluster.exceptions.TaskclusterFailure,
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            print(f"ERROR fetching {task_id}: {exc}", file=sys.stderr)
            failed = True
            continue

        if args.logs:
            try:
                if summary["runId"] is None:
                    raise ValueError("Task has no runs; no log is available yet")
                log_text = fetch_log(queue, task_id, summary["runId"], args.artifact or DEFAULT_LOG)
                if pattern is not None:
                    log_text = "\n".join(line for line in log_text.splitlines() if pattern.search(line))
                summary["log"] = log_text
            except (taskcluster.exceptions.TaskclusterFailure, RuntimeError, ValueError) as exc:
                summary["log_error"] = str(exc)
                print(f"ERROR fetching log for {task_id}: {exc}", file=sys.stderr)
                failed = True
        results.append(summary)

    if args.as_json:
        print(json.dumps(results, indent=2))
    else:
        for i, summary in enumerate(results):
            if i:
                print("\n---\n")
            print_summary(summary)
            if "log" in summary:
                print("\nLog:")
                print(summary["log"])
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
