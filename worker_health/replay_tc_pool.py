#!/usr/bin/env python3
"""Continuously replay a fair mix of level-1 jobs into available pool capacity."""

import argparse
import curses
import logging
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import taskcluster
from replay_tc_task import positive_int, queue_argument, queue_client
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich.text import Text
from worker_health.replay_controller import (
    ControllerState,
    ReplayController,
    SchedulingStopped,
    state_path,
)
from worker_health.replay_dashboard import Dashboard, LogBuffer, run_dashboard
from worker_health.replay_report import ReplayReport
from worker_health.replay_session_picker import pick_session
from worker_health.replay_sessions import create_session, list_sessions, load_session, write_json

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Credentials and likely required scopes:
  Active monitoring reads ~/.tc_token, including with --dry-run.
  Viewing an existing session is offline and does not read credentials.

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
    parser.add_argument("--from-pool", type=queue_argument, help="Source pool ID or worker pool URL")
    parser.add_argument(
        "-q",
        "--queue",
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
    parser.add_argument("--session", help="Session directory (or its report.json) to view")
    parser.add_argument("--resume", action="store_true", help="Resume monitoring and update an existing session")
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Start fresh session coverage for the specified pools",
    )
    parser.add_argument(
        "--submit-on-start",
        action="store_true",
        help="Enable submissions immediately in an active session",
    )
    parser.add_argument("--report", help="Additional JSON report copy (default report stays in the session directory)")
    args = parser.parse_args()
    if bool(args.from_pool) != bool(args.queue):
        parser.error("--from-pool and --queue must be supplied together")
    if args.from_pool and args.from_pool == args.queue:
        parser.error("Source and destination pools must differ")
    if args.session and (args.from_pool or args.new_session):
        parser.error("--session cannot be combined with pool arguments or --new-session")
    if args.new_session and args.resume:
        parser.error("--new-session and --resume select different actions")
    if not (args.from_pool or args.session) and (args.new_session or args.resume or args.submit_on_start):
        parser.error("Specify both pools or --session for this action")
    if args.failure_backoff > 3600:
        parser.error("--failure-backoff must be at most 3600 seconds")
    return args


def select_session(args, interactive):
    if not args.session and not args.from_pool:
        if not interactive:
            raise ValueError("Specify --session PATH or both --from-pool and --queue; the picker requires a terminal")
        choice = pick_session()
        if choice is None:
            return None
        args.session = choice.path
        args.from_pool, args.queue = choice.source, choice.destination
        args.resume = choice.action == "resume"
        args.new_session = choice.action == "new"
    if args.session:
        session = load_session(args.session)
        existing = True
    else:
        matching = [
            session
            for session in list_sessions()
            if session.source == args.from_pool and session.destination == args.queue
        ]
        existing = bool(matching) and not args.new_session
        if existing:
            session = matching[0]
        else:
            if args.resume:
                raise ValueError("No matching session to resume; use --new-session to start one")
            session = create_session(args.from_pool, args.queue)
    args.view_only = existing and not args.resume
    if args.view_only and args.submit_on_start:
        raise ValueError("Viewing does not submit tasks; add --resume or select --new-session")
    args.from_pool, args.queue = session.source, session.destination
    args.session_info = session
    args.session = str(session.path)
    args.submissions_enabled = args.submit_on_start
    args.session_warning = " | ".join(session.warnings())
    return session


def view_session(args, console, interactive):
    snapshot = args.session_info.report()
    if args.report and args.session_info.report_path.resolve() != Path(args.report).expanduser().resolve():
        write_json(Path(args.report).expanduser(), snapshot)
    if interactive and args.output == "table" and not args.once:
        Dashboard(args, LogBuffer(), lambda: snapshot, threading.Event(), threading.Event()).run()
        return bool(getattr(args, "resume_requested", False))
    console.print(Text(f"Saved snapshot — last sampled: {snapshot.get('sampled_at') or 'never'}", style="bold"))
    table = Table("Workload", "Coverage", "Pass", "Try", "Status")
    for row in snapshot.get("rows", []):
        table.add_row(*(str(row.get(key, "-")) for key in ("name", "coverage", "successes", "attempts", "status")))
    console.print(table)
    return False


def run_controller(args, console, stop=None, publish=None):
    stop = stop or threading.Event()
    wake = getattr(args, "replay_wake", None)
    report = None
    data = {"full_coverage": False}
    reason = "manual_stop"
    result = 0
    try:
        session = getattr(args, "session_info", None)
        if session and session.report().get("sampled_at") and not session.state_path.exists():
            raise ValueError("Session database is missing; view the saved report or start a new session")
        database_path = session.state_path if session else state_path(args.queue)
        with ControllerState(database_path, lock_path=str(state_path(args.queue)) + ".lock") as state:
            report = ReplayReport(args)
            args.replay_report = report
            data = dict(report.latest)
            logger.info("Coverage report: %s", report.path)
            queue = queue_client(authenticated=True)
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
                    if not failed:
                        data["sampled_at"] = datetime.now(UTC).isoformat()
                        controller.sampled_at = data["sampled_at"]
                    report.write(data)
                    if session:
                        args.session_warning = " | ".join(session.warnings())
                    if publish:
                        publish(controller.snapshot())
                    elif args.output == "table":
                        console.print(controller.render())
                    summary = (
                        controller.workers,
                        controller.outstanding,
                        controller.limit,
                        data["passed"],
                        data["eligible"],
                        data["excluded"],
                        controller.message,
                    )
                    logger.info(
                        "Workers=%s outstanding=%s/%s | Coverage=%s/%s (%s excluded) | %s",
                        controller.workers,
                        controller.outstanding,
                        controller.limit,
                        data["passed"],
                        data["eligible"],
                        data["excluded"],
                        controller.message,
                        extra={"replay_summary": summary},
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
                final_data = controller.report()
                if data.get("sampled_at"):
                    final_data["sampled_at"] = data["sampled_at"]
                data = final_data
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
    interactive = console.is_terminal and sys.stdin.isatty()
    try:
        session = select_session(args, interactive)
        if session is None:
            return 0
        action = "Viewing" if args.view_only else "Resuming" if args.resume else "Monitoring new"
        console.print(Text(f"{action} session started {session.started_at}"))
        for warning in session.warnings():
            console.print(Text("⚠ " + warning, style="bold yellow"))
        if args.view_only:
            if not view_session(args, console, interactive):
                console.print(
                    Text(f"Report: {session.report_path}\nResume monitoring: {session.resume_command()}"),
                    soft_wrap=True,
                )
                return 0
            args.view_only = False
            args.submissions_enabled = False
        if args.output != "table" or args.once or not interactive:
            result = run_controller(args, console)
            console.print(
                Text(
                    f"Report: {session.report_path}\nResume monitoring: {session.resume_command()}\n"
                    "Add --submit-on-start to enable submissions immediately; use --new-session with pools for fresh coverage.",
                ),
                soft_wrap=True,
            )
            return result
    except (OSError, ValueError, KeyError, TypeError, curses.error) as exc:
        logger.error("Cannot open session: %s", exc)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 0
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
    console.print(
        Text(
            f"Resume monitoring: {session.resume_command()}\nAdd --submit-on-start to enable submissions immediately.",
        ),
        soft_wrap=True,
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
