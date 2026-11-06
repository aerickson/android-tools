import curses
import logging
import threading
from types import SimpleNamespace

from worker_health.replay_dashboard import Dashboard, LogBuffer, PaneCursor, run_dashboard


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
