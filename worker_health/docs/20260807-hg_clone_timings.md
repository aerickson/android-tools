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

The Ubuntu 24.04 `e2.micro` baseline was created in
`taskcluster-imaging/us-east1-b` with this command:

```bash
gcloud compute instances create hg-clone-benchmark-20260807-1715 \
  --project taskcluster-imaging \
  --zone us-east1-b \
  --machine-type e2-micro \
  --image-family ubuntu-2404-lts-amd64 \
  --image-project ubuntu-os-cloud \
  --boot-disk-size 50GB \
  --boot-disk-type pd-balanced \
  --labels=purpose=hg-clone-benchmark,owner=aerickson
```

The 50-GB disk provides room for the clone, working copy, and benchmark
artifacts. The instance name is intentionally timestamped and should be
deleted when this baseline run is no longer needed.

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

## Expected duration

For a successful full clone and working-copy update, the expected end-to-end
duration depends strongly on sustained network throughput, CPU, memory, and
storage performance.

| Worker profile | Reasonable end-to-end range |
| --- | ---: |
| 1 vCPU / 1 GB VM with modest network | 25–60+ minutes; may run out of memory |
| Typical CI worker with 4+ cores and SSD storage | 8–20 minutes |
| Fast network, NVMe storage, and ample CPU/RAM | 4–10 minutes |

The 6.55 GB bundle alone has a theoretical transfer floor of about nine
minutes at a sustained 100 Mbps, four to five minutes at 250 Mbps, and about
one minute at 1 Gbps. These figures exclude bundle processing and working-copy
update, which are CPU-, memory-, and storage-intensive for this approximately
one-million-file repository.

At the observed 4.98 MB/s on `aerickson-hg-benchmarking`, the bundle transfer
took 22m 27.7s. A sub-30-minute completed clone on its 1-vCPU, 1-GB
`e2.micro` instance would therefore be optimistic, and failure during the
working-copy update is consistent with memory pressure.

The 40-minute Taskcluster timeouts warrant investigation, but their incomplete
logs do not yet isolate a network, CPU, storage, memory, or clone-bundle
selection cause.

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
