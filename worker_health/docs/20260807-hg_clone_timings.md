# Mercurial clone timings

Research log started 2026-08-07.

## Question

What factors account for differences in `hg clone` duration across workers?

## Measurements

All runs cloned `https://hg-edge.mozilla.org/mozilla-unified`. The Taskcluster
runs were stopped by their 40-minute maximum runtime, so their durations are
lower bounds rather than successful clone timings.

| Date/time (UTC) | Worker | Command | Observed duration | Outcome |
| --- | --- | --- | ---: | --- |
| 2026-08-07 (time not recorded) | `aerickson-hg-benchmarking` | `time hg clone https://hg-edge.mozilla.org/mozilla-unified` | 23m 43.376s | Killed after the clone bundle had been applied and the working copy was being updated. |
| 2026-08-07 20:04:36–20:44:37 | `bitbar/s24-02` | `hg clone https://hg-edge.mozilla.org/mozilla-unified` | 40m 01s | Taskcluster aborted the task at its 40-minute maximum while adding changesets. |
| 2026-08-07 22:55:59–23:35:51 | `mdc1/t-linux64-ms-012` | `hg clone https://hg-edge.mozilla.org/mozilla-unified` | 40m 00s | Taskcluster aborted the task at its 40-minute maximum while adding files. |

### `aerickson-hg-benchmarking`

The clone downloaded and applied the bundle before it was killed during the
working-copy update.

| Metric | Value |
| --- | ---: |
| Files transferred | 997,644 |
| Bundle size | 6.55 GB |
| Transfer time | 1,347.7s (22m 27.7s) |
| Reported transfer rate | 4.98 MB/s |
| Shell real time | 23m 43.376s |
| Shell user time | 13m 48.123s |
| Shell system time | 8m 51.398s |
| Instance | GCP `e2.micro`, `us-east` |
| Public IP | `35.229.52.135` |

The bundle was
`https://storage.googleapis.com/moz-hg-bundles-gcp-us-east1/mozilla-unified/de4ec93e5258aeb6dd288cfa438f0a4b06e95b18.stream-v2.hg`.
After bundle application, Mercurial added 321 changesets (1,421 changes to
1,023 files), ending at `a4f987db41c8`, then was killed during `updating to
branch default`.

### Taskcluster runs

| Task | Worker pool | Worker | Progress when aborted | Task result |
| --- | --- | --- | --- | --- |
| [`-ZY3cSXkToaubxDVn_Y1gA`](https://firefox-ci-tc.services.mozilla.com/tasks/-ZY3cSXkToaubxDVn_Y1gA/runs/0) | `proj-autophone/gecko-t-bitbar-gw-perf-s24` | `bitbar/s24-02` | 778,223 / 964,277 changesets | Failed: max runtime exceeded |
| [`OUFtHEcsSYyJ-5SZXY8W5g`](https://firefox-ci-tc.services.mozilla.com/tasks/OUFtHEcsSYyJ-5SZXY8W5g) | `releng-hardware/gecko-t-linux-talos-1804` | `mdc1/t-linux64-ms-012` | 879,449 / 993,181 files | Failed: max runtime exceeded |

The progress counters use different clone phases and totals, so they are not a
direct speed comparison. Both runs are incomplete.

## Observations

- The standalone benchmark completed the bundle-transfer and bundle-application
  phases within 23m 43s, but did not complete the working-copy update.
- Neither Taskcluster worker completed within the 40-minute task limit.
- The two Taskcluster logs stopped in different phases: `s24-02` while adding
  changesets and `t-linux64-ms-012` while adding files.

## Follow-up

- Use `./ct_scripts/hg_clone_network_check.py` for future runs. Treat its
  output as the standardized results format, and improve the script as new
  measurements reveal missing context or useful diagnostics.
- Run successful, comparable clones with a duration that exceeds the observed
  completion time, and capture the Mercurial version and configuration.
- Separate and time bundle download, bundle application, post-bundle change
  processing, and working-copy update.
- Capture CPU, memory, storage, and network characteristics for each worker.
