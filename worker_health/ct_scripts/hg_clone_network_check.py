#!/usr/bin/env python3
"""Run a reproducible, instrumented Mercurial clone benchmark.

This script deliberately has no third-party Python dependencies.  It creates
an isolated virtual environment outside the checkout and runs the pinned
Mercurial version from that environment.  Provisioning is not included in the
benchmark timings.
"""

from __future__ import print_function

import argparse
import datetime
import json
import os
import platform
import re
import resource
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import venv


# Change this deliberately.  All compared runs must use the same version.
MERCURIAL_VERSION = "7.0.2"
# Increment the patch version when a change affects benchmark behavior,
# measurements, or output. Formatting-only changes do not require a bump.
# Bump minor or major when the result schema changes incompatibly.
SCRIPT_VERSION = "1.0.1"
DEFAULT_REPOSITORY_URL = "https://hg-edge.mozilla.org/mozilla-unified"
RESULT_SCHEMA_VERSION = 1


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_cache_dir():
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return os.path.join(cache_home, "hg-clone-network-check")
    return os.path.join(os.path.expanduser("~"), ".cache", "hg-clone-network-check")


def parse_args():
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument(
        "--destination",
        default="mozilla-unified",
        help="New clone destination (must not already exist).",
    )
    parser.add_argument("--output-dir", default="hg-clone-network-check-" + timestamp)
    parser.add_argument("--venv-dir", default=os.path.join(default_cache_dir(), "venv-mercurial-" + MERCURIAL_VERSION))
    parser.add_argument("--skip-update", action="store_true", help="Only run 'hg clone --noupdate'.")
    parser.add_argument("--skip-public-ip", action="store_true")
    return parser.parse_args()


def run_checked(command, **kwargs):
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
    if completed.returncode:
        raise RuntimeError(
            "command failed ({}): {}\n{}".format(completed.returncode, " ".join(command), completed.stderr.strip()),
        )
    return completed.stdout.strip()


def hg_version(hg_path):
    output = run_checked([hg_path, "version", "--quiet"])
    match = re.search(r"\(version ([^)]+)\)", output)
    if not match:
        raise RuntimeError("could not determine Mercurial version from: {}".format(output))
    return match.group(1)


def provision_hg(venv_dir):
    hg_path = os.path.join(venv_dir, "bin", "hg")
    if os.path.isfile(hg_path):
        installed_version = hg_version(hg_path)
        if installed_version == MERCURIAL_VERSION:
            return hg_path, False

    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir)
    print("Provisioning Mercurial {} in {}...".format(MERCURIAL_VERSION, venv_dir), file=sys.stderr)
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python_path = os.path.join(venv_dir, "bin", "python")
    run_checked([python_path, "-m", "pip", "install", "--disable-pip-version-check", "mercurial==" + MERCURIAL_VERSION])
    installed_version = hg_version(hg_path)
    if installed_version != MERCURIAL_VERSION:
        raise RuntimeError("expected Mercurial {}, found {}".format(MERCURIAL_VERSION, installed_version))
    return hg_path, True


def cpu_model():
    try:
        with open("/proc/cpuinfo") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("model name") or line.startswith("Hardware"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def memory_bytes():
    try:
        with open("/proc/meminfo") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def storage_info(path):
    stat = os.statvfs(path)
    return {
        "path": os.path.abspath(path),
        "free_bytes": stat.f_bavail * stat.f_frsize,
        "total_bytes": stat.f_blocks * stat.f_frsize,
    }


def resolve_host(url):
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return {"error": "no hostname in URL"}
    started = time.monotonic()
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None)})
        return {"host": host, "addresses": addresses, "elapsed_seconds": time.monotonic() - started}
    except socket.gaierror as exc:
        return {"host": host, "error": str(exc), "elapsed_seconds": time.monotonic() - started}


def public_ip():
    started = time.monotonic()
    try:
        with urllib.request.urlopen("https://wtfismyip.com/text", timeout=15) as response:
            value = response.read().decode("utf-8").strip()
        return {"value": value, "elapsed_seconds": time.monotonic() - started}
    except Exception as exc:  # Keep a failed diagnostic from invalidating the benchmark.
        return {"error": str(exc), "elapsed_seconds": time.monotonic() - started}


def host_metadata(destination_parent, repo_url):
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": sys.version,
        "cpu_count": os.cpu_count(),
        "cpu_model": cpu_model(),
        "memory_bytes": memory_bytes(),
        "storage": storage_info(destination_parent),
        "repository_dns": resolve_host(repo_url),
    }


