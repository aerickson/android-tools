#!/usr/bin/env bash

set -e
# set -x

PROV_AND_WORKER_TYPE="proj-autophone/gecko-t-lambda-alpha-a55"

# check that count argument is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <count> [additional arguments]"
    exit 1
fi

COUNT="$1"
shift

pipenv run ./create_tc_task.py -q "$PROV_AND_WORKER_TYPE" -c "$COUNT" "$@"
