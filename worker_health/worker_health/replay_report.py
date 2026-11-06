"""Atomic, credential-free coverage report output."""

import json
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


class ReplayReport:
    def __init__(self, args):
        self.lock = threading.Lock()
        self.finished = False
        self.latest = {}
        self.started_at = datetime.now(timezone.utc).isoformat()
        if args.report:
            self.path = Path(args.report).expanduser().absolute()
        else:
            destination = re.sub(r"[^a-zA-Z0-9_.-]", "-", args.queue)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            with tempfile.NamedTemporaryFile(
                prefix=f"replay-tc-pool-{destination}-{stamp}-",
                suffix=".json",
                dir="/tmp",
                delete=False,
            ) as output:
                self.path = Path(output.name)
        args.report = str(self.path)
        self.base = {
            "version": 1,
            "source_pool": args.from_pool,
            "destination_pool": args.queue,
            "started_at": self.started_at,
            "stop_on_full_coverage": getattr(args, "stop_on_full_coverage", True),
            "exit_on_full_coverage": getattr(args, "exit_on_full_coverage", False),
        }
        self.write({"full_coverage": False, "message": "Starting"})

    def write(self, data, exit_reason=None):
        with self.lock:
            if self.finished:
                return
            self._write(data, exit_reason)
            self.latest = data
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
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "exit_reason": exit_reason,
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as output:
                temporary = Path(output.name)
                json.dump(report, output, indent=2, ensure_ascii=False)
                output.write("\n")
            temporary.replace(self.path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
