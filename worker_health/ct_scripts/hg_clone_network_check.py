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
import hashlib
import json
import os
import platform
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request


# Change these deliberately. All compared runs must use the same named
# configuration and its pinned component versions.
CONFIGURATIONS = {
    "latest-without-robust-checkout": {
        "mercurial_version": "7.0.2",
        "python_command": "python3",
        "python_manager": "system",
        "robustcheckout_revision": None,
    },
    "bitbar-docker-with-robustcheckout": {
        "mercurial_version": "5.9.3",
        "python_command": "3.9",
        "python_manager": "uv",
        "robustcheckout_revision": "260e22f03e984e0ced16b6c5ff63201cdef0a1f6",  # pragma: allowlist secret
        "robustcheckout_url_template": "https://hg.mozilla.org/mozilla-central/raw-file/{revision}/testing/mozharness/external_tools/robustcheckout.py",
    },
    "latest-with-robustcheckout": {
        "mercurial_version": "7.0.2",
        "python_command": "python3",
        "python_manager": "system",
        "robustcheckout_revision": "ef45ece67c97ba67a13182c97496d8a90b768eea",  # pragma: allowlist secret
        "robustcheckout_url_template": "https://hg.mozilla.org/hgcustom/version-control-tools/raw-file/{revision}/hgext/robustcheckout/__init__.py",
    },
}
DEFAULT_CONFIGURATION = "latest-without-robust-checkout"
# Increment the patch version when a change affects benchmark behavior,
# measurements, or output. Formatting-only changes do not require a bump.
# Bump minor or major when the result schema changes incompatibly.
SCRIPT_VERSION = "1.2.0"
# Match the hg-edge endpoint used by the observed CI clone task. Use
# hg.mozilla.org only for explicit URL/redirect comparison experiments.
DEFAULT_REPOSITORY_URL = "https://hg-edge.mozilla.org/mozilla-unified"
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 15
PRODUCTION_ROBUSTCHECKOUT_REPOSITORY_URL = "https://hg.mozilla.org/try"
PRODUCTION_ROBUSTCHECKOUT_UPSTREAM_URL = "https://hg.mozilla.org/mozilla-unified"
PRODUCTION_ROBUSTCHECKOUT_SPARSE_PROFILE = "build/sparse-profiles/perftest"
PRODUCTION_ROBUSTCHECKOUT_REVISION = "f069124083780d79af44fba3b71699b90a7a63d7"  # pragma: allowlist secret
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
        "--checkout-mode",
        choices=("clone", "robustcheckout"),
        default="clone",
        help="Checkout workflow to measure (default: clone).",
    )
    parser.add_argument(
        "--robustcheckout-repository-url",
        default=PRODUCTION_ROBUSTCHECKOUT_REPOSITORY_URL,
        help="Repository URL passed to hg robustcheckout.",
    )
    parser.add_argument(
        "--robustcheckout-upstream-url",
        default=PRODUCTION_ROBUSTCHECKOUT_UPSTREAM_URL,
        help="Upstream URL passed to hg robustcheckout.",
    )
    parser.add_argument(
        "--robustcheckout-sparse-profile",
        default=PRODUCTION_ROBUSTCHECKOUT_SPARSE_PROFILE,
        help="Sparse profile passed to hg robustcheckout.",
    )
    parser.add_argument(
        "--robustcheckout-revision",
        default=PRODUCTION_ROBUSTCHECKOUT_REVISION,
        help="Revision passed to hg robustcheckout.",
    )
    parser.add_argument("--sharebase", help="Shared-store path passed to hg robustcheckout.")
    parser.add_argument(
        "--destination",
        help="Checkout destination (defaults to mozilla-unified for clone or gecko for robustcheckout).",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join("out", "hg-clone-network-check-" + timestamp),
        help="Directory for benchmark logs and JSON artifacts (default: out/hg-clone-network-check-<timestamp>).",
    )
    parser.add_argument("--configuration", choices=sorted(CONFIGURATIONS), default=DEFAULT_CONFIGURATION)
    parser.add_argument("--venv-dir", help="Override the configuration-specific virtualenv path.")
    parser.add_argument("--skip-update", action="store_true", help="Skip the separate update phase in clone mode.")
    parser.add_argument("--skip-public-ip", action="store_true")
    parser.add_argument(
        "--telemetry-interval",
        type=float,
        default=DEFAULT_TELEMETRY_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=(
            f"Seconds between clone resource samples (default: {DEFAULT_TELEMETRY_INTERVAL_SECONDS}). "
            "Set to 0 to disable periodic telemetry."
        ),
    )
    parser.add_argument(
        "--install-system-dependencies",
        action="store_true",
        help="Install missing interpreter support before provisioning (apt for system Python; uv for Bitbar Python).",
    )
    args = parser.parse_args()
    if args.telemetry_interval < 0:
        parser.error("--telemetry-interval must be non-negative")
    return args


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


