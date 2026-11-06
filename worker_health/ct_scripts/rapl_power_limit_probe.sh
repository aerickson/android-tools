#!/usr/bin/env bash

# Emit the active Intel RAPL topology and all exposed power-limit constraints.
# `/sys/class/powercap/intel-rapl*` entries are sysfs links, so use shell glob
# expansion rather than `find /sys/class/powercap` (which does not follow them).

echo "Hostname: $(hostname)"

found_rapl_zone=0
for zone in /sys/class/powercap/intel-rapl*; do
    [[ -d "$zone" ]] || continue
    found_rapl_zone=1

    echo "Zone: $(basename "$zone")"
    for value_file in \
        "$zone"/name \
        "$zone"/enabled \
        "$zone"/constraint_*_name \
        "$zone"/constraint_*_power_limit_uw \
        "$zone"/constraint_*_time_window_us; do
        [[ -e "$value_file" ]] || continue
        printf '%s: ' "$(basename "$value_file")"
        sudo -n cat "$value_file"
    done
done

if (( ! found_rapl_zone )); then
    echo "RAPL: absent"
fi
