#!/usr/bin/env python3
"""Run interleaved Mercurial clone benchmark configurations reproducibly.

The sibling hg_clone_network_check.py owns provisioning and individual-run
measurements. This wrapper runs configurations in alternating order, retains
their artifacts, and removes only each disposable clone worktree between runs.
"""

import argparse
import datetime
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys


# Increment the patch version when a change affects comparison behavior or
# output. Formatting-only changes do not require a bump.
SCRIPT_VERSION = "1.0.0"
DEFAULT_CONFIGURATIONS = (
    "latest-without-robust-checkout",
    "bitbar-docker-with-robustcheckout",
)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_output_dir():
    return "hg-clone-configuration-comparison-" + datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ",
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", help="Path to hg_clone_network_check.py (defaults to sibling, then ~/).")
    # Match the hg-edge endpoint used by the observed CI clone task.
    parser.add_argument("--repo-url", default="https://hg-edge.mozilla.org/mozilla-unified")
    parser.add_argument("--output-dir", default=default_output_dir())
    parser.add_argument("--repetitions", type=int, default=3, help="Runs per configuration (default: 3).")
    parser.add_argument("--configurations", nargs="+", default=list(DEFAULT_CONFIGURATIONS), metavar="CONFIGURATION")
    parser.add_argument(
        "--install-system-dependencies",
        action="store_true",
        help="Pass this to the first run of each configuration only.",
    )
    parser.add_argument("--skip-public-ip", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the schedule without running or creating files.")
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop after a failed run (cleanup still occurs).",
    )
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    if len(args.configurations) != 2 or len(set(args.configurations)) != 2:
        parser.error("--configurations requires exactly two distinct configuration names")
    return args


