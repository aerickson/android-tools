#!/usr/bin/env python3

import argparse
import subprocess
import sys

HOSTS = [f"devicepool-{i}.relops.mozops.net" for i in range(3)]

ENV_FILES = [
    "/etc/bitbar/bitbar.env",
    "/etc/bitbar/bitbar-v3.env",
    "/etc/bitbar/lambdatest.env",
]

ALL_FILES = [
    "/etc/telegraf/telegraf.d/devicepool.conf",
    "/etc/systemd/system/bitbar-last_started_alert.service",
    "/home/bitbar/.bitbar_slack_alert.toml",
    "/home/bitbar/.bitbar_influx_logger.toml",
] + ENV_FILES


def checksum_file(host, path):
    result = subprocess.run(
        ["ssh", host, f"sudo openssl sha256 {path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip()}"
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description="Checksum config files on the devicepool fleet for comparison.")
    parser.add_argument(
        "--env-only",
        action="store_true",
        help="only check env files (bitbar.env, bitbar-v3.env, lambdatest.env)",
    )
    args = parser.parse_args()

    files = ENV_FILES if args.env_only else ALL_FILES

    for path in files:
        print(f"{path}:")
        for host in HOSTS:
            print(f"  {checksum_file(host, path)}")
        print()


if __name__ == "__main__":
    sys.exit(main())
