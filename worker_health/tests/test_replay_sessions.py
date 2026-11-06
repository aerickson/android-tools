import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import replay_tc_pool as cli
from worker_health.replay_picker import Discovery
from worker_health.replay_report import ReplayReport
from worker_health.replay_session_picker import SessionChoice, session_screen

from worker_health import replay_sessions as sessions

SOURCE = "releng-hardware/source"
DESTINATION = "releng-hardware/destination"
NOW = datetime(2026, 11, 6, 19, tzinfo=UTC)


@pytest.fixture(autouse=True)
def state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def make_session(mocker, started=NOW, sampled=None):
    mocker.patch.object(sessions, "utc_now", return_value=started)
    session = sessions.create_session(SOURCE, DESTINATION)
    if sampled:
        sessions.write_json(session.report_path, {**session.report(), "sampled_at": sampled.isoformat()})
    return session


def arguments(mocker, *extra):
    mocker.patch("sys.argv", ["replay_tc_pool.py", *extra])
    return cli.parse_args()


def test_session_name_and_stable_identity(mocker):
    session = make_session(mocker)
    assert session.path.name.startswith("20261106T190000Z__releng-hardware-source__to__releng-hardware-destination__")
    assert sessions.load_session(session.report_path) == session
    assert "--resume" in session.resume_command()
    assert sessions.load_session(session.path).started_at == NOW.isoformat()


def test_picker_order_uses_sample_not_report_write_time(mocker):
    older = make_session(mocker, NOW - timedelta(days=10), NOW)
    newer = make_session(mocker, NOW - timedelta(days=1), NOW - timedelta(hours=2))
    unsampled = make_session(mocker)
    sessions.write_json(newer.report_path, {**newer.report(), "reported_at": (NOW + timedelta(days=1)).isoformat()})
    assert sessions.list_sessions() == [older, newer, unsampled]
    assert len(older.warnings(NOW)) == 1  # Old but actively sampled.
    assert len(older.warnings(NOW + timedelta(days=8))) == 2


def test_invalid_session_files_are_skipped(mocker):
    session = make_session(mocker)
    sessions.write_json(session.path / "session.json", [])
    assert not sessions.list_sessions()


def test_existing_session_view_has_no_side_effects(mocker):
    session = make_session(mocker, NOW - timedelta(days=10), NOW - timedelta(days=8))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in session.path.iterdir()}
    queue = mocker.patch.object(cli, "queue_client", side_effect=AssertionError("view contacted Taskcluster"))
    connect = mocker.patch("sqlite3.connect", side_effect=AssertionError("view opened SQLite"))
    arguments(mocker, "--session", str(session.path), "--output", "log")
    assert cli.main() == 0
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in session.path.iterdir()}
    queue.assert_not_called()
    connect.assert_not_called()


def test_pool_arguments_view_latest_session(mocker):
    session = make_session(mocker, sampled=NOW)
    args = arguments(mocker, "--from-pool", SOURCE, "--queue", DESTINATION)
    assert cli.select_session(args, False) == session
    assert args.view_only and not args.submissions_enabled


def test_first_launch_and_new_session_are_monitoring_only(mocker):
    args = arguments(mocker, "--from-pool", SOURCE, "--queue", DESTINATION)
    first = cli.select_session(args, False)
    assert not args.view_only and not args.submissions_enabled
    args = arguments(mocker, "--from-pool", SOURCE, "--queue", DESTINATION, "--new-session")
    second = cli.select_session(args, False)
    assert first.path != second.path
    assert first.report_path.exists()


@pytest.mark.parametrize("submit", [False, True])
def test_explicit_resume_controls_submissions(mocker, submit):
    session = make_session(mocker)
    extra = ["--submit-on-start"] if submit else []
    args = arguments(mocker, "--session", str(session.path), "--resume", *extra)
    cli.select_session(args, False)
    assert not args.view_only
    assert args.submissions_enabled == submit


def test_submission_flag_cannot_change_view_mode(mocker):
    session = make_session(mocker)
    before = session.report_path.read_bytes()
    args = arguments(mocker, "--session", str(session.path), "--submit-on-start")
    with pytest.raises(ValueError, match="add --resume"):
        cli.select_session(args, False)
    assert session.report_path.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [
        ["--from-pool", SOURCE],
        ["--resume"],
        ["--new-session"],
        ["--from-pool", SOURCE, "--queue", DESTINATION, "--resume", "--new-session"],
        ["--session", "/tmp/example", "--from-pool", SOURCE, "--queue", DESTINATION],
    ],
)
def test_invalid_mode_combinations(mocker, extra):
    with pytest.raises(SystemExit):
        arguments(mocker, *extra)


def test_bare_nonterminal_has_actionable_error(mocker):
    args = arguments(mocker)
    with pytest.raises(ValueError, match="picker requires a terminal"):
        cli.select_session(args, False)


def test_bare_picker_can_select_resume(mocker):
    session = make_session(mocker)
    mocker.patch.object(cli, "pick_session", return_value=SessionChoice("resume", path=str(session.path)))
    args = arguments(mocker)
    assert cli.select_session(args, True) == session
    assert not args.view_only and not args.submissions_enabled


@pytest.mark.parametrize("key,action", [(10, "view"), (ord("r"), "resume")])
def test_picker_actions_and_stale_warning(mocker, key, action):
    session = make_session(mocker, NOW - timedelta(days=10), NOW - timedelta(days=8))
    mocker.patch.object(sessions, "utc_now", return_value=NOW)
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 180)
    screen.getch.return_value = key
    assert session_screen(screen, [session]).action == action
    assert any("Stale:" in call.args[2] for call in screen.addnstr.call_args_list)


