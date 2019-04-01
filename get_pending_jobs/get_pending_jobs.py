#!/usr/bin/env python3

import sys
import logging
import json
import datetime
import math
import time
import pprint


try:
    import requests
    from tqdm import tqdm, trange
except:
    print("Please `pip3 install tqdm requests` or use the Pipfile.")
    sys.exit(1)

LOG_LEVELS = ["BONKERS", "INTENSE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DEFAULT_LOG_LEVEL = "WARNING"

REQUEST_DEBUGGING = False
REQUEST_CACHING = False

if REQUEST_CACHING:
    import requests_cache

    requests_cache.install_cache()

if REQUEST_DEBUGGING:
    # These two lines enable debugging at httplib level (requests->urllib3->http.client)
    # You will see the REQUEST, including HEADERS and DATA, and RESPONSE with HEADERS but without DATA.
    # The only thing missing will be the response.body which is not logged.
    try:
        import http.client as http_client
    except ImportError:
        # Python 2
        import httplib as http_client
    http_client.HTTPConnection.debuglevel = 1

    # You must initialize logging, otherwise you'll not see debug output.
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

#
# TC schema
#
#  Job 1
#  - Task 1
#  - Task 2
#


class PendingJobs:
    def __init__(self, log_level=3):
        # key: project, value: epoch timestamp of oldest pending job or task
        self.oldest_job_dict = {}
        self.oldest_task_dict = {}
        #
        self.log_level = log_level
        self.pp = pprint.PrettyPrinter(indent=4)

    def get_json(self, an_url):
        headers = {
            "accept-encoding": "gzip,deflate",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.14; rv:65.0) Gecko/20100101 Firefox/65.0",
        }
        if self.log_level <= 0:
            tqdm.write("Fetching %s... " % an_url)
        r = requests.get(an_url, headers=headers)
        return r.json()

    def get_push_pending_jobs(self, project, push_id, platform_filter=None):
        # phase 2: get jobs for each push

        # TODO: check push health, if complete, we can exit here.

        # TODO: don't hardcode these (the field legend is provided at the end of all requests) in 'job_property_names'
        # filtering for android-hw: fields 2, 15, 22
        #    2 is build platform
        #    15 is job_type_name (worker/test)
        #    22 is platform
        #   - seems like they all work
        #
        #       23 is pushid
        # field 26 is result (success, ???)
        # field 30 is state (completed, ???)
        key_pushid = 23
        key_platform = 22
        key_start_timestamp = 29
        key_state = 30
        key_submit_timestamp = 31
        key_job_type_name = 15

        # https://treeherder.mozilla.org/api/project/mozilla-central/jobs/?return_type=list&count=2000&push_id=443884

        # https://treeherder.mozilla.org/api/project/mozilla-central/jobs/?return_type=list&count=2000&push_id=443884&offset=2000

        oldest_task_timestamp = None
        pending_jobs = 0
        iteration = 0
        while True:
            if iteration == 0:
                res = self.get_json(
                    "https://treeherder.mozilla.org/api/project/%s/jobs/?return_type=list&count=2000&push_id=%s"
                    % (project, push_id)
                )
            else:
                offset = iteration * 2000
                res = self.get_json(
                    "https://treeherder.mozilla.org/api/project/%s/jobs/?return_type=list&count=2000&offset=%s&push_id=%s"
                    % (project, offset, push_id)
                )
            result_count = len(res["results"])
            for item in res["results"]:
                if item[key_state] == "pending":
                    if platform_filter:
                        if platform_filter in item[key_platform]:
                            pending_jobs += 1
                            # update oldest_task_timestamp
                            if (
                                not oldest_task_timestamp
                                or item[key_submit_timestamp] < oldest_task_timestamp
                            ):
                                oldest_task_timestamp = item[key_submit_timestamp]
                            # update self.oldest_task_dict
                            if project in self.oldest_task_dict:
                                if (
                                    item[key_submit_timestamp]
                                    < self.oldest_task_dict[project]
                                ):
                                    self.oldest_task_dict[project] = item[
                                        key_submit_timestamp
                                    ]
                            else:
                                self.oldest_task_dict[project] = item[
                                    key_submit_timestamp
                                ]
                            # print the job platform and job type name
                            # TODO: print this at the end, not for each task. also provide sorted count
                            if self.log_level <= 2:
                                tqdm.write(
                                    "  %s: %s |"
                                    % (item[key_platform], item[key_job_type_name])
                                )
                    else:
                        pending_jobs += 1
                        # update oldest_task_timestamp
                        if (
                            not oldest_task_timestamp
                            or item[key_submit_timestamp] < oldest_task_timestamp
                        ):
                            oldest_task_timestamp = item[key_submit_timestamp]
                        # update self.oldest_task_dict
                        if project in self.oldest_task_dict:
                            if (
                                item[key_submit_timestamp]
                                < self.oldest_task_dict[project]
                            ):
                                self.oldest_task_dict[project] = item[
                                    key_submit_timestamp
                                ]
                        else:
                            self.oldest_task_dict[project] = item[key_submit_timestamp]
                        if self.log_level <= 2:
                            tqdm.write(
                                "  %s: %s |"
                                % (item[key_platform], item[key_job_type_name])
                            )
            iteration += 1
            if result_count != 2000:
                return pending_jobs, oldest_task_timestamp
        # TODO: return array with task names (fix current inversion in ordering)
        return pending_jobs, oldest_task_timestamp

    def get_pending_jobs(
        self,
        projects,
        filter=None,
        pages=4,
        page_size=50,
        early_exit=True,
        progress_disabled=False,
    ):
        # phase 1: get try pushes

        last_seen_commit = ""
        results_dict = {}

        # determine if progress bars should stick around based on log level
        leave_progressbars = False
        if self.log_level <= 3:
            leave_progressbars = True

        # TODO: multithread?
        proj_iterator = tqdm(
            projects,
            desc="projects",
            leave=leave_progressbars,
            disable=progress_disabled,
        )
        for project in proj_iterator:
            proj_iterator.set_postfix(project=project)
            pending_job_total = 0
            results_dict[project] = 0
            jobs_inspected_per_project = 0
            early_exit_string = ""

            # TODO: integrate this into each line vs printing it here
            if self.log_level <= 3:
                tqdm.write("-- %s project" % project)

            push_pbar = tqdm(
                total=page_size * pages,
                desc="jobs",
                leave=leave_progressbars,
                disable=progress_disabled,
            )
            for i in range(0, pages):
                pending_jobs_this_page = 0
                # TODO: figure out how to avoid overlap (use a seen array for now?)
                # url = "https://treeherder.mozilla.org/api/project/mozilla-central/push/"
                # https://treeherder.mozilla.org/api/project/mozilla-central/push/?full=true&count=10&fromchange=63bd1994e17c43e699c23f11ca01266d48e61d1e
                # https://treeherder.mozilla.org/api/project/mozilla-central/push/?full=true&count=11&push_timestamp__lte=1552211644
                if i != 0:
                    url = (
                        "https://treeherder.mozilla.org/api/project/%s/push/?full=true&count=%s&tochange=%s"
                        % (project, page_size + 1, last_seen_commit)
                    )
                else:
                    url = (
                        "https://treeherder.mozilla.org/api/project/%s/push/?full=true&count=%s"
                        % (project, page_size)
                    )

                output = self.get_json(url)
                results = output["results"]

                for result in results:
                    jobs_inspected_per_project += 1
                    last_seen_commit = result["revision"]
                    count, oldest_task_timestamp = self.get_push_pending_jobs(
                        project, result["id"], filter
                    )
                    pending_jobs_this_page += count
                    pending_job_total += count
                    push_pbar.update(1)

                    # set oldest_record
                    since_string = ""
                    if count >= 1:
                        if self.log_level <= 1:
                            tqdm.write(self.pp.pformat(result))
                        # diff = diff_epoch_to_now(result["push_timestamp"])
                        diff_task = diff_epoch_to_now(oldest_task_timestamp)
                        since_string = ", oldest submitted %s ago" % human_time(
                            seconds=diff_task
                        )
                        self.oldest_job_dict[project] = result["push_timestamp"]

                    push_time = datetime.datetime.fromtimestamp(
                        result["push_timestamp"]
                    )
                    push_time_str = push_time.strftime("%Y/%m/%d %H:%M")

                    output_string = "%s:%s:%s: %s pending tasks%s" % (
                        push_time_str,
                        result["revision"][0:6],
                        result["author"],
                        count,
                        since_string,
                    )
                    if self.log_level <= 3 and count >= 1:
                        tqdm.write(output_string)
                    elif self.log_level <= 1:
                        tqdm.write(output_string)

                results_dict[project] += pending_jobs_this_page

                if self.log_level <= 1:
                    tqdm.write(
                        "pending jobs on page %s: %s" % (i + 1, pending_jobs_this_page)
                    )
                # don't print this message if we're on the last page already
                if early_exit and i + 1 != pages and pending_jobs_this_page == 0:
                    early_exit_string = ", exited early"
                    break
                pending_jobs_this_page = 0
            # TODO: display the date of the oldest scanned job
            # - help reveal how far back we scanned (currently not clear/visible)
            if self.log_level <= 3:
                # display pending job total
                filter_string = ""
                if args.filter:
                    filter_string = "'%s' " % filter
                tqdm.write(
                    "%s %spending tasks, inspected %s jobs%s"
                    % (
                        results_dict[project],
                        filter_string,
                        jobs_inspected_per_project,
                        early_exit_string,
                    )
                )
                # display oldest task
                if project in self.oldest_task_dict:
                    tqdm.write(
                        "oldest pending task submitted %s ago"
                        % (
                            human_time(
                                seconds=diff_epoch_to_now(
                                    self.oldest_task_dict[project]
                                )
                            )
                        )
                    )
            push_pbar.close()
        proj_iterator.close()
        return results_dict


