# test_hg_clone.py — testing notes

Reproducing the gecko CI `hg robustcheckout` to investigate intermittent
clone timeouts. See `test_hg_clone.py` for the script itself.

## Reference failure

- Job: https://treeherder.mozilla.org/logviewer?job_id=568912321&repo=mozilla-central
- Raw log: https://firefoxci.taskcluster-artifacts.net/Bsz6M9OuShezsw2fJn58uA/0/public/logs/live_backing.log
- Symptom: `[taskcluster:error] Task aborted - max run time exceeded` after
  ~40min, still streaming the bundle (~6.44 GB transferred at abort).
- Command: `hg robustcheckout --sharebase ... --purge --upstream
  https://hg.mozilla.org/mozilla-unified --sparseprofile
  build/sparse-profiles/perftest --revision c9b7663d5238... \
  https://hg.mozilla.org/mozilla-central <dest>`

## How to run

```bash
pipenv run python ./create_tc_task.py \
    -q proj-autophone/gecko-t-bitbar-gw-perf-a55 \
    -s ./test_hg_clone.py \
    -t 5400
```

Env overrides: `REVISION`, `SPARSE_PROFILE`, `UPSTREAM`, `REPO`, `PROGRESS_INTERVAL`.

## Runs

| Date (UTC)  | Task                                                                              | Pool                                  | Worker     | Result | Wall time | Notes |
|-------------|-----------------------------------------------------------------------------------|---------------------------------------|------------|--------|-----------|-------|
| 2026-05-28  | [hg58meOqTsue2I5EjUaaug](https://firefox-ci-tc.services.mozilla.com/tasks/hg58meOqTsue2I5EjUaaug) | proj-autophone/gecko-t-bitbar-gw-perf-a55 | bitbar.a55-57 (bitbar-ubuntu-???) | pass   | ~3 min    | Clean run, no repro. |
| 2026-05-28  | [ECmz_BUPRXmAWJsYRcA5LQ](https://firefox-ci-tc.services.mozilla.com/tasks/ECmz_BUPRXmAWJsYRcA5LQ) | proj-autophone/gecko-t-bitbar-gw-perf-a55 | **bitbar.a55-58** (bitbar-ubuntu-121) | **HANG** | timed out | Reproduced. Streamed 0 → 1.82 GB in ~10 s, then **stuck at 1.82 GB / 6.92 GB for 14+ min** with no further hg output. Real upstream resolved to `hg-edge.mozilla.org`. Worker was otherwise alive (disk free *increased* during the stall). |

### Identifying the worker

`TASKCLUSTER_WORKER_ID` / `TASKCLUSTER_WORKER_GROUP` env vars are **not set** on this generic-worker (v36.0.0) deployment, so the script's "TASKCLUSTER_WORKER_ID=?" line is uninformative. The reliable way to fingerprint the worker is the live log redirect URL the queue emits at task start, e.g.:

```
Uploading redirect artifact public/logs/live.log to URL
  https://firefoxci-websocktunnel.services.mozilla.com/bitbar.a55-58.60099/log/...
```

The middle segment (`bitbar.a55-58.60099`) is `workerGroup.workerId.pid`.

## Suspected bad workers

From [bug 2038441](https://treeherder.mozilla.org/intermittent-failures/bugdetails?startday=2026-05-21&endday=2026-05-28&tree=all&bug=2038441) (`High frequency [tier 2] Android perftest [taskcluster:error] Aborting task...`), the a55-* machines appearing repeatedly across 2026-05-21..28 include:

- **a55-58** — 3+ hits (05-28 06:02, 05-27 19:34, 05-27 15:46)
- a55-33, a55-34, a55-35, a55-52 — multiple hits each

a55-57 passed cleanly, so the failure is not pool-wide. Worth pinning the test to a55-58 next.

## Pinning a test to a specific worker

Taskcluster `createTask` cannot target a specific worker — workers claim from the pool. Options:

1. **Quarantine the other workers** in the pool before submitting (requires `queue:quarantine-worker:*` scope, which `create_tc_task.py`'s token already has), submit one task, then lift the quarantine.
2. **Flood + identify**: submit N tasks and look at which worker picked each up. The script now logs `TASKCLUSTER_WORKER_ID`, `TASKCLUSTER_WORKER_GROUP`, and `hostname` near the top of its output so you can correlate after the fact.

## Observations

- 3 min on a55-57 vs 40 min timeout on the failing job — suggests the original
  failure is intermittent (hg.m.o load, network blip, or a specific worker's
  uplink) rather than a structural issue with the clone command itself.

## Next experiments to try

- Loop on the same pool to catch intermittents: `-c 20` or `--continuous-mode`.
- Target other pools / specific workers seen failing.
- Drop `--sparseprofile` to force a full checkout (cold-cache worst case).
- Add `--time --profile` to hg config to capture where time is spent on a slow run.
