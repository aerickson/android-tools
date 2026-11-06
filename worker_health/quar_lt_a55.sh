#!/usr/bin/env bash

set -e
set -x

PROV="proj-autophone"
WTYPE="gecko-t-lambda-perf-a55"

VENV=$(pipenv --venv)
"$VENV"/bin/python ./quarantine_tool.py "$PROV" "$WTYPE" "$@"
