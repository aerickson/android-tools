#!/usr/bin/env python3

# checks if jobs are running on the workers specified (and if they're quarantined?)

# takes same args as quarantine-tool

import argparse
import re
import sys

from worker_health import status

# import ipdb


def natural_sort_key(s, _nsre=re.compile("([0-9]+)")):
    return [int(text) if text.isdigit() else text.lower() for text in _nsre.split(s)]


def csv_strs(vstr, sep=","):
    """Convert a string of comma separated values to strings
    @returns iterable of strings
    """
    values = []
    for v0 in vstr.split(sep):
        try:
            v = str(v0)
            values.append(v)
        except ValueError as err:
            raise argparse.ArgumentError("Invalid value %s, values must be a number (%s)" % (vstr, err))
    return values


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("provisioner")
    parser.add_argument("worker_type")
    parser.add_argument("host_csv", type=csv_strs)
    args = parser.parse_args()
    args.hosts = args.host_csv

    si = status.Status(args.provisioner, args.worker_type)

    # print(args)
    not_idle_hosts = si.show_jobs_running_report(args.hosts)

    # TODO: have flag for all mode (re: should this return 0 if any idle workers? vs only if they're all?)
    # if there are any idle hosts, return 0, else 1
    if len(not_idle_hosts) != len(args.hosts):
        sys.exit(0)
    sys.exit(1)
