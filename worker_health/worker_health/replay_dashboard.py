"""Fullscreen keyboard navigation for replay tasks and a bounded log tail."""

import curses
import logging
import threading
import time
from collections import deque

from create_tc_task import format_elapsed


class LogBuffer(logging.Handler):
    def __init__(self, limit=2000):
        super().__init__()
        self.lines = deque(maxlen=limit)
        self.sequence = 0
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        # Handler.handle holds its lock while emit runs; readers acquire it too.
        for line in self.format(record).splitlines():
            self.sequence += 1
            self.lines.append((self.sequence, record.levelno, line))

    def snapshot(self):
        self.acquire()
        try:
            return list(self.lines)
        finally:
            self.release()


class PaneCursor:
    """Anchor a cursor by row identity as snapshots grow, shrink, or reorder."""

    def __init__(self, follow=False):
        self.keys = []
        self.selected = None
        self.top = 0
        self.follow = follow
        self.seen = 0

    def sync(self, keys, height):
        previous = self.keys.index(self.selected) if self.selected in self.keys else 0
        self.keys = list(keys)
        if self.keys:
            if self.follow:
                self.selected = self.keys[-1]
                self.seen = self.selected
            elif self.selected not in self.keys:
                self.selected = self.keys[min(previous, len(self.keys) - 1)]
        else:
            self.selected = None
        self.ensure_visible(height)

    def ensure_visible(self, height):
        index = self.keys.index(self.selected) if self.selected in self.keys else 0
        self.top = max(0, min(self.top, max(0, len(self.keys) - height)))
        if index < self.top:
            self.top = index
        elif index >= self.top + height:
            self.top = max(0, index - height + 1)

    def position_label(self, height):
        total = len(self.keys)
        if not total:
            return "[0 of 0]"
        end = min(total, self.top + height)
        label = f"[{self.top + 1}–{end} of {total}]"
        if self.top:
            label += " ↑ earlier"
        if end < total:
            label += " ↓ more"
        return label

    def move(self, command, height, logs=False):
        if not self.keys:
            return
        index = self.keys.index(self.selected)
        if command == "end":
            index = len(self.keys) - 1
        else:
            if logs:
                if self.follow:
                    self.seen = self.keys[-1]
                self.follow = False
            index = (
                0
                if command == "home"
                else index + {"up": -1, "down": 1, "page_up": -height, "page_down": height}[command]
            )
        self.selected = self.keys[max(0, min(index, len(self.keys) - 1))]
        if logs and command in ("down", "page_down", "end") and self.selected == self.keys[-1]:
            self.follow = True
            self.seen = self.keys[-1]
        self.ensure_visible(height)


