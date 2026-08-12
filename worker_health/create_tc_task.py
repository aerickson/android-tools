#!/usr/bin/env python3

"""
create_tc_task.py

Creating a TC token:
  1. Go to URL and create a token:
    https://firefox-ci-tc.services.mozilla.com/auth/clients

    with the following scopes:
      queue:create-task:*
      queue:quarantine-worker:*
      queue:scheduler-id:*
      queue:pending-count:*
      queue:claimed-count:*

  2. Place the ~/.tc_token file with contents similar to:
    {
        "clientId": "mozilla-auth0/ad|Mozilla-LDAP|...",
        "accessToken": "<ACCESS_TOKEN>"
    }

"""

import argparse
import os
import json
import rstr
import shlex
import time
import logging
import sys
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
import datetime

import alive_progress
import taskcluster
import math
from rich.table import Table

DEFAULT_COMMAND_TIMEOUT = 70
DEFAULT_COMMAND_TIMEOUT_BUFFER = 10
DEFAULT_DOCKER_IMAGE = "ubuntu:24.04"
BITBAR_DASHBOARD_URL = "https://mozilla-v3.bitbar.com//#testing/device-session"
BITBAR_SCRIPTVARS_PATH = "/builds/taskcluster/scriptvars.json"


def default_bash_command(command_timeout_seconds):
    duration = max(1, command_timeout_seconds - DEFAULT_COMMAND_TIMEOUT_BUFFER)
    return f'for ((i=1;i<={duration};i++)); do echo "$i"; sleep 1; done'


def prepend_bitbar_dashboard_link(queue, bash_command):
    """Print the Bitbar dashboard URL before commands run on Bitbar workers."""
    if "bitbar" not in queue.lower():
        return bash_command

    testdroid_ids = (
        "$(python3 -c 'import json; "
        f'values=json.load(open("{BITBAR_SCRIPTVARS_PATH}")); '
        'print("/".join(values[f"TESTDROID_{key}_ID"] for key in ("PROJECT", "BUILD", "RUN")))'
        "')"
    )
    dashboard_url = f"{BITBAR_DASHBOARD_URL}/{testdroid_ids}"
    return f'echo "Bitbar test run: {dashboard_url}"; {bash_command}'


def build_payload(payload_format, bash_command, command_timeout_seconds, env, docker_image):
    if payload_format == "docker-worker":
        return {
            "image": docker_image,
            "command": ["/bin/bash", "-lc", bash_command],
            "maxRunTime": command_timeout_seconds,
            **({"env": env} if env else {}),
        }

    return {
        "command": [["/bin/bash", "-c", f"mkdir -p out; {bash_command}"]],
        "maxRunTime": command_timeout_seconds,
        "artifacts": [
            {
                "type": "directory",
                "name": "public/out",
                "path": "out",
            },
        ],
        **({"env": env} if env else {}),
    }