def test_session_report_copy_and_sample_freshness(mocker, tmp_path):
    session = make_session(mocker, NOW - timedelta(days=1), NOW - timedelta(hours=2))
    copy = tmp_path / "export.json"
    args = SimpleNamespace(session_info=session, report=str(copy), from_pool=SOURCE, queue=DESTINATION)
    report = ReplayReport(args)
    assert report.latest["started_at"] == session.started_at
    assert report.latest["sampled_at"] == (NOW - timedelta(hours=2)).isoformat()
    report.write({"passed": 2, "sampled_at": NOW.isoformat()})
    report.finish("manual_stop")
    assert json.loads(copy.read_text()) == session.report()
    assert session.report()["sampled_at"] == NOW.isoformat()


def test_real_monitoring_cycle_and_resume_do_not_submit(mocker):
    from worker_health import replay_controller

    queue, manager, auth = mocker.Mock(), mocker.Mock(), mocker.Mock()
    manager.listWorkers.return_value = {"workers": []}
    queue.taskQueueCounts.return_value = {"pendingTasks": 0, "claimedTasks": 0}
    auth.currentScopes.return_value = {"scopes": ["*"]}
    mocker.patch.object(cli, "queue_client", return_value=queue)
    mocker.patch.object(cli.taskcluster, "WorkerManager", return_value=manager)
    mocker.patch.object(cli.taskcluster, "Auth", return_value=auth)
    mocker.patch.object(replay_controller, "load_discovery", return_value=Discovery([], 0, 0, 0, 0))
    arguments(mocker, "--from-pool", SOURCE, "--queue", DESTINATION, "--once", "--output", "log")
    assert cli.main() == 0
    session = sessions.list_sessions()[0]
    assert session.report()["sampled_at"]
    assert not session.report()["submissions_enabled"]
    started = session.started_at
    arguments(mocker, "--session", str(session.path), "--resume", "--once", "--output", "log")
    assert cli.main() == 0
    assert session.report()["started_at"] == started
    queue.createTask.assert_not_called()


def test_new_session_does_not_inherit_successes(mocker):
    from worker_health.replay_controller import ControllerState

    old = make_session(mocker, sampled=NOW)
    with ControllerState(old.state_path) as state:
        state.ensure_job("old", "Old task")
        with state.db:
            state.db.execute("UPDATE workloads SET successes=4")
    args = arguments(mocker, "--from-pool", SOURCE, "--queue", DESTINATION, "--new-session")
    new = cli.select_session(args, False)
    with ControllerState(new.state_path) as state:
        assert not state.db.execute("SELECT * FROM workloads").fetchall()
    with ControllerState(old.state_path) as state:
        assert state.history("old")["successes"] == 4


def test_failed_resume_does_not_erase_report_or_advance_sample(mocker):
    from worker_health.replay_controller import ControllerState

    session = make_session(mocker, sampled=NOW)
    with ControllerState(session.state_path):
        pass
    sessions.write_json(session.report_path, {**session.report(), "rows": [{"name": "Saved evidence"}], "passed": 3})
    arguments(mocker, "--session", str(session.path), "--resume", "--once", "--output", "log")
    mocker.patch.object(cli, "queue_client", side_effect=ValueError("Missing credentials"))
    assert cli.main() == 1
    report = session.report()
    assert report["rows"] == [{"name": "Saved evidence"}]
    assert report["sampled_at"] == NOW.isoformat()
    assert report["exit_reason"] == "error"


def test_active_destination_lock_prevents_report_mutation(mocker):
    from rich.console import Console
    from worker_health.replay_controller import ControllerState, state_path

    session = make_session(mocker, sampled=NOW)
    with ControllerState(session.state_path):
        pass
    before = session.report_path.read_bytes()
    args = arguments(mocker, "--session", str(session.path), "--resume")
    cli.select_session(args, False)
    with ControllerState(state_path(DESTINATION)):
        assert cli.run_controller(args, Console()) == 1
    assert session.report_path.read_bytes() == before


def test_picker_new_session_prompts_for_pools(mocker):
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    mocker.patch("curses.echo")
    mocker.patch("curses.noecho")
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 180)
    screen.getch.return_value = ord("n")
    screen.getstr.side_effect = [SOURCE.encode(), DESTINATION.encode()]
    choice = session_screen(screen, [])
    assert choice == SessionChoice("new", source=SOURCE, destination=DESTINATION)


def test_view_export_does_not_rewrite_source(mocker, tmp_path):
    session = make_session(mocker, sampled=NOW)
    before = session.report_path.read_bytes(), session.report_path.stat().st_mtime_ns
    export = tmp_path / "export.json"
    arguments(mocker, "--session", str(session.path), "--report", str(export), "--output", "log")
    assert cli.main() == 0
    assert (session.report_path.read_bytes(), session.report_path.stat().st_mtime_ns) == before
    assert json.loads(export.read_text()) == session.report()


def test_missing_database_cannot_silently_reset_session(mocker):
    session = make_session(mocker, sampled=NOW)
    before = session.report_path.read_bytes()
    arguments(mocker, "--session", str(session.path), "--resume", "--once", "--output", "log")
    queue = mocker.patch.object(cli, "queue_client")
    assert cli.main() == 1
    queue.assert_not_called()
    assert session.report_path.read_bytes() == before
    assert not session.state_path.exists()
