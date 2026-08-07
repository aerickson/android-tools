#!/bin/bash
#
# android_show_version_and_root_status.sh
# Version: 1.0
# Date: 2026-05-12

set -e
set -x

# Your code here
set -e
set -x

ADB="${ADB:-adb}"

echo "Waiting for device..."
"$ADB" wait-for-device

echo "Android OS version:"
"$ADB" shell getprop ro.build.version.release

echo "SDK:"
"$ADB" shell getprop ro.build.version.sdk

echo "Build fingerprint:"
"$ADB" shell getprop ro.build.fingerprint

echo
echo "Checking root and su approval..."
if "$ADB" shell 'command -v su >/dev/null 2>&1'; then
  if "$ADB" shell "su -c 'setenforce 0'" >/dev/null 2>&1; then
    echo "PASS: device is rooted and su permissions are approved"
    echo "SELinux mode:"
    "$ADB" shell getenforce
  else
    echo "FAIL: su exists, but root permission was denied or setenforce failed"
    exit 1
  fi
else
  echo "FAIL: su binary not found, device does not appear rooted"
  exit 1
fi
