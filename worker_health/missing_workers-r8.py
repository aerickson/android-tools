#!/usr/bin/env python3

import argparse

from worker_health import fitness

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # parser.add_argument(
    #     "-v",
    #     "--verbose",
    #     action="count",
    #     dest="log_level",
    #     default=0,
    #     help="specify multiple times for even more verbosity.",
    # )
    parser.add_argument(
        "-i",
        "--inventory_file",
        help=f"path to ronin_puppet inventory file, default is {fitness.get_r8_inventory_path()}",
        default=fitness.get_r8_inventory_path(),
    )
    args = parser.parse_args()

    PROVISIONER = "releng-hardware"

    exclude_dict = {}

    f = fitness.Fitness(log_level=0, provisioner=PROVISIONER, alert_percent=85)
    # TODO: use verbosity=args.log_level
    # TODO: use exclude_dict=exclude_dict
    f.r8_worker_report(args.inventory_file)
