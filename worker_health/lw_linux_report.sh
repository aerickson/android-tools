#!/usr/bin/env bash

set -e
# set -x

# d1804=$(./lw_1804.sh | wc -l)
# d1804_NOQ=$(./lw_1804_no_quarantined.sh | wc -l)
# d2404=$(./lw_2404.sh | wc -l)
# d2404_NOQ=$(./lw_2404_no_quarantined.sh | wc -l)



# 1804
#
#
PROV="releng-hardware"
WTYPE="gecko-t-linux-talos-1804"
d1804=$(pipenv run ./list_workers.py "$PROV" "$WTYPE")
d1804_C=$(echo "$d1804" | wc -l)
Q_HOSTS=$(pipenv run ./quarantine_tool.py "$PROV" "$WTYPE" show "$@")
QUAR_GREP_FILTER=$(echo "$Q_HOSTS" | tr ',' '|')
d1804_NOQ_C=$(echo "$d1804" | grep -cvE "$QUAR_GREP_FILTER")

# 2404
PROV="releng-hardware"
WTYPE="gecko-t-linux-talos-2404"
d2404=$(pipenv run ./list_workers.py "$PROV" "$WTYPE")
d2404_C=$(echo "$d2404" | wc -l)
Q_HOSTS=$(pipenv run ./quarantine_tool.py "$PROV" "$WTYPE" show "$@")
QUAR_GREP_FILTER=$(echo "$Q_HOSTS" | tr ',' '|')
d2404_NOQ_C=$(echo "$d2404" | grep -cvE "$QUAR_GREP_FILTER")


echo "Linux Talos 1804 Worker count (non-quarantined/total): $d1804_NOQ_C/$d1804_C"
echo "Linux Talos 2404 Worker count (non-quarantined/total): $d2404_NOQ_C/$d2404_C"
