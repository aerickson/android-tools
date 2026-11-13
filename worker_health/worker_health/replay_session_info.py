"""Display metadata and reproducible commands for the current replay session."""

import shlex
from datetime import UTC, datetime
from pathlib import Path


def report_path(args, snapshot):
    session = getattr(args, "session_info", None)
    return session.report_path if session else snapshot.get("report_path") or getattr(args, "report", None)


def started_label(args, snapshot):
    session = getattr(args, "session_info", None)
    value = session.started_at if session else snapshot.get("started_at")
    if not value:
        return "unknown"
    try:
        return datetime.fromisoformat(value).astimezone(UTC).strftime("%b %d, %Y, %H:%M UTC")
    except ValueError:
        return str(value)


def current_invocation(args, snapshot):
    command = ["pipenv", "run", "python", "replay_tc_pool.py"]
    session = getattr(args, "session_info", None)
    session_path = str(session.path) if session else getattr(args, "session", None)
    viewing = getattr(args, "view_only", False)
    if session_path:
        command.extend(["--session", str(session_path)])
        if not viewing:
            command.append("--resume")
    else:
        command.extend(["--from-pool", args.from_pool, "--queue", args.queue])
    if not viewing and getattr(args, "submissions_enabled", False):
        command.append("--submit-on-start")
    targeted = bool(getattr(args, "task_ids", []))
    if not targeted:
        for name, default in (("stop_on_full_coverage", True), ("exit_on_full_coverage", False)):
            command.append("--" + ("" if getattr(args, name, default) else "no-") + name.replace("_", "-"))
    for name in (
        "successes_per_type",
        "worker_active_minutes",
        "tasks_per_worker",
        "poll_interval",
        "submit_interval",
        "source_refresh_minutes",
        "failure_backoff",
        "output",
    ):
        value = getattr(args, name, None)
        if targeted and name == "successes_per_type":
            continue
        if value is not None:
            command.extend(["--" + name.replace("_", "-"), str(value)])
    for name in ("dry_run", "once"):
        if getattr(args, name, False):
            command.append("--" + name.replace("_", "-"))
    copy_path = getattr(getattr(args, "replay_report", None), "copy_path", None)
    if not copy_path:
        candidate = getattr(args, "report", None)
        canonical = report_path(args, snapshot)
        if candidate and (
            not session or Path(candidate).expanduser().absolute() != Path(canonical).expanduser().absolute()
        ):
            copy_path = candidate
    if copy_path:
        command.extend(["--report", str(copy_path)])
    return shlex.join(command)
