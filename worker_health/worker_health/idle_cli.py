"""Command-line interfaces for checking and waiting for idle Taskcluster workers."""

import argparse

from worker_health import status


def csv_hosts(value):
    """Parse a comma-separated worker list."""
    return value.split(",")


def workers_idle_main(argv=None):
    """Return zero when at least one requested worker is idle."""
    parser = argparse.ArgumentParser()
    parser.add_argument("provisioner")
    parser.add_argument("worker_type")
    parser.add_argument("host_csv", type=csv_hosts)
    args = parser.parse_args(argv)

    worker_status = status.Status(args.provisioner, args.worker_type)
    busy_hosts = worker_status.show_jobs_running_report(args.host_csv)
    return 0 if len(busy_hosts) != len(args.host_csv) else 1


def wait_for_tc_idle_main(argv=None):
    """Wait for all requested workers, or any one worker, to become idle."""
    parser = argparse.ArgumentParser(description="Wait for specified hosts to be idle.")
    parser.add_argument("--provisioner", "-p", required=True, help="Provisioner name (e.g., releng-hardware)")
    parser.add_argument("--worker-type", "-w", required=True, help="Worker type (e.g., gecko-t-linux-talos-1804)")
    parser.add_argument("hosts", nargs="+", help="List of hostnames to check")
    parser.add_argument("--single", "-s", action="store_true", help="Exit when any node is idle")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show polling progress")
    args = parser.parse_args(argv)

    worker_status = status.Status(args.provisioner, args.worker_type)
    if args.single:
        worker_status.wait_for_idle_hosts(args.hosts, sleep_time=10, show_indicator=args.verbose)
    else:
        worker_status.wait_until_no_jobs_running(args.hosts, sleep_seconds=10, show_indicator=args.verbose)
    return 0
