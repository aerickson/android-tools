#!/usr/bin/env python

# generate_v1.py: balance devices between tc-w and g-w workers (queues)
#                 within queue types (p2-unit, g5-perf).
#
#                 used during tc-w -> g-w migration.

import hashlib
import json
import os
import pprint
import sys
import time
from collections import OrderedDict
from urllib.request import urlopen

import yaml

verbose = False

## trying to get nothing vs null in outputted yml
## https://stackoverflow.com/questions/37200150/can-i-dump-blank-instead-of-null-in-yaml-pyyaml
# def represent_none(self, _):
#     return self.represent_scalar('tag:yaml.org,2002:null', '')
# def represent_none(self, data):
#     return self.represent_scalar(u'tag:yaml.org,2002:null',
#                               u'')
# yaml.add_representer(type(None), represent_none)


class DPCGException(Exception):
    pass


class DevicePoolConfigGenerator:
    def __init__(self, daemon_mode=True, config_dir=None):
        self.config_file_name = "config.yml"
        self.prev_config_file_name = "config.yml.prev"
        self.original_config_file_name = "config.yml.original"
        self.raw_file = "config-dg-raw.yml"
        self.sleep_time_min = 5
        self.sleep_time_sec = self.sleep_time_min * 60

        self.daemon_mode = daemon_mode
        if config_dir:
            self.config_dir = config_dir
        else:
            self.config_dir = os.getcwd()

    def get_path(self, filename):
        return os.path.abspath(os.path.join(self.config_dir, filename))

    @staticmethod
    def get_digest(file_path):
        h = hashlib.sha256()

        with open(file_path, "rb") as file:
            while True:
                # Reading is buffered, so we can read smaller chunks.
                chunk = file.read(h.block_size)
                if not chunk:
                    break
                h.update(chunk)

        return h.hexdigest()

    @staticmethod
    def get_url(url):
        data = urlopen(url).read()
        output = json.loads(data)
        return output

    @staticmethod
    def array_to_dict_of_none(array):
        result_dict = {}
        for item in array:
            result_dict[item] = None
        return result_dict

    # https://stackoverflow.com/questions/13423759/percent-list-slicing
    # TODO: add tests for this
    @staticmethod
    def split_list(a_list, slice_start=0, slice_end=0.5):
        newList = a_list[
            int(len(a_list) * slice_start) : int(
                len(a_list) * slice_end,
            )  # noqa: E203
        ]
        return newList

    def main(self):
        if not self.daemon_mode:
            self.generate()
        else:
            while True:
                # TODO: infinite loop over running and sleeping X minutes
                self.generate()
                print("Sleeping for %s minutes..." % self.sleep_time_min)
                print("--")
                time.sleep(self.sleep_time_sec)

    def generate(self):
        # TODO: take path to this file as arg, don't imply it's in .
        if not os.path.exists(self.get_path(self.original_config_file_name)):
            raise DPCGException("Can't find %s in %s!" % (self.original_config_file_name, self.config_dir))

        # #    "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-unit-p2",
        # #    "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-perf-p2",
        # #    "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-batt-p2",
        # #    "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-unit-g5",
        # #    "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-perf-g5",
        # #    "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-batt-g5",
        # #    "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-test-g5",
        #     "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-bitbar-gw-batt-p2",
        #     "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-bitbar-gw-perf-p2",
        #     "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-bitbar-gw-unit-p2",
        #     "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-bitbar-gw-batt-g5",
        #     "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-bitbar-gw-perf-g5",
        #     "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-bitbar-gw-unit-g5",
        #     "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-bitbar-gw-test-g5",

        # raw device groups to queues to check
        # tc-w queue first
        mapping_dict = {}
        mapping_dict["motog5-perf"] = {
            "tc-w": "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-perf-g5",
            "g-w": "",
        }
        mapping_dict["pixel2-perf"] = {
            "tc-w": "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-perf-p2",
            "g-w": "",
        }
        mapping_dict["pixel2-unit"] = {
            "tc-w": "https://queue.taskcluster.net/v1/pending/proj-autophone/gecko-t-ap-unit-p2",
            "g-w": "",
        }

        config_yml = None
        # load config
        with open(self.get_path(self.original_config_file_name), "r") as stream:
            try:
                config_yml = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)
        if verbose:
            print("---------------- phase 0: read original config file")
            # pprint.pprint(config_yml)

        # load raw yaml / find pools we're working on
        dg_raw_yml = None
        # TODO: generate config-dg-raw.yml vs having to make it manually
        with open(self.get_path(self.raw_file), "r") as stream:
            try:
                dg_raw_yml = yaml.safe_load(stream)["device_groups"]
            except yaml.YAMLError as exc:
                print(exc)
        if verbose:
            print("---------------- phase 1: load configuration")
            pprint.pprint(dg_raw_yml)

        # see if there are tasks present
        tasks_present = {}
        # TODO: this should iterate over dg_raw_yml
        for k, v in mapping_dict.items():
            current_count = self.get_url(v["tc-w"])["pendingTasks"]
            tasks_present[k] = current_count
        if verbose:
            print("---------------- phase 2: get tc queue status")
            pprint.pprint(tasks_present)

        # form decision
        decision = {}
        # result:
        #   decision["motog5-perf"] = 0.8
        #   decision["pixel2-perf"] = 0.3
        # TODO: consider more advanced ways of deciding this
        for k, v in tasks_present.items():
            # print("%s: %s" % (k, v))
            if v >= 4:
                decision[k] = 0.8
            else:
                decision[k] = 0.0
        if verbose:
            print("---------------- phase 3: set split ratios")
            pprint.pprint(decision)

        # split the raw yml according to decision above
        split_dict = OrderedDict()
        for key, arr in decision.items():
            sorted_keys = sorted(dg_raw_yml[key].keys())
            split_dict[key] = self.split_list(sorted_keys, 0, decision[key])
            split_dict["%s-2" % key] = self.split_list(sorted_keys, decision[key], 1)
        if verbose:
            print("---------------- phase 4: split device groups")
            for k, v in split_dict.items():
                # print("%s: %s" % (k, len(v)))
                pprint.pprint(v)

        # convert array to dict
        for key, arr in split_dict.items():
            dg_raw_yml[key] = self.array_to_dict_of_none(arr)
        if verbose:
            print("---------------- phase 5: convert arrays to dicts")
            pprint.pprint(dg_raw_yml)

        # with open('testing.yml', 'w') as outfile:
        #     yaml.dump(dg_raw_yml, outfile)

        # combine config file with our new sections
        if verbose:
            print("---------------- phase 7: combine original with modified groups")
        for key, arr in split_dict.items():
            config_yml["device_groups"][key] = arr

        # pprint.pprint(config_yml)
        # sys.exit(0)

        # move old config to .prev
        if os.path.exists(self.get_path(self.config_file_name)):
            os.rename(
                self.get_path(self.config_file_name),
                self.get_path(self.prev_config_file_name),
            )
        # write it
        with open(self.get_path(self.config_file_name), "w") as outfile:
            yaml.dump(config_yml, outfile)
        if verbose:
            print("---------------- phase 8: write merged config")

        # final report
        ts = time.localtime()
        print(time.strftime("%Y-%m-%d %H:%M:%S", ts))
        print("tasks pending: %s" % pprint.pformat(tasks_present))
        print("decided ratio: %s" % pprint.pformat(decision))
        count_dict = {}
        for k, v in split_dict.items():
            count_dict[k] = len(v)
        print("device counts: %s" % pprint.pformat(count_dict))
        print("Config written.")

        # TODO: automated sanity check... how?
        # - check that the top halfs are identical?
        # - check that total number of devices is the same
        # - check that for un-managed groups, the device count is the same

        if os.path.exists(self.get_path(self.prev_config_file_name)):
            digest_old = self.get_digest(self.get_path(self.prev_config_file_name))
            digest_new = self.get_digest(self.get_path(self.config_file_name))
            if digest_old != digest_new:
                print("Config change detected.")
                if self.daemon_mode:
                    # TODO: do restart?
                    pass
            else:
                # print("Config did not change.")
                pass
        else:
            print("No .prev file exists, not diffing config files.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d",
        "--daemon",
        default=False,
        action="store_true",
        help="run in daemon mode (continuously)",
    )
    parser.add_argument("-c", "--config-dir", help="path to the directory with config.yml")
    args = parser.parse_args()

    dpcg = DevicePoolConfigGenerator(config_dir=args.config_dir, daemon_mode=args.daemon)
    try:
        dpcg.main()
    except DPCGException as e:
        print("ERROR: %s:" % e.message)
        sys.exit(1)
