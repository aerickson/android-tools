#!/usr/bin/env python3

import argparse
import re
import pprint
import pendulum

from worker_health import quarantine
from worker_health import tc
from worker_health.utils import date_in_past, human_delta


def natural_sort_key(s, _nsre=re.compile("([0-9]+)")):
    return [int(text) if text.isdigit() else text.lower() for text in _nsre.split(s)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("provisioner")
    # don't make it required in argparse, require below (so we can list the available worker types)
    parser.add_argument("worker_type", nargs="?")

    sub_parsers = parser.add_subparsers(help="action to take:", dest="action")

    # show
    parser_show = sub_parsers.add_parser("show", help="show quarantined hosts")
    parser_show.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="enable verbose output (can be used multiple times for increased verbosity)",
    )
    # show-all
    parser_show_all = sub_parsers.add_parser(
        "show-all",
        help="show all hosts in a pool",
    )
    # quarantine
    parser_quarantine = sub_parsers.add_parser(
        "quarantine",
        help="quarantine a set of hosts",
    )
    parser_quarantine.add_argument(
        "-r",
        "--reason",
        help="why the instance is being quarantined",
    )
    parser_quarantine.add_argument("hosts", nargs="?")
    # lift
    parser_lift = sub_parsers.add_parser(
        "lift",
        help="lift the quarantine on a set of hosts",
    )
    parser_lift.add_argument(
        "-r",
        "--reason",
        help="why the quarantine is being lifted",
    )
    parser_lift.add_argument("hosts", nargs="?")

    args = parser.parse_args()

    # import pprint
    # import sys
    # pprint.pprint(args)
    # sys.exit(1)

    if args.provisioner is not None and ("worker_type" not in args or args.worker_type is None):
        results = tc.get_worker_types(args.provisioner, 0)
        # worker_types = [item['workerType'] for item in results['workerTypes']]
        worker_types_string = ""
        for item in results["workerTypes"]:
            worker_types_string += "  " + item["workerType"] + "\n"
        parser.error(
            f"you must specify a worker_type.\n\nvalid worker types for provisioner {args.provisioner}:\n{worker_types_string.rstrip()}",
        )
        # show worker_types in provisioner

    if args.action == "quarantine":
        if args.hosts is None:
            parser.error("you must specify a comma-separated string of hosts")
        host_arr = args.hosts.split(",")
        q = quarantine.Quarantine()
        if args.reason:
            q.quarantine(
                args.provisioner,
                args.worker_type,
                host_arr,
                reason=args.reason,
            )
        else:
            q.quarantine(args.provisioner, args.worker_type, host_arr)
    elif args.action == "lift":
        if args.hosts is None:
            parser.error("you must specify a comma-separated string of hosts")
        host_arr = args.hosts.split(",")
        q = quarantine.Quarantine()
        if args.reason:
            q.lift_quarantine(
                args.provisioner,
                args.worker_type,
                host_arr,
                reason=args.reason,
            )
        else:
            q.lift_quarantine(args.provisioner, args.worker_type, host_arr)
    elif args.action == "show":
        # TODO: check that the worker_type is valid
        # TODO: -v shows reason, -vv shows full json
        q = quarantine.Quarantine()
        # results = q.get_quarantined_workers(provisioner=args.provisioner, worker_type=args.worker_type)
        results = q.get_quarantined_workers_structured(
            provisioner=args.provisioner,
            worker_type=args.worker_type,
        )
        if not results:
            print("no results")
        else:
            quarantine_info = results["quarantine_info"]
            quarantined_workers = results["quarantined_workers"]
            formatted_workers = sorted(
                quarantined_workers,
                key=lambda d: "{0:0>8}".format(d.replace("macmini-r8-", "")),
            )
            if args.verbose == 3:
                print(",".join(formatted_workers))
                pprint.pprint(quarantine_info)
            if args.verbose == 1 or args.verbose == 2:
                for device in quarantine_info:
                    print(f"{device}:")
                    for entry in quarantine_info[device]:
                        reason = entry["quarantineInfo"]
                        date = entry["updatedAt"]
                        date_obj = pendulum.parse(date)
                        date_until = entry["quarantineUntil"]
                        user = entry["clientId"].split("|")[2]
                        in_past = date_in_past(date_until)
                        time_diff = pendulum.now() - date_obj
                        output_line = ""
                        if in_past:
                            # lifting
                            output_line = f"  L/{user}: {reason},  {human_delta(time_diff.total_seconds())} ago"
                        else:
                            # quarantining
                            output_line = f"  Q/{user}: {reason},  {human_delta(time_diff.total_seconds())} ago"
                        if args.verbose == 2:
                            print(output_line)
                    if args.verbose == 1:
                        print(output_line)  # only show latest event at -v/1
            else:
                print(",".join(formatted_workers))
    elif args.action == "show-all":
        # TODO: check that the worker_type is valid
        results = tc.get_workers(args.provisioner, args.worker_type)
        output = ""

        # sort just based on the numerical element of the workerId
        # how to avoid needing multiple of these for each type?
        #   - split on '-' and others and use last part?
        sorted_list_of_dicts = sorted(
            results["workers"],
            key=lambda d: "{0:0>8}".format(d["workerId"].replace("macmini-r8-", "")),
        )

        for item in sorted_list_of_dicts:
            output += "%s," % item["workerId"]
        # trim last comma off
        print(output[0:-1])

        # TODO: add switch that uses this mode
        # csv
        # print("workerId")
        # for item in results["workers"]:
        #     # print("%s,%s" % (item["workerGroup"], item["workerId"]))
        #     print(item["workerId"])
    else:
        parser.error("please specify an action")
