# Replay coverage controls and reports

Implemented behavior and design decisions for `replay_tc_pool.py`.

## Session lifecycle

- Sessions live under `$XDG_STATE_HOME/worker-health/replay-sessions`, defaulting
  to `~/.local/state/worker-health/replay-sessions`. Directory names include the
  UTC start timestamp, source pool, destination pool, and a unique suffix.
- Each contains `session.json`, private SQLite state, and `report.json`.
- Opening an existing session defaults to viewing: no network, credential
  access, database changes, report updates, or advancing elapsed timers.
- `--resume` explicitly enables monitoring, discovery, reconciliation, and
  report updates. Submissions remain disabled unless `--submit-on-start` is
  given or S is pressed in the active dashboard.
- `--new-session` with both pools starts fresh coverage without changing old
  sessions. Supplying both pools alone views the latest matching session, or
  creates a monitoring session if none exists. No legacy coverage is imported.
- Bare launch opens a picker: Enter views, R resumes, N starts a new session
  with editable pool defaults. The picker performs no remote work.
- Sort by last successful sample, newest first, with unsampled sessions last.
  Highlight the newest session and separately warn about sessions started 7+
  days ago and those not sampled for 7+ days. Warnings do not add confirmation
  prompts. Viewing old sessions preserves their snapshots.
- Print the exact resume command on exit. Resume preserves the original
  session timestamp; it never implicitly enables submissions.
- Keep a shared destination lock across sessions, including compatibility with
  the previous per-destination controller lock.

## Completion indicator

- Show a bold green `★ Full coverage: N/N eligible types` in the dashboard and
  regular table output, with a log message when coverage becomes complete.
- Full coverage means at least one observed successful controller replay for
  every eligible workload type observed during this session. Queued or running tasks
  do not count. An empty eligible set (`0/0`) is not full coverage.
- Display types excluded for missing scopes separately, including the count,
  so completion does not imply that excluded types passed.
- Coverage and all observed types persist across restarts of the same session.
  Types absent from current source history show `history only` and retain their
  place in coverage totals. Only currently discovered types can be scheduled;
  untested disappearing types cannot make coverage complete. There is no rolling
  expiration within a session. Coverage does not establish coverage of a new
  worker image or of every individual worker. State this scope in the report.
- Discovery is based on bounded recent source history; completion describes
  the accumulated session workload set. Report discovery time and any partial or
  unavailable history, and do not automatically stop or exit on uncertain
  discovery.

## Stop and exit on full coverage

- These are separate settings. Pause when covered is on by default in both
  interactive and unattended runs; exit when covered is off by default.
- `--stop-on-full-coverage` pauses new submissions when coverage becomes full,
  while keeping the process/dashboard open, monitoring outstanding tasks,
  refreshing discovery, and updating the report. Covered types are not rerun
  while waiting for uncovered types to succeed. Newly discovered uncovered
  name/scope variants automatically resume scheduling if submissions are enabled; after they succeed,
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

- Active monitoring writes the session's canonical `report.json`.
- `--report PATH` writes an additional copy. Viewing can export the saved
  snapshot without modifying the canonical report.
- Show the actual path at startup, in the dashboard, and on exit.
- Update the same file atomically after each completed cycle and on clean exit,
  including manual stop. Mark incomplete reports as incomplete.
- Include source/destination pools, run start and report timestamps, discovery
  freshness/completeness, coverage definition and totals, excluded types with
  reasons, both stop/exit-on-full-coverage settings, whether submissions are enabled or
  paused, and exit reason when available.
- For each workload, include its identity/name, eligibility, attempts,
  successes, failures, latest outcome, and supporting task IDs/links. List
  outstanding tasks separately. Preserve the distinction between historical
  successes and tasks submitted during this invocation.
- Include only report data; omit credentials and full replay task definitions.
- Keep session start, invocation start, report write time, and last successful
  sample time separate. Viewing and saving do not advance sampling freshness.
- Report write failures must be visible. A requested report that cannot be
  written must not result in a silent successful exit.

Use `--new-session` to validate a fresh worker image without relying on older
session successes.
