import json
import os
import pprint

import taskcluster

from worker_health import fitness

# see https://github.com/mozilla-platform-ops/relops-infra/blob/master/quarantine_tc.py
# for prior art


class QuarantineError(RuntimeError):
    pass


class Quarantine:
    tc_queue = None
    tc_worker_manager = None
    root_url = "https://firefox-ci-tc.services.mozilla.com"

    def __init__(self):
        with open(os.path.expanduser("~/.tc_token")) as json_file:
            data = json.load(json_file)
        creds = {"clientId": data["clientId"], "accessToken": data["accessToken"]}

        self.tc_queue = taskcluster.Queue(
            {"rootUrl": self.root_url, "credentials": creds},
        )
        self.tc_worker_manager = taskcluster.WorkerManager(
            {"rootUrl": self.root_url, "credentials": creds},
        )

    def quarantine(
        self,
        provisioner_id,
        worker_type,
        host_arr,
        reason="worker-health api call: quarantine",
        duration="10 years",
        verbose=True,
    ):
        # TODO: if host is already quarantined, short-circuit and return

        # try to detect worker group
        wgs = self.get_worker_groups(
            provisioner=provisioner_id,
            worker_type=worker_type,
        )
        if len(wgs) > 1:
            raise QuarantineError(
                "can't guess workerGroup, multiple present. support not implemented yet.",
            )
        if len(wgs) == 0:
            raise QuarantineError(f"couldn't find a matching workerType ('{worker_type}')!")
        worker_group = wgs[0]

        for a_host in host_arr:
            if "-" in duration:
                if verbose:
                    print(f"lifting quarantine on {a_host}... ")
            else:
                if verbose:
                    print(f"adding {a_host} to quarantine... ")
            self.tc_queue.quarantineWorker(
                provisioner_id,
                worker_type,
                worker_group,
                a_host,
                {
                    "quarantineUntil": taskcluster.fromNow(duration),
                    "quarantineInfo": reason,
                },
            )

    def lift_quarantine(
        self,
        provisioner,
        worker_type,
        device_arr,
        reason="worker-health api call: lifting quarantine",
        verbose=True,
    ):
        # TODO: catch exception and wrap?
        self.quarantine(
            provisioner,
            worker_type,
            device_arr,
            duration="-1 year",
            reason=reason,
            verbose=verbose,
        )

    # TODO: use the implementation in tc_helpers?
    def get_worker_groups(self, provisioner, worker_type):
        f = fitness.Fitness(log_level=0, provisioner=provisioner, alert_percent=85)
        output = f.get_workers(worker_type)["workers"]
        worker_groups = {}
        for element in output:
            a_worker_group = element["workerGroup"]
            worker_groups[a_worker_group] = True
        return list(worker_groups.keys())

    def get_workers(self, provisioner, worker_type):
        f = fitness.Fitness(log_level=0, provisioner=provisioner, alert_percent=85)
        # TODO: relocate this function to a base lib
        output = f.get_workers(worker_type)
        return output

    def get_quarantined_workers(self, provisioner, worker_type):
        workers = []
        self.tc_queue.listWorkers(
            provisioner,
            worker_type,
            query={"quarantined": "true"},
            paginationHandler=lambda outcome: workers.extend(outcome.get("workers", [])),
        )

        quarantined_workers = {}
        for item in workers:
            hostname = item["workerId"]
            quarantined_workers[hostname] = item.get("quarantineUntil")
        return quarantined_workers

    # TODO: make this run faster by only getting details when needed
    #   - passing in details=False and only doing first query?
    # TODO: replace all usages of get_quarantined_workers() with this
    def get_quarantined_workers_structured(self, provisioner, worker_type, skip_details=False):
        result_dict = {}
        workers = []
        self.tc_queue.listWorkers(
            provisioner,
            worker_type,
            query={"quarantined": "true"},
            paginationHandler=lambda outcome: workers.extend(outcome.get("workers", [])),
        )

        quarantined_workers = []
        for item in workers:
            hostname = item["workerId"]
            if not skip_details:
                worker = self.tc_worker_manager.getWorker(
                    provisioner,
                    worker_type,
                    item["workerGroup"],
                    hostname,
                )
                quarantine_info = worker.get("quarantineDetails", [])
            else:
                quarantine_info = []

            # print(hostname)
            # pprint.pprint(item)
            quarantined_workers.append(hostname)
            result_dict[hostname] = quarantine_info
        # return quarantined_workers
        result_dict = {
            "quarantined_workers": quarantined_workers,
            "quarantine_info": result_dict,
        }
        return result_dict

    def print_quarantined_workers(self, provisioner, worker_type):
        output = self.get_quarantined_workers(provisioner, worker_type)
        count = len(output)
        print(f"quarantined workers ({count}): {list(output.keys())}")


if __name__ == "__main__":
    # test get_quarantined_workers_with_details()
    q = Quarantine()
    prov = "proj-autophone"
    wt = "gecko-t-bitbar-gw-perf-a55"
    results = q.get_quarantined_workers_structured(provisioner=prov, worker_type=wt)
    devices = results["quarantined_workers"]
    print(f"quarantined workers ({len(devices)}): {devices}")

    print()

    # print("quarantined workers (%s): %s" % (len(devices), pprint.pformat(devices)))
    print(
        f"quarantined workers with details ({len(results)}): {pprint.pformat(results)}",
    )
