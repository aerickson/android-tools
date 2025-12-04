#!/usr/bin/env bash

set -euo pipefail
set -x

pipenv run ./list_workers.py releng-hardware gecko-t-linux-talos-2404 "$@"
