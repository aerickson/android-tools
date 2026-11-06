#!/usr/bin/env bash

set -euo pipefail

PROV_AND_WORKER_TYPE="releng-hardware/gecko-t-linux-talos-2404-wayland"

if [[ -z "${1:-}" ]]; then
    echo "Usage: $0 <count> [additional create_tc_task.py arguments]" >&2
    exit 1
fi

COUNT="$1"
shift

pipenv run -- python ./create_tc_task.py -q "$PROV_AND_WORKER_TYPE" -c "$COUNT" "$@"
