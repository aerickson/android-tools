#!/bin/bash

echo "Hostname: $(hostname)"
echo "BIOS: $(sudo dmidecode -s bios-version)"
echo "BIOS Date: $(sudo dmidecode -s bios-release-date)"
echo "CPU: $(lscpu | awk -F: '/Model name/{print $2}')"
echo "Microcode: $(grep microcode /proc/cpuinfo | head -1 | awk '{print $3}')"

if [ -d /sys/class/powercap ]; then
    echo "RAPL: present"
    find /sys/class/powercap -name constraint_0_power_limit_uw \
        -exec sh -c 'printf "%s: " "$1"; cat "$1"' _ {} \;
else
    echo "RAPL: absent"
fi