def resolve_runner(value):
    candidates = []
    if value:
        candidates.append(Path(value).expanduser())
    candidates.extend(
        (Path(__file__).with_name("hg_clone_network_check.py"), Path.home() / "hg_clone_network_check.py"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SystemExit("could not find hg_clone_network_check.py; supply --runner PATH")


def build_schedule(configurations, repetitions):
    schedule = []
    for repetition in range(repetitions):
        order = configurations if repetition % 2 == 0 else tuple(reversed(configurations))
        schedule.extend((repetition + 1, configuration) for configuration in order)
    return schedule


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def load_measurement(run_output):
    result_path = run_output / "results.json"
    if not result_path.is_file():
        return {"results_path": str(result_path), "results_available": False}
    try:
        result = json.loads(result_path.read_text())
    except json.JSONDecodeError as exc:
        return {"results_path": str(result_path), "results_available": False, "results_error": str(exc)}
    phases = result.get("phases", {})
    total = sum(phase.get("elapsed_seconds", 0) for phase in phases.values())
    return {
        "results_path": str(result_path),
        "results_available": True,
        "runner_script_version": result.get("script_version"),
        "mercurial_version": result.get("mercurial_version"),
        "mercurial_python_version": result.get("mercurial_python_version"),
        "effective_robustcheckout": result.get("mercurial_configuration", {}).get("effective_robustcheckout"),
        "phase_seconds": {name: phase.get("elapsed_seconds") for name, phase in phases.items()},
        "total_phase_seconds": total,
        "runner_error": result.get("runner_error"),
    }


def write_summary(path, results):
    lines = [
        "# Mercurial clone configuration comparison",
        "",
        "- Started: {}".format(results["started_at"]),
        "- Runner: `{}`".format(results["runner"]),
        "- Repetitions per configuration: {}".format(results["repetitions"]),
        "- Cleanup: each clone destination under `worktrees/` is removed after its run; run artifacts remain.",
        "",
        "## Runs",
        "",
        "| Order | Repetition | Configuration | Exit | Total phases | Artifacts |",
        "| ---: | ---: | --- | ---: | ---: | --- |",
    ]
    for run in results["runs"]:
        measurement = run.get("measurement", {})
        total = measurement.get("total_phase_seconds")
        formatted_total = "{:.3f}s".format(total) if total is not None else "not available"
        lines.append(
            "| {order} | {repetition} | `{configuration}` | {exit_code} | {total} | `{output}` |".format(
                order=run["order"],
                repetition=run["repetition"],
                configuration=run["configuration"],
                exit_code=run["exit_code"],
                total=formatted_total,
                output=run["output_dir"],
            ),
        )
    lines.extend(("", "## Successful timing summaries", ""))
    for configuration in results["configurations"]:
        values = [
            run["measurement"]["total_phase_seconds"]
            for run in results["runs"]
            if run["configuration"] == configuration
            and run["exit_code"] == 0
            and "total_phase_seconds" in run.get("measurement", {})
        ]
        if values:
            lines.append(
                "- `{}`: n={}, median {:.3f}s, min {:.3f}s, max {:.3f}s".format(
                    configuration,
                    len(values),
                    statistics.median(values),
                    min(values),
                    max(values),
                ),
            )
        else:
            lines.append("- `{}`: no successful timings".format(configuration))
    path.write_text("\n".join(lines) + "\n")


def ensure_child(path, parent):
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise RuntimeError("refusing cleanup outside worktrees: {}".format(path)) from exc


def main():
    args = parse_args()
    runner = resolve_runner(args.runner)
    schedule = build_schedule(args.configurations, args.repetitions)
    print("Comparison script: {}".format(SCRIPT_VERSION))
    print("Runner: {}".format(runner))
    for order, (repetition, configuration) in enumerate(schedule, start=1):
        print("Run {}/{}: repetition {}, {}".format(order, len(schedule), repetition, configuration))
    if args.dry_run:
        return 0

    output_dir = Path(args.output_dir).expanduser().resolve()
    if output_dir.exists():
        raise SystemExit("output directory already exists: {}".format(output_dir))
    output_dir.mkdir(parents=True)
    worktrees = output_dir / "worktrees"
    worktrees.mkdir()
    results = {
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "started_at": utc_now(),
        "runner": str(runner),
        "repository_url": args.repo_url,
        "configurations": args.configurations,
        "repetitions": args.repetitions,
        "runs": [],
    }
    results_path = output_dir / "comparison-results.json"
    summary_path = output_dir / "summary.md"
    installed = set()
    failed = False
    for order, (repetition, configuration) in enumerate(schedule, start=1):
        run_output = output_dir / "run-{:02d}-{}".format(order, configuration)
        destination = (worktrees / "clone-{:02d}-{}".format(order, configuration)).resolve()
        ensure_child(destination, worktrees.resolve())
        command = [
            sys.executable,
            str(runner),
            "--repo-url",
            args.repo_url,
            "--configuration",
            configuration,
            "--output-dir",
            str(run_output),
            "--destination",
            str(destination),
        ]
        if args.skip_public_ip:
            command.append("--skip-public-ip")
        if args.install_system_dependencies and configuration not in installed:
            command.append("--install-system-dependencies")
            installed.add(configuration)
        print("\n=== Run {}/{}: {} ===".format(order, len(schedule), configuration), flush=True)
        completed = subprocess.run(command)
        run = {
            "order": order,
            "repetition": repetition,
            "configuration": configuration,
            "command": command,
            "exit_code": completed.returncode,
            "output_dir": str(run_output),
            "destination": str(destination),
            "measurement": load_measurement(run_output),
        }
        if destination.exists():
            print("Cleaning up clone worktree: {}".format(destination), flush=True)
            shutil.rmtree(destination)
            run["destination_cleaned"] = True
        else:
            run["destination_cleaned"] = False
        results["runs"].append(run)
        results["last_updated_at"] = utc_now()
        write_json(results_path, results)
        write_summary(summary_path, results)
        if completed.returncode:
            failed = True
            if args.stop_on_failure:
                break
    results["finished_at"] = utc_now()
    write_json(results_path, results)
    write_summary(summary_path, results)
    print("Comparison results written to {}".format(results_path))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
