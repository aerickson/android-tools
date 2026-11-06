#!/usr/bin/env python3

import argparse
import json
import os
import textwrap

import taskcluster


def fg(text, color):
    return "\33[38;5;" + str(color) + "m" + text + "\33[0m"


def bg(text, color):
    return "\33[48;5;" + str(color) + "m" + text + "\33[0m"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        epilog=textwrap.dedent(
            """\
        If the search term is omitted, all provisioners and
        worker types will be listed.
        """,
        ),
    )
    parser.add_argument(
        "search_term",
        type=str,
        nargs="?",
        help="string to search for",
        default=None,
    )
    # add verbose option
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print more information about the search",
    )
    args = parser.parse_args()

    search_term = args.search_term

    with open(os.path.expanduser("~/.tc_quarantine_token")) as json_file:
        data = json.load(json_file)
    root_url = "https://firefox-ci-tc.services.mozilla.com"
    creds = {"clientId": data["clientId"], "accessToken": data["accessToken"]}

    queue = taskcluster.Queue({"rootUrl": root_url, "credentials": creds})

    # if verbose, show queue with pprint
    # if args.verbose:
    #     import pprint

    #     print("Queue object details:")
    #     pprint.pprint(queue.__dict__)
    #     print("\n")

    p_count = 0
    wt_count = 0
    m_count = 0

    if not search_term:
        for item in queue.listProvisioners()["provisioners"]:
            p_count += 1
            provisioner_id = item["provisionerId"]
            print(
                "https://firefox-ci-tc.services.mozilla.com/provisioners/%s" % fg(provisioner_id, 13),
            )
            for wt in queue.listWorkerTypes(provisioner_id)["workerTypes"]:
                # if verbose show wt with pprint
                if args.verbose:
                    import pprint

                    print("WorkerType object details:")
                    pprint.pprint(wt)
                    print("\n")
                wt_count += 1
                worker_type = wt["workerType"]
                # import pdb; pdb.set_trace()
                print(
                    "  https://firefox-ci-tc.services.mozilla.com/provisioners/%s/worker-types/%s"
                    % (provisioner_id, fg(worker_type, 14)),
                )
        print("found %s provisionerIds and %s workerTypes" % (p_count, wt_count))

    else:
        for item in queue.listProvisioners()["provisioners"]:
            p_count += 1
            provisioner_id = item["provisionerId"]
            if search_term in provisioner_id:
                m_count += 1
                print(
                    "https://firefox-ci-tc.services.mozilla.com/provisioners/%s" % fg(provisioner_id, 13),
                )
            for wt in queue.listWorkerTypes(provisioner_id)["workerTypes"]:
                wt_count += 1
                worker_type = wt["workerType"]
                # import pdb; pdb.set_trace()
                if search_term in worker_type:
                    m_count += 1
                    print(
                        "https://firefox-ci-tc.services.mozilla.com/provisioners/%s/worker-types/%s"
                        % (provisioner_id, fg(worker_type, 14)),
                    )
        print(
            "%s matches, scanned %s provisionerIds and %s workerTypes" % (m_count, p_count, wt_count),
        )
