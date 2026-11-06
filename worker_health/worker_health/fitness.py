import getpass
import json
import os
import pprint
import subprocess
import sys
from multiprocessing.pool import ThreadPool
from time import time as timer

import humanhash  # provided by humanhash3
import pendulum
import taskcluster
from natsort import natsorted

from worker_health import quarantine, utils
from worker_health.health import logger

WORKERTYPE_THREAD_COUNT = 4
TASK_THREAD_COUNT = 6
ALERT_PERCENT = 0.85
ALERT_TIME = 60
DEFAULT_PROVISIONER = "proj-autophone"


class NoDataForHostException(Exception):
    pass


def get_r8_inventory_path():
    return f"/Users/{getpass.getuser()}/git/ronin_puppet/inventory.d/macmini-r8.yaml"


class Fitness:
    def __init__(
        self,
        log_level=0,
        # TODO: don't default this here, but set if not given?
        provisioner=DEFAULT_PROVISIONER,
        alert_percent=ALERT_PERCENT,
        alert_time=ALERT_TIME,
        testing_mode=False,
    ):
        self.args = None
        self.verbosity = log_level
        self.humanize_hashes = False
        self.alert_percent = alert_percent
        self.alert_time = alert_time
        self.provisioner = provisioner
        self.queue_counts = {}
        self.worker_id_maxlen = 0
        self.quarantine = quarantine.Quarantine()
        self.quarantine_data = {}
        self.tc_url_root = "https://firefox-ci-tc.services.mozilla.com/api/queue/v1"

    def get_worker_jobs(self, queue, worker_type, worker):
        # TODO: need to get worker-group...
        url = f"{self.tc_url_root}/provisioners/{self.provisioner}/worker-types/{queue}/workers/{worker_type}/{worker}"
        return utils.get_jsonc(url, self.verbosity)

    def get_task_status(self, taskid):
        _url, output, exception = utils.get_jsonc2(f"{self.tc_url_root}/task/{taskid}/status")
        return taskid, output, exception

    def format_workertype_fitness_report_result(self, res):
        return_string = ""
        worker_id = res["worker_id"]
        del res["worker_id"]

        if self.args.humanize_hashes:
            # TODO: have to do this per worker type? ugh!!!!!
            # - currently only works for aws-metal (probably other tc worker ids also though...)
            #   - format: i-${hexhash}
            # possible solution: hash the entire name... best solution anyways.

            # some worker_ids have a dash, take the second part
            try:
                h_sanitized = worker_id.split("-")[1]
            except IndexError:
                h_sanitized = worker_id

            hh = humanhash.humanize(h_sanitized, words=3)
            return_string += ("%s (%s)" % (worker_id, hh)).ljust(self.worker_id_maxlen + 36)
        else:
            return_string += worker_id.ljust(self.worker_id_maxlen + 2)
        return_string += self.sr_dict_format(res)
        return return_string

    def fitness_report_single_host(self, worker_type, worker_id):
        self.get_pending_tasks_multi([worker_type])
        url = (
            f"{self.tc_url_root}/provisioners/{self.provisioner}/worker-types/{worker_type}/workers?limit=5"
            # "https://queue.taskcluster.net/v1/provisioners/%s/worker-types/%s/workers?limit=5"
        )
        worker_group_result = utils.get_jsonc(url, self.verbosity)
        if len(worker_group_result["workers"]) == 0:
            print("%s.%s: %s" % (worker_type, worker_id, "no data"))
            return
        worker_group = worker_group_result["workers"][0]["workerGroup"]
        self.quarantine_data[worker_type] = self.quarantine.get_quarantined_workers(self.provisioner, worker_type)
        _worker, result_object, _e = self.device_fitness_report(worker_type, worker_group, worker_id)
        result_object["worker_id"] = worker_id
        return result_object

    def main(self, provisioner, worker_type, worker_id):
        # TODO: show when worker last started a task (taskStarted in TC)
        # - aws metal nodes has quarantined nodes that have been deleted that never drop off from worker-data

        start = timer()
        worker_count = 0
        working_count = 0
        # TODO: for this calculation, should we use a count of hosts that are reporting (vs all)?
        sr_total = 0

        # host mode
        # TODO: rewrite/eliminate this block...
        #   - can't code in else below be smarter about queries (so we don't need this)?
        if worker_type and worker_id:
            worker_count = 1
            self.get_pending_tasks_multi([worker_type])
            url = (
                f"{self.tc_url_root}/provisioners/{self.provisioner}/worker-types/{worker_type}/workers?limit=5"
                # "https://queue.taskcluster.net/v1/provisioners/%s/worker-types/%s/workers?limit=5"
            )
            # print(url)
            worker_group_result = utils.get_jsonc(url, self.verbosity)
            # worker_group = worker_group_result['workerTypes'][0][]
            # import pprint
            # pprint.pprint(worker_group_result)
            # sys.exit()
            if len(worker_group_result["workers"]) == 0:
                print("%s.%s: %s" % (worker_type, worker_id, "no data"))
                return
            worker_group = worker_group_result["workers"][0]["workerGroup"]
            self.quarantine_data[worker_type] = self.quarantine.get_quarantined_workers(self.provisioner, worker_type)
            _worker, res_obj, _e = self.device_fitness_report(worker_type, worker_group, worker_id)
            res_obj["worker_id"] = worker_id
            try:
                res_obj = self.fitness_report_single_host(worker_type, worker_id)
            except NoDataForHostException as e:
                print(f"{worker_type}.{worker_id}: {e}")
                sys.exit(1)
            sr_total += res_obj["sr"]
            print("%s.%s" % (worker_type, self.format_workertype_fitness_report_result(res_obj)))
        else:
            # queue mode
            if worker_type:
                worker_types = [worker_type]
            # provisioner mode
            else:
                worker_types_result = self.get_worker_types(provisioner)
                worker_types = []
                if "workerTypes" in worker_types_result:
                    for provisioner in worker_types_result["workerTypes"]:
                        worker_type = provisioner["workerType"]
                        worker_types.append(worker_type)
                    # print(worker_types)
                else:
                    logger.warning("error fetching workerTypes, results are incomplete!")
            self.get_pending_tasks_multi(worker_types)

            # TODO: process and then display? padding of worker_id is not consistent for whole provisioner report
            # - because we haven't scanned the potentially longest worker_ids when we display
            #   the first worker_group's data
            for a_worker_type in worker_types:
                wt, res_obj, _e = self.workertype_fitness_report(a_worker_type)
                for item in res_obj:
                    worker_count += 1
                    sr_total += item["sr"]
                    if item.get("state") and "working" in item.get("state"):
                        working_count += 1
                    if self.args.only_show_alerting:
                        if "alerts" in item:
                            print(
                                "%s.%s"
                                % (
                                    wt,
                                    self.format_workertype_fitness_report_result(item),
                                ),
                            )
                    else:
                        print("%s.%s" % (wt, self.format_workertype_fitness_report_result(item)))
        # if to protect from divide by 0 (happens on request failures)
        if worker_count:
            # TODO: show alerting count
            print(
                "%s workers queried in %s seconds (%s working), average SR %s%%"
                % (
                    worker_count,
                    round((timer() - start), 2),
                    working_count,
                    round((sr_total / worker_count * 100), 2),
                ),
            )

    def get_pending_tasks(self, queue):
        _url, output, exception = utils.get_jsonc2(
            "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/pending/%s/%s"
            # "https://queue.taskcluster.net/v1/pending/%s/%s"
            % (self.provisioner, queue),
        )
        return queue, output, exception

    def get_pending_tasks_multi(self, queues):
        try:
            results = ThreadPool(TASK_THREAD_COUNT).imap_unordered(self.get_pending_tasks, queues)
        except Exception as e:
            print(e)
        for queue, result, _error in results:
            self.queue_counts[queue] = result["pendingTasks"]

    # for provisioner report...
    def get_worker_types(self, provisioner):
        # https://queue.taskcluster.net/v1/provisioners/proj-autophone/worker-types?limit=100
        return utils.get_jsonc(
            "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/provisioners/%s/worker-types?limit=100"
            # "https://queue.taskcluster.net/v1/provisioners/%s/worker-types?limit=100"
            % provisioner,
            self.verbosity,
        )

    def get_worker_report(self, worker_type, suffix="test.releng.mdc1.mozilla.com"):
        url = (
            "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/provisioners/%s/worker-types/%s/workers?limit=200"
            % (self.provisioner, worker_type)
        )
        try:
            workers_result = utils.get_jsonc(url, self.verbosity)
        except Exception as e:
            workers_result = []
            print(e)

        seen_workers = []
        for worker in workers_result["workers"]:
            worker_id = worker["workerId"]
            seen_workers.append(f"{worker_id}.{suffix}")

        return seen_workers

    def r8_worker_report(
        self,
        path_to_r8_inventory_file=get_r8_inventory_path(),
        # exclude_dict={},
    ):
        import yaml

        with open(path_to_r8_inventory_file, "r") as file:
            data = yaml.safe_load(file)

        result = {}
        for group in data.get("groups", []):
            name = group.get("name")
            targets = group.get("targets", [])
            result[name] = targets

        # for each name, do a worker lookup and then check against the list
        missing_dict = {}
        for pool_name, pool_host_list in result.items():
            seen_hosts = self.get_worker_report(pool_name)
            for host in pool_host_list:
                if host not in seen_hosts:
                    if pool_name not in missing_dict:
                        missing_dict[pool_name] = []
                    missing_dict[pool_name].append(host)

        print("missing workers:")
        pprint.pprint(missing_dict)

    # TODO: rename linux_moonshot_worker_report?
    def moonshot_worker_report(self, worker_type, args=None, exclude_dict={}):
        url = (
            "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/provisioners/%s/worker-types/%s/workers?limit=200"
            % (self.provisioner, worker_type)
        )
        try:
            workers_result = utils.get_jsonc(url, self.verbosity)
        except Exception as e:
            workers_result = []
            print(e)

        # TODO: figure out how to make these args and not mess with generation
        start = 1
        end = 280
        #
        worker_prefix = "t-linux64-ms-"

        generated_workers = []
        linux_win_counter = start
        for i in range(start, end + 1):
            if linux_win_counter == 46:
                linux_win_counter = 1
            if linux_win_counter <= 15:
                worker_name = "%s%03d" % (worker_prefix, i)
                generated_workers.append(worker_name)

            linux_win_counter += 1

        seen_workers = []
        if "workers" in workers_result:
            for item in workers_result["workers"]:
                seen_workers.append(item["workerId"])

        quarantined_workers = self.quarantine.get_quarantined_workers(self.provisioner, worker_type)

        s_w = set(seen_workers)
        e_w = set(generated_workers) - set(exclude_dict)
        missing = e_w - s_w
        e_count = len(e_w)
        m_count = len(missing)
        s_count = len(s_w)

        print("pool size: %s" % (len(generated_workers)))
        print("- excluded workers (%s): %s" % (len(exclude_dict), pprint.pformat(exclude_dict)))

        print("actual pool size: %s" % e_count)

        print("quarantined workers (%s): %s" % (len(quarantined_workers), list(quarantined_workers.keys())))
        print(
            "missing workers (%s/%s): %s"
            % (
                m_count,
                e_count,
                utils.pformat_term(sorted(missing)),
            ),
        )

        if args and args.log_level:
            print()
            print("expected workers (%s): %s" % (e_count, sorted(generated_workers)))
            print()
            print("seen workers: (%s): %s" % (s_count, sorted(seen_workers)))
            print()

    # used for packet.net
    def simple_worker_report(self, worker_type, worker_prefix="packet-", worker_count=60):
        url = (
            "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/provisioners/%s/worker-types/%s/workers?limit=100"
            % (self.provisioner, worker_type)
        )
        # print(url)
        try:
            workers_result = utils.get_jsonc(url, self.verbosity)
        except Exception as e:
            workers_result = []
            print(e)
        # print(workers_result)

        expected_workers = []
        for i in range(0, worker_count):
            expected_workers.append("%s%s" % (worker_prefix, i))

        seen_workers = []
        if "workers" in workers_result:
            for item in workers_result["workers"]:
                seen_workers.append(item["workerId"])
        # pprint.pprint(workers_result)

        # for item in natsorted(seen_workers):
        #     print(item)

        # should show 46
        e_w = set(expected_workers)
        s_w = set(seen_workers)
        # missing = natsorted(s_w.symmetric_difference(e_w))
        missing = e_w - s_w
        m_count = len(missing)
        print("missing workers (%s): %s" % (m_count, sorted(missing)))
        print("%s workers total" % worker_count)

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

    # returns str:worker_type, dict/list?:worker_results, error
    def workertype_fitness_report(self, worker_type):
        # load quarantine data
        self.quarantine_data[worker_type] = self.quarantine.get_quarantined_workers(self.provisioner, worker_type)

        outcome = self.get_workers(worker_type)

        worker_ids = []
        for worker in outcome["workers"]:
            worker_id = worker["workerId"]
            worker_group = worker["workerGroup"]
            self.worker_id_maxlen = max(len(worker_id), self.worker_id_maxlen)
            worker_ids.append((worker_type, worker_group, worker_id))

        if len(worker_ids) == 0:
            print("%s: no workers reporting (could be due to no jobs)" % worker_type)
            worker_type, None, None

        results = []
        try:
            results = ThreadPool(WORKERTYPE_THREAD_COUNT).starmap(self.device_fitness_report, worker_ids)
        except Exception as e:
            raise (e)
        worker_results = []
        for a_tuple in results:
            worker_id = a_tuple[0]
            result = a_tuple[1]
            # error = a_tuple[2]
            if result:
                result["worker_id"] = worker_id
                worker_results.append(result)
        # sort naturally/numerically
        if self.args.sort_order == "sr":
            worker_results = natsorted(worker_results, key=lambda i: i["sr"])
        elif self.args.sort_order == "worker_id":
            worker_results = natsorted(worker_results, key=lambda i: i["worker_id"])
        else:
            raise Exception("unknown sort_order (%s)" % self.args.sort_order)
        return worker_type, worker_results, None

    # basically how print does it but with float padding
    def sr_dict_format(self, sr_dict):
        if not isinstance(sr_dict, dict):
            raise Exception("input should be a dict")
        result_string = "{"
        for key, value in sr_dict.items():
            if key == "state":
                continue

            result_string += "%s: " % key
            # debugging
            # print()

            if isinstance(value, str):
                result_string += "'%s'" % value
            elif isinstance(value, int):
                result_string += "{:2d}".format(value)
            elif isinstance(value, list):
                result_string += pprint.pformat(value, width=sys.maxsize)
            elif isinstance(value, float):
                # the only float is success rate
                result_string += utils.graph_percentage(value)
                # can be up to 13... "a few seconds"
                result_string += " {:.1%}".format(value).rjust(7)
            elif isinstance(value, type(None)):
                result_string += "never".rjust(10)
            elif isinstance(value, pendulum.DateTime):
                # result_string += str(value) # .diff_for_humans(pendulum.now())
                # result_string += value.format('YYYY-MM-DD HH:mm:ss zz')
                result_string += (
                    # pass True as a 2nd parameter to remove the modifiers ago, from now, etc.
                    pendulum.now(tz="UTC").diff_for_humans(value, True).rjust(10)
                )
            else:
                raise Exception("unknown type (%s)" % type(value))
            result_string += ", "
        # on last, trim the space
        result_string = result_string[0:-2]
        result_string += "}"
        return result_string

    def device_fitness_report(self, queue, worker_group, device):
        results = self.get_worker_jobs(queue, worker_group, device)
        task_successes = 0
        task_failures = 0
        task_runnings = 0
        task_exceptions = 0
        task_last_started_timestamp = None
        task_history_success_array = []  # 1 for success, 0 for failure or exception

        task_ids = []
        for task in results["recentTasks"]:
            task_id = task["taskId"]
            task_ids.append(task_id)

        try:
            # we want this ordered
            results = ThreadPool(TASK_THREAD_COUNT).imap(self.get_task_status, task_ids)
        except Exception as e:
            print(e)
        for task_id, result, error in results:
            if error is None:
                task_state = None
                # filter out jobs that are gone
                if "code" in result:
                    if result["code"] == "ResourceNotFound":
                        continue
                try:
                    task_state = result["status"]["state"]
                    # pprint.pprint(result)
                except KeyError:
                    print("strange result: ")
                    pprint.pprint(result)
                    print(result["code"] == "ResourceNotFound")
                    continue

                # record the last started time
                if "runs" in result["status"]:
                    for run in result["status"]["runs"]:
                        if "started" in run:
                            temp_date = pendulum.parse(run["started"])
                            if task_last_started_timestamp is None:
                                task_last_started_timestamp = temp_date
                            # if temp_date more recent (larger), use it as the oldest date
                            if temp_date > task_last_started_timestamp:
                                task_last_started_timestamp = temp_date

                # TODO: gather exception stats
                if task_state == "running":
                    task_runnings += 1
                elif task_state == "exception":
                    task_exceptions += 1
                    task_history_success_array.append(0)
                elif task_state == "failed":
                    task_failures += 1
                    task_history_success_array.append(0)
                elif task_state == "completed":
                    retries_left = result["status"]["retriesLeft"]
                    if retries_left != 5:
                        runs = result["status"]["runs"]
                        for run in runs:
                            if run["workerId"] == device:
                                run_state = run["state"]
                                if run_state == "exception":
                                    task_exceptions += 1
                                    task_history_success_array.append(0)
                                elif run_state == "running":
                                    task_runnings += 1
                                elif run_state == "completed":
                                    task_successes += 1
                                    task_history_success_array.append(1)
                                elif run_state == "failed":
                                    task_failures += 1
                                    task_history_success_array.append(0)
                                else:
                                    raise Exception("Shouldn't be here!")
                    else:
                        task_successes += 1
                        task_history_success_array.append(1)
                if self.verbosity:
                    print("%s.%s: %s: %s" % (queue, device, task_id, task_state))
            else:
                # TODO: should return exception? only getting partial truth...
                pass
                # print("error fetching %r: %s" % (task_id, error))

        total = task_failures + task_successes
        results_obj = {}
        success_ratio_calculated = False
        if total > 0:
            success_ratio = task_successes / total
            # print("sr: %s/%s=%s" % (task_successes, total, success_ratio))
            results_obj["sr"] = success_ratio
            success_ratio_calculated = True
        else:
            results_obj["sr"] = float(0)
        results_obj["suc"] = task_successes
        results_obj["cmp"] = total
        results_obj["exc"] = task_exceptions
        results_obj["rng"] = task_runnings
        results_obj["ls"] = task_last_started_timestamp

        # note if no jobs in queue
        if queue in self.queue_counts:
            if self.queue_counts[queue] == 0:
                # if "notes" not in results_obj:
                #     results_obj["notes"] = []
                results_obj.setdefault("notes", []).append("No jobs in queue.")
                jobs_present = False
            else:
                jobs_present = True
        else:
            logger.warn("Strange, no queue count data for %s" % queue)

        # ping alerts
        if self.args.ping:
            if self.args.ping_host:
                cmd = [
                    "ssh",
                    self.args.ping_host,
                    "ping -c 1 -i 0.3 -w 1 %s.%s" % (device, self.args.ping_domain),
                ]
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if res.returncode != 0:
                    if "alerts" not in results_obj:
                        results_obj["alerts"] = []
                    results_obj["alerts"].append("Not pingable!")
                # TODO: write to notes that it is pingable?
            else:
                # -W (vs -w) is BSD/OS X specific
                # TODO: make OS independent
                cmd_str = "/sbin/ping -c 1 -i 0.3 -W 1 %s.%s" % (
                    device,
                    self.args.ping_domain,
                )
                cmd = cmd_str.split(" ")
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if res.returncode != 0:
                    if "alerts" not in results_obj:
                        results_obj["alerts"] = []
                    results_obj["alerts"].append("Not pingable!")

        # alert if success ratio is low
        if success_ratio_calculated:
            if success_ratio < self.alert_percent:
                # if "alerts" not in results_obj:
                #     results_obj["alerts"] = []
                results_obj.setdefault("alerts", []).append(
                    "Low health!",
                    # "Low health (less than %s)!" % self.alert_percent
                )

        # alert if most recent tests have consecutively failed
        total_consecutive_failures_from_end = utils.consecutive_non_ones_from_end(task_history_success_array)
        if total_consecutive_failures_from_end >= 2:
            results_obj.setdefault("alerts", []).append(
                "Consecutive failures (%s)!" % total_consecutive_failures_from_end,
            )

        # alert if worker hasn't worked in self.alert_time minutes
        dt = pendulum.now(tz="UTC")
        comparison_dt = dt.subtract(minutes=self.alert_time)
        if jobs_present and not task_last_started_timestamp:
            # seems to always to occur also when below condition is also met?!?
            results_obj.setdefault("alerts", []).append("No work!")
        elif jobs_present and task_last_started_timestamp < comparison_dt:
            results_obj.setdefault("alerts", []).append("No work in %sm!" % self.alert_time)
        else:
            results_obj.setdefault("state", []).append("working")

        # alert if lots of exceptions
        if task_exceptions >= 3:
            results_obj.setdefault("alerts", []).append("High exceptions!")

        # alert if no work done
        if total == 0 and task_exceptions == 0 and task_runnings == 0:
            # if "alerts" not in results_obj:
            #     results_obj["alerts"] = []
            results_obj.setdefault("alerts", []).append("No work done!")

        # quarantine
        if device in self.quarantine_data[queue]:
            quarantine_until = self.quarantine_data[queue][device]
            if quarantine_until:
                until_dt = pendulum.parse(quarantine_until)
                diff_seconds = abs((pendulum.now(tz="UTC") - until_dt).total_seconds())
                duration_str = utils.human_delta(diff_seconds)
                if until_dt > pendulum.now(tz="UTC"):
                    alert_msg = f"Quarantined (expires in {duration_str})."
                else:
                    alert_msg = f"Quarantined (expired {duration_str} ago)."
            else:
                alert_msg = "Quarantined."
            results_obj.setdefault("alerts", []).append(alert_msg)

        return device, results_obj, None