class TCClient:
    def __init__(
        self,
        queue,
        dry_run=False,
        bash_command=None,
        command_timeout_seconds=DEFAULT_COMMAND_TIMEOUT,
        requests_timeout=60,
        env=None,
        payload_format="generic-worker",
        docker_image=None,
    ):
        self.root_url = "https://firefox-ci-tc.services.mozilla.com"
        try:
            with open(os.path.expanduser("~/.tc_token")) as json_file:
                data = json.load(json_file)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Error reading ~/.tc_token: {e}")
        try:
            creds = {"clientId": data["clientId"], "accessToken": data["accessToken"]}
        except KeyError as e:
            raise RuntimeError(f"Missing key in ~/.tc_token: {e}")
        self.queue = queue
        self.dry_run = dry_run
        self.command_timeout_seconds = command_timeout_seconds
        self.bash_command = prepend_bitbar_dashboard_link(
            queue,
            bash_command or default_bash_command(command_timeout_seconds),
        )
        self.env = env or {}
        self.payload_format = payload_format
        self.docker_image = docker_image
        self.queue_object = taskcluster.Queue(
            {"rootUrl": self.root_url, "credentials": creds, "timeout": requests_timeout},
        )

    def create_task(self):
        # prepare args
        user = os.environ.get("USER")
        # format: "2025-08-22T18:18:05.351Z"
        datetime_string_format = "%Y-%m-%dT%H:%M:%S.000Z"
        current_time = time.strftime(datetime_string_format, time.gmtime())
        three_hours_from_now = time.strftime(datetime_string_format, time.gmtime(time.time() + 3 * 60 * 60))
        task_id = gen_task_id()

        create_task_args = {
            "taskQueueId": self.queue,
            # "schedulerId": "taskcluster-ui",
            "created": current_time,
            "deadline": three_hours_from_now,
            "payload": build_payload(
                self.payload_format,
                self.bash_command,
                self.command_timeout_seconds,
                self.env,
                self.docker_image,
            ),
            "metadata": {
                "name": "test-task",
                "description": "An **example** test task",
                "owner": f"{user}@mozilla.com",
                "source": "http://github.com/mozilla-platform-ops/android-tools",
            },
        }
        if not self.dry_run:
            try:
                self.queue_object.createTask(task_id, create_task_args)
                logging.info(f"Task created successfully (https://firefox-ci-tc.services.mozilla.com/tasks/{task_id}).")
            except Exception as e:
                logging.error(f"Failed to create task: {e}")
        else:
            time.sleep(0.5)
            logging.info(f"[Dry Run] Task ID would be: {task_id}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create a Taskcluster task using the Taskcluster Python client library.",
    )
    parser.add_argument(
        "--queue",
        "-q",
        required=True,
        help="Taskcluster queue id (e.g. proj-autophone/gecko-t-bitbar-gw-test-2)",
    )
    parser.add_argument("--count", "-c", type=int, default=1, help="Number of tasks to create (default: 1)")
    cmd_group = parser.add_mutually_exclusive_group()
    cmd_group.add_argument(
        "--bash-command",
        "-b",
        default=None,
        help=(
            "Command to run in the task "
            f"(default: print once per second for --command-timeout minus {DEFAULT_COMMAND_TIMEOUT_BUFFER}s)"
        ),
    )
    cmd_group.add_argument(
        "--script-file",
        "-s",
        metavar="FILE",
        help="Shell script file to use as the task command (mutually exclusive with --bash-command)",
    )
    parser.add_argument(
        "--script-args",
        metavar="ARGS",
        help="Shell-style arguments to append to --script-file (for example: '--configuration bitbar-docker-with-robustcheckout').",
    )
    parser.add_argument(
        "--command-timeout",
        "-t",
        type=int,
        default=DEFAULT_COMMAND_TIMEOUT,
        help=f"Command timeout in seconds (default: {DEFAULT_COMMAND_TIMEOUT})",
    )
    parser.add_argument(
        "--payload-format",
        choices=("generic-worker", "docker-worker"),
        default="generic-worker",
        help="Task payload schema (default: generic-worker)",
    )
    parser.add_argument(
        "--docker-image",
        default=None,
        help=f"Docker image for --payload-format docker-worker (default: {DEFAULT_DOCKER_IMAGE})",
    )
    parser.add_argument(
        "--dry-run",
        "-D",
        action="store_true",
        help="Simulate task creation without actually creating tasks",
    )
    parser.add_argument(
        "--continuous-mode",
        "-C",
        action="store_true",
        help="Enable continuous mode (ensures a continuous load in a queue by monitoring and launching more jobs when the queue is below the limit).",
    )
    parser.add_argument(
        "--continuous-mode-limit",
        "-L",
        type=int,
        default=30,
        help="The minimum number of jobs to keep in the queue (default: 30)",
    )
    parser.add_argument(
        "--requests-timeout",
        "-R",
        type=int,
        default=60,
        help="Requests timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--env",
        "-e",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Set a payload env var (repeatable). Example: --env FLEETBENCH_ARGS='adb --serial X --json'",
    )
    parser.add_argument(
        "--continuous-mode-check-interval",
        "-I",
        type=int,
        default=15,
        help="Continuous mode check interval in seconds (default: 15)",
    )
    return parser.parse_args()


# generate strings that match regex
def gen_task_id():
    regex = r"^[A-Za-z0-9_-]{8}[Q-T][A-Za-z0-9_-][CGKOSWaeimquy26-][A-Za-z0-9_-]{10}[AQgw]$"
    return rstr.xeger(regex)


def is_interactive_terminal():
    return sys.stdout.isatty()


class UILogHandler(logging.Handler):
    def __init__(self, buffer, layout, bar_size=3):
        super().__init__()
        self.buffer = buffer
        self.layout = layout
        self.bar_size = bar_size

    def get_max_lines(self):
        # Get the current terminal height and subtract bar size
        import shutil

        height = shutil.get_terminal_size((80, 24)).lines
        return max(1, height - self.bar_size)

    def emit(self, record):
        msg = self.format(record)
        self.buffer.append(msg)
        max_lines = self.get_max_lines()
        if len(self.buffer) > max_lines:
            del self.buffer[0 : len(self.buffer) - max_lines]


