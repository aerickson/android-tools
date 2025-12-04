#!/usr/bin/env python

# waits for the hosts passed in to be idle
# - have a mode that exits on one or all
#
# example usage:
#   ./wait_for_tc_idle.py \
#     -p releng-hardware -w gecko-t-linux-talos-1804 \
#     t-linux64-ms-239,t-linux64-ms-240 -v

import argparse
import logging
import time
from worker_health import status


def main():
    parser = argparse.ArgumentParser(description="Wait for specified hosts to be idle.")
    parser.add_argument("--provisioner", "-p", required=True, help="Provisioner name (e.g., releng-hardware)")
    parser.add_argument("--worker-type", "-w", required=True, help="Worker type (e.g., gecko-t-linux-talos-1804)")
    parser.add_argument("hosts", nargs="+", help="List of hostnames to check")
    # add a --single argument, that enables a mode that exits when any node is idle
    parser.add_argument(
        "--single",
        "-s",
        action="store_true",
        help="Exit when any node is idle (vs default when all are idle)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.CRITICAL,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger(__name__)
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logger.setLevel(log_level)

    SLEEP_INTERVAL = 10  # seconds

    si = status.Status(args.provisioner, args.worker_type)
    hosts_with_non_completed_or_failed_jobs = si.get_hosts_running_jobs(args.hosts)

    logger.info(f"Waiting for hosts to be idle: {args.hosts}...")
    while True:
        # pprint.pprint(hosts_with_non_completed_or_failed_jobs)
        if args.single:
            # check if any hosts are idle
            input_set = set(args.hosts)
            result_set = set(hosts_with_non_completed_or_failed_jobs)
            difference = input_set - result_set
            if difference:
                logger.debug(f"Hosts no longer busy: {difference}")
                break
            pass
        else:
            # check if all hosts are idle
            if not hosts_with_non_completed_or_failed_jobs:
                logger.debug(f"All hosts are idle: {args.hosts}")
                break
        logger.debug(
            f"Hosts with non-completed or failed jobs: {hosts_with_non_completed_or_failed_jobs}. Sleeping {SLEEP_INTERVAL} seconds before rechecking...",
        )
        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()
