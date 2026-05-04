#!/usr/bin/env bash

set -e

. ./common.sh

the_host=$1

if [ -z "$the_host" ]; then
  echo "A destination host must be specified."
  exit 1
fi

set -x
# TODO: grab this from SOPS vs local clone of devicepool
scp "$BITBAR_ENV_FILE_PATH" "$the_host":bitbar.env
ssh "$the_host" 'sudo mv bitbar.env /etc/bitbar/ && sudo chown root:bitbar /etc/bitbar/bitbar.env && sudo chmod 660 /etc/bitbar/bitbar.env'

scp "$BITBAR_V3_ENV_FILE_PATH" "$the_host":bitbar-v3.env
ssh "$the_host" 'sudo mv bitbar-v3.env /etc/bitbar/ && sudo chown root:bitbar /etc/bitbar/bitbar-v3.env && sudo chmod 660 /etc/bitbar/bitbar-v3.env'

scp "$LT_ENV_FILE_PATH" "$the_host":lambdatest.env
ssh "$the_host" 'sudo mv lambdatest.env /etc/bitbar/ && sudo chown root:bitbar /etc/bitbar/lambdatest.env && sudo chmod 660 /etc/bitbar/lambdatest.env'
