#!/usr/bin/env python3

import json
import os
import pdb
import sys

import taskcluster

#
# docs: https://pypi.org/project/taskcluster/
#

# don't need to create separate credentials
# per tomprince:
#   You can use taskcluster signin to get creds (and can use -s <scope>, possibly multiple times, to restrict scopes).


with open(os.path.expanduser("~/.tc_quarantine_token")) as json_file:
    data = json.load(json_file)
creds = {"clientId": data["clientId"], "accessToken": data["accessToken"]}

queue = taskcluster.Queue({"rootUrl": "https://firefox-ci-tc.services.mozilla.com", "credentials": creds})

pdb.set_trace()

sys.exit()
