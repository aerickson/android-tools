#!/usr/bin/env bash

set -e
# set -x


# 2404
PROV="proj-autophone"
WTYPE="gecko-t-lambda-perf-a55"
d2404=$(pipenv run ./list_workers.py "$PROV" "$WTYPE")
d2404_C=$(echo "$d2404" | wc -l)
Q_HOSTS=$(pipenv run ./quarantine_tool.py "$PROV" "$WTYPE" show "$@")
QUAR_GREP_FILTER=$(echo "$Q_HOSTS" | tr ',' '|')
d2404_NOQ_C=$(echo "$d2404" | grep -cvE "$QUAR_GREP_FILTER")


echo "LT A55 Worker count (non-quarantined/total): $d2404_NOQ_C/$d2404_C"
