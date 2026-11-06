# worker health tools

Tools to ensure Taskcluster workers are not idle and performing well (success rate).

## Pool Classifier moved

Pool Classifier now lives in its own repository:

```text
git@github.com:mozilla-platform-ops/pool-classifier.git
```

Use that repository for Pool Classifier development, deployment, issues, and
operator documentation. Do not deploy Pool Classifier from this repository.

## TODO

- move code into https://github.com/mozilla-platform-ops/relops-infra

## setup

### Python and related

For all tools, run in the pipenv.

```
# one-time setup
# install pipenv
pipenv install

# for every use
pipenv shell
# run command
```

### Taskcluster credentials

Many of the tools make calls against Taskcluster. We use a token stored in ~/.tc_token.

```bash
{
    "clientId": "mozilla-auth0/ad|Mozilla-LDAP|aerickson/quarantine-workers",
    "accessToken": "REDACTED"
}
```

You can create a client token at https://firefox-ci-tc.services.mozilla.com/auth/clients.

For worker quarantine operations, the required scope is:

```bash
queue:quarantine-worker:*
```

### Replay client scopes

`replay_tc_task.py` reads `clientId` and `accessToken` from `~/.tc_token` when
submitting jobs. Configure the client at the
[Taskcluster clients page](https://firefox-ci-tc.services.mozilla.com/auth/clients).
For level-1 Talos/Browsertime replays into the `tc-v115` pool, use:

```text
queue:create-task:lowest:releng-hardware/gecko-t-linux-talos-2404-tc-v115
queue:create-task:project:none
queue:scheduler-id:-
secrets:get:project/perftest/gecko/level-1/*
generic-worker:cache:gecko-level-1-*
```

The first three scopes allow submission at lowest priority, in project `none`,
with scheduler `-`. Replace the destination pool in the first scope when
targeting another pool. Existing `queue:create-task:*` and
`queue:scheduler-id:*` grants already cover those three requirements.

The secret wildcard covers level-1 perftest secrets, including `perftest-login`.
The cache wildcard covers level-1 pip and uv caches, including versioned and
hashed names, without updating the client for each new cache name. Both cache
and secret permissions are needed for jobs that request them; granting secret
access alone does not authorize cache use. These scopes covered all payload
requirements in a sample of 62 level-1 jobs from the source Talos pool.

The
[`cli-tools` client](https://firefox-ci-tc.services.mozilla.com/auth/clients/mozilla-auth0%2Fad%7CMozilla-LDAP%7Caerickson%2Fcli-tools)
also has these workload permissions:

```text
secrets:get:project/releng/gecko/build/level-1/*
queue:get-artifact:project/gecko/*
```

The first covers level-1 build-related secrets, including
`conditioned-profiles`, which lives outside the perftest secret namespace. The
second covers private Gecko artifact namespaces requested by some workloads,
including `project/gecko/talos-pdfs/*`. Artifact namespaces are not inherently
SCM-level-specific; this grant covers matching Gecko artifacts across levels.
Replay's level-1 source checks still apply. The client has no level-3 secret or
cache grants.

Together with its existing `queue:create-task:*`, `queue:scheduler-id:*`,
`generic-worker:cache:gecko-level-1-*`, and
`secrets:get:project/perftest/gecko/level-1/*`, these grants cover submission,
level-1 caches, both known level-1 secret namespaces, and private Gecko
artifacts. New workloads may still request permissions in other namespaces.

Keep this client limited to level 1. Do not grant level-3 secret or cache scopes,
or broader wildcards that include level 3. Select a completed level-1 source
task, such as one from try. The tool preserves source scopes and resource
references. Replay rejects sources with non-level-1 SCM markers, resource scopes,
or cache names, and sources without an explicit level-1 marker. Wildcard source
scopes that cover level-3 perftest secrets or Gecko caches are also rejected.
There is no level-3 override or automatic downgrade.

Keep secret wildcard prefixes inside each `level-1/` subtree to retain the
level-1 boundary. Use the missing-scope details in the picker to identify
additional permissions requested by a chosen level-1 workload.

A public `--dry-run` can run without a token; an existing token is used when
available. A successful preview checks task state and explicit artifact inputs,
but does not verify that the client can delegate every source scope or submit
the clone. Task creation enforces those permissions and reports missing scopes
as `InsufficientScopes` (HTTP 403). Compare the preview's `tasks[].task.scopes`
with the client's configured scopes when diagnosing a failure. Interactive
picker submissions additionally check the client's effective scopes before
asking for confirmation. The picker fetches current client scopes before
displaying jobs, dims entries with missing permissions, and prevents selecting
those entries for submission. Missing scopes appear in the selected row's
details. Dry-run mode allows inspecting their previews.

## overview

### create_tc_task.py

Submit test tasks to a Taskcluster worker queue. By default, each task sleeps
for 60 seconds and prints an increasing counter once per second in its live log.

For two tasks that each sleep for five minutes with the counter:

```bash
pipenv run python ./create_tc_task.py \
  -q releng-hardware/gecko-t-linux-talos-2404-tc-v115 \
  -c 2 --command-timeout 310
```

The default loop runs for `--command-timeout` minus a 10-second buffer, so
`310` gives 300 seconds of sleep. `--bash-command` or `--script-file` replaces
that loop; for example, `--bash-command 'sleep 300' --command-timeout 330`
sleeps silently for five minutes with a 30-second timeout buffer.

Add `--wait` to monitor task status in the CLI; the per-second counter appears
in the task's live log. Use `pipenv run python ./create_tc_task.py --help` for
all options.

With `--wait`, an interactive terminal shows a live table with task status and
worker names, followed by results and a summary. Task IDs link to Taskcluster
in terminals that support links. Redirected table output shows the final table
and summary.

The live table shows elapsed time as `HH:MM:SS`, refreshed while waiting even
when task status does not change. This measures time since monitoring began,
including time pending in the queue. The final table and log summary retain the
elapsed time; wait-mode JSON adds a top-level `elapsed_seconds` value.
In a terminal, the live table footer offers Q or Escape to stop watching without
cancelling tasks. Ctrl+C also exits monitoring. Single-key input is enabled only
for the interactive table, and terminal settings are restored when it closes.

Use `--wait --output log` for status updates in logs, or
`--wait --output json` for a JSON object containing a `tasks` list with each
task's ID, state, result, worker ID, and URL. JSON results go to stdout;
submission progress and diagnostics go to stderr. Wait mode exits nonzero if
any submission fails or any task finishes as failed or exception.

### replay_tc_pool.py

Continuously replay a mix of completed level-1 workloads into another pool:

```bash
pipenv run python ./replay_tc_pool.py \
  --from-pool releng-hardware/gecko-t-linux-talos-2404 \
  --queue releng-hardware/gecko-t-linux-talos-2404-tc-v115
```

With no arguments, the tool opens a local session picker. Sessions are sorted
by last successful sample, newest first; the newest is highlighted. Sessions
started 7+ days ago and those not sampled for 7+ days have separate warnings.
Enter views a saved snapshot, R resumes monitoring, and N creates a new session
(using the selected pools as editable defaults).

| Arguments | Behavior |
| --- | --- |
| `--from-pool SRC --queue DST` | View the latest matching session, or create a new monitoring session if none exists |
| `--session PATH` | View a saved session offline, with no scans, credentials, SQLite writes, or report updates |
| `--session PATH --resume` | Resume monitoring and update that session, with submissions paused |
| Pool arguments plus `--resume` | Resume the latest matching session; error if none exists |
| Pool arguments plus `--new-session` | Start fresh coverage, preserving all previous sessions |
| Add `--submit-on-start` to a new/resumed session | Enable submissions immediately |

Supplying `--submit-on-start` while viewing is an error; explicitly resume or
start a new session. Monitoring continues with submissions paused until S is
pressed in the dashboard or `--submit-on-start` is supplied. Active monitoring
reads `~/.tc_token`; viewing does not. Use `--resume --dry-run --once` with an
existing session to validate a scheduling cycle without submitting.

Sessions are stored under `$XDG_STATE_HOME/worker-health/replay-sessions`
(default: `~/.local/state/worker-health/replay-sessions`). Each directory is
named with its UTC start timestamp, sanitized source and destination pool names,
and a unique suffix, and contains `session.json`, private `state.sqlite3`, and
`report.json`. On exit, the tool prints the report path and exact resume command.
Resuming keeps the original session start time; a new invocation alone never
enables submissions. Old per-destination controller databases are left intact
and are not automatically imported into fresh sessions.

Defaults count non-quarantined destination workers active within 90 minutes and
allow one outstanding task per worker. Standalone workers count as one even
when their API `capacity` field is zero. Pool-wide pending and claimed task
counts include other submitters' work; locally tracked tasks also provide a
minimum outstanding count. Queue counts are approximate, so simultaneous
external submissions can briefly exceed the target. No workers, unavailable
capacity data, or failed permission checks prevent new submissions.

The controller checks every 60 seconds and submits at most one task per cycle,
with at least 60 seconds between submission attempts. Configure these defaults
with `--worker-active-minutes`, `--tasks-per-worker`, `--poll-interval`, and
`--submit-interval`. Capacity and scopes are checked again after input validation.

Source history is refreshed every 10 minutes (`--source-refresh-minutes`). As
with the interactive picker, discovery groups workload types by name and scopes,
uses their latest completed source tasks, and sees bounded recent worker
history. Every discovered type is retained for the lifetime of its session,
including across restarts. Types absent from the latest scan show `history only`
when idle; they retain their coverage and remain in the totals. Only types in
the current scan are candidates for new replays. An untested type disappearing
does not make coverage complete. Start a new session for a fresh workload set;
there is no rolling expiration of types within a session.

Types without a successful session replay are prioritized; ties
rotate by fewest attempts and oldest attempt, with equally ranked types shuffled
for variety in the initial sweep. A type has at most one controller
replay in flight. By default, covered types are not replayed again: once all
eligible types have succeeded, submissions pause while monitoring and discovery
continue. With submissions enabled, newly discovered uncovered name/scope variants resume scheduling;
new source tasks for an already-covered type do not. Use
`--no-stop-on-full-coverage` for continuous rotation, or
`--exit-on-full-coverage` to exit successfully at the first confirmed full
coverage point. Exit is off by default and works independently of pausing.
Types missing scopes are skipped and counted separately. Invalid inputs and failed tasks
receive an exponential retry delay starting at five minutes and capped at one
hour (`--failure-backoff` sets the initial delay).

The live table shows active workers, outstanding tasks, and per-type successes,
attempts, and readiness. A separate Coverage column shows green `passed`, red
`unsuccessful`, cyan `awaiting result`, or dim `untested`; Status describes
scheduling readiness independently, so a passed type can still be ready to run
again. In-flight rows show running or queued status and an Elapsed column:
running time uses the current run's actual start timestamp, while queued time
is measured from submission. Timers update between API polls, and restarting
recovers running start timestamps from Taskcluster.

Interactive table mode uses a fullscreen dashboard with a fixed summary,
scrollable task table, and a log pane below it. The table continuously prioritizes
in-flight types, types with failures, and covered types, preserving the selected
workload's cursor identity as statuses change. All workloads remain accessible through
scrolling. Scheduling and API calls run independently of keyboard navigation.
Both pane headers show the visible row range and total, with `↑ earlier` and
`↓ more` when content is offscreen. Log positions refer to the retained buffer.

- Tab switches between task and log panes; the focused pane is highlighted.
- In view mode, R resumes monitoring; load controls are disabled and elapsed
  times remain frozen at the saved sample.
- In active monitoring, S toggles submissions (default off on every launch).
- F toggles pause-when-covered (default on); E independently toggles
  exit-when-covered (default off). Changes wake the scheduler; they take effect
  at its next coverage check. The settings remain visible in the dashboard.
- Up/Down moves the cursor one row and scrolls as needed.
- Page Up/Left and Page Down/Right move one screen.
- Home/End jumps to the first/last row.
- Logs follow new messages until you scroll back. Scrolling down to the newest
  line resumes following; End jumps there immediately and resumes following.
  A new lines counter shows arrivals while paused. The log buffer retains 2,000 lines.
- Q or Ctrl+C exits and restores the terminal. Submitted tasks keep running;
  an already-issued API request may complete after exit.

Terminal resizing adjusts both panes. `--once` and non-interactive table output
print a regular table; `--output log` keeps plain logging for unattended runs.

A bold green `★ Full coverage: N/N eligible types` marks confirmed coverage;
`0/0` and incomplete discovery do not count as complete. Coverage means observed
successful session replays across restarts, not coverage of every worker or
a newly deployed image. The denominator includes every eligible type observed
during this session, even those absent from the current bounded source history.

Active sessions update their canonical `report.json`; `--report PATH` also
writes a copy at the requested location. In view mode it exports the saved
snapshot without changing the original. The path is shown at startup, in the
dashboard, and on exit. Reports update atomically after each active cycle and
on clean exit, including incomplete runs. `sampled_at` records the last successful
monitoring cycle; simply viewing, saving, or failing a cycle does not advance it.
Reports include coverage and
discovery details, settings, exclusions and missing scopes, per-workload
attempts/successes/failures, task links, outstanding work, and an exit reason.
Task entries distinguish existing tasks from submissions during this invocation.
Credentials and replay task definitions are omitted. Report write failures are
reported as errors. See the [coverage design notes](docs/REPLAY_COVERAGE_PLAN.md).

Each session's SQLite state preserves coverage, backoff, rate limits, workload
identities, source task IDs, and in-flight work across restarts. Coverage
counts this controller's tasks, not historical replays submitted by other tools.
Submissions are also recorded in the interactive picker's coverage database.
Private state files retain exact replay definitions so an uncertain submission
can be retried with the same task ID rather than creating a duplicate. A local
destination lock prevents controllers in different sessions from targeting the same pool;
run only one controller per destination, including across machines.

### replay_tc_task.py

Clone a completed real job under a fresh task ID. The destination defaults to
the source pool; `--queue` selects another compatible pool. Both task/run URLs
and worker pool URLs are accepted as well as IDs.

Preview a completed level-1 Talos job on the `tc-v115` pool (see
[Replay client scopes](#replay-client-scopes) for client permissions):

```bash
pipenv run python ./replay_tc_task.py RuTU-6ofSBqIdd6-CQqv4g \
  --queue releng-hardware/gecko-t-linux-talos-2404-tc-v115 --dry-run
```

`--dry-run` prints a yellow `DRY RUN` banner to stderr, checks the source and reused
inputs, then prints a JSON plan with `"dry_run": true`, `"tasks_submitted": 0`,
and the full proposed task definitions. The listed task IDs have not been
submitted. Preview JSON is dimmed in a terminal; redirected output stays plain
JSON. To submit two copies with live task status:

```bash
pipenv run python ./replay_tc_task.py RuTU-6ofSBqIdd6-CQqv4g \
  --queue releng-hardware/gecko-t-linux-talos-2404-tc-v115 --count 2 --wait
```

The source must have state `completed` and be a level-1 task. Its command, environment, mounts,
runtime limit, scopes, and dependency references are preserved. Dependencies
are checked for usable states; explicit artifact references in mounts, task
images, `MOZ_FETCHES`, and Firefox CI artifact URLs are checked for existence,
expiration, and error artifacts. This does not check external downloads,
artifacts discovered at runtime, or guarantee that inputs remain available
until execution.

Clones share a new diagnostic task group with scheduler `-` and project `none`.
Original routes, tags, and reporting metadata are removed, so these jobs do
not report into the source Treeherder push. They use lowest priority, zero
queue retries, a 24-hour deadline, and seven-day task/output artifact retention.
The operator becomes the task owner, and the source task is linked in the
description alongside the full test name and source pool, and recorded in
`extra.replay`.

Revision environment variables such as `GECKO_HEAD_REV` and
`MOZ_SOURCE_CHANGESET` remain intact so the workload uses the original source
revision. They do not attach the replay to a Treeherder push: the standard
[Treeherder ingestion handler](https://github.com/mozilla/treeherder/blob/master/treeherder/etl/taskcluster_pulse/handler.py)
requires a `tc-treeherder` route and `extra.treeherder`, both of which are absent
from the clone. Logs and artifacts can still mention the original revision.

Cross-pool replay preserves the payload and scopes without translating them;
the destination needs a compatible worker implementation, OS, tools, features,
and permissions. Submission uses `~/.tc_token` and requires the source payload's
scopes plus permission to create tasks on the destination with scheduler `-`
and project `none`. A public preview can run without a token. Selecting a task
from a particular worker does not target that same host.

To choose a workload interactively from a source pool:

```bash
pipenv run python ./replay_tc_task.py \
  --from-pool releng-hardware/gecko-t-linux-talos-2404 \
  --queue releng-hardware/gecko-t-linux-talos-2404-tc-v115
```

Omit `--queue` to replay into the source pool. `--from-pool` and an explicit
source task are mutually exclusive; picker mode requires an interactive
terminal. `--filter speedometer` pre-filters the list, and `--count 2` submits
two copies of each selected job. `--dry-run` opens the picker and prints the
whole batch's preview without submitting. Batch previews include a `sources`
list and each clone's source in `extra.replay.sourceTaskId`.

Picker submissions wait for results by default. Use `--no-wait` to return
immediately after submission, or press Q, Escape, or Ctrl+C during monitoring to exit while
the submitted jobs continue running. Explicit task-ID invocations still require
`--wait` to enable monitoring.

Replay monitoring shows an approximate ETA based on the source task's last
completed run. It counts down from each replay's actual run start, excluding
queue time. Pending tasks show the expected runtime once running, and runs
that exceed the estimate show how far over they are. No estimate is shown when
the source lacks completed-run timestamps. Wait-mode JSON includes
`expected_runtime_seconds` when available (a task-ID-to-seconds mapping for
multiple workloads); log updates include each task's estimate.
When waiting for multiple tasks, the table and logs also show their summed
estimated task time, counting each copy and flagging missing estimates. This
excludes queue time; parallel tasks can finish in less wall-clock time.

Discovery reads recent history from every worker in the pool (currently up to
20 tasks per worker), deduplicates task IDs, and keeps completed level-1 jobs.
Repeated jobs with the same name and scopes are grouped together; selection
uses the most recently completed task in each group. The picker shows runtime,
completion time, client access, secret requirements, and the number of observed tasks per
group. Type to filter, use arrows or Page Up/Down to move, and Tab to toggle
jobs. Checkboxes and a selected count show your choices, which persist across
filters. Enter proceeds with all checked jobs, or the highlighted job when none
are checked; Escape cancels. Ctrl+R preserves selections by task ID and reports
any selected tasks no longer present in refreshed history.
Task inputs and effective client scopes are checked for the whole batch before
submission. A validation failure identifies the affected job and submits nothing.
One confirmation shows each source, destination, copies per job, and the total
task count. Confirmation defaults to no. All clones share a new task group and
are monitored together; submission failures still report and monitor tasks
already created.
The selected job name, task URL, worker, and completion time are logged before
input and permission checks so failures can be traced back to the selection.

Discovery results are cached for 10 minutes under
`$XDG_CACHE_HOME/worker-health/replay-picker` (default:
`~/.cache/worker-health/replay-picker`). Cache files are private to the user and
contain task definitions and history, with no client credentials or effective
scope snapshots. Use `--cache-minutes N` to change the lifetime or
`--cache-minutes 0` to disable caching. Use `--refresh` when starting the command,
or Ctrl+R inside the picker, to fetch fresh worker history and client scopes.
The filter is preserved on Ctrl+R. Effective client scopes and selected task
inputs are checked live before submission, including when history is cached.

When `--queue` differs from `--from-pool`, the picker adds target coverage in
the style of `tcw`'s history badges. `★ N` counts distinct successful tasks
observed for that test name on the destination. Jobs with no successes show
`running`, `queued`, `failed`, `exception`, or `unseen`; unavailable or incomplete
history can show `unknown`. The header summarizes how many source job types
have succeeded on the target, and the details show target outcomes and the
latest observed target task ID and worker. Type `unseen` to find coverage gaps,
`passed` to show successful workloads, or combine these with a test-name filter.
Same-pool picker mode omits target coverage.
Successful target workloads are green, queued or running workloads are yellow,
and failures are red. Unseen or unknown jobs use the terminal's normal color.
Missing-permission rows remain dimmed; terminals without
color support retain the textual badges and labels.

Coverage matches the full test name, recognizing the tool's `Replay: ` prefix;
it covers job types across revisions and cache-scope variants, rather than
proving an identical source revision ran. Only level-1 jobs are counted. An
`unseen` badge means no matching run has been observed locally or in recent
worker history. It does not prove the job has never run, and a pool's existing
coverage does not establish coverage of a newly deployed worker image.

Observed outcomes persist in a private SQLite database at
`$XDG_STATE_HOME/worker-health/replay-coverage.sqlite3` (default:
`~/.local/state/worker-health/replay-coverage.sqlite3`), so known successes survive
the worker-history window. Copy submissions are recorded as queued and count
as successes only after completion is observed. Pending local submissions are
rechecked on the next target scan even if absent from worker history. The
database contains task IDs, names, pools, states, workers, and timestamps; it
contains no credentials or task payloads. Target scans use the same
`--cache-minutes` lifetime and refresh controls; disabling scan caching retains
the persistent observation history. New submissions and monitored results
invalidate the target scan cache.

The picker lists recent observed workloads, not the complete pool history.
Workers whose history is unavailable are reported and skipped. TODO: integrate
Pool Classifier's task-ID history to discover older jobs beyond this window.

`--wait --output log` and `--wait --output json` use the same monitoring formats
as `create_tc_task.py`. When monitoring is disabled, JSON output lists submitted
task IDs and URLs. Diagnostics go to stderr. A run URL selects the task definition;
artifact references without an explicit run ID still resolve to the latest
dependency run.

### fitness.py

Shows each worker's success rate and various concerning conditions like, consecutive failures, lack of work.

Provides a report on a provisioner and worker-type.

Not specific to Bitbar (works on all taskcluster provisioners).

![fitness.py](images/fitness_py_example.png)

```
./fitness_check.py -h

# to report on all worker types under a provisioner
./fitness_check.py -p PROVISIONER
# for a specific worker-type in the provisioner
./fitness_check.py -p PROVISIONER WORKER-TYPE
```

### missing_workers

For static hardware pools (like moonshots, macs, and bitbar), alert if a worker hasn't worked in more than a day (they disappear from TC output in 24 hours).

If a queue doesn't have work, we can't verify they're functioning (via the currently used method - TC doesn't show static worker status unless working (I think) in https://firefox-ci-tc.services.mozilla.com/docs/reference/platform/queue/api#listWorkers).

```bash
./missing_workers.py -h
```

### safe_runner

Runs a command on a set of hosts once each has been quarantined and no jobs are running.

Features:
- integrates with Taskcluster API to quarantine, check no jobs are running, and lift quarantine
- ability to resume from a state file
- OS X speech support for updates
- pre-quarantine feature (quarantines several hosts) so there's less waiting for jobs to finish
- command output is logged to file

Potential issues:
- If ssh is giving you issues with Bolt, append '--native-ssh' to your Bolt command.

```bash
# for options
./safe_runner.py -h

# basic usage
./safe_runner.py -r sr_state_dir_xyz
# edit config and set options
vi sr_state_dir_xyz/runner_state.toml
./safe_runner.py -r sr_state_dir_xyz

# resume from existing state file and set options
./safe_runner.py -r sr_state_dir_xyz -t -R -P 11
```

### unsafe_runner

Similar to safe_runner, but doesn't deal with any quarantine stuff.

### quarantine_tool

Lists quarantined hosts, quarantines, lifts quarantine, and lists all workers in a workerType.

```bash
# show quarantined
./quarantine_tool.py proj-autophone gecko-t-bitbar-gw-unit-p2 show

# show all workers
./quarantine_tool.py proj-autophone gecko-t-bitbar-gw-unit-p2 show-all

./quarantine_tool.py proj-autophone gecko-t-bitbar-gw-unit-p2 quarantine pixel2-01
./quarantine_tool.py proj-autophone gecko-t-bitbar-gw-unit-p2 lift pixel2-01
```

### slack_alert

Sends scheduled slack message about problem hosts.

```
./slack_alert.py -h
```

### influx_logger

Logs worker health metrics to influx.

```
./influx_logger.py -h
```
