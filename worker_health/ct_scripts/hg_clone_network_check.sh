#!/usr/bin/env bash

set -euo pipefail

python3 -m venv .hg-venv
source .hg-venv/bin/activate
python -m pip install --upgrade pip mercurial

hg --version
curl wtfismyip.com/text
hg clone https://hg-edge.mozilla.org/mozilla-unified