def run_curses_mode(main_func, *args, **kwargs):
    layout = Layout()
    layout.split_column(
        Layout(name="main", ratio=1),
        Layout(name="bar", size=3),
    )

    import threading
    import traceback

    log_buffer = []
    log_handler = UILogHandler(log_buffer, layout, bar_size=3)
    log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

    # Remove default StreamHandler(s) to prevent duplicate output
    for handler in root_logger.handlers[:]:
        if isinstance(handler, logging.StreamHandler):
            root_logger.removeHandler(handler)

    stop_flag = threading.Event()
    thread_exception = [None]

    # --- Add state for bar ---
    bar_state = {
        "queue": getattr(args[0], "queue", None) if args else None,
        "pending": None,
        "last_check": None,
        "next_check": None,
        "last_created": None,  # <-- NEW
    }

    def human_delta(dt):
        # dt: seconds
        if dt is None:
            return "--"
        if dt < 60:
            return f"{int(dt)}s"
        elif dt < 3600:
            return f"{int(dt // 60)}m {int(dt % 60)}s"
        else:
            return f"{int(dt // 3600)}h {int((dt % 3600) // 60)}m"

    # TODO: show when queue last did work (any worker ran a task)
    def update_bar():
        now = datetime.datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        queue = bar_state.get("queue", "--")
        pending = bar_state.get("pending", "--")
        last_check = bar_state.get("last_check", None)
        next_check = bar_state.get("next_check", None)
        last_created = bar_state.get("last_created", None)
        last_check_str = human_delta((now - last_check).total_seconds()) if last_check else "--"
        next_check_str = human_delta((next_check - now).total_seconds()) if next_check else "--"
        last_created_str = human_delta((now - last_created).total_seconds()) if last_created else "--"

        left_text = f"[black][bold]{os.path.basename(__file__)}[/bold] | {time_str}[/black]"
        right_text = (
            f"[black] {queue} | "
            f"PENDING: {pending} | "
            f"LAST_CHECK: {last_check_str} ago | "
            f"NEXT_CHECK: in {next_check_str} | "
            f"LAST_CREATED: {last_created_str} ago[/black]"
        )

        table = Table.grid(expand=True)
        table.add_column(justify="left")
        table.add_column(justify="right")
        table.add_row(left_text, right_text)

        layout["bar"].update(Panel(table, style="on blue"))

    def update_main():
        layout["main"].update(Panel(Text("\n".join(log_buffer), justify="left")))

    def run_main():
        try:
            # Pass stop_flag to main_func
            main_func(*args, layout["main"], bar_state, stop_flag=stop_flag, **kwargs)
        except KeyboardInterrupt:
            stop_flag.set()
        except Exception:
            exc_type, exc_value, exc_tb = sys.exc_info()
            thread_exception[0] = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        finally:
            stop_flag.set()

    t = threading.Thread(target=run_main)
    t.start()
    with Live(layout, refresh_per_second=2, screen=True):
        try:
            while not stop_flag.is_set():
                update_bar()
                update_main()
                time.sleep(1)
        except KeyboardInterrupt:
            stop_flag.set()
    t.join()
    logging.getLogger().removeHandler(log_handler)
    if thread_exception[0]:
        layout["main"].update(
            Panel(Text(f"Thread exited with error:\n{thread_exception[0]}", justify="left", style="red")),
        )
        with Live(layout, refresh_per_second=2, screen=True):
            time.sleep(10)


# --- Update main_continuous_mode to update bar_state ---
def main_continuous_mode(tcclient, args, main_layout, bar_state, stop_flag=None):
    check_interval = args.continuous_mode_check_interval
    logging.info("Entering continuous mode loop. Press Ctrl-C to exit.")
    while not (stop_flag and stop_flag.is_set()):
        try:
            try:
                now = datetime.datetime.now()
                bar_state["last_check"] = now
                bar_state["next_check"] = now + datetime.timedelta(seconds=check_interval)
                queue_count = tcclient.queue_object.taskQueueCounts(args.queue).get("pendingTasks", 0)
                bar_state["pending"] = queue_count
                main_layout.update(Panel(Text(f"{args.queue} pending tasks/jobs: {queue_count}", justify="center")))
                if queue_count < args.continuous_mode_limit:
                    logging.info(f"Below job limit of {args.continuous_mode_limit}.")
                    logging.info(f"Creating {args.count} tasks...")
                    created = 0
                    for i in range(args.count):
                        tcclient.create_task()
                        bar_state["last_created"] = datetime.datetime.now()
                        created += 1
                    logging.info(f"Done creating {created} tasks.")
                    updated_count = tcclient.queue_object.taskQueueCounts(args.queue).get("pendingTasks", 0)
                    bar_state["pending"] = updated_count
                else:
                    logging.info(
                        f"At or above job limit of {args.continuous_mode_limit} ({queue_count}), not creating tasks.",
                    )
            except Exception as e:
                if taskcluster.TaskclusterRestFailure and isinstance(e, taskcluster.TaskclusterRestFailure):
                    logging.error(f"Taskcluster API error: {e}")
                else:
                    logging.error(f"Error fetching queue task/job counts: {e}")
                logging.warning(f"Will retry after {check_interval} seconds.")
                main_layout.update(Panel(Text(f"Error: {e}", justify="center", style="red")))
            # Replace time.sleep(check_interval) with a responsive sleep
            sleep_step = 0.1
            steps = math.ceil(check_interval / sleep_step)
            for _ in range(steps):
                if stop_flag and stop_flag.is_set():
                    break
                time.sleep(sleep_step)
        except KeyboardInterrupt:
            if stop_flag:
                stop_flag.set()
            break


