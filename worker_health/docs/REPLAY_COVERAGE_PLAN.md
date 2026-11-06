# Replay coverage controls and reports

Implemented behavior and design decisions for `replay_tc_pool.py`.

## Completion indicator

- Show a bold green `★ Full coverage: N/N eligible types` in the dashboard and
  regular table output, with a log message when coverage becomes complete.
- Full coverage means at least one observed successful controller replay for
  every currently discovered eligible workload type. Queued or running tasks
  do not count. An empty eligible set (`0/0`) is not full coverage.
- Display types excluded for missing scopes separately, including the count,
  so completion does not imply that excluded types passed.
- Coverage persists across restarts. It does not establish coverage of a new
  worker image or of every individual worker. State this scope in the report.
- Discovery is based on bounded recent source history; completion describes
  that discovered workload set. Report discovery time and any partial or
  unavailable history, and do not automatically stop or exit on uncertain
  discovery.

## Stop and exit on full coverage

- These are separate settings. Pause when covered is on by default in both
  interactive and unattended runs; exit when covered is off by default.
- `--stop-on-full-coverage` pauses new submissions when coverage becomes full,
  while keeping the process/dashboard open, monitoring outstanding tasks,
  refreshing discovery, and updating the report. Covered types are not rerun
  while waiting for uncovered types to succeed. Newly discovered uncovered
  name/scope variants automatically resume scheduling; after they succeed,
  submissions pause again. New source tasks for already-covered types do not
  trigger new replays.
- `--exit-on-full-coverage` writes the report and exits when coverage becomes
  full. This works independently of the stop setting. If both are enabled,
  exiting takes precedence.
- Provide `--no-stop-on-full-coverage` and `--no-exit-on-full-coverage` to
  explicitly disable the respective settings. Disabling pause allows continuous
  rotation through covered types.
- Add an `F` dashboard key to toggle stop-on-full-coverage and an `E` key to
  toggle exit-on-full-coverage. Display both settings and whether submissions
  are paused; include both keys in the help footer. Toggles wake the scheduler.
  Neither key immediately quits: enabling exit takes effect on the next
  confirmed coverage check.
- Check completion after reconciling task outcomes and refreshing discovery
  and eligibility, before submitting another task. Existing persisted successes
  can therefore cause a restarted controller to stop or exit without submitting
  work if the corresponding setting is enabled.
- On automatic exit, write the report, restore the terminal, print the
  completion summary and report path, and exit successfully. Already submitted
  tasks keep running and are listed in the report.
- With both settings disabled, continue scheduling and update the completion
  indicator as the discovered workload set changes.

## Reports

- Always write a JSON report. Default to a unique file under `/tmp`, such as
  `/tmp/replay-tc-pool-<destination>-<timestamp>-<unique>.json`; sanitize the
  destination pool name for use in a filename.
- `--report PATH` selects an explicit report location instead.
- Show the actual path at startup, in the dashboard, and on exit.
- Update the same file atomically after each completed cycle and on clean exit,
  including manual stop. Mark incomplete reports as incomplete.
- Include source/destination pools, run start and report timestamps, discovery
  freshness/completeness, coverage definition and totals, excluded types with
  reasons, both stop/exit-on-full-coverage settings, whether submissions are
  paused, and exit reason when available.
- For each workload, include its identity/name, eligibility, attempts,
  successes, failures, latest outcome, and supporting task IDs/links. List
  outstanding tasks separately. Preserve the distinction between historical
  successes and tasks submitted during this invocation.
- Include only report data; omit credentials and full replay task definitions.
- Report write failures must be visible. A requested report that cannot be
  written must not result in a silent successful exit.

## Follow-up consideration

A future coverage-reset or coverage-since option would support validation of a
fresh worker image without relying on older persisted successes.
