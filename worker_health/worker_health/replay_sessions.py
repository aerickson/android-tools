"""Persistent coverage sessions and read-only session discovery."""

import json
import os
import re
import shlex
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


def utc_now():
    return datetime.now(UTC)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Session timestamps must include a timezone")
    return parsed


def sessions_root():
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "worker-health" / "replay-sessions"


def write_json(path, data):
    """Replace a snapshot atomically, using private temporary files."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write("\n")
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@dataclass
class ReplaySession:
    path: Path
    source: str
    destination: str
    started_at: str

    @property
    def report_path(self):
        return self.path / "report.json"

    @property
    def state_path(self):
        return self.path / "state.sqlite3"

    def report(self):
        try:
            data = json.loads(self.report_path.read_text())
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise TypeError(f"Invalid report: {self.report_path}")
        return data

    def configuration(self):
        return json.loads((self.path / "session.json").read_text())

    def sampled_at(self):
        return self.report().get("sampled_at")

    def warnings(self, now=None):
        now = now or utc_now()
        warnings = []
        if now - timestamp(self.started_at) >= timedelta(days=7):
            warnings.append("Session started 7+ days ago; consider --new-session")
        sampled = self.sampled_at()
        if sampled and now - timestamp(sampled) >= timedelta(days=7):
            warnings.append("Stale: no samples for 7+ days")
        return warnings

    def resume_command(self):
        return shlex.join(["pipenv", "run", "python", "replay_tc_pool.py", "--session", str(self.path), "--resume"])


def load_session(path):
    path = Path(path).expanduser().absolute()
    if path.name in ("session.json", "report.json", "state.sqlite3"):
        path = path.parent
    data = json.loads((path / "session.json").read_text())
    if not isinstance(data, dict):
        raise TypeError(f"Invalid session metadata: {path}")
    if data.get("version") != 1:
        raise ValueError(f"Unsupported session format: {path}")
    source, destination, started = data["source_pool"], data["destination_pool"], data["started_at"]
    if not isinstance(source, str) or not isinstance(destination, str) or source == destination:
        raise ValueError(f"Invalid session pools: {path}")
    timestamp(started)
    return ReplaySession(path, source, destination, started)


def list_sessions():
    sessions = []
    for path in sessions_root().glob("*/session.json"):
        try:
            session = load_session(path)
            sampled = session.sampled_at()
            # Validate timestamps before sorting; ignore incomplete/corrupt sessions.
            order = timestamp(sampled) if sampled else datetime.min.replace(tzinfo=UTC)
            sessions.append((order, timestamp(session.started_at), str(session.path), session))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return [entry[-1] for entry in sorted(sessions, key=lambda entry: entry[:3], reverse=True)]


def create_session(source, destination):
    now = utc_now()

    def slug(value):
        return re.sub(r"[^a-zA-Z0-9_.-]", "-", value)[:85]

    name = f"{now:%Y%m%dT%H%M%SZ}__{slug(source)}__to__{slug(destination)}__{uuid.uuid4().hex[:8]}"
    path = sessions_root() / name
    path.mkdir(parents=True, mode=0o700)
    metadata = {"version": 1, "source_pool": source, "destination_pool": destination, "started_at": now.isoformat()}
    write_json(path / "session.json", metadata)
    write_json(
        path / "report.json",
        {
            **metadata,
            "sampled_at": None,
            "full_coverage": False,
            "rows": [],
            "passed": 0,
            "eligible": 0,
            "message": "New session; not yet sampled",
        },
    )
    return ReplaySession(path, source, destination, metadata["started_at"])
