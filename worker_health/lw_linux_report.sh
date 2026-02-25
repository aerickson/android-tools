#!/usr/bin/env bash

set -e

echo "Linux Talos 1804 Worker count: $(./lw_1804.sh | wc -l)"
echo "Linux Talos 1804 Worker count (no quarantined): $(./lw_1804_no_quarantined.sh | wc -l)"
echo ""
echo "Linux Talos 2404 Worker count: $(./lw_2404.sh | wc -l)"
echo "Linux Talos 2404 Worker count (no quarantined): $(./lw_2404_no_quarantined.sh | wc -l)"
