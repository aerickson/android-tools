# ct_scripts

## Overview

These are diagnostic shell scripts that can be used with `create_tc_task.py`
and it's `ct_*.sh` driver scripts (or other tools like
mozilla-bitbar-devicepool's `lt_run_cmd`)..

## Mercurial clone telemetry

`hg_clone_network_check.py` writes its output under `out/` by default, so it
is uploaded as Taskcluster's `public/out` artifact. It records a sample every
15 seconds while each clone or update phase runs. Each phase is atomically
persisted to `<phase>.telemetry.json` after every sample, so the last sample
is available even when Taskcluster terminates a timed-out task. Samples in
`results.json` include the container's default-route network byte counters,
the Mercurial process CPU and I/O counters, cgroup CPU statistics, and
filesystem capacity. Set `--telemetry-interval 0` to disable sampling, or
supply another interval in seconds.

To reproduce the production-style `robustcheckout` path that timed out in
Taskcluster task `dS1mEb9oS3yZKcJsukvhog`, use
`--checkout-mode robustcheckout` with the
`bitbar-docker-with-robustcheckout` configuration. Its defaults match that
task's `try` URL, `mozilla-unified` upstream, perftest sparse profile, and
revision; override any `--robustcheckout-*` option to test another job.

## Speedtest.net benchmark

`speedtest_net_benchmark.sh` downloads Ookla's official CLI into a temporary
directory, runs one non-interactive measurement, and writes its JSON response
to the Taskcluster `public/out` artifact directory.  To submit independent
S24 measurements, use the existing generic S24 driver:

```bash
./ct-bitbar-gw-perf-s24.sh 5 -s ./ct_scripts/speedtest_net_benchmark.sh -t 600
```

The task timeout above is ten minutes and can be adjusted as needed.
