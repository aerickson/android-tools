#!/usr/bin/env bash

set -e
# set -x

PROV="releng-hardware"
WTYPE="gecko-t-linux-talos-2404"

VENV=$(pipenv --venv)
"$VENV"/bin/python ./quarantine_tool.py "$PROV" "$WTYPE" "$@"
