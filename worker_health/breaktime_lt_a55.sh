#!/usr/bin/env bash

set -e
set -x

./quar_lt_a55.sh quarantine -r 'lots of failures, take a break' -d '3 days' "$@"
