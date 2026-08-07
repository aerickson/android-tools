#!/usr/bin/env bash

# Run the RAPL power-limit probe concurrently and retry hosts that are
# temporarily unavailable. Each attempt keeps separate output and error files
# so a failed SSH attempt never overwrites a successful result.

set -o pipefail
set -u

parallelism=20
ssh_timeout_seconds=15
retry_delay_seconds=30
retry_window_seconds=480

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
host_list="${1:-/Users/aerickson/git/fleetroll_mvp/configs/host-lists/linux/all.list}"
probe_script="$script_dir/../ct_scripts/rapl_power_limit_probe.sh"
run_dir="$script_dir/pssh_rapl_power_limit_probe_$(date +%Y%m%d-%H%M%S)"

if ! command -v pssh >/dev/null 2>&1; then
    echo "pssh is required but was not found in PATH." >&2
    exit 1
fi

if [[ ! -f "$host_list" ]]; then
    echo "Host list not found: $host_list" >&2
    exit 1
fi

if [[ ! -f "$probe_script" ]]; then
    echo "Probe script not found: $probe_script" >&2
    exit 1
fi

mkdir -p "$run_dir"
pending_hosts="$run_dir/pending-hosts.txt"
next_pending_hosts="$run_dir/next-pending-hosts.txt"
completed_hosts="$run_dir/completed-hosts.txt"
deadline=$(( $(date +%s) + retry_window_seconds ))

# parallel-ssh permits comments, but normalize the file for progress tracking.
sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$host_list" > "$pending_hosts"
: > "$completed_hosts"

attempt=1
while [[ -s "$pending_hosts" ]]; do
    now=$(date +%s)
    if (( now >= deadline )); then
        echo "Retry window expired."
        break
    fi

    attempt_dir="$run_dir/attempt-$attempt"
    mkdir -p "$attempt_dir/output" "$attempt_dir/errors"
    pending_count=$(wc -l < "$pending_hosts" | tr -d ' ')
    echo "Attempt $attempt: probing $pending_count host(s)."

    pssh \
        --hosts "$pending_hosts" \
        --par "$parallelism" \
        --timeout "$ssh_timeout_seconds" \
        --outdir "$attempt_dir/output" \
        --errdir "$attempt_dir/errors" \
        --extra-args '-o PasswordAuthentication=no -o BatchMode=yes -o StrictHostKeyChecking=accept-new' \
        --send-input \
        'bash -s' < "$probe_script"
    pssh_status=$?
    echo "pssh exit status: $pssh_status"

    : > "$next_pending_hosts"
    while IFS= read -r host; do
        output_file="$attempt_dir/output/$host"
        if [[ -s "$output_file" ]] && grep -q '^Hostname: ' "$output_file"; then
            printf '%s\n' "$host" >> "$completed_hosts"
        else
            printf '%s\n' "$host" >> "$next_pending_hosts"
        fi
    done < "$pending_hosts"
    mv "$next_pending_hosts" "$pending_hosts"

    remaining_count=$(wc -l < "$pending_hosts" | tr -d ' ')
    completed_count=$(wc -l < "$completed_hosts" | tr -d ' ')
    echo "Completed: $completed_count; remaining: $remaining_count."

    if [[ -s "$pending_hosts" ]]; then
        echo "Waiting $retry_delay_seconds seconds before retrying unavailable hosts."
        sleep "$retry_delay_seconds"
    fi
    attempt=$((attempt + 1))
done

if [[ -s "$pending_hosts" ]]; then
    cp "$pending_hosts" "$run_dir/uncollected-hosts.txt"
    echo "Some hosts were not collected. See $run_dir/uncollected-hosts.txt" >&2
    exit 1
fi

echo "Probe complete. Results are in $run_dir"
