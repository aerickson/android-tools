import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
import replay_tc_pool as cli
from worker_health.replay_session_info import current_invocation, report_path, started_label
from worker_health.replay_sessions import ReplaySession


@pytest.mark.parametrize("viewing", [False, True])
def test_invocation_round_trips_current_options(mocker, tmp_path, viewing):
    session = ReplaySession(tmp_path / "session with spaces", "source/pool", "dest/pool", "2026-11-05T18:47:11Z")
    args = SimpleNamespace(
        session_info=session,
        from_pool=session.source,
        queue=session.destination,
        view_only=viewing,
        submissions_enabled=True,
        stop_on_full_coverage=False,
        exit_on_full_coverage=True,
        worker_active_minutes=45,
        tasks_per_worker=2,
        poll_interval=30,
        submit_interval=120,
        source_refresh_minutes=15,
        failure_backoff=600,
        output="table",
        dry_run=True,
        once=False,
        report=str(session.report_path),
        replay_report=SimpleNamespace(copy_path=tmp_path / "extra report.json"),
    )
    command = shlex.split(current_invocation(args, {}))
    assert command[:4] == ["pipenv", "run", "python", "replay_tc_pool.py"]
    mocker.patch("sys.argv", command[3:])
    parsed = cli.parse_args()
    assert parsed.session == str(session.path)
    assert parsed.resume == (not viewing)
    assert parsed.submit_on_start == (not viewing)
    assert not parsed.stop_on_full_coverage
    assert parsed.exit_on_full_coverage
    for name in (
        "worker_active_minutes",
        "tasks_per_worker",
        "poll_interval",
        "submit_interval",
        "source_refresh_minutes",
        "failure_backoff",
        "output",
        "dry_run",
        "once",
    ):
        assert getattr(parsed, name) == getattr(args, name)
    assert parsed.report == str(args.replay_report.copy_path)
    assert report_path(args, {}) == session.report_path
    assert started_label(args, {}) == "Nov 05, 2026, 18:47 UTC"


def test_invocation_preserves_paused_submissions_and_omits_canonical_report(tmp_path):
    session = ReplaySession(tmp_path, "source/pool", "dest/pool", "2026-11-05T10:47:11-08:00")
    args = SimpleNamespace(
        session_info=session,
        view_only=False,
        submissions_enabled=False,
        report=str(session.report_path),
    )
    command = shlex.split(current_invocation(args, {}))
    assert "--resume" in command
    assert "--submit-on-start" not in command
    assert "--report" not in command
    assert started_label(args, {}) == "Nov 05, 2026, 18:47 UTC"


def test_pool_invocation_without_session(mocker):
    args = SimpleNamespace(from_pool="source/pool", queue="dest/pool", report="/tmp/report.json")
    command = shlex.split(current_invocation(args, {}))
    mocker.patch("sys.argv", command[3:])
    parsed = cli.parse_args()
    assert parsed.from_pool == args.from_pool
    assert parsed.queue == args.queue
    assert Path(parsed.report) == Path(args.report)
