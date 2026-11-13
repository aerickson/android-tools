import curses
import io
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from worker_health.replay_dashboard import Dashboard, LogBuffer, PaneCursor, run_dashboard
from worker_health.replay_permissions import permission_summary
from worker_health.replay_report import ReplayReport
from worker_health.replay_sessions import ReplaySession


def test_tui_summary_changes_and_heartbeat_keep_plain_logging_complete(mocker):
    mocker.patch("worker_health.replay_dashboard.time.monotonic", side_effect=[0, 60, 120, 719, 720])
    logs = LogBuffer()
    output = io.StringIO()
    logger = logging.getLogger("summary-test")
    mocker.patch.object(logger, "level", logging.INFO)
    mocker.patch.object(logger, "handlers", [logs, logging.StreamHandler(output)])
    mocker.patch.object(logger, "propagate", False)
    logger.info("waiting: 1 worker", extra={"replay_summary": (1, "waiting")})
    logger.info("waiting: 1 worker", extra={"replay_summary": (1, "waiting")})
    logger.info("waiting: 2 workers", extra={"replay_summary": (2, "waiting")})
    logger.warning("API warning")
    logger.info("waiting: 2 workers", extra={"replay_summary": (2, "waiting")})
    logger.info("waiting: 2 workers", extra={"replay_summary": (2, "waiting")})
    lines = [entry[2] for entry in logs.snapshot()]
    assert len(lines) == 4
    assert "1 worker" in lines[0]
    assert "2 workers" in lines[1]
    assert "API warning" in lines[2]
    assert "2 workers" in lines[3]
    assert len(output.getvalue().splitlines()) == 6


def test_monitoring_spinner_does_not_advance_last_successful_check(mocker):
    args = SimpleNamespace(from_pool="source/pool", queue="dest/pool", view_only=False)
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    clock = mocker.patch("worker_health.replay_dashboard.time.monotonic", return_value=0)
    snapshot = {"sampled_at": "2026-11-07T08:40:43+00:00"}
    initial = view.activity_label(snapshot)
    clock.return_value = 0.5
    assert view.activity_label(snapshot) != initial
    assert view.activity_label(snapshot).endswith("Last check: 08:40:43 UTC")
    args.view_only = True
    saved = view.activity_label(snapshot)
    clock.return_value = 100
    assert view.activity_label(snapshot) == saved
    assert saved.startswith("VIEW ONLY")


def test_full_pool_names_and_compact_footer(mocker):
    args = SimpleNamespace(
        from_pool="releng-hardware/gecko-t-linux-talos-2404",
        queue="releng-hardware/gecko-t-linux-talos-2404-tc-v115",
        session_info=ReplaySession(Path("/tmp/session"), "source", "dest", "2026-11-05T18:47:11Z"),
    )
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (24, 80)
    view.draw(screen)
    rendered = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert args.from_pool in rendered
    assert args.queue in rendered
    assert "Started: Nov 05, 2026, 18:47 UTC | I: Session info" in rendered
    assert "Report:" not in rendered
    assert "/tmp/session" not in rendered
    assert "SUBMISSIONS PAUSED" in rendered


def test_session_info_navigation_and_open_report(mocker, tmp_path):
    session = ReplaySession(tmp_path / "long session name", "source/pool", "dest/pool", "2026-11-05T18:47:11Z")
    args = SimpleNamespace(
        session_info=session,
        from_pool=session.source,
        queue=session.destination,
        submissions_enabled=False,
        stop_on_full_coverage=True,
    )
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    browser = mocker.patch("worker_health.replay_dashboard.webbrowser.open", return_value=True)
    mocker.patch(
        "worker_health.replay_dashboard.threading.Thread",
        side_effect=lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)),
    )
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (20, 80)
    screen.getch.side_effect = [ord("i"), ord("s"), ord("f"), ord("o"), curses.KEY_END, 27, ord("q")]
    view.screen(screen)
    browser.assert_called_once_with(session.report_path.as_uri())
    assert view.info_top == view.info_limit > 0
    assert not args.submissions_enabled
    assert args.stop_on_full_coverage
    assert not view.info_open
    assert view.stop.is_set()


