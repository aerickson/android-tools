#!/usr/bin/env python3

import argparse
import difflib
import hashlib
import os
import subprocess
import sys

BOLD_CYAN = "\033[1;36m"
DIM = "\033[2m"
DIM_RED = "\033[2;31m"
DIM_GREEN = "\033[2;32m"
RESET = "\033[0m"


def color(text, code):
    if sys.stdout.isatty():
        return f"{code}{text}{RESET}"
    return text


def color_diff_line(line):
    if line.startswith("+"):
        return color(line, DIM_GREEN)
    if line.startswith("-"):
        return color(line, DIM_RED)
    return color(line, DIM)


HOSTS = [f"devicepool-{i}.relops.mozops.net" for i in range(3)]

HOME = os.path.expanduser("~")
DEVICEPOOL_REPO = f"{HOME}/git/mozilla-bitbar-devicepool"

# remote path -> local source file
ENV_FILE_MAP = {
    "/etc/bitbar/bitbar.env": f"{DEVICEPOOL_REPO}/bitbar_env.sh",
    "/etc/bitbar/bitbar-v3.env": f"{DEVICEPOOL_REPO}/bitbar_env-v3-server.sh",
    "/etc/bitbar/lambdatest.env": f"{DEVICEPOOL_REPO}/lt_env.sh",
}

ENV_FILES = list(ENV_FILE_MAP.keys())

ALL_FILES = [
    "/etc/telegraf/telegraf.d/devicepool.conf",
    "/etc/systemd/system/bitbar-last_started_alert.service",
    "/home/bitbar/.bitbar_slack_alert.toml",
    "/home/bitbar/.bitbar_influx_logger.toml",
] + ENV_FILES


def local_sha256(path):
    try:
        data = open(path, "rb").read()
        return hashlib.sha256(data).hexdigest()
    except FileNotFoundError:
        return None


def fetch_remote(host, path):
    result = subprocess.run(
        ["ssh", host, f"sudo cat {path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "No such file or directory" in result.stderr:
            return None
        raise RuntimeError(f"{host}: {result.stderr.strip()}")
    return result.stdout


def show_diff(local_path, local_lines, host, remote_content):
    if remote_content is None:
        print(f"  {host}: MISSING (cannot diff)")
        return
    remote_lines = remote_content.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            remote_lines,
            local_lines,
            fromfile=f"{host}:{local_path}",
            tofile=f"local/{os.path.basename(local_path)}",
        ),
    )
    if not diff:
        print(f"  {host}: identical to local")
    else:
        print(f"  {host}: differs from local")
        for line in diff:
            print(f"    {color_diff_line(line)}", end="")
        print()


def checksum_remote(host, path):
    result = subprocess.run(
        ["ssh", host, f"sudo openssl sha256 {path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "No such file or directory" in result.stderr:
            return None, "MISSING"
        return None, f"ERROR: {result.stderr.strip()}"
    # output format: SHA256(/path)= <hexhash>
    line = result.stdout.strip()
    try:
        remote_hash = line.split("= ", 1)[1]
    except IndexError:
        return None, line
    return remote_hash, line


def main():
    parser = argparse.ArgumentParser(description="Checksum config files on the devicepool fleet for comparison.")
    parser.add_argument(
        "-e",
        "--env-only",
        action="store_true",
        help="only check env files (bitbar.env, bitbar-v3.env, lambdatest.env)",
    )
    parser.add_argument(
        "-d",
        "--diff",
        action="store_true",
        help="show unified diff between local source and remote (env files only)",
    )
    args = parser.parse_args()

    files = ENV_FILES if args.env_only else ALL_FILES

    for path in files:
        print(color(f"{path}:", BOLD_CYAN))
        local_path = ENV_FILE_MAP.get(path)
        local_hash = None

        if local_path:
            local_hash = local_sha256(local_path)
            if local_hash:
                print(f"  local ({os.path.basename(local_path)}): {local_hash}")
            else:
                print(f"  local ({os.path.basename(local_path)}): MISSING")

        if args.diff and local_path and local_hash:
            local_lines = open(local_path).readlines()
            for host in HOSTS:
                remote_content = fetch_remote(host, path)
                show_diff(local_path, local_lines, host, remote_content)
        else:
            for host in HOSTS:
                remote_hash, output = checksum_remote(host, path)
                if local_hash and remote_hash:
                    match = "matches local" if remote_hash == local_hash else "DIFFERS from local"
                    print(f"  {host}: {output} [{match}]")
                else:
                    print(f"  {host}: {output}")
        print()


if __name__ == "__main__":
    sys.exit(main())
