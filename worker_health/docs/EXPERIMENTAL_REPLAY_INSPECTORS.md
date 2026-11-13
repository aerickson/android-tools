# Experimental replay inspectors

Status: proposed experiment; no inspector support is implemented yet.

Tracking epic: `andt-l7b` (experimental replay inspectors).

## Purpose

Investigate a particular test issue by replaying an instrumented Try task on
compatible baseline and canary pools, collecting evidence from successful and
failed runs, and presenting findings alongside the original evidence.

Start with `gauntlet-images` as one explicitly selected Python inspector.
Replay owns execution, provenance, and artifact transport. The workload/browser
harness produces diagnostic evidence. Inspectors select and interpret evidence.

## Investigator workflow

1. Identify the bug and the evidence needed to assess it.
2. Instrument the workload/browser and implement an inspector for that evidence.
3. Submit a Try task containing those instrumented inputs. Verify that diagnostics
   can be enabled and produce a manifest and bodies, including after test failure.
4. Select that source task and explicitly enable the inspector when replaying it.
5. Replay the same instrumented inputs and settings on desired compatible pools.
6. Inspect successful and failed runs; retain originals and link findings in the
   report. Record intentional differences between baseline and canary.
7. Reinspect existing tasks when necessary without submitting more work.

Proposed CLI shape (not available yet):

```bash
pipenv run python replay_tc_task.py SOURCE_TASK \
  --queue PROVISIONER/WORKER_TYPE --inspector gauntlet-images --wait
```

Both replay CLIs should share the same inspector implementation. The exact
existing-task inspection command will be settled during implementation. With
`--no-wait`, print how to inspect the submitted task later; do not imply that
collection occurred in the background.

## Minimal inspector contract

Use a local registry of named Python implementations. No external plugin
packaging or automatic inspector discovery is required.

Each inspector has a stable name/version, serializable settings, and two hooks:

- `prepare(context) -> preparation`: request workload-supported environment or
  argument changes and diagnostic upload declarations. Replay validates and
  applies requests to a copied task definition, records the modifications, and
  includes them in dry-run output. The source definition remains unchanged.
- `inspect(context, artifacts, output_dir) -> result`: select artifacts through
  the shared artifact API, validate evidence, and return structured findings,
  collection completeness, and evidence references. Derived output goes in the
  inspector's managed directory.

Context includes source/replay task IDs, the actual run ID and outcome, available
task definitions and input revisions, worker identity, and recorded preparation
settings. Missing provenance is represented as unknown rather than inferred.
For existing tasks without replay linkage, retain the available task/run identity
and explicitly mark unavailable source or preparation metadata.

A preparation request must not silently switch historical builds or test bundles.
Instrumented inputs must be selected explicitly. A flag cannot upgrade an old
Firefox binary. Unsupported preparation should fail clearly before submission.

## Artifact ownership and storage

Replay provides a shared API to list artifacts and request downloads by name or
prefix. It owns authentication, pagination, bounded retries/timeouts, safe local
paths, byte storage, and hashes. Inspectors do not each implement Taskcluster
transport. Downloads must not be able to escape the managed directory.

Store originals once per task/run in a shared immutable area; each inspector has
its own version/settings-specific directory for results and derived files. For
example, under a chosen collection root:

```text
tasks/<task-id>/runs/<run-id>/
  provenance.json
  originals/<safe-artifact-path>
  inspectors/<name>/<version-and-settings-key>/
    result.json
    derived/
```

The exact layout is internal. Evidence references should resolve without copying
original bodies into each inspector's output. Reinspection reuses verified
originals and writes results atomically; it must not corrupt an earlier result.

Preserve harness manifests and bodies unchanged. Integrity verification is
generic; image-specific manifest interpretation belongs in `gauntlet-images`.
Recording a download hash alone is not verification against an expected hash:
when the manifest supplies hashes and lengths, compare them to downloaded bytes.

Upload declarations must match the worker implementation and collect files after
test failures. Reuse an existing artifact directory when possible. The shared
clone code already preserves declarations and sets seven-day expiration; local
collection retains downloaded evidence beyond remote artifact availability.
Neither path guarantees evidence after worker loss before upload.

