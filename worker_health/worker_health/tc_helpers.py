import json
import os

import taskcluster

from worker_health import utils


# extracted functions from Fitness
class TCHelper:
    def __init__(
        self,
        provisioner,
        log_level=0,
    ):
        self.args = None
        self.provisioner = provisioner
        self.log_level = log_level

    def get_worker_jobs(self, queue, worker_type, worker):
        # TODO: need to get worker-group...
        url = (
            "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/provisioners/%s/worker-types/%s/workers/%s/%s"
            % (self.provisioner, queue, worker_type, worker)
        )
        # print(url)
        return utils.get_jsonc(url, self.log_level)

    def get_task_status(self, taskid):
        _url, output, exception = utils.get_jsonc2(
            "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/%s/status" % taskid,
            # "https://queue.taskcluster.net/v1/task/%s/status" % taskid
        )
        return taskid, output, exception

    def get_workers(self, worker_type):
        # TODO: improve this (don't explode if missing)
        with open(os.path.expanduser("~/.tc_token")) as json_file:
            data = json.load(json_file)
        creds = {"clientId": data["clientId"], "accessToken": data["accessToken"]}
        queue = taskcluster.Queue(
            {
                "rootUrl": "https://firefox-ci-tc.services.mozilla.com",
                "credentials": creds,
            },
        )

        outcome = queue.listWorkers(self.provisioner, worker_type)
        return outcome

    def get_worker_groups(self, worker_type):
        output = self.get_workers(worker_type)["workers"]
        worker_groups = {}
        for element in output:
            a_worker_group = element["workerGroup"]
            worker_groups[a_worker_group] = True
        return list(worker_groups.keys())