def copy_stream(source, destinations):
    while True:
        block = source.read(65536)
        if not block:
            break
        for destination in destinations:
            destination.write(block)
            destination.flush()


def run_phase(name, command, output_dir, environment):
    stdout_path = os.path.join(output_dir, name + ".stdout.log")
    stderr_path = os.path.join(output_dir, name + ".stderr.log")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_at = utc_now()
    started = time.monotonic()
    with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
        print("+ {}".format(" ".join(command)), file=sys.stderr)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        stdout_thread = threading.Thread(target=copy_stream, args=(process.stdout, (stdout_file, sys.stdout.buffer)))
        stderr_thread = threading.Thread(target=copy_stream, args=(process.stderr, (stderr_file, sys.stderr.buffer)))
        stdout_thread.start()
        stderr_thread.start()
        return_code = process.wait()
        stdout_thread.join()
        stderr_thread.join()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "command": command,
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": time.monotonic() - started,
        "exit_code": return_code,
        "child_cpu_seconds": {
            "user_seconds": after.ru_utime - before.ru_utime,
            "system_seconds": after.ru_stime - before.ru_stime,
        },
        "stdout_log": os.path.basename(stdout_path),
        "stderr_log": os.path.basename(stderr_path),
    }


def directory_size_bytes(path):
    total = 0
    for root, _, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += os.path.getsize(os.path.join(root, filename))
            except OSError:
                pass
    return total


def write_json(path, value):
    with open(path, "w") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")


def write_summary(path, results):
    lines = [
        "Mercurial clone network check",
        "started: {}".format(results["benchmark_started_at"]),
        "host: {}".format(results["metadata"]["hostname"]),
        "Mercurial: {}".format(results.get("mercurial_version", "not provisioned")),
    ]
    for name, phase in results["phases"].items():
        lines.append("{}: {:.3f}s (exit {})".format(name, phase["elapsed_seconds"], phase["exit_code"]))
    lines.append("")
    with open(path, "w") as output:
        output.write("\n".join(lines))


def main():
    args = parse_args()
    destination = os.path.abspath(args.destination)
    output_dir = os.path.abspath(args.output_dir)
    destination_parent = os.path.dirname(destination)
    if os.path.exists(destination):
        raise SystemExit("destination already exists: {}".format(destination))
    if not os.path.isdir(destination_parent):
        raise SystemExit("destination parent does not exist: {}".format(destination_parent))
    if os.path.exists(output_dir):
        raise SystemExit("output directory already exists: {}".format(output_dir))
    os.makedirs(output_dir)

    results = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "benchmark_started_at": utc_now(),
        "mercurial_requested_version": MERCURIAL_VERSION,
        "repository_url": args.repo_url,
        "destination": destination,
        "metadata": host_metadata(destination_parent, args.repo_url),
        "phases": {},
    }
    if not args.skip_public_ip:
        results["metadata"]["public_ip"] = public_ip()

    try:
        hg_path, provisioned = provision_hg(os.path.abspath(args.venv_dir))
        results["mercurial_path"] = hg_path
        results["mercurial_version"] = hg_version(hg_path)
        results["venv_provisioned_this_run"] = provisioned
        hgrc_path = os.path.join(output_dir, "benchmark.hgrc")
        with open(hgrc_path, "w"):
            pass
        environment = dict(os.environ, HGRCPATH=hgrc_path)
        results["mercurial_configuration"] = {"HGRCPATH": os.path.basename(hgrc_path)}

        clone = run_phase(
            "clone",
            [hg_path, "clone", "--noupdate", args.repo_url, destination],
            output_dir,
            environment,
        )
        results["phases"]["clone"] = clone
        if clone["exit_code"] == 0 and not args.skip_update:
            results["phases"]["update"] = run_phase(
                "update",
                [hg_path, "--repository", destination, "update"],
                output_dir,
                environment,
            )
        if os.path.exists(destination):
            results["destination_size_bytes"] = directory_size_bytes(destination)
    except Exception as exc:
        results["runner_error"] = str(exc)
        write_json(os.path.join(output_dir, "results.json"), results)
        raise
    finally:
        results["benchmark_finished_at"] = utc_now()
        write_json(os.path.join(output_dir, "results.json"), results)
        write_summary(os.path.join(output_dir, "summary.txt"), results)

    print("Results written to {}".format(os.path.join(output_dir, "results.json")))
    failed_phases = [name for name, phase in results["phases"].items() if phase["exit_code"] != 0]
    return 1 if failed_phases else 0


if __name__ == "__main__":
    sys.exit(main())
