# RAPL comparison: `t-linux64-ms-221` and `t-linux64-ms-223`

Collected 2026-07-24 via a read-only SSH query of every `intel-rapl*` zone
under `/sys/class/powercap`.

## Result

The two hosts have identical RAPL configuration. Every zone name, enabled
state, constraint name, power-limit value, and time-window value matched.

`t-linux64-ms-221` is the faster comparison host and
`t-linux64-ms-223` is the slower one. Because their complete observed
powercap trees, enabled states, power limits, and time windows match, a
different configured RAPL power cap is not a credible explanation for that
performance difference. This does not rule out dynamic behavior such as
thermal throttling, actual package power draw, or a configuration change after
the sample was collected.

| RAPL zone | State | Constraint | Power limit | Time window |
| --- | --- | --- | ---: | ---: |
| `package-0` | enabled | `long_term` | 45,000,000 µW (45 W) | 27,983,872 µs |
| `package-0` | enabled | `short_term` | 56,250,000 µW (56.25 W) | 2,440 µs |
| `core` | disabled | `long_term` | 0 µW | 976 µs |
| `uncore` | disabled | `long_term` | 0 µW | 976 µs |
| `dram` | disabled | `long_term` | 0 µW | 976 µs |

Both hosts also have the parent `/sys/class/powercap/intel-rapl` zone enabled.

## Full powercap-tree comparison

For completeness, a second read-only comparison collected every readable file
under `/sys/class/powercap`, following sysfs symlinks and excluding the dynamic
`energy_uj` counters:

```bash
sudo find -L /sys/class/powercap -type f ! -name energy_uj -print0 |
while IFS= read -r -d '' file; do
  rel=${file#/sys/class/powercap/}
  printf '%-80s %s\n' "$rel" "$(cat "$file" 2>/dev/null)"
done | sort
```

The two normalized outputs were identical: 415 lines on each host and an
empty `diff`. This additionally covers readable non-constraint powercap and
runtime attributes, not only the Intel RAPL zone configuration above. The
saved comparison output is in
`pssh_powercap_full_compare_20260724-140839/`.

## Collection note

The earlier fleet probe reported only `RAPL: present` because its command used
`find /sys/class/powercap ...` without following the sysfs class symlinks. The
actual constraint files are located beneath the linked `intel-rapl:*` zone
directories and were therefore not reached by that scan.