def test_permissions_keys_recheck_without_changing_submission_settings(mocker):
    args = SimpleNamespace(
        from_pool="source/pool",
        queue="dest/pool",
        submissions_enabled=False,
        stop_on_full_coverage=True,
        replay_wake=threading.Event(),
    )
    snapshot = {"permissions": permission_summary([], args.queue, checked_at="before")}
    view = Dashboard(args, LogBuffer(), lambda: snapshot, threading.Event(), threading.Event())
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    keys = iter([ord("p"), ord("a"), ord("s"), ord("f"), ord("r"), 27, ord("q")])

    def getch():
        key = next(keys)
        if key == 27:
            assert args.replay_wake.is_set()
            assert view.recheck_pending
            snapshot["permissions"]["checked_at"] = "after"
            view.draw(screen)
            assert not view.recheck_pending
        return key

    screen.getch.side_effect = getch
    view.screen(screen)
    assert view.full_setup
    assert not args.submissions_enabled
    assert args.stop_on_full_coverage
    assert view.stop.is_set()


def test_permissions_saved_view_does_not_recheck_or_resume(mocker):
    args = SimpleNamespace(from_pool="source/pool", queue="dest/pool", view_only=True, replay_wake=threading.Event())
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    screen.getch.side_effect = [ord("p"), ord("r"), ord("q"), ord("r")]
    view.screen(screen)
    assert not args.replay_wake.is_set()
    assert not view.recheck_pending
    assert args.resume_requested


def test_permissions_opens_active_firefox_ci_client(mocker):
    args = SimpleNamespace(from_pool="source/pool", queue="dest/pool")
    client_id = "mozilla-auth0/ad|Mozilla-LDAP|aerickson/cli-tools"
    permissions = permission_summary([], args.queue, client_id=client_id)
    view = Dashboard(args, LogBuffer(), lambda: {"permissions": permissions}, threading.Event(), threading.Event())
    browser = mocker.patch("worker_health.replay_dashboard.webbrowser.open", return_value=True)
    mocker.patch(
        "worker_health.replay_dashboard.threading.Thread",
        side_effect=lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)),
    )
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    screen.getch.side_effect = [ord("p"), ord("o"), 27, ord("q")]
    view.screen(screen)
    browser.assert_called_once_with(
        "https://firefox-ci-tc.services.mozilla.com/auth/clients/"
        "mozilla-auth0%2Fad%7CMozilla-LDAP%7Caerickson%2Fcli-tools",
    )
    assert view.client_notice == "Client page opened in your browser."


def test_permissions_screen_scrolls_to_exact_scopes_and_shows_artifact_breadth(mocker):
    args = SimpleNamespace(from_pool="source/pool", queue="dest/pool", report="/tmp/session/report.json")
    permissions = permission_summary(
        [
            {"name": "workload-" + str(i), "missing_scopes": ["queue:get-artifact:project/gecko/talos-pdfs/*"]}
            for i in range(30)
        ],
        args.queue,
    )
    snapshot = {"permissions": permissions}
    view = Dashboard(args, LogBuffer(), lambda: snapshot, threading.Event(), threading.Event())
    view.permissions_open = True
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (20, 120)
    view.draw(screen)
    initial = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert "across SCM levels" in initial
    assert "suggested-scopes.txt" in initial
    view.permission_top = view.permission_limit
    screen.reset_mock()
    view.draw(screen)
    last = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert "workload-29" in last


def test_task_cursor_follows_identity_when_rows_change():
    cursor = PaneCursor()
    cursor.sync(["a", "b", "c", "d", "e"], 2)
    cursor.move("down", 2)
    cursor.move("down", 2)
    assert cursor.selected == "c"
    assert cursor.top == 1
    cursor.sync(["e", "a", "b", "c", "d"], 2)
    assert cursor.selected == "c"
    assert cursor.top <= 3 < cursor.top + 2
    cursor.sync(["a", "b"], 2)
    assert cursor.selected == "b"


