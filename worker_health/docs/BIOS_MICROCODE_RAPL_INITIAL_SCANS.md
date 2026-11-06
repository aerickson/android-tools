# BIOS, microcode, and RAPL: initial fleet scans

Collected 2026-07-24 with `bios_and_cpu_speed_probe.sh` from the initial
parallel SSH scan and its retry scan:

- `pssh_bios_cpu_speed_probe_20260724-124901/output`
- `pssh_bios_cpu_speed_probe_20260724-130845/attempt-*/output`

## Coverage

The configured Linux host list contains 280 hosts. The first scan returned
results for 198 hosts; the retry scan added four distinct hosts, for 202
unique results (72.1%). The remaining 78 hosts did not return probe output in
these two scans, so the findings below are not fleet-wide conclusions.

All 202 responding hosts reported the same CPU model:

```text
Intel(R) Xeon(R) CPU E3-1585L v5 @ 3.00GHz
```

## BIOS and microcode result

Every responding host reports BIOS version `H07`. The fleet is split into two
BIOS-date and microcode groups:

| BIOS version | BIOS date | Microcode | Hosts |
| --- | --- | --- | ---: |
| `H07` | `02/26/2019` | `0xc6` | 196 |
| `H07` | `03/10/2020` | `0xda` | 6 |

The six hosts with the later BIOS date and microcode are:

```text
t-linux64-ms-272
t-linux64-ms-273
t-linux64-ms-274
t-linux64-ms-275
t-linux64-ms-276
t-linux64-ms-277
```

This is a clear firmware/microcode split despite the shared BIOS version
string. The first group should not be assumed to be out of policy without a
defined target BIOS date and microcode revision.

## RAPL result and limitation

All 202 responding hosts reported `RAPL: present`. In this probe, that means
only that `/sys/class/powercap` exists.

It does **not** establish that RAPL domains or constraints are enabled. The
probe did not follow the sysfs class symlinks into `intel-rapl:*` zones, so its
`find` command did not read the constraint files. A separate detailed RAPL
query of `t-linux64-ms-221` and `t-linux64-ms-223` found identical configured
limits; see [RAPL comparison](RAPL_COMPARISON_MS_221_MS_223.md). That result is
useful for those two hosts only, not proof of fleet-wide RAPL enablement.

## Follow-up

Run the retrying probe against the 78 hosts without output, then use the
detailed RAPL probe to collect zone `enabled` state and power constraints
across the full fleet before making an enablement claim.
