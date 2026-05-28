#!/usr/bin/env python3
"""
test_hg_clone.py — reproduce the hg robustcheckout that gecko CI runs, so we
can test for clone timeouts on a worker without a full Firefox taskgraph.

Use with create_tc_task.py:
    pipenv run python ./create_tc_task.py \\
        -q proj-autophone/gecko-t-bitbar-gw-perf-a55 \\
        -s ./test_hg_clone.py \\
        -t 5400

The original failing command (job 568912321) was:
    hg robustcheckout \\
        --sharebase <task>/checkouts/hg-shared \\
        --purge \\
        --config extensions.robustcheckout=<task>/robustcheckout.py \\
        --upstream https://hg.mozilla.org/mozilla-unified \\
        --sparseprofile build/sparse-profiles/perftest \\
        --revision c9b7663d5238675798b95b93b88e515116db868a \\
        https://hg.mozilla.org/mozilla-central <task>/checkouts/gecko

It timed out after ~40min still streaming the bundle.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone

REVISION = os.environ.get("REVISION", "c9b7663d5238675798b95b93b88e515116db868a")
SPARSE_PROFILE = os.environ.get("SPARSE_PROFILE", "build/sparse-profiles/perftest")
UPSTREAM = os.environ.get("UPSTREAM", "https://hg.mozilla.org/mozilla-unified")
REPO = os.environ.get("REPO", "https://hg.mozilla.org/mozilla-central")
ROBUSTCHECKOUT_URL = (
    "https://hg.mozilla.org/hgcustom/version-control-tools/raw-file/tip/hgext/robustcheckout/__init__.py"
)
PROGRESS_INTERVAL = int(os.environ.get("PROGRESS_INTERVAL", "30"))


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd, **kwargs):
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, **kwargs)


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path, followlinks=False):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def df_free(path):
    s = shutil.disk_usage(path)
    return human(s.free)


def progress_loop(sharebase, workdir, stop_event):
    while not stop_event.wait(PROGRESS_INTERVAL):
        try:
            size = human(dir_size(sharebase))
        except Exception:
            size = "?"
        try:
            free = df_free(workdir)
        except Exception:
            free = "?"
        log(f"progress: sharebase={size} free={free}")


def fetch_robustcheckout(dest):
    log(f"fetching robustcheckout extension from {ROBUSTCHECKOUT_URL}")
    req = urllib.request.Request(ROBUSTCHECKOUT_URL)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with open(dest, "wb") as f:
        f.write(data)
    log(f"robustcheckout.py size: {len(data)} bytes")


def main():
    if not shutil.which("hg"):
        log("FATAL: hg not on PATH")
        return 2

    workdir = tempfile.mkdtemp(prefix="hg-clone-test.")
    sharebase = os.path.join(workdir, "hg-shared")
    dest = os.path.join(workdir, "gecko")
    robustcheckout = os.path.join(workdir, "robustcheckout.py")
    os.makedirs(sharebase, exist_ok=True)

    log(f"workdir: {workdir}")
    log(f"TASKCLUSTER_WORKER_ID={os.environ.get('TASKCLUSTER_WORKER_ID', '?')}")
    log(f"TASKCLUSTER_WORKER_GROUP={os.environ.get('TASKCLUSTER_WORKER_GROUP', '?')}")
    log(f"hostname={os.uname().nodename}")
    run(["hg", "--version"], check=False)
    log(f"disk free at {workdir}: {df_free(workdir)}")

    rc = 1
    stop_event = threading.Event()
    progress_thread = None
    try:
        fetch_robustcheckout(robustcheckout)

        progress_thread = threading.Thread(target=progress_loop, args=(sharebase, workdir, stop_event), daemon=True)
        progress_thread.start()

        cmd = [
            "hg",
            "--config",
            f"extensions.robustcheckout={robustcheckout}",
            "--config",
            "ui.timeout=1800",
            "--config",
            "ui.timeout.warn=300",
            "robustcheckout",
            "--sharebase",
            sharebase,
            "--purge",
            "--upstream",
            UPSTREAM,
            "--sparseprofile",
            SPARSE_PROFILE,
            "--revision",
            REVISION,
            REPO,
            dest,
        ]
        log(f"starting robustcheckout (rev={REVISION})")
        start = time.time()
        try:
            result = subprocess.run(cmd)
            rc = result.returncode
        except KeyboardInterrupt:
            log("interrupted")
            rc = 130
        elapsed = int(time.time() - start)
        log(f"robustcheckout exit={rc} elapsed={elapsed}s")
    finally:
        stop_event.set()
        if progress_thread is not None:
            progress_thread.join(timeout=2)

        try:
            log(f"sharebase size: {human(dir_size(sharebase))}")
        except Exception:
            pass
        try:
            log(f"dest size:      {human(dir_size(dest))}")
        except Exception:
            pass
        log(f"disk free at {workdir}: {df_free(workdir)}")
        log(f"cleanup: removing {workdir}")
        shutil.rmtree(workdir, ignore_errors=True)

    return rc


if __name__ == "__main__":
    # Forward SIGTERM (taskcluster's timeout signal) so we get a clean log line.
    def _term(signum, _frame):
        log(f"received signal {signum}, exiting")
        sys.exit(143)

    signal.signal(signal.SIGTERM, _term)
    sys.exit(main())
