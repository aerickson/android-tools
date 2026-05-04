#!/usr/bin/env bash

set -e

. ./common.sh

# goal: checksum config files on the devicepool fleet for comparison

# TODO: make this bring configs back to localhost and show diffs

CHECKSUM_TO_USE='sha256'
CHECKSUM_COMMAND="openssl $CHECKSUM_TO_USE"

# do it
for file in TELEGRAF_CONFIG_FILE LAST_STARTED_SERVICE_FILE SLACK_ALERT_CONFIG_FILE INFLUX_LOGGER_CONFIG_FILE BITBAR_ENV_FILE BITBAR_V3_ENV_FILE LT_ENV_FILE; do
    echo "${!file}: "
    for host_number in 0 1 2; do
        hostname="devicepool-${host_number}.relops.mozops.net"
        # echo "checking ${hostname}..."
        # shellcheck disable=SC2029
        ssh "${hostname}" "sudo ${CHECKSUM_COMMAND} ${!file}"
    done
done
