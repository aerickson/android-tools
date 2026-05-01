#!/usr/bin/env python3
# filepath: /Users/aerickson/git/android-tools/tc_search/graphql_test2.py

# import json

import requests

# Base URL for the Firefox CI TaskCluster GraphQL API
API_URL = "https://firefox-ci-tc.services.mozilla.com/graphql"

# Common headers for all requests
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://firefox-ci-tc.services.mozilla.com",
    "Referer": "https://firefox-ci-tc.services.mozilla.com/",
}


def get_worker_types(provisioner_id="releng-hardware", limit=1000):
    """Get worker types for a specific provisioner"""
    query = """
    query ViewWorkerTypes($provisionerId: String!, $workerTypesConnection: PageConnection) {
      workerTypes(provisionerId: $provisionerId, connection: $workerTypesConnection) {
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
            provisionerId
            workerType
            stability
            description
            expires
            lastDateActive
            pendingTasks
            __typename
          }
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
    """

    variables = {
        "provisionerId": provisioner_id,
        "workerTypesConnection": {"limit": limit},
    }

    payload = {
        "operationName": "ViewWorkerTypes",
        "variables": variables,
        "query": query,
    }

    response = requests.post(API_URL, headers=headers, json=payload)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error fetching worker types: {response.status_code}")
        print(response.text)
        return None


def get_workers(provisioner_id, worker_type, limit=1000):
    """Get workers for a specific worker type"""
    worker_pool_id = f"{provisioner_id}/{worker_type}"

    query = """
    query ViewWorkers($provisionerId: String!, $workerType: String!, $workerPoolId: String!, $workersConnection: PageConnection, $quarantined: Boolean, $workerState: String) {
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
    """

    variables = {
        "provisionerId": provisioner_id,
        "workerType": worker_type,
        "workerPoolId": worker_pool_id,
        "workersConnection": {"limit": limit},
        "quarantined": None,
        "workerState": None,
    }

    payload = {"operationName": "ViewWorkers", "variables": variables, "query": query}

    response = requests.post(API_URL, headers=headers, json=payload)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error fetching workers for {worker_type}: {response.status_code}")
        print(response.text)
        return None


def main():
    # Get worker types
    worker_types_data = get_worker_types()
    if not worker_types_data:
        return

    # Extract worker types
    worker_type_edges = worker_types_data.get("data", {}).get("workerTypes", {}).get("edges", [])
    worker_types = [edge["node"]["workerType"] for edge in worker_type_edges]

    print(f"Found {len(worker_types)} worker types for provisioner 'releng-hardware'")

    # Print header
    print(
        "\nProvisioner | Worker Type | Worker Group | Worker ID | State | Last Active",
    )
    print("----------- | ----------- | ------------ | --------- | ----- | -----------")

    # For each worker type, get workers
    for worker_type in worker_types:
        workers_data = get_workers("releng-hardware", worker_type)

        if not workers_data:
            continue

        worker_edges = workers_data.get("data", {}).get("workers", {}).get("edges", [])

        if not worker_edges:
            continue

        for edge in worker_edges:
            worker = edge["node"]
            worker_id = worker.get("workerId", "Unknown")
            worker_group = worker.get("workerGroup", "Unknown")
            worker_state = worker.get("state", "Unknown")
            last_active = worker.get("lastDateActive", "Never")

            # Single line per worker with all required information
            print(
                f"releng-hardware | {worker_type} | {worker_group} | {worker_id} | {worker_state} | {last_active}",
            )


if __name__ == "__main__":
    main()
