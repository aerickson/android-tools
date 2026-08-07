# Fleet RAPL power-limit scan

Collected 2026-07-24 with `run_parallel_rapl_power_limit_probe.sh` and
`rapl_power_limit_probe.sh`. Results are retained in
`pssh_rapl_power_limit_probe_20260724-141749/`.

The probe used `pssh` with 20 concurrent connections, a 15-second per-host
timeout, and an eight-minute retry window. It reads each RAPL sysfs value with
`sudo -n cat`, avoiding the constraint-file permission failures from the
earlier run.

## Coverage

| Result | Hosts | Share of 280-host inventory |
| --- | ---: | ---: |
| Complete RAPL result | 208 | 74.3% |
| Uncollected after nine attempts | 72 | 25.7% |

All 208 collected results contained complete constraint values; none had a
permission-denied constraint read. The remaining hosts are listed in
`pssh_rapl_power_limit_probe_20260724-141749/uncollected-hosts.txt`.

## RAPL configuration

Every one of the 208 collected hosts produced the same complete RAPL profile.
No configuration variation was observed within the collected population.

| Zone | Enabled | Constraint | Power limit | Time window |
| --- | --- | --- | ---: | ---: |
| `intel-rapl` parent | yes | — | — | — |
| `package-0` | yes | `long_term` | 45,000,000 µW (45 W) | 27,983,872 µs |
| `package-0` | yes | `short_term` | 56,250,000 µW (56.25 W) | 2,440 µs |
| `core` | no | `long_term` | 0 µW | 976 µs |
| `uncore` | no | `long_term` | 0 µW | 976 µs |
| `dram` | no | `long_term` | 0 µW | 976 µs |

This confirms uniform configured RAPL limits and enabled states for the 208
responding hosts. It does not establish fleet-wide uniformity while 72 hosts
remain uncollected.

For the `t-linux64-ms-221` (fast) versus `t-linux64-ms-223` (slow) comparison,
this makes differing configured RAPL limits an unlikely cause of the observed
speed difference: both hosts match this profile, and their full readable
powercap trees also matched. The next investigation should focus on dynamic
conditions—actual power draw, CPU frequency, thermal throttling, workload, or
other firmware/runtime state—rather than static RAPL caps.

## Collection gaps

The final retry produced explicit SSH diagnostics for 47 of the 72
uncollected hosts:

| Final-attempt diagnostic | Hosts | Affected hosts |
| --- | ---: | --- |
| DNS resolution failed | 30 | `t-linux64-ms-241` through `t-linux64-ms-270` |
| SSH authentication denied | 14 | `ms-038`–`ms-044`, `ms-114`, `ms-161`, `ms-180`, `ms-271`, and `ms-278`–`ms-280` |
| SSH connection refused | 3 | `ms-066`, `ms-153`, `ms-166` |

The other 25 uncollected hosts had no stderr file from the final `pssh`
attempt. Their precise failure cause needs separate reachability or SSH
diagnosis.

## Follow-up

Resolve the DNS, SSH authentication, and connection-refusal cases, then rerun
the probe against only `uncollected-hosts.txt`. Keep the current 208-host
result as the baseline for detecting any configuration differences in the
remaining hosts.

The detailed two-host full powercap-tree comparison is recorded separately in
[RAPL comparison](RAPL_COMPARISON_MS_221_MS_223.md).
