#!/usr/bin/env bash

# Run Ookla's official Speedtest.net CLI and retain its JSON response as the
# Taskcluster public artifact at out/speedtest.json.  The CLI is downloaded to
# a disposable directory so this probe does not change the Bitbar worker.

set -euo pipefail

SPEEDTEST_VERSION="${SPEEDTEST_VERSION:-1.2.0}"
SPEEDTEST_ARCH="$(uname -m)"

case "$SPEEDTEST_ARCH" in
    x86_64|amd64) SPEEDTEST_ARCH="x86_64" ;;
    aarch64|arm64) SPEEDTEST_ARCH="aarch64" ;;
    *)
        printf 'Unsupported Speedtest CLI architecture: %s\n' "$SPEEDTEST_ARCH" >&2
        exit 2
        ;;
esac

mkdir -p out
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

ARCHIVE="$WORK_DIR/speedtest.tgz"
SPEEDTEST_URL="https://install.speedtest.net/app/cli/ookla-speedtest-${SPEEDTEST_VERSION}-linux-${SPEEDTEST_ARCH}.tgz"

printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee out/benchmark-metadata.txt
printf 'hostname=%s\n' "$(hostname)" | tee -a out/benchmark-metadata.txt
printf 'speedtest_url=%s\n' "$SPEEDTEST_URL" | tee -a out/benchmark-metadata.txt

curl --fail --location --retry 3 --connect-timeout 30 \
    --output "$ARCHIVE" "$SPEEDTEST_URL"
tar -xzf "$ARCHIVE" -C "$WORK_DIR"

"$WORK_DIR/speedtest" --version | tee -a out/benchmark-metadata.txt

# JSON includes the selected server, public IP, latency, jitter, packet loss,
# download, and upload measurements.  Keep stderr separately for diagnosis.
"$WORK_DIR/speedtest" --accept-license --accept-gdpr --format=json \
    >out/speedtest.json 2>out/speedtest.stderr
cat out/speedtest.json