def diff_epoch_to_now(an_epoch_time):
    now = time.time()
    return now - an_epoch_time


def handler(sig, frame):
    tqdm.write("Received Ctrl-C. Exiting...")
    sys.exit(0)


# from https://stackoverflow.com/questions/6574329/how-can-i-produce-a-human-readable-difference-when-subtracting-two-unix-timestam
def human_time(*args, **kwargs):
    secs = float(datetime.timedelta(*args, **kwargs).total_seconds())
    # units = [("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)]
    units = [("day", 86400), ("hour", 3600), ("minute", 60)]
    parts = []
    for unit, mul in units:
        if secs / mul >= 1 or mul == 1:
            if mul > 1:
                n = int(math.floor(secs / mul))
                secs -= n * mul
            else:
                n = secs if secs != int(secs) else int(secs)
            parts.append("%s %s%s" % (n, unit, "" if n == 1 else "s"))
    return ", ".join(parts)


if __name__ == "__main__":
    import argparse
    import signal

    signal.signal(signal.SIGINT, handler)

    PAGE_SIZE = 20
    PAGES = 3

    parser = argparse.ArgumentParser(
        usage="%(prog)s [options]",
        description="Scan treeherder to get a count of pending jobs.",
    )
    # TODO: make this take a csv vs a single
    parser.add_argument(
        "--project",
        "-p",
        help="a single project to inspect for pending jobs (defaults to use mozilla-inbound, autoland, try, and mozilla-central)",
    )
    parser.add_argument(
        "--filter", "-f", help="require pending jobs to match this string"
    )
    parser.add_argument(
        "-d",
        "-disable-progress-bars",
        dest="no_progress",
        action="store_true",
        help="don't display progress bars",
    )
    parser.add_argument(
        "--page-size",
        default=PAGE_SIZE,
        dest="page_size",
        type=int,
        help="how many results per page to fetch (default is %s)" % PAGE_SIZE,
    )
    parser.add_argument(
        "--pages",
        default=PAGES,
        type=int,
        help="how many pages of results should we inspect (default is %s)" % PAGES,
    )
    parser.add_argument(
        "-n",
        "--no-early-exit",
        dest="no_early_exit",
        action="store_true",
        help="don't exit early if no pending jobs found on a page",
    )
    # handle multiple -v args (like -vvv)
    parser.add_argument(
        "--verbose", "-v", action="append_const", dest="log_level", const=-1
    )
    # TODO: reqeusts-cache behind an arg
    args = parser.parse_args()

    early_exit = True
    if args.no_early_exit:
        early_exit = False

    # For each "-v" flag, adjust the logging verbosity accordingly
    # making sure to clamp off the value from 0 to 4, inclusive of both
    log_level = LOG_LEVELS.index(DEFAULT_LOG_LEVEL)
    for adjustment in args.log_level or ():
        log_level = min(len(LOG_LEVELS) - 1, max(log_level + adjustment, 0))
    log_level_name = LOG_LEVELS[log_level]
    if log_level <= 1:
        tqdm.write("log_level is: %s (%s)" % (log_level, log_level_name))

    pj = PendingJobs(log_level=log_level)

    # sanity check args.project
    projects = ["try", "mozilla-inbound", "autoland", "mozilla-central"]
    if args.project:
        if args.project in projects:
            projects = [args.project]
        else:
            print(
                "invalid project specified. valid projects are: %s"
                % (", ".join(projects))
            )
            sys.exit(1)

    if args.filter:
        results_dict = pj.get_pending_jobs(
            projects,
            args.filter,
            args.pages,
            args.page_size,
            early_exit,
            args.no_progress,
        )
    else:
        results_dict = pj.get_pending_jobs(
            projects,
            args.filter,
            args.pages,
            args.page_size,
            early_exit,
            args.no_progress,
        )

    # display a final summary of results
    grand_total = 0
    filter_string = ""
    if args.filter:
        filter_string = "'%s' " % args.filter

    if log_level <= 3:
        print("-- summary")

    for key in results_dict:
        grand_total += results_dict[key]
        if key in pj.oldest_task_dict:
            diff = diff_epoch_to_now(pj.oldest_task_dict[key])
            print(
                "%s project: pending %stasks: %s, oldest pending submitted %s ago"
                % (key, filter_string, results_dict[key], human_time(seconds=diff))
            )
        else:
            print(
                "%s project: pending %stasks: %s"
                % (key, filter_string, results_dict[key])
            )
    if len(projects) > 1:
        print("total pending %stasks: %s" % (filter_string, grand_total))
