#!/usr/bin/env bash

set -e
set -x

SCRIPTPATH="$( cd "$(dirname "$0")" ; pwd -P )"
"$SCRIPTPATH"/get_pending_jobs.py --filter 'android-hw' "$@"
