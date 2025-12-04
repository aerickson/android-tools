#!/usr/bin/env bash

set -e
set -x

PROV="releng-hardware"
WTYPE="gecko-t-linux-talos-1804"

pipenv run ./wait_for_tc_idle.py -p $PROV -w $WTYPE "$@"
