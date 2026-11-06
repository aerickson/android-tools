"""Atomic, credential-free coverage report output."""

import threading
from datetime import UTC, datetime
from pathlib import Path

from worker_health.replay_sessions import create_session, write_json


class ReplayReport:
    def __init__(self, args):
        self.lock = threading.Lock()
        self.finished = False
        self.latest = {}
        invocation_started = datetime.now(UTC).isoformat()
        session = getattr(args, "session_info", None)
        if session is None and not args.report:
            session = create_session(args.from_pool, args.queue)
            args.session_info = session
        self.copy_path = None
        if session:
            self.path = session.report_path
            self.latest = session.report()
            if args.report and Path(args.report).expanduser().absolute() != self.path:
                self.copy_path = Path(args.report).expanduser().absolute()
        else:
            self.path = Path(args.report).expanduser().absolute()
        self.started_at = session.started_at if session else invocation_started
        args.report = str(self.path)
        self.base = {
            "version": 1,
            "source_pool": args.from_pool,
            "destination_pool": args.queue,
            "started_at": self.started_at,
            "invocation_started_at": invocation_started,
            "stop_on_full_coverage": getattr(args, "stop_on_full_coverage", True),
            "exit_on_full_coverage": getattr(args, "exit_on_full_coverage", False),
        }
        self.write({**self.latest, **self.base, "message": "Starting monitoring"})

    def write(self, data, exit_reason=None):
        with self.lock:
            if self.finished:
                return
            self.latest = self._write(data, exit_reason)
            self.finished = exit_reason is not None

    def finish(self, reason):
        """Finalize the last observed data even if API work is still in progress."""
        with self.lock:
            if not self.finished:
                self._write(self.latest, reason)
                self.finished = True

    def _write(self, data, exit_reason=None):
        report = {
            **self.base,
            **data,
            "reported_at": datetime.now(UTC).isoformat(),
            "invocation_started_at": self.base["invocation_started_at"],
            "sampled_at": data.get("sampled_at", self.latest.get("sampled_at")),
            "exit_reason": exit_reason,
        }
        write_json(self.path, report)
        if self.copy_path:
            write_json(self.copy_path, report)
        return report
