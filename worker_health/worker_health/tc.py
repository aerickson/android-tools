import os
import json
import taskcluster

from worker_health import utils


def get_worker_types(provisioner, verbosity):
    # https://queue.taskcluster.net/v1/provisioners/proj-autophone/worker-types?limit=100
    return utils.get_jsonc(
        "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/provisioners/%s/worker-types?limit=100"
        # "https://queue.taskcluster.net/v1/provisioners/%s/worker-types?limit=100"
        % provisioner,
        verbosity,
    )


# def get_workers(provisioner, worker_type, verbosity):
#     # https://queue.taskcluster.net/v1/provisioners/proj-autophone/worker-types/mac-mini-r8/workers?limit=100
#     return utils.get_jsonc(
#         "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/provisioners/%s/worker-types/%s/workers?limit=100"
#         % (provisioner, worker_type),
#         verbosity,
#     )


def get_workers(provisioner, worker_type):
    # TODO: improve this (don't explode if missing)
    if "TC_TOKEN_FILE" in os.environ:
        token_file = os.path.expanduser(os.environ["TC_TOKEN_FILE"])
    else:
        token_file = os.path.expanduser("~/.tc_token")
    try:
        with open(token_file) as json_file:
            data = json.load(json_file)
    except FileNotFoundError:
        raise RuntimeError(f"Token file not found: {token_file}")
    creds = {"clientId": data["clientId"], "accessToken": data["accessToken"]}
    queue = taskcluster.Queue(
        {
            "rootUrl": "https://firefox-ci-tc.services.mozilla.com",
            "credentials": creds,
        },
    )

    outcome = queue.listWorkers(provisioner, worker_type)
    return outcome