def test_log_follow_resumes_when_scrolling_to_newest_line():
    cursor = PaneCursor(follow=True)
    cursor.sync(range(1, 11), 3)
    assert cursor.selected == 10 and cursor.top == 7
    cursor.move("up", 3, logs=True)
    cursor.sync(range(1, 15), 3)
    assert cursor.selected == 9 and not cursor.follow
    assert 14 - cursor.seen == 4
    for _ in range(5):
        cursor.move("down", 3, logs=True)
    assert cursor.selected == 14 and cursor.follow and cursor.seen == 14
    cursor.sync(range(1, 16), 3)
    assert cursor.selected == 15
    cursor.move("up", 3, logs=True)
    cursor.move("page_down", 3, logs=True)
    assert cursor.follow and cursor.selected == 15


def test_page_movement_and_resize_keep_cursor_visible():
    cursor = PaneCursor()
    cursor.sync(range(100), 10)
    cursor.move("page_down", 10)
    assert cursor.selected == 10
    cursor.move("page_up", 10)
    assert cursor.selected == 0
    cursor.move("end", 10)
    cursor.sync(range(100), 3)
    assert cursor.selected == 99 and cursor.top == 97
    cursor.move("home", 3)
    assert cursor.selected == 0 and cursor.top == 0


def test_empty_and_expired_log_rows_are_safe():
    cursor = PaneCursor(follow=True)
    cursor.move("up", 4, logs=True)
    cursor.sync([1, 2, 3], 2)
    cursor.move("home", 2, logs=True)
    cursor.sync([10, 11, 12], 2)
    assert cursor.selected == 10
    cursor.sync([], 2)
    assert cursor.selected is None and cursor.top == 0


def test_log_buffer_is_bounded_and_keeps_multiline_messages():
    logs = LogBuffer(limit=3)
    logs.handle(logging.LogRecord("test", logging.INFO, "", 0, "first\nsecond", (), None))
    logs.handle(logging.LogRecord("test", logging.WARNING, "", 0, "third\nfourth", (), None))
    entries = logs.snapshot()
    assert [row[0] for row in entries] == [2, 3, 4]
    assert entries[-1] == (4, logging.WARNING, "fourth")


def test_keyboard_aliases_focus_and_quit(mocker):
    logs = LogBuffer()
    for i in range(30):
        logs.handle(logging.LogRecord("test", logging.INFO, "", 0, "line %s", (i,), None))
    stop, done = threading.Event(), threading.Event()
    view = Dashboard(SimpleNamespace(from_pool="source/pool", queue="target/pool"), logs, dict, stop, done)
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    screen.getch.side_effect = [9, curses.KEY_UP, curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_END, ord("q")]
    view.screen(screen)
    assert view.focus == 1
    assert view.log_cursor.follow
    assert stop.is_set()


def test_task_priority_updates_preserve_selected_workload(mocker):
    args = SimpleNamespace(from_pool="source/pool", queue="target/pool")
    snapshot = {
        "rows": [
            {
                "key": name,
                "name": name,
                "coverage": "untested",
                "successes": "0",
                "attempts": "0",
                "status": "ready",
                "elapsed": "-",
            }
            for name in ["first", "second", "third"]
        ],
    }
    view = Dashboard(args, LogBuffer(), lambda: snapshot, threading.Event(), threading.Event())
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    view.draw(screen)
    view.tasks.move("down", 4)
    snapshot["rows"][2].update(coverage="awaiting result", status="running")
    snapshot["rows"] = [snapshot["rows"][2], *snapshot["rows"][:2]]
    view.draw(screen)
    assert view.order == ["third", "first", "second"]
    assert view.tasks.selected == "second"


