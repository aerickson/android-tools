#!/usr/bin/env bash

set -e
# set -x

echo ""
echo "WARNING: report is likely wrong for amazon ec2 and bare IP addresses"
echo ""

./missing-workers-r8.py -i /Users/"$USER"/git/ronin_puppet/inventory.d/macmini-m2.yaml