def main_one_off_mode(tcclient, args):
    # This function is used for curses mode main content
    with alive_progress.alive_bar(args.count, unit=" jobs", enrich_print=False) as bar:
        for i in range(args.count):
            tcclient.create_task()
            bar()


def _build_command_from_script(script_path, script_args=None):
    with open(script_path) as f:
        content = f.read().strip()
    arguments = shlex.split(script_args) if script_args else []

    # Detect interpreter from shebang or file extension
    first_line = content.splitlines()[0] if content else ""
    if first_line.startswith("#!"):
        shebang = first_line[2:].strip()
        # e.g. "/usr/bin/env python3" -> "python3", "/bin/bash" -> "bash"
        interpreter = shebang.split()[-1] if shebang else "bash"
    elif script_path.endswith(".py"):
        interpreter = "python3"
    else:
        interpreter = "bash"

    if "bash" in interpreter or "sh" in interpreter:
        if arguments:
            raise ValueError("--script-args is supported only for non-shell --script-file inputs")
        return content

    # For non-bash interpreters, wrap in a heredoc so bash feeds the script correctly
    command = "{} - {} << 'SCRIPT_EOF'\n{}\nSCRIPT_EOF".format(
        interpreter,
        " ".join(shlex.quote(argument) for argument in arguments),
        content,
    )
    return command


def main():
    args = parse_args()
    if args.payload_format == "docker-worker" and args.docker_image is None:
        args.docker_image = DEFAULT_DOCKER_IMAGE
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if args.script_file:
        bash_command = _build_command_from_script(args.script_file, args.script_args)
    else:
        bash_command = args.bash_command or default_bash_command(args.command_timeout)

    env = {}
    for pair in args.env:
        if "=" not in pair:
            sys.exit(f"--env expects KEY=VAL, got: {pair!r}")
        k, v = pair.split("=", 1)
        env[k] = v

    tcclient = TCClient(
        args.queue,
        dry_run=args.dry_run,
        bash_command=bash_command,
        command_timeout_seconds=args.command_timeout,
        requests_timeout=args.requests_timeout,
        env=env,
        payload_format=args.payload_format,
        docker_image=args.docker_image,
    )
    if args.dry_run:
        logging.info("Dry Run mode is enabled. No tasks will be created.")

    if args.continuous_mode:
        logging.info(
            f"Starting in continuous mode. Job count: {args.count}, job limit: {args.continuous_mode_limit}, check interval: {args.continuous_mode_check_interval}s.",
        )
        if is_interactive_terminal():
            run_curses_mode(main_continuous_mode, tcclient, args)
        else:
            # fallback: plain logging
            while True:
                try:
                    try:
                        queue_count = tcclient.queue_object.taskQueueCounts(args.queue).get("pendingTasks", 0)
                        logging.info(f"{args.queue} pending tasks/jobs: {queue_count}")
                        if queue_count < args.continuous_mode_limit:
                            logging.info(
                                f"Below job limit of {args.continuous_mode_limit}, starting {args.count} jobs...",
                            )
                            for i in range(args.count):
                                tcclient.create_task()
                    except Exception as e:
                        if taskcluster.TaskclusterRestFailure and isinstance(e, taskcluster.TaskclusterRestFailure):
                            logging.error(f"Taskcluster API error: {e}")
                        else:
                            logging.error(f"Error fetching queue task/job counts: {e}")
                        logging.warning(f"Will retry after {args.continuous_mode_check_interval} seconds.")
                    time.sleep(args.continuous_mode_check_interval)
                except KeyboardInterrupt:
                    break
    else:
        with alive_progress.alive_bar(args.count, unit=" jobs", enrich_print=False) as bar:
            for i in range(args.count):
                tcclient.create_task()
                bar()


if __name__ == "__main__":
    main()
