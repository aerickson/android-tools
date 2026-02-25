#!/usr/bin/env bash

set -e
set -x

PROV="proj-autophone"
WTYPE="gecko-t-lambda-perf-a55"

./list_workers.py "$PROV" "$WTYPE"
