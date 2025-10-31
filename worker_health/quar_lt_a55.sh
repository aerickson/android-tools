#!/usr/bin/env bash

set -e
set -x

PROV="proj-autophone"
WTYPE="gecko-t-lambda-perf-a55"

pipenv run ./quarantine_tool.py "$PROV" "$WTYPE" "$@"
