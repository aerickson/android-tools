#!/usr/bin/env python3
"""Continuously replay a fair mix of level-1 jobs into available pool capacity."""

import argparse
import logging
import sqlite3
import sys
import threading

import taskcluster
from replay_tc_task import positive_int, queue_argument, queue_client
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text
from worker_health.replay_controller import (
    ControllerState,
    ReplayController,
    SchedulingStopped,
    state_path,
)
from worker_health.replay_dashboard import LogBuffer, run_dashboard
from worker_health.replay_report import ReplayReport

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Credentials and likely required scopes:
  Reads clientId and accessToken from ~/.tc_token, including with --dry-run.

  Submission scopes (replace DESTINATION with the pool passed to --queue):
    queue:create-task:lowest:DESTINATION
    queue:create-task:project:none
    queue:scheduler-id:-

  Example destination scope:
    queue:create-task:lowest:releng-hardware/gecko-t-linux-talos-2404-tc-v115

  Common level-1 Talos/Browsertime source scopes (workload dependent):
    secrets:get:project/perftest/gecko/level-1/*
    secrets:get:project/releng/gecko/build/level-1/*
    generic-worker:cache:gecko-level-1-*
    queue:get-artifact:project/gecko/*

  Effective client scopes must also cover every scope requested by each source
  task. Jobs with missing scopes are skipped. Broader existing grants can cover
  the submission scopes above. The artifact wildcard spans SCM levels; replay
  still accepts only level-1 sources. See README.md, Replay client scopes.
""",
    )
    parser.add_argument("--from-pool", required=True, type=queue_argument, help="Source pool ID or worker pool URL")
    parser.add_argument(
        "-q",
        "--queue",
        required=True,
        type=queue_argument,
        help="Destination pool ID or worker pool URL",
    )
    parser.add_argument("--worker-active-minutes", type=positive_int, default=90)
    parser.add_argument(
        "--tasks-per-worker",
        type=positive_int,
        default=1,
        help="Maximum outstanding tasks per active worker",
    )
    parser.add_argument(
        "--poll-interval",
        type=positive_int,
        default=60,
        help="Seconds between scheduling cycles (default: 60)",
    )
    parser.add_argument(
        "--submit-interval",
        type=positive_int,
        default=60,
        help="Minimum seconds between submissions (default: 60)",
    )
    parser.add_argument("--source-refresh-minutes", type=positive_int, default=10)
    parser.add_argument(
        "--failure-backoff",
        type=positive_int,
        default=300,
        help="Initial retry delay in seconds, doubling up to one hour",
    )
    parser.add_argument("--dry-run", action="store_true", help="Check permissions and inputs without submitting tasks")
    parser.add_argument("--once", action="store_true", help="Run one scheduling cycle and exit")
    parser.add_argument("--output", choices=("table", "log"), default="table")
    parser.add_argument(
        "--stop-on-full-coverage",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pause submissions while covered; resume for new uncovered types (default: on)",
    )
    parser.add_argument(
        "--exit-on-full-coverage",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Write the report and exit when full coverage is confirmed (default: off)",
    )
    parser.add_argument("--report", help="JSON report path (default: unique file under /tmp)")
    args = parser.parse_args()
    if args.from_pool == args.queue:
        parser.error("Source and destination pools must differ")
    if args.failure_backoff > 3600:
        parser.error("--failure-backoff must be at most 3600 seconds")
    return args


def run_controller(args, console, stop=None, publish=None):
    stop = stop or threading.Event()
    wake = getattr(args, "replay_wake", None)
    report = None
    data = {"full_coverage": False}
    reason = "manual_stop"
    result = 0
    try:
        report = ReplayReport(args)
        args.replay_report = report
        logger.info("Coverage report: %s", report.path)
        queue = queue_client(authenticated=True)
        with ControllerState(state_path(args.queue)) as state:
            controller = ReplayController(
                queue,
                taskcluster.WorkerManager(queue.options),
                taskcluster.Auth(queue.options),
                state,
                args,
                stop_requested=stop.is_set,
            )
            if publish:
                original_progress = controller.progress

                def progress(message):
                    original_progress(message)
                    publish(controller.snapshot())

                controller.progress = progress
            try:
                while not stop.is_set():
                    if wake:
                        wake.clear()
                    failed = False
                    try:
                        controller.tick()
                    except SchedulingStopped:
                        break
                    except (taskcluster.exceptions.TaskclusterFailure, ValueError, KeyError) as exc:
                        controller.coverage_confirmed = False
                        controller.exit_ready = controller.paused = False
                        controller.message = f"Cycle paused: {exc}"
                        logger.warning("%s", controller.message)
                        failed = True
                    finally:
                        controller.refresh_view()
                    data = controller.report()
                    report.write(data)
                    if publish:
                        publish(controller.snapshot())
                    elif args.output == "table":
                        console.print(controller.render())
                    logger.info(
                        "Workers=%s outstanding=%s/%s | Coverage=%s/%s (%s excluded) | %s",
                        controller.workers,
                        controller.outstanding,
                        controller.limit,
                        data["passed"],
                        data["eligible"],
                        data["excluded"],
                        controller.message,
                    )
                    if not failed and controller.exit_ready:
                        reason = "full_coverage"
                        break
                    if args.once:
                        reason = "cycle_failed" if failed else "once"
                        result = int(failed)
                        break
                    (wake or stop).wait(args.poll_interval)
            finally:
                controller.refresh_view()
                data = controller.report()
    except KeyboardInterrupt:
        logger.info("Stopped scheduling; submitted tasks keep running. Restart to resume tracking.")
    except (OSError, sqlite3.Error, ValueError, taskcluster.exceptions.TaskclusterFailure) as exc:
        reason = "error"
        data["error"] = str(exc)
        logger.error("Controller stopped: %s", exc)
        result = 1
    finally:
        if report:
            try:
                report.write(data, exit_reason=reason)
                logger.info("Coverage report: %s (%s)", report.path, reason)
                args.replay_exit_summary = (
                    f"★ Full coverage: {data['passed']}/{data['eligible']} eligible types. "
                    if reason == "full_coverage"
                    else "Stopped scheduling; submitted tasks keep running. "
                ) + f"Report: {report.path}"
            except OSError as exc:
                logger.error("Could not write coverage report %s: %s", report.path, exc)
                result = 1
    return result


def main():
    args = parse_args()
    console = Console(stderr=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_time=False, show_path=False)],
    )
    if args.output != "table" or args.once or not console.is_terminal or not sys.stdin.isatty():
        return run_controller(args, console)
    logs = LogBuffer()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    root.handlers = [logs]
    root.setLevel(logging.INFO)
    try:
        result = run_dashboard(args, logs, lambda stop, publish: run_controller(args, console, stop, publish))
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)
    if result:
        for _, _, line in logs.snapshot()[-5:]:
            console.print(Text(line))
    else:
        logger.info("%s", getattr(args, "replay_exit_summary", "Stopped scheduling; submitted tasks keep running."))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