class Dashboard:
    def __init__(self, args, logs, get_snapshot, stop, done):
        self.args, self.logs, self.get_snapshot = args, logs, get_snapshot
        self.stop, self.done = stop, done
        self.tasks = PaneCursor()
        self.log_cursor = PaneCursor(follow=True)
        self.order = []
        self.focus = 0
        self.colors = {}

    def run(self):
        return curses.wrapper(self.screen)

    def screen(self, screen):
        screen.keypad(True)
        screen.timeout(100)
        try:
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                for number, color in enumerate(
                    (curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_CYAN, curses.COLOR_YELLOW),
                    1,
                ):
                    curses.init_pair(number, color, -1)
                    self.colors[number] = curses.color_pair(number)
        except curses.error:
            pass
        while True:
            try:
                self.draw(screen)
                key = screen.getch()
                if key in (ord("q"), ord("Q"), 3):
                    self.stop.set()
                    if getattr(self.args, "replay_wake", None):
                        self.args.replay_wake.set()
                    return
                viewing = getattr(self.args, "view_only", False)
                if viewing and key in (ord("r"), ord("R")):
                    self.args.resume_requested = True
                    return
                if not viewing and key in (ord("s"), ord("S")):
                    self.args.submissions_enabled = not getattr(self.args, "submissions_enabled", False)
                    logging.getLogger(__name__).info(
                        "Submissions %s",
                        "enabled" if self.args.submissions_enabled else "paused",
                    )
                    if getattr(self.args, "replay_wake", None):
                        self.args.replay_wake.set()
                if not viewing and key in (ord("f"), ord("F"), ord("e"), ord("E")):
                    setting = "stop_on_full_coverage" if key in (ord("f"), ord("F")) else "exit_on_full_coverage"
                    setattr(self.args, setting, not getattr(self.args, setting, setting == "stop_on_full_coverage"))
                    logging.getLogger(__name__).info("%s: %s", setting, getattr(self.args, setting))
                    if getattr(self.args, "replay_wake", None):
                        self.args.replay_wake.set()
                if key == 9:
                    self.focus = 1 - self.focus
                height = self.dimensions(*screen.getmaxyx())[self.focus]
                commands = {
                    curses.KEY_UP: "up",
                    curses.KEY_DOWN: "down",
                    curses.KEY_PPAGE: "page_up",
                    curses.KEY_LEFT: "page_up",
                    curses.KEY_NPAGE: "page_down",
                    curses.KEY_RIGHT: "page_down",
                    curses.KEY_HOME: "home",
                    curses.KEY_END: "end",
                }
                if key in commands:
                    (self.tasks if self.focus == 0 else self.log_cursor).move(
                        commands[key],
                        height,
                        logs=self.focus == 1,
                    )
                if self.done.is_set():
                    return
            except KeyboardInterrupt:
                self.stop.set()
                if getattr(self.args, "replay_wake", None):
                    self.args.replay_wake.set()
                return

    @staticmethod
    def dimensions(height, _width):
        log_lines = min(8, max(3, (height - 8) // 3))
        return max(1, height - log_lines - 11), log_lines

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()

        def line(y, text, style=0, x=1):
            if 0 <= y < height and x < width - 1:
                try:
                    screen.addnstr(y, x, text, max(0, width - x - 1), style)
                except curses.error:
                    pass

        if height < 16 or width < 80:
            line(0, "Resize to at least 80 columns x 16 rows. Q / Ctrl+C: exit")
            screen.refresh()
            return
        snapshot = self.get_snapshot()
        task_height, log_height = self.dimensions(height, width)
        line(0, f"Replay: {self.args.from_pool} → {self.args.queue}", curses.A_BOLD)
        line(
            1,
            f"Workers: {snapshot.get('workers', 0)} | Outstanding: {snapshot.get('outstanding', 0)}/{snapshot.get('limit', 0)} | "
            f"{'★ Full coverage' if snapshot.get('full_coverage') else 'Coverage'}: "
            f"{snapshot.get('passed', 0)}/{snapshot.get('eligible', 0)} eligible types | {snapshot.get('excluded', 0)} excluded",
            self.colors.get(1, 0) | curses.A_BOLD if snapshot.get("full_coverage") else 0,
        )
        by_key = {row["key"]: row for row in snapshot.get("rows", [])}
        # Snapshots already prioritize active and covered workloads. Preserve the
        # cursor's workload identity, rather than freezing the rows' initial order.
        self.order = list(by_key)
        self.tasks.sync(self.order, task_height)
        task_style = self.colors.get(3, 0) | curses.A_BOLD if self.focus == 0 else curses.A_DIM
        task_header = f"━━━ Tasks {self.tasks.position_label(task_height)}"
        line(2, task_header + " " + "━" * max(0, width - len(task_header) - 3), task_style)
        name_width = max(12, width - 58)
        line(
            3,
            f"  {'Workload':<{name_width}} {'Coverage':<15} {'Pass':>4} {'Try':>4} {'Status':<12} Elapsed",
            curses.A_UNDERLINE,
        )
        for y, key in enumerate(self.order[self.tasks.top : self.tasks.top + task_height], 4):
            row = by_key[key]
            color = {"passed": 1, "unsuccessful": 2, "awaiting result": 3}.get(row["coverage"], 0)
            style = self.colors.get(color, 0)
            if key == self.tasks.selected:
                style |= curses.A_REVERSE if self.focus == 0 else curses.A_BOLD
            elapsed = row["elapsed"]
            if (
                not getattr(self.args, "view_only", False)
                and row["status"] in ("running", "queued", "waiting", "uncertain")
                and elapsed not in ("-", "unknown")
            ):
                hours, minutes, seconds = map(int, elapsed.split(":"))
                elapsed = format_elapsed(
                    hours * 3600 + minutes * 60 + seconds + max(0, time.time() - row["observed_at"]),
                )
            name = row["name"]
            if len(name) > name_width:
                name = name[: name_width - 1] + "…"
            line(
                y,
                f"{'›' if key == self.tasks.selected else ' '} {name:<{name_width}} {row['coverage']:<15} {row['successes']:>4} {row['attempts']:>4} {row['status']:<12.12} {elapsed}",
                style,
            )
        logs = self.logs.snapshot()
        self.log_cursor.sync([entry[0] for entry in logs], log_height)
        new = max(0, logs[-1][0] - self.log_cursor.seen) if logs and not self.log_cursor.follow else 0
        log_start = 4 + task_height
        log_style = self.colors.get(3, 0) | curses.A_BOLD if self.focus == 1 else curses.A_DIM
        label = "following" if self.log_cursor.follow else f"paused · {new} new lines · End to follow"
        log_header = f"━━━ Logs {self.log_cursor.position_label(log_height)} · {label}"
        line(log_start, log_header + " " + "━" * max(0, width - len(log_header) - 3), log_style)
        for y, (sequence, level, text) in enumerate(
            logs[self.log_cursor.top : self.log_cursor.top + log_height],
            log_start + 1,
        ):
            style = self.colors.get(2 if level >= logging.ERROR else 4 if level >= logging.WARNING else 0, 0)
            if sequence == self.log_cursor.selected and self.focus == 1:
                style |= curses.A_REVERSE
            line(y, f"{'›' if sequence == self.log_cursor.selected else ' '} {text}", style)
        pause = "on" if getattr(self.args, "stop_on_full_coverage", True) else "off"
        exiting = "on" if getattr(self.args, "exit_on_full_coverage", False) else "off"
        viewing = getattr(self.args, "view_only", False)
        line(height - 6, f"Session: {getattr(self.args, 'session', '-')}", curses.A_DIM)
        line(height - 5, getattr(self.args, "session_warning", ""), self.colors.get(4, 0) | curses.A_BOLD)
        submissions = "on" if getattr(self.args, "submissions_enabled", False) else "off"
        line(
            height - 4,
            (
                f"VIEW ONLY — saved sample: {snapshot.get('sampled_at') or 'never'} | R: resume monitoring"
                if viewing
                else f"S: Submit [{submissions}] | F: Pause covered [{pause}] | E: Exit covered [{exiting}] | "
                f"{'PAUSED' if snapshot.get('paused') or submissions == 'off' else 'Scheduling'}"
            ),
            curses.A_BOLD,
        )
        line(height - 3, snapshot.get("message", "Starting"), self.colors.get(3, 0))
        report_path = (
            self.args.session_info.report_path
            if getattr(self.args, "session_info", None)
            else getattr(self.args, "report", None) or "initializing"
        )
        line(height - 2, f"Report: {report_path}", curses.A_DIM)
        line(height - 1, "Tab: pane | ↑/↓: row | PgUp/← PgDn/→: screen | Home/End | Q/Ctrl+C: stop", curses.A_DIM)
        screen.refresh()


def run_dashboard(args, logs, worker):
    """Run API work off the UI thread; the worker owns its clients and SQLite connection."""
    stop, done = threading.Event(), threading.Event()
    args.replay_wake = threading.Event()
    lock = threading.Lock()
    current = {}
    result = [0]

    def publish(snapshot):
        nonlocal current
        with lock:
            current = snapshot

    def get_snapshot():
        with lock:
            return current

    def work():
        try:
            result[0] = worker(stop, publish)
        except Exception:
            logging.getLogger(__name__).exception("Controller stopped unexpectedly")
            result[0] = 1
        finally:
            done.set()

    thread = threading.Thread(target=work, name="replay-scheduler", daemon=True)
    thread.start()
    ui_failed = False
    try:
        Dashboard(args, logs, get_snapshot, stop, done).run()
    except curses.error as exc:
        logging.getLogger(__name__).error("Cannot open fullscreen dashboard: %s; use --output log", exc)
        ui_failed = True
    finally:
        stop.set()
        args.replay_wake.set()
        thread.join(timeout=1)
        if not done.is_set() and getattr(args, "replay_report", None):
            try:
                args.replay_report.finish("manual_stop")
                args.replay_exit_summary = f"Stopped scheduling; submitted tasks keep running. Report: {args.report}"
            except OSError as exc:
                logging.getLogger(__name__).error("Could not finalize coverage report %s: %s", args.report, exc)
                ui_failed = True
    return 1 if ui_failed else result[0]
