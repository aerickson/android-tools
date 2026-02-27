#!/usr/bin/env bash

set -e

./create_tc_task.py -q proj-autophone/gecko-t-bitbar-gw-test-1 -c 100 -C -L 200 -I 120 "$@"
