# import json
import pprint

import requests


# query stolen from:
# https://firefox-ci-tc.services.mozilla.com/provisioners/proj-autophone/worker-types/gecko-t-bitbar-gw-perf-a55
def get_tc_workers(provisioner, workerType):
    url = "https://firefox-ci-tc.services.mozilla.com/graphql"

    workerPoolId = f"{provisioner}/{workerType}"

    # Format the query for better readability
    query = {
        "operationName": "ViewWorkers",
        "variables": {
            "workerPoolId": workerPoolId,
            "provisionerId": provisioner,
            "workerType": workerType,
            "workersConnection": {"limit": 1000},
            "quarantined": None,
            "workerState": None,
        },
        "query": """
            query ViewWorkers(
                $provisionerId: String!,
                $workerType: String!,
                $workerPoolId: String!,
                $workersConnection: PageConnection,
                $quarantined: Boolean,
                $workerState: String
            ) {
                workers(
                    provisionerId: $provisionerId
                    workerType: $workerType
                    connection: $workersConnection
                    isQuarantined: $quarantined
                    workerState: $workerState
                ) {
                    pageInfo {
                        hasNextPage
                        hasPreviousPage
                        cursor
                        previousCursor
                        nextCursor
                        __typename
                    }
                    edges {
                        node {
                            workerId
                            workerGroup
                            latestTask {
                                run {
                                    taskId
                                    runId
                                    started
                                    resolved
                                    state
                                    __typename
                                }
                                __typename
                            }
                            firstClaim
                            quarantineUntil
                            lastDateActive
                            state
                            capacity
                            providerId
                            workerPoolId
                            __typename
                        }
                        __typename
                    }
                    __typename
                }
                WorkerPool(workerPoolId: $workerPoolId) {
                    workerPoolId
                    __typename
                }
                workerType(provisionerId: $provisionerId, workerType: $workerType) {
                    actions {
                        name
                        description
                        title
                        url
                        __typename
                    }
                    __typename
                }
                provisioners {
                    edges {
                        node {
                            provisionerId
                            __typename
                        }
                        __typename
                    }
                    __typename
                }
            }
        """,
    }

    # Make the request
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=query, headers=headers)

    # Print the results
    if response.status_code == 200:
        # print(json.dumps(response.json(), indent=2))
        pass
    else:
        raise Exception(
            "Error getting workers from Taskcluster. "
            f"Status code: {response.status_code}. Status text: {response.text}",
        )

    return response.json()


if __name__ == "__main__":
    provisioner = "proj-autophone"
    workerType = "gecko-t-bitbar-gw-perf-a55"
    pprint.pprint(get_tc_workers(provisioner, workerType))
