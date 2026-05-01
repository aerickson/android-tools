#!/usr/bin/env python

# generate.py: allocates workers to queues based on queue
#               counts among device types (p2, g5).
#

import hashlib
import json
import os
import pprint
import subprocess
import sys
import time

import yaml

if sys.version_info <= (3, 0):
    print("Sorry, requires Python 3.x, not Python 2.x.")
    sys.exit(1)


from collections import OrderedDict
from urllib.error import HTTPError
from urllib.request import urlopen

verbose = False


class DPCGException(Exception):
    pass


class DevicePoolConfigGenerator:
    def __init__(self, daemon_mode=True, config_dir=None):
        self.config_file_name = "config.yml"
        self.prev_config_file_name = "config.yml.prev"
        self.original_config_file_name = "config.yml.original"
        self.raw_file = "config-dg-raw.yml"
        self.sleep_time_min = 15
        self.sleep_time_sec = self.sleep_time_min * 60

        self.total_managed_devices = None

        self.daemon_mode = daemon_mode
        if config_dir:
            self.config_dir = config_dir
        else:
            self.config_dir = os.getcwd()

    def get_path(self, filename):
        path = os.path.abspath(os.path.join(self.config_dir, filename))
        # print(path)
        return path

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

    def split_dict_based_on_device(self, a_dict):
        dev_split_dict = {}
        for k, v in a_dict.items():
            # print("%s: %s" % (k, v))
            if "motog5" in k:
                dev_split_dict.setdefault("motog5", {})[k] = v
            elif "pixel2" in k:
                dev_split_dict.setdefault("pixel2", {})[k] = v
        return dev_split_dict

    def device_type_from_string(self, a_string):
        if "pixel2" in a_string or "p2" in a_string:
            return "pixel2"
        elif "motog5" in a_string or "g5" in a_string:
            return "motog5"
        else:
            raise Exception("invalid input string '%s'!" % a_string)

    def queue_type_from_string(self, a_string):
        if "unit" in a_string:
            return "unit"
        elif "perf" in a_string:
            return "perf"
        elif "batt" in a_string:
            return "batt"
        elif "test" in a_string or "builder" in a_string:
            return "test"
        else:
            raise Exception("invalid input string '%s'!" % a_string)

    def extract_devices_from_device_groups(self, device_groups, device_groups_to_extract):
        result_dict = OrderedDict()
        for dge in device_groups_to_extract:
            dg_device_type = self.device_type_from_string(dge)
            for device_id, _v in device_groups[dge].items():
                result_dict.setdefault(dg_device_type, set()).add(device_id)

        return result_dict

    def main(self):
        if not self.daemon_mode:
            try:
                self.generate()
            except HTTPError:
                print("request failed")
                sys.exit(1)
        else:
            while True:
                # TODO: infinite loop over running and sleeping X minutes
                try:
                    self.generate()
                except HTTPError:
                    print("request failed, skipping this cycle...")
                print("Sleeping for %s minutes..." % self.sleep_time_min)
                print("--")
                time.sleep(self.sleep_time_sec)

    def device_structure_count(self, a_dict):
        total = 0
        for _k, v in a_dict.items():
            total += len(v)
        return total

    def list_to_empty_dict(self, a_list):
        res_dict = {}
        for item in a_list:
            res_dict[item] = " "
        return res_dict

    def generate(self):
        # TODO: take path to this file as arg, don't imply it's in .
        if not os.path.exists(self.get_path(self.original_config_file_name)):
            raise DPCGException("Can't find %s in %s!" % (self.original_config_file_name, self.config_dir))

        #### configuration ideas
        # meta:
        #   config = urls, devicepool groups managed, minimum config
        #
        # urls: see below, simpler?
        # devicepool groups managed: ['pixel2-perf-2', 'pixel2-perf-2', 'motog5-perf-2']
        # _min_host_dict = {'motog5': {'unit': 10, 'perf': 0},
        #                   'pixel2': {'unit': 0, 'perf': 0}
        #                 }
        minimum_device_dict = {
            "pixel2-perf-2": 4,
            "pixel2-unit-2": 8,
            "motog5-perf-2": 6,
        }

        # raw device groups to queues to check
        # tc-w queue first
        mapping_dict = {}
        mapping_dict["motog5-perf-2"] = {
            "g-w": "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/pending/proj-autophone/gecko-t-bitbar-gw-perf-g5",
        }
        mapping_dict["pixel2-perf-2"] = {
            "g-w": "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/pending/proj-autophone/gecko-t-bitbar-gw-perf-p2",
        }
        mapping_dict["pixel2-unit-2"] = {
            "g-w": "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/pending/proj-autophone/gecko-t-bitbar-gw-unit-p2",
        }

        if verbose:
            print("---------------- phase 0: show hardcoded options")
            print("minimum device dict: ")
            pprint.pprint(minimum_device_dict)

        config_yml = None
        # load config
        with open(self.get_path(self.original_config_file_name), "r") as stream:
            try:
                config_yml = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)
        if verbose:
            print("---------------- phase 0.5: read original config file")
            print("output disabled, TODO: add -vv support")
            # pprint.pprint(config_yml)

        # load raw yaml / find pools we're working on
        split_dict = OrderedDict()  # stores our modified device groups
        dg_raw_yml = config_yml["device_groups"]
        managed_dgs = sorted(["pixel2-unit-2", "pixel2-perf-2", "motog5-perf-2"])
        managed_devices_by_type_dict = self.extract_devices_from_device_groups(dg_raw_yml, managed_dgs)

        total_managed_devices = self.device_structure_count(managed_devices_by_type_dict)
        if verbose:
            print("---------------- phase 1: load managed devices")
            print("managed device groups:")
            pprint.pprint(managed_dgs)
            print("managed devices:")
            pprint.pprint(managed_devices_by_type_dict)
            print("number of managed devices: %s" % total_managed_devices)

        # see if there are tasks present
        tasks_present = {}
        for k, v in mapping_dict.items():
            current_count = self.get_url(v["g-w"])["pendingTasks"]
            tasks_present[k] = current_count
        if verbose:
            print("---------------- phase 2: get tc queue status")
            pprint.pprint(tasks_present)

        ### TESTING
        # tasks_present = {'motog5-perf-2': 100, 'pixel2-perf-2': 100, 'pixel2-unit-2': 0}
        # tasks_present = {'motog5-perf-2': 100, 'pixel2-perf-2': 0, 'pixel2-unit-2': 100}
        # tasks_present = {'motog5-perf-2': 100, 'pixel2-unit-2': 0, 'pixel2-perf-2': 100}
        # tasks_present = {'motog5-perf-2': 10, 'pixel2-perf-2': 38, 'pixel2-unit-2': 2}

        # form decision
        decision = {}
        dev_split_dict = self.split_dict_based_on_device(tasks_present)
        for device_type, queue_list in dev_split_dict.items():
            queue_len = len(queue_list)
            if queue_len == 1:
                queue_name = next(iter(queue_list))
                decision.setdefault(device_type, {})[queue_name] = 1.0
            elif queue_len == 2:
                # calculate sum for use in ratio
                sum = 0
                for dg, v in queue_list.items():
                    sum += v
                for dg, v in queue_list.items():
                    if sum != 0:
                        decision.setdefault(device_type, {})[dg] = float(v) / float(sum)
                    else:
                        decision.setdefault(device_type, {})[dg] = 0
            else:
                raise Exception("my mind is blown! don't know how to handle this (%s)" % queue_len)

        ### TESTING: override this value
        # decision = {'motog5': {'motog5-perf-2': 1.0},
        #             'pixel2': {'pixel2-unit-2': 0.8636363636363636, 'pixel2-perf-2': 0.13636363636363635,
        #             }}
        #
        #  decision = {'motog5': {'motog5-perf-2': 1.0},
        # 'pixel2': {'pixel2-perf-2': 0.6666666666666666,
        #             'pixel2-unit-2': 0.3333333333333333}}

        if verbose:
            print("---------------- phase 3: set split ratios")
            # pprint.pprint(dev_split_dict)
            pprint.pprint(decision)

        # phase 3 output format
        #     {'motog5': {'motog5-perf-2': 1.0},
        #      'pixel2': {'pixel2-perf-2': 1.0, 'pixel2-unit-2': 0.0}}

        # sys.exit()

        device_decision = {}
        for dt, v in decision.items():
            queue_count = len(v)
            if queue_count == 1:
                queue_name = next(iter(v))
                device_type_total = len(managed_devices_by_type_dict[dt])
                device_decision.setdefault(dt, {})[queue_name] = device_type_total
            elif queue_count == 2:
                device_type_total = len(managed_devices_by_type_dict[dt])
                devices_left = device_type_total
                queues_handled = 1
                # TODO: indicate that this is based on sorting (and minimums), or figure out a better way to sort... an importance weight?
                # sort this, we need small percentage first so it gets a minimum
                for queue, percentage in sorted(v.items(), key=lambda x: x[1], reverse=False):
                    if queues_handled == 1:
                        device_decision.setdefault(dt, {})[queue] = int(percentage * devices_left)
                        # add minimum devices if configured
                        if queue in minimum_device_dict.keys():
                            if device_decision.setdefault(dt, {})[queue] < minimum_device_dict[queue]:
                                device_decision.setdefault(dt, {})[queue] = minimum_device_dict[queue]
                                if verbose:
                                    print("setting minimum for %s" % queue)
                        devices_left -= device_decision.setdefault(dt, {})[queue]
                    else:
                        device_decision.setdefault(dt, {})[queue] = devices_left
                    queues_handled += 1
            else:
                raise Exception("my mind is blown 2! don't know how to handle this (%s)" % queue_count)

        if verbose:
            print("---------------- phase 3.5: convert ratios to devices (with minimums)")
            print("device decision: ")
            pprint.pprint(device_decision)

            temp_counts = {}
            print("count of allocated devices: ")
            for dev_type, device_group in device_decision.items():
                for k, v in device_group.items():
                    temp_counts[dev_type] = temp_counts.get(dev_type, 0) + v
                    temp_counts["total"] = temp_counts.get("total", 0) + v
            pprint.pprint(temp_counts)

            # TODO: sanity check here
            if temp_counts["total"] != total_managed_devices:
                print("WARNING: counts don't match (should be %s)" % total_managed_devices)

        for device_type, device_groups in device_decision.items():
            if len(device_groups) == 1:
                queue_name = next(iter(device_groups))
                devices_to_work_with = sorted(managed_devices_by_type_dict[device_type])
                split_dict[queue_name] = devices_to_work_with
            if len(device_groups) == 2:
                devices_to_work_with = sorted(managed_devices_by_type_dict[device_type])
                devices_allocated = 0
                for dg, host_count in device_groups.items():
                    split_dict[dg] = devices_to_work_with[
                        devices_allocated :   (  # noqa: E203
                            int(host_count) + devices_allocated
                        )
                    ]
                    devices_allocated = devices_allocated + int(host_count) + devices_allocated

        if verbose:
            print("---------------- phase 4: split device groups")
            pprint.pprint(split_dict)

        # convert array to dict
        for key, arr in split_dict.items():
            dg_raw_yml[key] = self.list_to_empty_dict(arr)
        if verbose:
            print("---------------- phase 5: convert arrays to dicts")
            pprint.pprint(dg_raw_yml)

        # with open('testing.yml', 'w') as outfile:
        #     yaml.dump(dg_raw_yml, outfile)

        # combine config file with our new sections
        if verbose:
            print("---------------- phase 7: combine original with modified groups")
        for key, arr in dg_raw_yml.items():
            config_yml["device_groups"][key] = arr

        # if verbose:
        #     print("---------------- phase 7.5: sanity check")

        # final report
        ts = time.localtime()
        print("current time: %s" % time.strftime("%Y-%m-%d %H:%M:%S", ts))
        print("tasks pending: %s" % pprint.pformat(tasks_present))
        print("minimum devices: %s" % pprint.pformat(minimum_device_dict))
        print("decided ratio: %s" % pprint.pformat(decision))
        count_dict = {}
        total_devices = 0
        for k, v in split_dict.items():
            count_dict[k] = len(v)
            total_devices += len(v)
        print("device allocations: %s" % pprint.pformat(count_dict))
        print("devices used/managed: %s/%s" % (total_devices, total_managed_devices))

        if total_devices != total_managed_devices:
            print("Not all devices were used! (%s, %s)" % (total_devices, total_managed_devices))
            sys.exit(1)

        if verbose:
            print("---------------- phase 8: write merged config")
        # move old config to .prev
        if os.path.exists(self.get_path(self.config_file_name)):
            os.rename(
                self.get_path(self.config_file_name),
                self.get_path(self.prev_config_file_name),
            )
        # write it
        with open(self.get_path(self.config_file_name), "w") as outfile:
            yaml.dump(config_yml, outfile)
        print("Config written (backup made also).")

        # TODO: automated sanity check... how?
        # - check that the top halfs are identical?
        # - check that total number of devices is the same
        # - check that for un-managed groups, the device count is the same

        if os.path.exists(self.get_path(self.prev_config_file_name)):
            digest_old = self.get_digest(self.get_path(self.prev_config_file_name))
            digest_new = self.get_digest(self.get_path(self.config_file_name))
            if digest_old != digest_new:
                print("Config change detected.")
                if verbose:
                    print(digest_new)
                    print(digest_old)
                if self.daemon_mode:
                    # TODO: use a separate flag to whether we restart the service?
                    restart_command = subprocess.run(["systemctl", "restart", "bitbar"])
                    if restart_command.returncode == 0:
                        print("service restarted")
                    else:
                        print(
                            "service NOT restarted. restart command likely failed (rc: %s)"
                            % restart_command.returncode,
                        )
            else:
                print("Config did not change.")
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
    parser.add_argument("-v", "--verbose", help="verbose mode", action="store_true", default=False)
    args = parser.parse_args()

    # hacky
    verbose = args.verbose

    dpcg = DevicePoolConfigGenerator(config_dir=args.config_dir, daemon_mode=args.daemon)
    try:
        dpcg.main()
    except DPCGException as e:
        print("ERROR: %s:" % e)
        sys.exit(1)
