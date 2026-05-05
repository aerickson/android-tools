#!/usr/bin/env python3

import argparse

from worker_health import status

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="lists taskcluster workers given a provisioner and worker_type")
    parser.add_argument("provisioner", help="e.g. releng-hardware or gecko-t")
    parser.add_argument("worker_type", help="e.g. gecko-t-osx-1015-r8")
    parser.set_defaults(mode="py")
    # mode options
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--human",
        "-u",
        help="human readable output format",
        action="store_const",
        const="human",
        dest="mode",
    )
    group.add_argument(
        "--csv",
        "-c",
        help="comma separated value format",
        action="store_const",
        const="csv",
        dest="mode",
    )
    parser.add_argument(
        "--omit-quarantined",
        "-o",
        help="exclude quarantined workers from output",
        action="store_true",
        default=False,
    )
    # parse it
    args = parser.parse_args()

    si = status.Status(args.provisioner, args.worker_type)
    if args.mode == "human":
        si.list_workers_human(omit_quarantined=args.omit_quarantined)
    elif args.mode == "csv":
        si.list_workers_csv(omit_quarantined=args.omit_quarantined)
    elif args.mode == "py":
        si.list_workers_py(omit_quarantined=args.omit_quarantined)
    else:
        raise Exception("shouldn't be here")