## Triggering and reporting

For the experiment, run selected inspectors for all observed terminal runs,
including completed, failed, and exception outcomes. Preserve actual run IDs;
never resolve evidence through a moving "latest run" alias. An exception run
without artifacts is still an inspectable result.

The pool controller's terminal-state reconciliation is the enqueue/dispatch
integration point; the single-task wait path uses the same runner. Keep collection
bounded and outside the scheduling decision path. A durable background queue and
automatic restart recovery are deferred: explicit reinspection is the recovery
mechanism for the first version. Allow in-flight inspection to finish or record
interruption when exiting on coverage; never report unattempted work as collected.

Keep three independent facts in reports:

- Task outcome: completed, failed, or exception.
- Inspector execution: succeeded, errored, or interrupted.
- Evidence collection: collected, partial, missing, or explicitly unsupported.

Collected requires a valid manifest and all required referenced bodies available
and verified. Partial means some required evidence is missing or invalid; retain
the available evidence and explain gaps. Missing means the required diagnostic
manifest/evidence was not found. Unsupported requires an explicit harness signal
or a known capability declaration, never merely an absent file. Inspector errors
must not change task outcome or count as evidence of a test regression.

Link findings and manifests from reports. Record inspector version/settings, task
modifications, source/replay IDs, run ID, actual worker identity, and known input
revisions. Clearly label modified replays and unavailable fields.

## First consumer: gauntlet image diagnostics

Define the versioned harness schema and successful/failing example fixtures
before implementing its inspector. The harness must:

- Explicitly signal diagnostic support and schema version.
- Capture original responses with requested/final URLs, status, content type,
  content encoding, body length, and SHA-256. Preserve empty, HTML, and other
  unexpected bodies; redact credentials from URLs.
- Label the capture stage: encoded HTTP body, decoded response body, or decoder
  input. Record transformations; claim decoder-input bytes only when captured
  there. A later independent fetch is not evidence of the original response.
- Assign request/response and operation IDs with explicit links. Content hashes
  identify bodies, not individual attempts. Deduplicate stored bodies by hash.
- Identify `HTMLImageElement.decode()` versus `createImageBitmap()`, outcome,
  exception details, and available browser/decoder versions.
- Capture corresponding successful operations as well as failures, and write a
  manifest linking operations, responses, and stored bodies.

Replay cannot provide this capture through ronin_puppet changes alone. The
gauntlet workload/Firefox harness needs instrumentation. The inspector can compare
response hashes/bytes and decode outcomes, but captured bytes may not reproduce
failures that depend on timing or browser state.

## Comparison and experimental success criteria

Use matching instrumentation and diagnostic settings for baseline and canary.
Record intentional differences in browser builds, workload revisions, and host
configuration. Do not attribute a difference solely to a pool when other inputs
also changed. Host/browser metadata may itself require harness-produced evidence.

The first milestone is complete when an instrumented Try source can be replayed
on two compatible pools, collecting verified evidence from successful and failed
runs, and producing useful findings with task/run-specific evidence links.
Reinspection must work without resubmission. Missing, partial, unsupported, and
inspector-error cases must be distinguishable without changing task outcomes.

Validate with harness schema fixtures, meaningful tests of task preparation and
artifact verification, and an end-to-end experiment. All project commands use
`pipenv run`.

## Implementation sequence and deferred work

1. Define gauntlet enablement, supported inputs, output path, schema, and fixtures;
   implement harness capture in its owning repository.
2. Add the Python registry/two-hook contract and recorded task preparation to both
   replay paths.
3. Add shared artifact storage/downloads, explicit existing-task inspection, and
   structured reporting; implement `gauntlet-images` against the fixtures.
4. Exercise baseline/canary runs and document the concrete investigator commands.

Defer automatic capability discovery, external plugin packaging, arbitrary plugin
commands, a durable background collection queue, automatic inspection recovery,
and a generalized comparison engine. Revisit abstractions after a second concrete
inspector exposes what needs to be shared.
