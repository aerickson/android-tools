#!/usr/bin/env bash

set -e
# set -x

# check that count argument is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <count> [additional arguments]"
    exit 1
fi

COUNT="$1"
shift

./create_tc_task.py -q releng-hardware/gecko-t-linux-talos-2404 -c "$COUNT" "$@"