def test_quit_stays_responsive_while_worker_is_busy(mocker):
    entered = threading.Event()
    finished = threading.Event()
    thread_ids = []

    def worker(stop, publish):
        thread_ids.append(threading.get_ident())
        publish({"message": "Waiting for API"})
        entered.set()
        assert stop.wait(2)
        finished.set()
        return 0

    def screen(view):
        assert entered.wait(2)
        assert view.get_snapshot()["message"] == "Waiting for API"
        view.stop.set()

    mocker.patch.object(Dashboard, "run", autospec=True, side_effect=screen)
    assert run_dashboard(SimpleNamespace(), LogBuffer(), worker) == 0
    assert finished.is_set()
    assert thread_ids != [threading.get_ident()]


def test_coverage_keys_toggle_independently_and_wake_scheduler(mocker):
    args = SimpleNamespace(
        from_pool="source/pool",
        queue="target/pool",
        stop_on_full_coverage=True,
        exit_on_full_coverage=False,
        replay_wake=threading.Event(),
    )
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    screen.getch.side_effect = [ord("f"), ord("e"), ord("q")]
    view.screen(screen)
    assert not args.stop_on_full_coverage
    assert args.exit_on_full_coverage
    assert args.replay_wake.is_set()


def test_quit_finalizes_report_while_api_is_busy(mocker, tmp_path):
    args = SimpleNamespace(from_pool="source/pool", queue="target/pool", report=str(tmp_path / "report.json"))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def worker(stop, publish):
        args.replay_report = ReplayReport(args)
        args.replay_report.write({"full_coverage": False, "passed": 1})
        entered.set()
        try:
            assert release.wait(5)
            args.replay_report.write({"full_coverage": True, "passed": 2})
            return 0
        finally:
            finished.set()

    def screen(view):
        assert entered.wait(2)
        view.stop.set()

    mocker.patch.object(Dashboard, "run", autospec=True, side_effect=screen)
    try:
        assert run_dashboard(args, LogBuffer(), worker) == 0
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["exit_reason"] == "manual_stop"
        assert report["passed"] == 1
    finally:
        release.set()
        assert finished.wait(2)
    assert json.loads((tmp_path / "report.json").read_text()) == report


def test_view_mode_blocks_load_controls_and_requires_resume(mocker):
    args = SimpleNamespace(
        from_pool="source/pool",
        queue="target/pool",
        view_only=True,
        submissions_enabled=False,
        stop_on_full_coverage=True,
        exit_on_full_coverage=False,
    )
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    screen.getch.side_effect = [ord("s"), ord("f"), ord("e"), ord("r")]
    view.screen(screen)
    assert args.resume_requested
    assert not args.submissions_enabled
    assert args.stop_on_full_coverage and not args.exit_on_full_coverage


def test_submit_key_enables_active_monitoring(mocker):
    args = SimpleNamespace(
        from_pool="source/pool",
        queue="target/pool",
        view_only=False,
        submissions_enabled=False,
        replay_wake=threading.Event(),
    )
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    screen.getch.side_effect = [ord("s"), ord("q")]
    view.screen(screen)
    assert args.submissions_enabled and args.replay_wake.is_set()


def test_plus_raises_success_target_and_wakes_scheduler(mocker):
    args = SimpleNamespace(from_pool="source/pool", queue="target/pool", replay_wake=threading.Event())
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 120)
    screen.getch.side_effect = [ord("+"), ord("q")]
    view.screen(screen)
    assert args.successes_per_type == 2
    assert args.replay_wake.is_set()


def test_targeted_dashboard_cannot_change_fixed_batch_policy(mocker):
    args = SimpleNamespace(
        from_pool="source/pool",
        queue="target/pool",
        task_ids=["A" * 22],
        attempts_per_target=3,
        stop_on_full_coverage=False,
        exit_on_full_coverage=False,
        successes_per_type=1,
        replay_wake=threading.Event(),
    )
    view = Dashboard(args, LogBuffer(), dict, threading.Event(), threading.Event())
    mocker.patch("curses.curs_set")
    mocker.patch("curses.has_colors", return_value=False)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 180)
    screen.getch.side_effect = [ord("f"), ord("e"), ord("+"), ord("q")]
    view.screen(screen)
    assert not args.stop_on_full_coverage
    assert not args.exit_on_full_coverage
    assert args.successes_per_type == 1
    assert args.attempts_per_target == 3
