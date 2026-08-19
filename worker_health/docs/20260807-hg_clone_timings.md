# Mercurial clone timings

Research log started 2026-08-07.

## Motivation

Bitbar hosts have been unable to clone `mozilla-unified` before the task
timeout since their move from the San Jose, California datacenter to a new,
nominally faster datacenter in Florida. This research compares clone behavior
across Bitbar, existing Taskcluster workers, and a controlled GCP baseline to
identify whether network location, storage, CPU, memory, or clone configuration
is responsible.

The CDN whitelist includes these Bitbar source ranges:

```text
# Old DC
205.234.8.112/28

# New DC
128.136.193.129/26
```

The authoritative Terraform definition is the `bitbar_ips` output in
[`modules/waf_ip_allowlists/outputs.tf`](https://github.com/mozilla/webservices-infra/blob/main/modules/waf_ip_allowlists/outputs.tf).
The Mercurial Fastly module consumes it through `sigsci_site_list.bitbar_ip_list`
in [`hg/tf/modules/fastly/main.tf`](https://github.com/mozilla/webservices-infra/blob/main/hg/tf/modules/fastly/main.tf).
Both locations are shown in [webservices-infra PR #10726](https://github.com/mozilla/webservices-infra/pull/10726),
`fix(hg): add IP blocks for LambdaTest and new Bitbar DC (Bug 1922180)`.

### Current Fastly rate-limit IP bypasses

As read from the current `main` branch, the Mercurial Fastly
`rate_limit_unprivileged` rule bypasses its 50-requests-per-minute limit for
four IP lists, totaling 229 IPv4 CIDRs or addresses:

| List | Entries | Contents |
| --- | ---: | --- |
| GCP | 225 | Google Cloud IPv4 ranges for `us-central1`, `us-west1`, `northamerica-northeast1`, and `us-east1`; maintained from Google Cloud's published `cloud.json` data. |
| Bitbar | 2 | `205.234.8.112/28`, `128.136.193.129/26` |
| LambdaTest | 1 | `209.58.137.41/32` |
| Mozilla MDC1 | 1 | `63.245.208.0/23` |

The rule also excludes known user agents and `/bundles/*` requests. These are
rate-limit bypasses in this Fastly rule, not proof that every CDN or WAF policy
allows the same traffic. The GCP baseline VM's public IP, `34.138.201.81`, is
within the included `34.138.0.0/15` range.

## Question

What factors account for differences in `hg clone` duration across workers?

## GCP baseline setup

The replacement Ubuntu 24.04 `e2-standard-2` baseline is created in
`taskcluster-imaging/us-east1-b` with this command:

```bash
gcloud compute instances create aerickson-hg-clone-benchmark-20260807-standard2 \
  --project taskcluster-imaging \
  --zone us-east1-b \
  --machine-type e2-standard-2 \
  --image-family ubuntu-2404-lts-amd64 \
  --image-project ubuntu-os-cloud \
  --boot-disk-size 50GB \
  --boot-disk-type pd-balanced \
  --labels=purpose=hg-clone-benchmark,owner=aerickson
```

The 50-GB disk provides room for the clone, working copy, and benchmark
artifacts. Benchmark instance names must begin with `aerickson-` and should be
timestamped or otherwise uniquely identified. Delete the instance when its
baseline run is no longer needed.

## Measurements

All runs cloned `https://hg-edge.mozilla.org/mozilla-unified`. The earlier
Taskcluster runs were stopped by their 40-minute maximum runtime, so their
durations are lower bounds rather than successful clone timings.

## Bitbar Mercurial tooling

The Bitbar Docker image installs a pinned Mercurial version:

```text
/Users/aerickson/git/mozilla-bitbar-docker/Dockerfile:192
pip3 install mercurial==5.9.3
```

It fetches `robustcheckout.py` directly from `mozilla-central` at revision
`260e22f03e984e0ced16b6c5ff63201cdef0a1f6`; no separate extension version is
declared:

```text
/Users/aerickson/git/mozilla-bitbar-docker/Dockerfile:95
ADD https://hg.mozilla.org/mozilla-central/raw-file/260e22f03e984e0ced16b6c5ff63201cdef0a1f6/testing/mozharness/external_tools/robustcheckout.py /usr/local/src/robustcheckout.py
```

The extension is enabled by the Bitbar Taskcluster configuration:

```text
/Users/aerickson/git/mozilla-bitbar-docker/taskcluster/hgrc:13
[extensions]
robustcheckout = /usr/local/src/robustcheckout.py
```

The Dockerfile installs this configuration system-wide at
`/etc/mercurial/hgrc.d/mozilla.rc` (`Dockerfile:115`).

The timeout task shown above invokes bare `hg clone`; enabling the extension
does not by itself establish that the task invoked a `robustcheckout` command.

## Benchmark configurations

`hg_clone_network_check.py` selects a named, pinned configuration with
`--configuration` and records the effective configuration in `results.json`.

| Configuration | Mercurial | Python | Per-run hgrc |
| --- | --- | --- | --- |
| `latest-without-robust-checkout` (default) | `7.0.2` | System `python3` | Empty `HGRCPATH`; the script verifies that `robustcheckout` is not enabled. |
| `bitbar-docker-with-robustcheckout` | `5.9.3` | Python `3.9`, resolved or installed through `uv` | Bitbar's progress, extension, host-security, diff, and pager settings; the script fetches the fixed `robustcheckout.py` revision and verifies it is enabled. |
| `latest-with-robustcheckout` | `7.0.2` | System `python3` | The same extension-oriented settings, with `robustcheckout` pinned to the current canonical version-control-tools revision `ef45ece67c97ba67a13182c97496d8a90b768eea`. |

The configuration name is intentionally stable while the exact component
versions remain explicit in the script and result data. The Bitbar
configuration mirrors the image's pinned Python 3.9 and Mercurial 5.9.3 rather
than using the Ubuntu 24.04 system Python.

The canonical current `robustcheckout` declares `minimumhgversion = b"4.5"`
and has no declared upper bound. Its source includes compatibility handling for
Mercurial 6.4 and 7.2 API changes. The `latest-with-robustcheckout`
configuration therefore tests the same Mercurial 7.0.2 and system Python 3.12
as the latest baseline, with the extension pinned at the latest extension-file
revision when this configuration was added. Mozilla's CI documentation advises
updating vendored `robustcheckout` alongside Mercurial upgrades because it uses
Mercurial internals.

| Date/time (UTC) | Worker | Command | Observed duration | Outcome |
| --- | --- | --- | ---: | --- |
| 2026-08-07 (time not recorded) | `aerickson-hg-benchmarking` | `time hg clone https://hg-edge.mozilla.org/mozilla-unified` | 23m 43.376s | Killed after the clone bundle had been applied and the working copy was being updated. |
| 2026-08-07 20:04:36–20:44:37 | `bitbar/s24-02` | `hg clone https://hg-edge.mozilla.org/mozilla-unified` | 40m 01s | Taskcluster aborted the task at its 40-minute maximum while adding changesets. |
| 2026-08-07 22:55:59–23:35:51 | `mdc1/t-linux64-ms-012` | `hg clone https://hg-edge.mozilla.org/mozilla-unified` | 40m 00s | Taskcluster aborted the task at its 40-minute maximum while adding files. |
| 2026-08-08 01:13:26–01:21:36 | `aerickson-hg-clone-benchmark-20260807-standard2` | `hg clone --noupdate`, then `hg update` | 8m 09.713s | Successful complete clone and working-copy update. |
| 2026-08-08 01:57:11–02:06:12 | `aerickson-hg-clone-benchmark-20260807-standard2` | `latest-without-robust-checkout` | 9m 00.622s | Successful complete clone and update; runner verified `robustcheckout` was disabled. |
| 2026-08-08 02:20:21–02:32:05 | `aerickson-hg-clone-benchmark-20260807-standard2` | `bitbar-docker-with-robustcheckout` | 11m 43.308s | Successful complete clone and update; runner verified the pinned robustcheckout extension was enabled. |
| 2026-08-10 19:59:51–20:09:25 | `aerickson-hg-clone-benchmark-20260807-standard2` | `latest-with-robustcheckout` | 9m 34.099s | Successful complete clone and update; runner verified the current canonical robustcheckout extension was enabled. |
| 2026-08-19 02:24:46–02:48:15 | `bitbar-ubuntu-157` (A55) | `bitbar-docker-with-robustcheckout` | 23m 29.141s | Successful complete clone and update; continuous Mercurial progress and per-phase telemetry artifacts were captured. [Task](https://firefox-ci-tc.services.mozilla.com/tasks/k8Dj0KSFQyiIcfjPHIDciw) |

### Bitbar A55 successful telemetry run

Task [`k8Dj0KSFQyiIcfjPHIDciw`](https://firefox-ci-tc.services.mozilla.com/tasks/k8Dj0KSFQyiIcfjPHIDciw)
completed on `bitbar-ubuntu-157` using script version `1.1.7`, Mercurial
`5.9.3`, Python `3.9.21`, and the `bitbar-docker-with-robustcheckout`
configuration. Its generated `HGRCPATH` loaded correctly, producing live
Mercurial progress output throughout clone and update.

| Phase | Elapsed | Child user CPU | Child system CPU | Exit |
| --- | ---: | ---: | ---: | ---: |
| `hg clone --noupdate` | 22m 45.134s | 1237.776s | 101.091s | 0 |
| `hg update` | 44.007s | 249.365s | 125.975s | 0 |
| Total Mercurial phases | 23m 29.141s | 1487.141s | 227.066s | 0 |

Clone telemetry recorded 2,912,319,491 received bytes on container `eth0`
over 1,365.127 seconds (about 17.1 Mb/s averaged across the full phase). The
cgroup reported 1,345.126 CPU seconds, close to one busy core for the phase,
and zero throttling. This confirms the clone was neither stuck nor cgroup CPU
limited, but it did not approach the host's nominal 10-Gb/s network capacity.

### `aerickson-hg-clone-benchmark-20260807-standard2`

The Ubuntu 24.04 GCP `e2-standard-2` baseline completed successfully using
the `latest-without-robust-checkout` configuration: `hg_clone_network_check.py`
version `1.0.4`, Mercurial `7.0.2`, and Python `3.12.3`. The runner used an
empty per-run `HGRCPATH` (`benchmark.hgrc`) to avoid user configuration
affecting the result.

| Phase | Elapsed | Child user CPU | Child system CPU | Exit |
| --- | ---: | ---: | ---: | ---: |
| `hg clone --noupdate` | 2m 34.675s | 95.381s | 47.560s | 0 |
| `hg update` | 5m 35.037s | 468.478s | 133.208s | 0 |
| Total Mercurial phases | 8m 09.713s | 563.860s | 180.768s | 0 |

The host had two vCPUs, 8 GB RAM, a 50-GB balanced persistent disk, and public
IP `35.229.52.135`. The completed destination occupied 10,870,455,572 bytes.
The runner started at 01:12:52 UTC and finished at 01:22:07 UTC; that broader
9m 15s interval includes package provisioning before the clone and final
directory-size collection after the update. The phase total is the comparable
clone timing.

### Verified `latest-without-robust-checkout` repeat

The configuration-aware repeat used script version `1.1.3` and verified that
the effective `robustcheckout` extension value was null. It used the same
Mercurial (`7.0.2`), Python (`3.12.3`), host, and repository as the initial
`e2-standard-2` baseline.

| Phase | Elapsed | Child user CPU | Child system CPU | Exit |
| --- | ---: | ---: | ---: | ---: |
| `hg clone --noupdate` | 3m 14.187s | 107.003s | 66.391s | 0 |
| `hg update` | 5m 46.435s | 486.156s | 143.491s | 0 |
| Total Mercurial phases | 9m 00.622s | 593.159s | 209.882s | 0 |

The phase total was 50.910s (10.4%) slower than the first successful
`e2-standard-2` run, while still completing successfully. The runner started
at 01:56:54 UTC and finished at 02:06:35 UTC; its total interval includes
setup and final directory-size collection outside the measured phases.

### `bitbar-docker-with-robustcheckout` comparison

The Bitbar-component configuration completed successfully with script version
`1.1.3`, Mercurial `5.9.3`, and `uv`-managed Python `3.9.25`. It verified that
the effective `robustcheckout` extension path was the fetched source at
revision `260e22f03e984e0ced16b6c5ff63201cdef0a1f6`, with SHA-256
`91cef21a3db52aedd3b3ba8b4c6b9d9b24e7c416ab80d10d58312960d226c095`.

| Phase | Elapsed | Child user CPU | Child system CPU | Exit |
| --- | ---: | ---: | ---: | ---: |
| `hg clone --noupdate` | 4m 43.215s | 194.187s | 74.583s | 0 |
| `hg update` | 7m 00.093s | 595.980s | 153.017s | 0 |
| Total Mercurial phases | 11m 43.308s | 790.167s | 227.599s | 0 |

This is a component-equivalent comparison, not a byte-for-byte rebuild of the
Bitbar Docker image: it ran on Ubuntu 24.04 with a `uv`-managed Python runtime.
Its phase total was 2m 42.686s (30.1%) slower than the verified
`latest-without-robust-checkout` repeat on the same host. The runner started at
02:15:26 UTC and finished at 02:32:28 UTC; this broader interval includes
dependency setup and final directory-size collection outside the measured
phases.

### Repeated interleaved configuration comparison

On 2026-08-10, the comparison runner performed three successful runs of each
configuration on `aerickson-hg-clone-benchmark-20260807-standard2`. It
alternated configuration order (`latest`, `Bitbar`, `Bitbar`, `latest`,
`latest`, `Bitbar`) and removed each clone worktree before starting the next
run. The runner version was `1.1.3`; all latest runs verified that
`robustcheckout` was disabled and all Bitbar-component runs verified the pinned
extension was enabled.

| Configuration | Clone median (range) | Update median (range) | Total median (range) |
| --- | ---: | ---: | ---: |
| `latest-without-robust-checkout` | 3m 07.870s (3m 04.285s–3m 10.198s) | 5m 53.283s (5m 45.534s–5m 54.088s) | 9m 01.153s (8m 49.819s–9m 04.286s) |
| `bitbar-docker-with-robustcheckout` | 4m 57.151s (4m 44.746s–4m 58.658s) | 7m 06.821s (7m 05.771s–7m 15.017s) | 12m 03.972s (11m 59.764s–12m 04.430s) |

The Bitbar-component configuration's median was 3m 02.819s (33.8%) slower
than the latest/no-robustcheckout median. The difference appears in both
measured phases: its median clone was 1m 49.282s slower (58.2%), and its
median update was 1m 12.488s slower (20.5%). This isolates a stable
configuration/component effect on this GCP host; it does not establish that
the same effect explains the Bitbar timeout, where network location and worker
resources differ.

### `latest-with-robustcheckout` result

The current canonical extension configuration completed successfully with
script version `1.1.4`, Mercurial `7.0.2`, and Python `3.12.3`, matching the
latest/no-robustcheckout component versions. It enabled robustcheckout revision
`ef45ece67c97ba67a13182c97496d8a90b768eea` from version-control-tools, with
SHA-256 `f39b5c1ea940956aded51609e6abb7089b6a000b0c9c1f4094378de337e93ab4`.

| Phase | Elapsed | Child user CPU | Child system CPU | Exit |
| --- | ---: | ---: | ---: | ---: |
| `hg clone --noupdate` | 3m 36.911s | 120.652s | 77.723s | 0 |
| `hg update` | 5m 57.189s | 500.209s | 145.344s | 0 |
| Total Mercurial phases | 9m 34.099s | 620.860s | 223.067s | 0 |

This single result is 32.946s (6.1%) slower than the three-run
`latest-without-robustcheckout` median. Most of that difference is in clone
(29.041s); update differs by only 3.906s. It is above the no-robustcheckout
three-run total range (8m 49.819s–9m 04.286s), but needs interleaved repeats
before treating the size of this effect as established.

### Repeated latest-Hg robustcheckout comparison

On 2026-08-10, a second comparison-runner experiment interleaved three
successful `latest-without-robust-checkout` control runs with three successful
`latest-with-robustcheckout` runs. All runs used runner version `1.1.4`,
Mercurial `7.0.2`, and Python `3.12.3`; each clone destination was removed
before the next run. The controls verified that robustcheckout was disabled,
and the test runs verified the pinned canonical extension was enabled.

| Configuration | Clone median (range) | Update median (range) | Total median (range) |
| --- | ---: | ---: | ---: |
| `latest-without-robust-checkout` | 3m 11.634s (3m 08.713s–3m 19.146s) | 5m 42.968s (5m 40.023s–5m 50.150s) | 8m 51.680s (8m 51.657s–9m 09.295s) |
| `latest-with-robustcheckout` | 3m 19.669s (3m 18.427s–3m 29.373s) | 5m 49.311s (5m 47.196s–5m 50.664s) | 9m 08.979s (9m 05.623s–9m 20.037s) |

The canonical robustcheckout configuration's median was 17.299s (3.3%)
slower. It was slower in all three matched control/test pairs: 10.742s
(2.0%), 17.322s (3.3%), and 13.943s (2.6%). The median difference comprises
8.034s in clone (4.2%) and 6.343s in update (1.9%). This controlled result is
the appropriate estimate of the extension/configuration effect; the earlier
one-off 6.1% result was higher than this interleaved estimate.

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

## Expected duration

For a successful full clone and working-copy update, the expected end-to-end
duration depends strongly on sustained network throughput, CPU, memory, and
storage performance.

| Worker profile | Reasonable end-to-end range |
| --- | ---: |
| Shared-core VM with 1 GB RAM and modest network | 25–60+ minutes; may run out of memory |
| Typical CI worker with 4+ cores and SSD storage | 8–20 minutes |
| Fast network, NVMe storage, and ample CPU/RAM | 4–10 minutes |

The 6.55 GB bundle alone has a theoretical transfer floor of about nine
minutes at a sustained 100 Mbps, four to five minutes at 250 Mbps, and about
one minute at 1 Gbps. These figures exclude bundle processing and working-copy
update, which are CPU-, memory-, and storage-intensive for this approximately
one-million-file repository.

At the observed 4.98 MB/s on `aerickson-hg-benchmarking`, the bundle transfer
took 22m 27.7s. A sub-30-minute completed clone on a 1-GB shared-core
`e2.micro` instance would therefore be optimistic, and failure during the
working-copy update is consistent with memory pressure.

During the current GCP baseline run, the load average was observed to peak at
approximately 14. Load average includes runnable and uninterruptible (often
I/O-waiting) tasks, so this alone does not distinguish CPU saturation from
storage or memory pressure. Capture CPU utilization, memory/swap state, and
disk I/O alongside future runs.

The `e2.micro` is not suitable for a complete clone baseline: its 1 GB of RAM
caused substantial kernel memory-reclaim activity (`kswapd`) and made the host
non-responsive during the run. Use at least a non-shared-core `e2-standard-2`
(2 vCPUs, 8 GB RAM) for a baseline intended to separate clone/network behavior
from VM resource exhaustion.

The 40-minute Taskcluster timeouts warrant investigation, but their incomplete
logs do not yet isolate a network, CPU, storage, memory, or clone-bundle
selection cause.

## Observations

- The standalone benchmark completed the bundle-transfer and bundle-application
  phases within 23m 43s, but did not complete the working-copy update.
- The controlled `e2-standard-2` baseline completed both Mercurial phases in
  8m 09.713s. This demonstrates that a GCP worker with adequate dedicated CPU
  and memory can complete the operation well below the 40-minute Taskcluster
  limit.
- The configuration-aware `latest-without-robust-checkout` repeat completed in
  9m 00.622s and verified that the robustcheckout extension was disabled.
- The Bitbar-component configuration completed in 11m 43.308s with the pinned
  robustcheckout extension enabled, 30.1% slower than the verified
  no-robustcheckout repeat on the same host.
- Three interleaved repeats confirmed this configuration difference: the
  Bitbar-component median was 12m 03.972s versus 9m 01.153s for
  latest/no-robustcheckout, a 33.8% increase.
- A single latest-Hg run with the current canonical robustcheckout extension
  completed in 9m 34.099s (6.1% above the earlier no-robustcheckout median).
- Three interleaved latest-Hg control/test pairs measured a smaller, consistent
  robustcheckout/configuration overhead: 17.299s (3.3%) at the median.
- The earlier `e2.micro` bare `hg clone` did not complete either: its captured
  output ends in `Killed` during `updating to branch default`. The new runner's
  small Python wrapper and log capture are not a plausible explanation for the
  memory pressure; the key differences may instead include the Mercurial
  version, repository/bundle growth, available memory/cache, background work,
  or variable shared-core CPU availability.
- Neither Taskcluster worker completed within the 40-minute task limit.
- The two Taskcluster logs stopped in different phases: `s24-02` while adding
  changesets and `t-linux64-ms-012` while adding files.

## Follow-up

- Use `./ct_scripts/hg_clone_network_check.py` for future runs. Treat its
  output as the standardized results format, and improve the script as new
  measurements reveal missing context or useful diagnostics.
- For an unattended comparison of the two named configurations, use
  `./ct_scripts/hg_clone_configuration_comparison.py --install-system-dependencies`.
  It performs three runs of each configuration in alternating order, retains
  each run's logs and `results.json`, removes only the corresponding disposable
  clone worktree between runs, and writes aggregate JSON plus a Markdown
  summary. The comparison script requires `hg_clone_network_check.py` beside it
  (or at `~/hg_clone_network_check.py` when copied to a benchmark host).
- On a fresh Ubuntu image that lacks `ensurepip`, invoke the default
  configuration with `--install-system-dependencies`; it runs `sudo apt-get
  update` and installs `python3-venv` before creating the isolated Mercurial
  virtual environment. For `bitbar-docker-with-robustcheckout`, the same flag
  installs `uv` when needed, then allows it to install its managed Python 3.9
  runtime without changing APT sources; it also installs `build-essential`,
  because Mercurial 5.9.3 is built from source on this host.
- Run successful, comparable clones with a duration that exceeds the observed
  completion time, and capture the Mercurial version and configuration.
- Run `latest-with-robustcheckout` to isolate the current canonical extension
  on the same Mercurial 7.0.2 and Python 3.12 baseline.
- Separate and time bundle download, bundle application, post-bundle change
  processing, and working-copy update.
- Capture CPU, memory, storage, and network characteristics for each worker.
