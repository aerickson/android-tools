"""Short-lived local cache of discovered replay workloads; credentials are never cached."""

from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time

from worker_health.replay_picker import Discovery, ReplayJob


def cache_path(pool):
    directory = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "worker-health" / "replay-picker"
    return directory / f"{hashlib.sha256(pool.encode()).hexdigest()[:24]}.json"


def load_discovery(pool, minutes):
    if minutes <= 0:
        return None
    try:
        cached = json.loads(cache_path(pool).read_text())
        age = time.time() - cached["saved_at"]
        if cached["version"] != 1 or cached["pool"] != pool or not 0 <= age < minutes * 60:
            return None
        data = cached["discovery"]
        jobs = [ReplayJob(**entry) for entry in data.pop("jobs")]
        data["cached_at"] = cached["saved_at"]
        return Discovery(jobs=jobs, **data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logging.warning("Ignoring unavailable or invalid picker cache: %s", exc)
        return None


def save_discovery(pool, discovery):
    path = cache_path(pool)
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump({"version": 1, "pool": pool, "saved_at": time.time(), "discovery": asdict(discovery)}, output)
        temporary.replace(path)
    except OSError as exc:
        logging.warning("Could not save picker history cache: %s", exc)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
