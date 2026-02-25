#!/usr/bin/env bash

set -e
# set -x

PROV="releng-hardware"
WTYPE="gecko-t-linux-talos-2404"

pipenv run ./quarantine_tool.py "$PROV" "$WTYPE" "$@"
