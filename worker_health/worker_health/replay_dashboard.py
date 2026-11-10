"""Fullscreen keyboard navigation for replay tasks and a bounded log tail."""

import curses
import logging
import threading
import time
import webbrowser
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from textwrap import wrap

from create_tc_task import format_elapsed

from worker_health.replay_permissions import ARTIFACT_NOTE, permission_summary
from worker_health.replay_session_info import current_invocation, report_path, started_label


class LogBuffer(logging.Handler):
    def __init__(self, limit=2000):
        super().__init__()
        self.lines = deque(maxlen=limit)
        self.sequence = 0
        self.last_summary = None
        self.last_summary_at = None
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        # Handler.handle holds its lock while emit runs; readers acquire it too.
        summary = getattr(record, "replay_summary", None)
        if summary is not None:
            now = time.monotonic()
            if summary == self.last_summary and self.last_summary_at is not None and now - self.last_summary_at < 600:
                return
            self.last_summary = summary
            self.last_summary_at = now
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
        self.permissions_open = False
        self.full_setup = False
        self.permission_top = 0
        self.permission_limit = 0
        self.recheck_pending = False
        self.recheck_previous = None
        self.client_notice = ""
        self.info_open = False
        self.info_top = 0
        self.info_limit = 0
        self.report_notice = ""

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
                for number, foreground, background in (
                    (5, curses.COLOR_BLACK, curses.COLOR_CYAN),
                    (6, curses.COLOR_BLACK, curses.COLOR_YELLOW),
                ):
                    curses.init_pair(number, foreground, background)
                    self.colors[number] = curses.color_pair(number)
        except curses.error:
            pass
        while True:
            try:
                self.draw(screen)
                key = screen.getch()
                if self.info_open:
                    if key in (ord("i"), ord("I"), ord("q"), ord("Q"), 27):
                        self.info_open = False
                    elif key in (ord("o"), ord("O")):
                        path = report_path(self.args, self.get_snapshot())
                        if path:
                            threading.Thread(target=self.open_report, args=(path,), daemon=True).start()
                    else:
                        page = max(1, screen.getmaxyx()[0] - 5)
                        moves = {
                            curses.KEY_UP: -1,
                            curses.KEY_DOWN: 1,
                            curses.KEY_PPAGE: -page,
                            curses.KEY_LEFT: -page,
                            curses.KEY_NPAGE: page,
                            curses.KEY_RIGHT: page,
                        }
                        if key == curses.KEY_HOME:
                            self.info_top = 0
                        elif key == curses.KEY_END:
                            self.info_top = self.info_limit
                        elif key in moves:
                            self.info_top = max(0, min(self.info_limit, self.info_top + moves[key]))
                    if key != 3:
                        if self.done.is_set():
                            return
                        continue
                if self.permissions_open:
                    if key in (ord("p"), ord("P"), ord("q"), ord("Q"), 27):
                        self.permissions_open = False
                    elif key in (ord("a"), ord("A")):
                        self.full_setup = not self.full_setup
                        self.permission_top = 0
                    elif key in (ord("o"), ord("O")):
                        url = self.permissions(self.get_snapshot())["client_url"]
                        threading.Thread(target=self.open_client, args=(url,), daemon=True).start()
                    elif key in (ord("r"), ord("R")) and not getattr(self.args, "view_only", False):
                        self.recheck_previous = self.permissions(self.get_snapshot()).get("checked_at")
                        self.recheck_pending = True
                        if getattr(self.args, "replay_wake", None):
                            self.args.replay_wake.set()
                    elif key in (
                        curses.KEY_UP,
                        curses.KEY_DOWN,
                        curses.KEY_PPAGE,
                        curses.KEY_NPAGE,
                        curses.KEY_LEFT,
                        curses.KEY_RIGHT,
                        curses.KEY_HOME,
                        curses.KEY_END,
                    ):
                        page = max(1, screen.getmaxyx()[0] - 5)
                        if key == curses.KEY_HOME:
                            self.permission_top = 0
                        elif key == curses.KEY_END:
                            self.permission_top = self.permission_limit
                        else:
                            delta = (
                                -1
                                if key == curses.KEY_UP
                                else 1
                                if key == curses.KEY_DOWN
                                else (-page if key in (curses.KEY_PPAGE, curses.KEY_LEFT) else page)
                            )
                            self.permission_top = max(0, min(self.permission_limit, self.permission_top + delta))
                    if key != 3:
                        if self.done.is_set():
                            return
                        continue
                if key in (ord("q"), ord("Q"), 3):
                    self.stop.set()
                    if getattr(self.args, "replay_wake", None):
                        self.args.replay_wake.set()
                    return
                if key in (ord("p"), ord("P")):
                    self.permissions_open = True
                    self.permission_top = 0
                    continue
                if key in (ord("i"), ord("I")):
                    self.info_open = True
                    self.info_top = 0
                    continue
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
                if not viewing and key == ord("+"):
                    self.args.successes_per_type = (getattr(self.args, "successes_per_type", None) or 1) + 1
                    logging.getLogger(__name__).info(
                        "Success target raised to %s per type",
                        self.args.successes_per_type,
                    )
                    if getattr(self.args, "replay_wake", None):
                        self.args.replay_wake.set()
                if key == 9:
                    self.focus = 1 - self.focus
                rows, columns = screen.getmaxyx()
                height = self.dimensions(rows, columns, len(self.pool_header(columns)))[self.focus]
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
    def dimensions(height, _width, header_rows=1):
        log_lines = min(8, max(3, (height - 8) // 3))
        log_lines = min(log_lines, max(1, height - header_rows - 10))
        return max(1, height - log_lines - 10 - (header_rows - 1)), log_lines

    def pool_header(self, width):
        title = f"Replay: {self.args.from_pool} → {self.args.queue}"
        if len(title) <= width - 2:
            return [title]
        return [
            part
            for text in (f"Source: {self.args.from_pool}", f"Destination: {self.args.queue}")
            for part in wrap(text, width - 2, break_on_hyphens=False)
        ]

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
        if self.info_open:
            self.draw_info(screen, snapshot, line, height, width)
            return
        if self.permissions_open:
            self.draw_permissions(screen, snapshot, line, height, width)
            return
        header = self.pool_header(width)
        if height < len(header) + 11:
            line(0, f"Resize to at least {len(header) + 11} rows to show full pool names. Q: exit")
            screen.refresh()
            return
        task_height, log_height = self.dimensions(height, width, len(header))
        for y, text in enumerate(header):
            line(y, text, curses.A_BOLD)
        line(
            len(header),
            f"Workers: {snapshot.get('workers', 0)} | Outstanding: {snapshot.get('outstanding', 0)}/{snapshot.get('limit', 0)} | "
            f"{'★ Full coverage' if snapshot.get('full_coverage') else 'Coverage'}: "
            f"{snapshot.get('passed', 0)}/{snapshot.get('eligible', 0)} eligible types | Target: {snapshot.get('successes_per_type', 1)} successes/type | {snapshot.get('excluded', 0)} excluded",
            self.colors.get(1, 0) | curses.A_BOLD if snapshot.get("full_coverage") else 0,
        )
        by_key = {row["key"]: row for row in snapshot.get("rows", [])}
        # Snapshots already prioritize active and covered workloads. Preserve the
        # cursor's workload identity, rather than freezing the rows' initial order.
        self.order = list(by_key)
        self.tasks.sync(self.order, task_height)
        task_style = self.colors.get(3, 0) | curses.A_BOLD if self.focus == 0 else curses.A_DIM
        task_header = f"━━━ Tasks {self.tasks.position_label(task_height)}"
        line(len(header) + 1, task_header + " " + "━" * max(0, width - len(task_header) - 3), task_style)
        name_width = max(12, width - 58)
        line(
            len(header) + 2,
            f"  {'Workload':<{name_width}} {'Coverage':<15} {'Pass':>4} {'Try':>4} {'Status':<12} Elapsed",
            curses.A_UNDERLINE,
        )
        for y, key in enumerate(self.order[self.tasks.top : self.tasks.top + task_height], len(header) + 3):
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
        log_start = len(header) + 3 + task_height
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
        line(height - 5, f"Started: {started_label(self.args, snapshot)} | I: Session info", self.colors.get(3, 0))
        selected_status = by_key.get(self.tasks.selected, {}).get("status", "") if self.focus == 0 else ""
        detail = selected_status if len(selected_status) > 12 else ""
        activity = self.activity_label(snapshot)
        line(height - 2, activity, self.colors.get(3, 0))
        detail = getattr(self.args, "session_warning", "") or detail
        if detail:
            line(height - 2, " | " + detail, self.colors.get(4, 0), x=len(activity) + 1)
        self.draw_action_bar(screen, snapshot, height - 4, width)
        blocked = self.permissions(snapshot)["blocked_count"]
        message = snapshot.get("message", "Starting")
        if blocked:
            message = f"{blocked} workloads blocked by missing scopes — P: permissions | {message}"
        line(height - 3, message, self.colors.get(3, 0))
        line(height - 1, "I: info | P: permissions | Tab: pane | ↑/↓ PgUp/PgDn Home/End | Q/Ctrl+C: stop", curses.A_DIM)
        screen.refresh()

    def activity_label(self, snapshot):
        sampled = snapshot.get("sampled_at")
        checked = "not yet completed"
        if sampled:
            try:
                checked = datetime.fromisoformat(sampled).astimezone(UTC).strftime("%H:%M:%S UTC")
            except ValueError:
                checked = str(sampled)
        if getattr(self.args, "view_only", False):
            mode = "VIEW ONLY"
        else:
            frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 2) % 10]
            mode = f"{frame} MONITORING"
        return f"{mode} | Last check: {checked}"

    def open_report(self, path):
        try:
            opened = webbrowser.open(Path(path).expanduser().absolute().as_uri())
        except (OSError, webbrowser.Error):
            opened = False
        self.report_notice = "Report opened." if opened else "Could not open report; use the full path below."

    def draw_info(self, screen, snapshot, line, height, width):
        heading = self.colors.get(3, 0) | curses.A_BOLD
        line(0, "Session info", heading)
        line(1, f"Started: {started_label(self.args, snapshot)}")
        line(2, self.report_notice, self.colors.get(4, 0))
        session = getattr(self.args, "session_info", None)
        session_path = str(session.path) if session else getattr(self.args, "session", None)
        content = [
            ("Mode", heading),
            ("View only" if getattr(self.args, "view_only", False) else "Live monitoring", 0),
            ("", 0),
            ("Source pool", heading),
            (self.args.from_pool, 0),
            ("", 0),
            ("Destination pool", heading),
            (self.args.queue, 0),
            ("", 0),
            ("Session directory", heading),
            (session_path or "Not available", 0),
            ("", 0),
            ("Report — O: open", heading),
            (str(report_path(self.args, snapshot) or "Initializing"), 0),
            ("", 0),
            ("Invocation with current settings", heading),
            (current_invocation(self.args, snapshot), 0),
            ("", 0),
            (
                "Run from the worker_health project directory. Wrapped text continues without omitted characters.",
                curses.A_DIM,
            ),
        ]
        lines = [
            (part, style)
            for text, style in content
            for part in (
                wrap(text, width - 3, replace_whitespace=False, drop_whitespace=False, break_on_hyphens=False) or [""]
            )
        ]
        available = max(1, height - 5)
        self.info_limit = max(0, len(lines) - available)
        self.info_top = min(self.info_top, self.info_limit)
        for y, (text, style) in enumerate(lines[self.info_top : self.info_top + available], 3):
            line(y, text, style)
        line(
            height - 2,
            f"Lines {self.info_top + 1}–{min(len(lines), self.info_top + available)} of {len(lines)}",
            curses.A_DIM,
        )
        line(height - 1, "I/Esc/Q: back | O: open report | ↑/↓ PgUp/PgDn Home/End: scroll", curses.A_BOLD)
        screen.refresh()

    def draw_action_bar(self, screen, snapshot, y, width):
        viewing = getattr(self.args, "view_only", False)
        style = self.colors.get(6 if viewing else 5, curses.A_REVERSE)
        if viewing:
            segments = [
                ("VIEW ONLY | R: Resume monitoring", True),
                (f" | Saved sample: {snapshot.get('sampled_at') or 'never'}", False),
            ]
        else:
            enabled = getattr(self.args, "submissions_enabled", False)
            settings = (
                ("S: Submit", enabled),
                ("F: Pause covered" if width >= 110 else "F: Pause", getattr(self.args, "stop_on_full_coverage", True)),
                ("E: Exit covered" if width >= 110 else "E: Exit", getattr(self.args, "exit_on_full_coverage", False)),
            )
            segments = [("+: Raise target | ", False)]
            for label, on in settings:
                if segments:
                    segments.append((" | ", False))
                segments.append((f"{label} [{'on' if on else 'off'}]", on))
            segments.append((f" | SUBMISSIONS {'ENABLED' if enabled else 'PAUSED'}", True))
        try:
            screen.addnstr(y, 0, " " * width, width, style)
            x = 1
            for text, bold in segments:
                remaining = width - x - 1
                if remaining <= 0:
                    break
                screen.addnstr(y, x, text, remaining, style | (curses.A_BOLD if bold else 0))
                x += len(text)
        except curses.error:
            pass

    def permissions(self, snapshot):
        return snapshot.get("permissions") or permission_summary(snapshot.get("rows", []), self.args.queue)

    def open_client(self, url):
        try:
            opened = webbrowser.open(url)
        except (OSError, webbrowser.Error):
            opened = False
        self.client_notice = (
            "Client page opened in your browser." if opened else "Could not open browser; copy the URL below."
        )

    def draw_permissions(self, screen, snapshot, line, height, width):
        permissions = self.permissions(snapshot)
        if self.recheck_pending and permissions.get("checked_at") != self.recheck_previous:
            self.recheck_pending = False
        heading = self.colors.get(3, 0) | curses.A_BOLD
        grant_style = self.colors.get(1, 0)
        missing_style = self.colors.get(2, 0) | curses.A_BOLD
        warning_style = self.colors.get(4, 0)
        link_style = self.colors.get(3, 0) | curses.A_UNDERLINE
        line(
            0,
            f"Permissions — {permissions['blocked_count']} workloads blocked by missing scopes",
            missing_style if permissions["blocked_count"] else grant_style | curses.A_BOLD,
        )
        viewing = getattr(self.args, "view_only", False)
        status = (
            "Saved permissions; resume monitoring to recheck."
            if viewing
            else "Rechecking permissions…"
            if self.recheck_pending
            else f"Last checked: {permissions.get('checked_at') or 'not yet checked'}"
        )
        if not viewing and snapshot.get("message", "").startswith("Cycle paused:"):
            status = snapshot["message"]
            self.recheck_pending = False
        line(1, status, warning_style if self.recheck_pending else curses.A_DIM)
        line(2, self.client_notice, warning_style)
        report_path = snapshot.get("report_path") or getattr(self.args, "report", None)
        if not report_path and getattr(self.args, "session_info", None):
            report_path = self.args.session_info.report_path
        content = [
            ("Firefox CI client page — O: open in browser", heading),
            (permissions["client_url"], link_style),
            ("", 0),
            (
                "Full recommended setup (includes grants not currently missing):"
                if self.full_setup
                else "Suggested grants for currently blocked workloads:",
                heading,
            ),
        ]
        scopes = permissions["recommended_scopes"] if self.full_setup else permissions["suggested_scopes"]
        content.extend(
            (scope, grant_style) for scope in (scopes or ["No additional grants needed for observed workloads."])
        )
        if any(scope.startswith("queue:get-artifact:project/gecko/") for scope in scopes):
            content.extend([("", 0), (ARTIFACT_NOTE, warning_style)])
        content.extend(
            [
                ("", 0),
                ("Suggestions use known level-1 secret/cache prefixes; unfamiliar scopes stay exact.", curses.A_DIM),
            ],
        )
        if report_path:
            content.extend(
                [
                    ("Copy currently needed grants from:", heading),
                    (str(Path(report_path).with_name("suggested-scopes.txt")), link_style),
                ],
            )
        content.extend([("", 0), ("Exact missing scopes and affected workloads:", heading)])
        for scope in permissions["missing_scopes"]:
            content.append((scope, missing_style))
            content.extend(
                ("  " + row["name"], curses.A_DIM) for row in permissions["workloads"] if scope in row["missing_scopes"]
            )
        if not permissions["missing_scopes"]:
            content.append(("None.", grant_style))
        lines = [
            (part, style)
            for item, style in content
            for part in (wrap(item, max(1, width - 3), replace_whitespace=False) or [""])
        ]
        available = max(1, height - 5)
        self.permission_limit = max(0, len(lines) - available)
        self.permission_top = min(self.permission_top, self.permission_limit)
        for y, (text, style) in enumerate(lines[self.permission_top : self.permission_top + available], 3):
            line(y, text, style)
        line(
            height - 2,
            f"Lines {self.permission_top + 1}–{min(len(lines), self.permission_top + available)} of {len(lines)}",
            curses.A_DIM,
        )
        line(
            height - 1,
            "P/Esc/Q: back | O: client page | A: needed/full | ↑/↓ PgUp/PgDn | "
            + ("R: recheck" if not viewing else "saved view"),
            curses.A_BOLD,
        )
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
