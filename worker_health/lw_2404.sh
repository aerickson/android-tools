#!/usr/bin/env bash

set -e
# set -x

PROV="releng-hardware"
WTYPE="gecko-t-linux-talos-2404"

pipenv run ./list_workers.py "$PROV" "$WTYPE"
