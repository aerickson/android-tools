#!/usr/bin/env python3

# drains host (quarantines and wait for jobs to finish),
#   runs a command, and then lifts the quarantine

import argparse

from worker_health import runner

VERSION = "2.0.0"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("runs a command against a set of hosts once they are quarantined and not working"),
    )
    parser.add_argument(
        "--resume_dir",
        "-r",
        required=True,
        metavar="RUN_DIR",
        help="recommend prefixing with 'sr_' if not pre-existing.",
    )
    parser.add_argument(
        "--do-not-randomize",
        "-N",
        action="store_true",
        help="don't randomize host list",
    )
    parser.add_argument(
        "--talk",
        "-t",
        action="store_true",
        help="use OS X's speech API to give updates",
    )
    parser.add_argument(
        "--reboot-host",
        "-R",
        action="store_true",
        help="reboot the host after command runs successfully.",
    )
    parser.add_argument(
        "--reset-state",
        "-S",
        action="store_true",
        help="reset the state section of the config file (all hosts to remaining_hosts)",
    )
    parser.add_argument(
        "--delete-output-files",
        "-O",
        action="store_true",
        help="delete the output files in the state directory",
    )
    parser.add_argument(
        "--dont-lift_quarantine",
        "-D",
        action="store_true",
        help=("don't lift the quarantine after successfully running. useful for pre-quarantined bad hosts."),
    )
    parser.add_argument(
        "--pre_quarantine_additional_host_count",
        "-P",
        help=(
            "quarantine the specified number of following hosts. "
            f"defaults to {runner.Runner.default_pre_quarantine_additional_host_count}. "
            "specify 0 to disable pre-quarantine."
        ),
        metavar="COUNT",
        type=int,
        default=runner.Runner.default_pre_quarantine_additional_host_count,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()
    # TODO: add as an exposed option?
    args.verbose = True

    runner.main(args, safe_mode=True)