def default_venv_dir(configuration_name, configuration):
    return os.path.join(
        default_cache_dir(),
        "venv-{}-mercurial-{}-{}".format(
            configuration_name,
            configuration["mercurial_version"],
            configuration["python_command"],
        ),
    )


def provision_hg(venv_dir, configuration, python_command):
    expected_version = configuration["mercurial_version"]
    hg_path = os.path.join(venv_dir, "bin", "hg")
    python_path = os.path.join(venv_dir, "bin", "python")
    if os.path.isfile(hg_path):
        installed_version = hg_version(hg_path)
        if installed_version == expected_version:
            return hg_path, python_path, False

    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir)
    print("Provisioning Mercurial {} in {}...".format(expected_version, venv_dir), file=sys.stderr)
    try:
        run_checked([python_command, "-m", "venv", venv_dir])
    except FileNotFoundError as exc:
        raise RuntimeError("required Python interpreter is unavailable: {}".format(python_command)) from exc
    run_checked([python_path, "-m", "pip", "install", "--disable-pip-version-check", "mercurial==" + expected_version])
    installed_version = hg_version(hg_path)
    if installed_version != expected_version:
        raise RuntimeError("expected Mercurial {}, found {}".format(expected_version, installed_version))
    return hg_path, python_path, True


def install_uv():
    print("Installing uv...", file=sys.stderr)
    request = urllib.request.Request(
        "https://astral.sh/uv/install.sh",
        headers={"User-Agent": "hg-clone-network-check/{}".format(SCRIPT_VERSION)},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        installer = response.read()
    with tempfile.NamedTemporaryFile("wb", delete=False) as installer_file:
        installer_file.write(installer)
        installer_path = installer_file.name
    try:
        subprocess.run(["sh", installer_path], check=True)
    finally:
        os.unlink(installer_path)

    for candidate in (os.path.expanduser("~/.local/bin/uv"), os.path.expanduser("~/.cargo/bin/uv")):
        if os.path.isfile(candidate):
            return candidate
    raise RuntimeError("uv installation completed but its executable was not found")


def resolve_python(configuration, install_missing):
    if configuration["python_manager"] == "system":
        if shutil.which(configuration["python_command"]) is None:
            raise RuntimeError("required Python interpreter is unavailable: {}".format(configuration["python_command"]))
        return configuration["python_command"], None

    uv_path = shutil.which("uv")
    if uv_path is None:
        if not install_missing:
            raise RuntimeError(
                "the Bitbar Docker configuration requires uv on PATH; rerun with --install-system-dependencies",
            )
        uv_path = install_uv()
    find = subprocess.run(
        [uv_path, "python", "find", configuration["python_command"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if find.returncode:
        if not install_missing:
            raise RuntimeError(
                "Python {} is unavailable; rerun with --install-system-dependencies to install it with uv".format(
                    configuration["python_command"],
                ),
            )
        print(
            "Installing Bitbar-compatible Python {} with uv...".format(configuration["python_command"]),
            file=sys.stderr,
        )
        subprocess.run([uv_path, "python", "install", configuration["python_command"]], check=True)
        find = subprocess.run(
            [uv_path, "python", "find", configuration["python_command"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    return find.stdout.strip(), run_checked([uv_path, "--version"])


def install_system_dependencies(configuration):
    if not (sys.platform.startswith("linux") and os.path.exists("/etc/debian_version")):
        raise RuntimeError("--install-system-dependencies is supported only on Debian/Ubuntu hosts")
    print("Updating apt package metadata...", file=sys.stderr)
    subprocess.run(["sudo", "apt-get", "update"], check=True)
    if configuration["python_manager"] == "uv":
        print("Installing C build tools for Mercurial 5.9.3...", file=sys.stderr)
        subprocess.run(["sudo", "apt-get", "install", "--yes", "build-essential"], check=True)
        return
    print("Installing python3-venv...", file=sys.stderr)
    subprocess.run(["sudo", "apt-get", "install", "--yes", "python3-venv"], check=True)


def robustcheckout_source(revision, url_template):
    source_directory = os.path.join(default_cache_dir(), "sources")
    url = url_template.format(revision=revision)
    source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    source_path = os.path.join(source_directory, "robustcheckout-{}-{}.py".format(revision, source_id))
    if not os.path.isfile(source_path):
        os.makedirs(source_directory, exist_ok=True)
        print("Fetching robustcheckout.py at {}...".format(revision), file=sys.stderr)
        with urllib.request.urlopen(url, timeout=60) as response, open(source_path, "wb") as output:
            shutil.copyfileobj(response, output)
    with open(source_path, "rb") as source_file:
        digest = hashlib.sha256(source_file.read()).hexdigest()
    return source_path, digest


def write_hgrc(output_dir, configuration):
    hgrc_path = os.path.join(output_dir, "benchmark.hgrc")
    revision = configuration["robustcheckout_revision"]
    if revision is None:
        with open(hgrc_path, "w"):
            pass
        return {"HGRCPATH": os.path.basename(hgrc_path)}

    robustcheckout_path, digest = robustcheckout_source(revision, configuration["robustcheckout_url_template"])
    with open(hgrc_path, "w") as output:
        output.write(
            """[progress]
delay = 1.0
refresh = 1.0
assume-tty = true

[extensions]
share =
sparse =
robustcheckout = {robustcheckout_path}

[hostsecurity]
minimumprotocol = tls1.2

[diff]
git = 1
showfunc = 1

[pager]
pager = LESS=FRSXQ less

[extensions]
histedit =
rebase =
""".format(robustcheckout_path=robustcheckout_path),
        )
    return {
        "HGRCPATH": os.path.basename(hgrc_path),
        "robustcheckout": {
            "revision": revision,
            "path": robustcheckout_path,
            "sha256": digest,
            "source_url": configuration["robustcheckout_url_template"].format(revision=revision),
        },
    }


def hgrc_environment(hgrc_path):
    """Return the child environment with a resolvable benchmark HGRCPATH."""
    return dict(os.environ, HGRCPATH=os.path.abspath(hgrc_path))


def verify_robustcheckout_configuration(hg_path, environment, hgrc_details):
    completed = subprocess.run(
        [hg_path, "showconfig", "extensions.robustcheckout"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError("could not inspect robustcheckout configuration: {}".format(completed.stderr.strip()))

    actual = completed.stdout.strip()
    if actual.startswith("extensions.robustcheckout="):
        actual = actual.partition("=")[2]
    expected = hgrc_details.get("robustcheckout", {}).get("path")
    if expected is None and actual:
        raise RuntimeError("robustcheckout is unexpectedly enabled: {}".format(actual))
    if expected is not None and actual != expected:
        raise RuntimeError("robustcheckout does not match the configured path: {}".format(actual))

    hgrc_details["effective_robustcheckout"] = actual or None
    return hgrc_details


def robustcheckout_command(hg_path, args, destination, robustcheckout_path):
    """Build the production-style robustcheckout invocation for telemetry runs."""
    if not robustcheckout_path:
        raise RuntimeError("robustcheckout mode requires a configuration that enables the robustcheckout extension")
    sharebase = os.path.abspath(args.sharebase or os.path.join(os.path.dirname(destination), "hg-shared"))
    return [
        hg_path,
        "robustcheckout",
        "--sharebase",
        sharebase,
        "--purge",
        "--config",
        "extensions.robustcheckout={}".format(robustcheckout_path),
        "--upstream",
        args.robustcheckout_upstream_url,
        "--sparseprofile",
        args.robustcheckout_sparse_profile,
        "--revision",
        args.robustcheckout_revision,
        args.robustcheckout_repository_url,
        destination,
    ]


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


def default_network_interface():
    """Return the container's default-route interface, if it is visible."""
    try:
        with open("/proc/net/route") as routes:
            next(routes)
            for line in routes:
                fields = line.split()
                if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return fields[0]
    except (OSError, StopIteration, ValueError):
        pass
    return None


def network_counters(interface):
    if interface is None:
        return None
    try:
        with open("/proc/net/dev") as network_devices:
            for line in network_devices:
                name, separator, values = line.partition(":")
                if separator and name.strip() == interface:
                    fields = values.split()
                    return {"interface": interface, "rx_bytes": int(fields[0]), "tx_bytes": int(fields[8])}
    except (OSError, ValueError, IndexError):
        pass
    return None


def process_cpu_seconds(pid):
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            # The command name can contain spaces and parentheses, so split after its final ')'.
            fields = stat_file.read().rsplit(")", 1)[1].split()
        clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return {"user_seconds": int(fields[11]) / clock_ticks, "system_seconds": int(fields[12]) / clock_ticks}
    except (OSError, IndexError, KeyError, ValueError):
        return None


def process_io_bytes(pid):
    try:
        values = {}
        with open(f"/proc/{pid}/io") as io_file:
            for line in io_file:
                key, separator, value = line.partition(":")
                if separator:
                    values[key] = int(value.strip())
        return {key: values[key] for key in ("read_bytes", "write_bytes", "rchar", "wchar") if key in values}
    except (OSError, ValueError):
        return None


def cgroup_cpu_stat():
    candidates = ["/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"]
    try:
        with open("/proc/self/cgroup") as cgroups:
            for line in cgroups:
                _, controllers, path = line.strip().split(":", 2)
                if not controllers or "cpu" in controllers.split(","):
                    candidates.append("/sys/fs/cgroup{}{}cpu.stat".format(path, "" if path.endswith("/") else "/"))
    except (OSError, ValueError):
        pass

    for candidate in candidates:
        try:
            values = {}
            with open(candidate) as stat_file:
                for line in stat_file:
                    key, value = line.split(None, 1)
                    values[key] = int(value)
            return {"path": candidate, "values": values}
        except (OSError, ValueError):
            continue
    return None


class PhaseTelemetry:
    def __init__(self, pid, interval_seconds, monitored_path, started, artifact_path):
        self.pid = pid
        self.interval_seconds = interval_seconds
        self.monitored_path = monitored_path
        self.started = started
        self.artifact_path = artifact_path
        self.interface = default_network_interface()
        self.samples = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def sample(self):
        sample = {"timestamp": utc_now(), "elapsed_seconds": time.monotonic() - self.started}
        network = network_counters(self.interface)
        if network is not None:
            sample["network"] = network
        cpu = process_cpu_seconds(self.pid)
        if cpu is not None:
            sample["process_cpu_seconds"] = cpu
        process_io = process_io_bytes(self.pid)
        if process_io is not None:
            sample["process_io_bytes"] = process_io
        cgroup_cpu = cgroup_cpu_stat()
        if cgroup_cpu is not None:
            sample["cgroup_cpu"] = cgroup_cpu
        try:
            sample["storage"] = storage_info(self.monitored_path)
        except OSError:
            pass
        self.samples.append(sample)
        write_json_atomic(self.artifact_path, self.result())

    def _run(self):
        while not self.stop_event.wait(self.interval_seconds):
            self.sample()

    def start(self):
        self.sample()
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()
        self.sample()

    def result(self):
        return {
            "enabled": True,
            "interval_seconds": self.interval_seconds,
            "default_route_interface": self.interface,
            "artifact": os.path.basename(self.artifact_path),
            "samples": self.samples,
        }


def run_phase(name, command, output_dir, environment, telemetry_interval, monitored_path):
    stdout_path = os.path.join(output_dir, name + ".stdout.log")
    stderr_path = os.path.join(output_dir, name + ".stderr.log")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_at = utc_now()
    started = time.monotonic()
    telemetry = None
    with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
        print("+ {}".format(" ".join(command)), file=sys.stderr)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        if telemetry_interval:
            telemetry = PhaseTelemetry(
                process.pid,
                telemetry_interval,
                monitored_path,
                started,
                os.path.join(output_dir, name + ".telemetry.json"),
            )
            telemetry.start()
        stdout_thread = threading.Thread(target=copy_stream, args=(process.stdout, (stdout_file, sys.stdout.buffer)))
        stderr_thread = threading.Thread(target=copy_stream, args=(process.stderr, (stderr_file, sys.stderr.buffer)))
        stdout_thread.start()
        stderr_thread.start()
        return_code = process.wait()
        stdout_thread.join()
        stderr_thread.join()
        if telemetry is not None:
            telemetry.stop()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = {
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
    if telemetry is None:
        result["telemetry"] = {"enabled": False}
    else:
        result["telemetry"] = telemetry.result()
    return result


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


def write_json_atomic(path, value):
    directory = os.path.dirname(path)
    with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".telemetry-", delete=False) as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        temporary_path = output.name
    os.replace(temporary_path, path)


def write_summary(path, results):
    lines = [
        "Mercurial clone network check",
        "started: {}".format(results["benchmark_started_at"]),
        "host: {}".format(results["metadata"]["hostname"]),
        "configuration: {}".format(results["configuration"]["name"]),
        "Mercurial: {}".format(results.get("mercurial_version", "not provisioned")),
    ]
    for name, phase in results["phases"].items():
        lines.append("{}: {:.3f}s (exit {})".format(name, phase["elapsed_seconds"], phase["exit_code"]))
    lines.append("")
    with open(path, "w") as output:
        output.write("\n".join(lines))


def main():
    args = parse_args()
    print("Python: {}".format(sys.version.replace("\n", " ")), file=sys.stderr)
    configuration = CONFIGURATIONS[args.configuration]
    if args.venv_dir is None:
        args.venv_dir = default_venv_dir(args.configuration, configuration)
    print("Configuration: {}".format(args.configuration), file=sys.stderr)
    default_destination = "gecko" if args.checkout_mode == "robustcheckout" else "mozilla-unified"
    checkout_repository_url = (
        args.robustcheckout_repository_url if args.checkout_mode == "robustcheckout" else args.repo_url
    )
    destination = os.path.abspath(args.destination or default_destination)
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
        "python_version": sys.version,
        "configuration": {
            "name": args.configuration,
            "mercurial_version": configuration["mercurial_version"],
            "python_command": configuration["python_command"],
            "python_manager": configuration["python_manager"],
            "telemetry_interval_seconds": args.telemetry_interval,
        },
        "benchmark_started_at": utc_now(),
        "mercurial_requested_version": configuration["mercurial_version"],
        "repository_url": checkout_repository_url,
        "checkout": {
            "mode": args.checkout_mode,
            "repository_url": checkout_repository_url,
        },
        "destination": destination,
        "metadata": host_metadata(destination_parent, checkout_repository_url),
        "phases": {},
    }

    def persist_partial_results(signum=None):
        if signum is not None:
            results["interrupted"] = {"signal": signal.Signals(signum).name, "timestamp": utc_now()}
        results["benchmark_finished_at"] = utc_now()
        write_json_atomic(os.path.join(output_dir, "results.json"), results)

    def handle_termination(signum, _frame):
        persist_partial_results(signum)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_termination)
    signal.signal(signal.SIGINT, handle_termination)

    if not args.skip_public_ip:
        results["metadata"]["public_ip"] = public_ip()

    try:
        python_command, uv_version = resolve_python(configuration, args.install_system_dependencies)
        if uv_version is not None:
            results["configuration"]["uv_version"] = uv_version
        if args.install_system_dependencies:
            install_system_dependencies(configuration)
        hg_path, mercurial_python_path, provisioned = provision_hg(
            os.path.abspath(args.venv_dir),
            configuration,
            python_command,
        )
        results["configuration"]["resolved_python"] = python_command
        results["mercurial_path"] = hg_path
        results["mercurial_python_path"] = mercurial_python_path
        results["mercurial_python_version"] = run_checked([mercurial_python_path, "--version"])
        results["mercurial_version"] = hg_version(hg_path)
        results["venv_provisioned_this_run"] = provisioned
        hgrc_details = write_hgrc(output_dir, configuration)
        hgrc_path = os.path.join(output_dir, hgrc_details["HGRCPATH"])
        environment = hgrc_environment(hgrc_path)
        results["mercurial_configuration"] = verify_robustcheckout_configuration(
            hg_path,
            environment,
            hgrc_details,
        )
        if args.checkout_mode == "robustcheckout":
            results["checkout"].update(
                {
                    "upstream_url": args.robustcheckout_upstream_url,
                    "sparse_profile": args.robustcheckout_sparse_profile,
                    "revision": args.robustcheckout_revision,
                    "sharebase": os.path.abspath(args.sharebase or os.path.join(destination_parent, "hg-shared")),
                },
            )
            phase_name = "robustcheckout"
            checkout_command = robustcheckout_command(
                hg_path,
                args,
                destination,
                hgrc_details["effective_robustcheckout"],
            )
        else:
            phase_name = "clone"
            checkout_command = [hg_path, "clone", "--noupdate", args.repo_url, destination]
        results["active_phase"] = {
            "name": phase_name,
            "telemetry_artifact": phase_name + ".telemetry.json" if args.telemetry_interval else None,
        }
        clone = run_phase(
            phase_name,
            checkout_command,
            output_dir,
            environment,
            args.telemetry_interval,
            destination_parent,
        )
        results["phases"][phase_name] = clone
        results.pop("active_phase", None)
        if args.checkout_mode == "clone" and clone["exit_code"] == 0 and not args.skip_update:
            results["active_phase"] = {
                "name": "update",
                "telemetry_artifact": "update.telemetry.json" if args.telemetry_interval else None,
            }
            results["phases"]["update"] = run_phase(
                "update",
                [hg_path, "--repository", destination, "update"],
                output_dir,
                environment,
                args.telemetry_interval,
                destination_parent,
            )
            results.pop("active_phase", None)
        if os.path.exists(destination):
            results["destination_size_bytes"] = directory_size_bytes(destination)
    except Exception as exc:
        results["runner_error"] = str(exc)
        write_json(os.path.join(output_dir, "results.json"), results)
        raise
    finally:
        persist_partial_results()
        write_summary(os.path.join(output_dir, "summary.txt"), results)

    print("Results written to {}".format(os.path.join(output_dir, "results.json")))
    failed_phases = [name for name, phase in results["phases"].items() if phase["exit_code"] != 0]
    return 1 if failed_phases else 0


if __name__ == "__main__":
    sys.exit(main())
