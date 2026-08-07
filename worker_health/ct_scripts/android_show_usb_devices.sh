#!/usr/bin/env bash
#
# android_show_usb_devices.sh
# Enumerate USB devices and Android devices visible to the worker.

set -euo pipefail

ADB="${ADB:-adb}"

echo "Host USB devices:"
if command -v lsusb >/dev/null 2>&1; then
  lsusb
else
  echo "WARN: lsusb is not installed or not on PATH"
fi

echo
echo "ADB devices:"
if command -v "$ADB" >/dev/null 2>&1; then
  # Run this before any device-specific ADB command so all detected states are
  # visible, including unauthorized and offline devices.
  "$ADB" devices -l
else
  echo "WARN: adb is not installed or not on PATH"
fi
