"""Local, read-only session picker with explicit view/resume/new actions."""

import curses
from argparse import ArgumentTypeError
from dataclasses import dataclass

from replay_tc_task import queue_argument

from worker_health.replay_sessions import list_sessions


@dataclass
class SessionChoice:
    action: str
    path: str | None = None
    source: str | None = None
    destination: str | None = None


def pick_session():
    return curses.wrapper(session_screen, list_sessions())


def session_screen(screen, sessions):
    screen.keypad(True)
    selected = 0
    warning_style = curses.A_BOLD
    recent_style = curses.A_BOLD
    try:
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            recent_style |= curses.color_pair(1)
            warning_style |= curses.color_pair(2)
    except curses.error:
        pass

    def line(y, value, style=0):
        height, width = screen.getmaxyx()
        if 0 <= y < height - 1:
            try:
                screen.addnstr(y, 1, value, max(0, width - 2), style)
            except curses.error:
                pass

    while True:
        screen.erase()
        height, _width = screen.getmaxyx()
        line(0, "Replay sessions — newest sample first", curses.A_BOLD)
        line(1, "Enter: view | R: resume monitoring | N: new session | Q: quit")
        visible = max(1, (height - 8) // 3)
        top = max(0, selected - visible + 1)
        if not sessions:
            line(3, "No saved sessions. N: create a session.")
        for index in range(top, min(len(sessions), top + visible)):
            session = sessions[index]
            data = session.report()
            style = curses.A_REVERSE if index == selected else 0
            if index == 0:
                style |= recent_style
            y = 3 + (index - top) * 3
            warnings = session.warnings()
            line(y, f"{'›' if index == selected else ' '} {session.source} → {session.destination}", style)
            line(
                y + 1,
                f"Sample: {data.get('sampled_at') or 'never'} | Started: {session.started_at} | "
                f"Coverage: {data.get('passed', 0)}/{data.get('eligible', 0)}",
                style,
            )
            line(
                y + 2,
                "⚠ " + " | ".join(warnings)
                if warnings
                else (
                    ("Most recently sampled session" if data.get("sampled_at") else "Not yet sampled")
                    if index == 0
                    else ""
                ),
                warning_style if warnings else curses.A_DIM,
            )
        line(height - 4, "Viewing never contacts Taskcluster or changes the saved report.")
        line(height - 3, "Resuming updates this session; submissions start paused.")
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 3, 27):
            return None
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            selected = max(0, min(len(sessions) - 1, selected + (1 if key == curses.KEY_DOWN else -1)))
        if sessions and key in (10, 13, curses.KEY_ENTER, ord("r"), ord("R")):
            action = "resume" if key in (ord("r"), ord("R")) else "view"
            return SessionChoice(action, path=str(sessions[selected].path))
        if key in (ord("n"), ord("N")):
            choice = prompt_new_session(screen, sessions[selected] if sessions else None)
            if choice:
                return choice


def prompt_new_session(screen, previous=None):
    """Read pools locally; no discovery or task submission in the picker."""
    error = ""
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        values = []
        try:
            curses.echo()
            for index, (label, default) in enumerate(
                (
                    ("Source pool", previous.source if previous else ""),
                    ("Destination pool", previous.destination if previous else ""),
                ),
            ):
                prompt = f"{label}{' [' + default + ']' if default else ''}: "
                screen.addnstr(index * 2 + 1, 1, prompt, max(0, width - 2))
                screen.refresh()
                raw = screen.getstr(index * 2 + 2, 1, max(1, width - 3)).decode("utf-8").strip()
                if raw == "\x1b":
                    return None
                values.append(queue_argument(raw or default))
            if values[0] == values[1]:
                raise ValueError("Source and destination pools must differ")
            return SessionChoice("new", source=values[0], destination=values[1])
        except (ValueError, UnicodeError, ArgumentTypeError) as exc:
            error = str(exc)
            screen.addnstr(min(6, height - 2), 1, error + " — press any key to retry", max(0, width - 2))
            screen.getch()
        except (KeyboardInterrupt, curses.error):
            return None
        finally:
            curses.noecho()
