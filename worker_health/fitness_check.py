#!/usr/bin/env python3

import argparse
import sys

from worker_health import fitness

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # TODO: currently only sorts within worker-group (so sometimes results aren't sorted)... ideally sort all results.
    parser.add_argument(
        "-s",
        "--success_rate",
        action="store_const",
        const="sr",
        default="worker_id",  # sorts by worker name by default
        dest="sort_order",
        help="sort results by success rate (default is worker_id).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        dest="log_level",
        default=0,
        help="specify multiple times for even more verbosity.",
    )
    parser.add_argument(
        "-a",
        "--alert-percent",
        default=fitness.ALERT_PERCENT,
        type=float,
        help="percentage of successful jobs to alert at. 0 to 1, defaults to %s." % fitness.ALERT_PERCENT,
    )
    parser.add_argument(
        "-t",
        "--alert-time",
        default=fitness.ALERT_TIME,
        type=int,
        help="alert if a worker hasn't worked in this many minutes, defaults to %s." % fitness.ALERT_TIME,
    )
    parser.add_argument(
        "-o",
        "--only-show-alerting",
        action="store_true",
        default=False,
        help="only show alerting hosts",
    )
    parser.add_argument(
        "-p",
        "--provisioner",
        default=fitness.DEFAULT_PROVISIONER,
        metavar="provisioner",
        help="provisioner to inspect, defaults to %s." % fitness.DEFAULT_PROVISIONER,
    )
    parser.add_argument(
        "-hh",
        "--humanize-hashes",
        default=False,
        action="store_true",
        help="hostnames are human-hashed",
    )
    parser.add_argument(
        "--ping",
        default=False,
        action="store_true",
        help="ping hosts also",
    )
    # TODO: can we get this from TC?
    parser.add_argument(
        "--ping-domain",
        default=None,
        metavar="subdomain.corp.com",
        help="subdomain to append to hosts",
    )
    parser.add_argument(
        "--ping-host",
        default=None,
        metavar="host",
        help="ssh to this host before pinging",
    )
    parser.add_argument(
        "worker_type_id",
        metavar="worker_type[.worker_id]",
        help="e.g. 'gecko-t-bitbar-gw-perf-p2.pixel2-21' or 'gecko-t-bitbar-gw-batt-g5'",
        nargs="?",
    )

    args = parser.parse_args()
    # print(args)
    # sys.exit(0)

    if not (0 < args.alert_percent < 1):
        print("ERROR: --alert-percent must be between 0 and 1.")
        sys.exit(1)

    # TODO: sanity check alert_time?

    arg_worker_type = None
    arg_worker_id = None
    if args.worker_type_id:
        arg_worker_type_id_split = args.worker_type_id.split(".")
        arg_worker_type = arg_worker_type_id_split[0]
        if len(arg_worker_type_id_split) == 2:
            arg_worker_id = arg_worker_type_id_split[1]

    f = fitness.Fitness(
        log_level=args.log_level,
        provisioner=args.provisioner,
        alert_percent=args.alert_percent,
        alert_time=args.alert_time,
    )
    # TODO: just pass args?
    f.args = args
    f.main(args.provisioner, arg_worker_type, arg_worker_id)
