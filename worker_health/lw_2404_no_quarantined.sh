#!/usr/bin/env bash

set -e
# set -x

PROV="releng-hardware"
WTYPE="gecko-t-linux-talos-2404"

QUAR_GREP_FILTER=$(pipenv run ./quarantine_tool.py "$PROV" "$WTYPE" show "$@" | tr ',' '|')
pipenv run ./list_workers.py "$PROV" "$WTYPE" | grep -vE "$QUAR_GREP_FILTER"
