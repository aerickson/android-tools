#!/usr/bin/env bash

# Read-only snapshot for comparing Linux hosts with different observed
# performance. It deliberately does not run a CPU benchmark or alter tuning.

set -o pipefail
set -u

section() {
    printf '\n### %s\n' "$1"
}

show_file() {
    local path="$1"

    if [[ -r "$path" ]]; then
        cat "$path"
    else
        printf '<unavailable>'
    fi
}

section 'identity'
printf 'hostname: '
hostname
printf 'timestamp_utc: '
date -u +%Y-%m-%dT%H:%M:%SZ
uname -a
printf 'uptime: '
uptime
printf 'kernel_cmdline: '
show_file /proc/cmdline
printf '\n'

section 'cpu_topology_and_microcode'
lscpu
printf '\nmicrocode_revisions:\n'
awk '/^microcode/ {print $3}' /proc/cpuinfo | sort | uniq -c
printf '\ncpu_mhz_summary:\n'
awk '/^cpu MHz/ {sum += $4; if (min == "" || $4 < min) min = $4; if ($4 > max) max = $4; count++} END {if (count) printf "cpus=%d min=%.3f avg=%.3f max=%.3f MHz\n", count, min, sum / count, max}' /proc/cpuinfo

section 'cpu_frequency_policy'
for property in scaling_driver scaling_governor scaling_min_freq scaling_max_freq cpuinfo_min_freq cpuinfo_max_freq; do
    printf '%s:\n' "$property"
    for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*; do
        value_file="$cpu_dir/cpufreq/$property"
        [[ -r "$value_file" ]] && cat "$value_file"
    done | sort | uniq -c
done

printf 'intel_pstate:\n'
for property in status no_turbo min_perf_pct max_perf_pct turbo_pct; do
    value_file="/sys/devices/system/cpu/intel_pstate/$property"
    if [[ -e "$value_file" ]]; then
        printf '%s: ' "$property"
        show_file "$value_file"
        printf '\n'
    fi
done

printf 'smt_active: '
show_file /sys/devices/system/cpu/smt/active
printf '\n'
printf 'intel_idle_max_cstate: '
show_file /sys/module/intel_idle/parameters/max_cstate
printf '\n'

section 'thermal_and_throttling'
for thermal_zone in /sys/class/thermal/thermal_zone*; do
    [[ -d "$thermal_zone" ]] || continue
    printf '%s type=' "$(basename "$thermal_zone")"
    show_file "$thermal_zone/type"
    printf ' temp_millicelsius='
    show_file "$thermal_zone/temp"
    printf '\n'
done

printf 'thermal_throttle_counters:\n'
for throttle_file in /sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/*; do
    [[ -r "$throttle_file" ]] || continue
    printf '%s: ' "${throttle_file#/sys/devices/system/cpu/}"
    cat "$throttle_file"
done

section 'current_pressure_and_memory'
printf 'cpu_pressure:\n'
show_file /proc/pressure/cpu
printf '\nmemory_pressure:\n'
show_file /proc/pressure/memory
printf '\nio_pressure:\n'
show_file /proc/pressure/io
printf '\n\n'
free -h
printf '\nvmstat (three one-second samples):\n'
vmstat 1 3

section 'storage'
df -hT /
findmnt -no SOURCE,FSTYPE,OPTIONS /
if command -v lsblk >/dev/null 2>&1; then
    lsblk -d -o NAME,MODEL,SIZE,ROTA,TYPE
fi
if command -v iostat >/dev/null 2>&1; then
    printf '\niostat (three one-second samples):\n'
    iostat -xz 1 3
fi

section 'turbostat'
if command -v turbostat >/dev/null 2>&1; then
    sudo -n turbostat --quiet --interval 1 --num_iterations 5 2>&1 || true
else
    echo 'turbostat: unavailable'
fi
