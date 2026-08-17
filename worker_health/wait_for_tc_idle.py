#!/usr/bin/env python3

import sys

from worker_health.idle_cli import wait_for_tc_idle_main


if __name__ == "__main__":
    sys.exit(wait_for_tc_idle_main())
