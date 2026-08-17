#!/usr/bin/env python3

import sys

from worker_health.idle_cli import workers_idle_main


if __name__ == "__main__":
    sys.exit(workers_idle_main())
