#!/usr/bin/env python3

import json
import os
import pprint
import random
import time

import taskcluster
from natsort import natsorted

from worker_health import tc_helpers

# class goal: see if a worker is working


class Status:
    tc_wm = None
    root_url = "https://firefox-ci-tc.services.mozilla.com"

    def __init__(self, provisioner, worker_type):
        self.provisioner = provisioner
        self.worker_type = worker_type

        with open(os.path.expanduser("~/.tc_token")) as json_file:
            data = json.load(json_file)
        creds = {"clientId": data["clientId"], "accessToken": data["accessToken"]}

        self.tc_wm = taskcluster.WorkerManager({"rootUrl": self.root_url, "credentials": creds})
        self.tc_h = tc_helpers.TCHelper(provisioner=self.provisioner)

    def wait_until_no_jobs_running(self, hosts, sleep_seconds=15, show_indicator=True):
        we_have_waited = False
        while True:
            jrd = self.get_hosts_running_jobs(hosts)
            if len(jrd) == 0:
                if show_indicator and we_have_waited:
                    print("")
                break
            time.sleep(sleep_seconds)
            we_have_waited = True
            if show_indicator:
                print(".", end="")

    # given list of hosts, return one that's idle (once one is available)
    def wait_for_idle_host(self, hosts, sleep_time=15):
        return random.choice(self.wait_for_idle_hosts(hosts, sleep_time))

    # given list of hosts, return those that are idle (once one is available)
    def wait_for_idle_hosts(self, hosts, sleep_time=15, show_indicator=True):
        hosts_set = set(hosts)
        while True:
            if show_indicator:
                print(".", end="", flush=True)
            hosts_with_non_completed_or_failed_jobs_set = set(self.get_hosts_running_jobs(hosts))
            hosts_idle = hosts_set - hosts_with_non_completed_or_failed_jobs_set
            if hosts_idle:
                if show_indicator:
                    print("")
                return list(hosts_idle)
            if show_indicator:
                print("z", end="", flush=True)
            time.sleep(sleep_time)

    def get_hosts_running_jobs(self, hosts, verbose=False):
        worker_groups = self.tc_h.get_worker_groups(self.worker_type)
        worker_group = worker_groups[0]

        hosts_with_non_completed_or_failed_jobs = []
        hosts_checked = []
        for host in hosts:
            hosts_checked.append(host)
            results = self.tc_h.get_worker_jobs(self.worker_type, worker_group, host)
            # pprint.pprint(results)
            if "recentTasks" not in results:
                # if no recent tasks, host is not running jobs
                if verbose:
                    print(f"get_hosts_running_jobs: {host}: no recentTasks")
                continue

            for result in results["recentTasks"]:
                task_id = result["taskId"]
                # pprint.pprint(task_id)
                _tid, status_blob, _exc = self.tc_h.get_task_status(task_id)
                # pprint.pprint(status_blob)
                try:
                    t_status = status_blob["status"]["state"]
                except KeyError:
                    # TODO: do additional checking if problems occur with this...
                    #   example response that we are catching here:
                    #
                    #      {'code': 'ResourceNotFound',
                    #       'message': '`NpY_UNDIRAW8Jaix-kc-SA` does not correspond to a task that '
                    #                  'exists.\n'
                    #                  'Are you sure this task exists?\n'
                    #                  '\n'
                    #                  '---\n'
                    #                  '\n'
                    #                  '* method:     status\n'
                    #                  '* errorCode:  ResourceNotFound\n'
                    #                  '* statusCode: 404\n'
                    #                  '* time:       2023-06-28T19:39:09.337Z',
                    #       'requestInfo': {'method': 'status',
                    #                       'params': {'taskId': 'NpY_UNDIRAW8Jaix-kc-SA'},
                    #                       'payload': {},
                    #                       'time': '2023-06-28T19:39:09.337Z'}}

                    # print(f"{task_id}: no task[status][state] assuming task is not running.")
                    continue
                if t_status != "completed" and t_status != "failed" and t_status != "exception":
                    # TODO: show task id and state if in verbose?
                    # pprint.pprint(status_blob["status"]["state"])
                    hosts_with_non_completed_or_failed_jobs.append(host)
        return hosts_with_non_completed_or_failed_jobs

    # TOOD: rename/decompose (fetch and present) this function
    def show_jobs_running_report(self, hosts):
        hosts_with_non_completed_or_failed_jobs = self.get_hosts_running_jobs(hosts)
        hosts_checked = hosts

        # TODO: move this print into get_hosts_running_jobs()
        #   - less useful now that it's just a len call vs the internal value from above?
        print(f"hosts checked ({len(hosts_checked)}): {hosts_checked}")
        print(
            f"hosts_with_non_completed_or_failed_jobs ({len(hosts_with_non_completed_or_failed_jobs)}): "
            f"{hosts_with_non_completed_or_failed_jobs}",
        )

        # return hosts not idle
        return hosts_with_non_completed_or_failed_jobs

    def list_workers_human(self):
        results = self.tc_wm.listWorkers(self.provisioner, self.worker_type)
        return_str = ""
        # TODO: fix natsorting, copy how _py does it?
        for result in natsorted(results["workers"]):
            return_str += f"{result['workerPoolId']} {result['workerGroup']} {result['workerId']}\n"
        print(return_str)
        return return_str

    def list_workers_csv(self):
        results = self.tc_wm.listWorkers(self.provisioner, self.worker_type)
        return_str = ""
        # TODO: fix natsorting, copy how _py does it?
        for result in natsorted(results["workers"]):
            return_str += f"{result['workerId']},"
        # trim trailing comma
        print(return_str[:-1])

    def list_workers_py(self):
        results = self.tc_wm.listWorkers(self.provisioner, self.worker_type)
        return_arr = []
        for result in results["workers"]:
            return_arr.append(result["workerId"])
        pprint.pprint(natsorted(return_arr))


if __name__ == "__main__":
    si = Status("releng-hardware", "gecko-t-osx-1015-r8")
    # example usage
    # si.show_jobs_running_report(
    #     [
    #         "macmini-r8-1",
    #         # "macmini-r8-2",
    #         # "macmini-r8-3",
    #         # "macmini-r8-4",
    #         # "macmini-r8-5",
    #         # "macmini-r8-7",
    #         # "macmini-r8-8",
    #         # "macmini-r8-9",
    #         # "macmini-r8-10",
    #     ],
    # )
    # for futher debugging
    # import ipdb; ipdb.set_trace()

    # TODO: pull out into binary `list_workers`
    # si.list_workers_human()
    # si.list_workers_csv()
    si.list_workers_py()
