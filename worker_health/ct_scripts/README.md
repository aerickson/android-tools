# ct_scripts

## Overview

These are diagnostic shell scripts that can be used with `create_tc_task.py`
and it's `ct_*.sh` driver scripts (or other tools like
mozilla-bitbar-devicepool's `lt_run_cmd`)..

## Speedtest.net benchmark

`speedtest_net_benchmark.sh` downloads Ookla's official CLI into a temporary
directory, runs one non-interactive measurement, and writes its JSON response
to the Taskcluster `public/out` artifact directory.  To submit independent
S24 measurements, use the existing generic S24 driver:

```bash
./ct-bitbar-gw-perf-s24.sh 5 -s ./ct_scripts/speedtest_net_benchmark.sh -t 600
```

The task timeout above is ten minutes and can be adjusted as needed.
