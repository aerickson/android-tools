import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import replay_tc_pool as cli
import taskcluster
from worker_health.replay_picker import Discovery, ReplayJob

from worker_health import replay_controller as controller

SOURCE = "releng-hardware/source"
DESTINATION = "releng-hardware/destination"
NOW = datetime(2026, 11, 5, 18, tzinfo=UTC)


def rest_error(code):
    return taskcluster.exceptions.TaskclusterRestFailure("API error", None, status_code=code)


def worker(name="worker", **changes):
    return {
        "workerGroup": "g",
        "workerId": name,
        "capacity": 0,
        "state": "standalone",
        "lastDateActive": NOW.isoformat(),
        **changes,
    }


@pytest.fixture
def setup(mocker, tmp_path):
    source = json.loads(
        (Path(__file__).parent / "fixtures/replay_browsertime_task.json").read_text().replace("level-3", "level-1"),
    )
    source["taskQueueId"] = SOURCE
    source["payload"]["env"]["MOZ_SCM_LEVEL"] = "1"
    jobs = []
    for index, name in enumerate(("Talos", "Speedometer", "Damp")):
        definition = deepcopy(source)
        definition["metadata"]["name"] = name
        jobs.append(ReplayJob(str(index) * 22, definition, NOW.isoformat(), 60, "source-host"))
    queue, manager, auth = mocker.Mock(), mocker.Mock(), mocker.Mock()
    manager.listWorkers.return_value = {"workers": [worker()]}
    queue.taskQueueCounts.return_value = {"pendingTasks": 0, "claimedTasks": 0}
    queue.status.return_value = {"status": {"state": "pending"}}
    queue.task.side_effect = lambda task_id: next(job.task for job in jobs if job.task_id == task_id)
    auth.currentScopes.return_value = {"scopes": ["*"]}
    args = SimpleNamespace(
        from_pool=SOURCE,
        queue=DESTINATION,
        worker_active_minutes=90,
        tasks_per_worker=1,
        source_refresh_minutes=10,
        failure_backoff=300,
        submit_interval=60,
        dry_run=False,
        submissions_enabled=True,
    )
    mocker.patch.object(controller, "load_discovery", return_value=Discovery(jobs, 1, 3, 3, 0))
    mocker.patch.object(controller, "save_discovery")
    mocker.patch.object(controller, "check_inputs", return_value=19)
    mocker.patch.object(controller, "remember_submission")
    mocker.patch.object(controller, "remember_results")
    mocker.patch.object(controller, "gen_task_id", side_effect=["A" * 22, "B" * 22, "C" * 22, "D" * 22])
    with controller.ControllerState(tmp_path / "controller.sqlite3") as state:
        instance = controller.ReplayController(queue, manager, auth, state, args, clock=lambda: NOW)
        yield instance, jobs


def test_worker_activity_pagination_deduplication_and_quarantine(mocker):
    manager = mocker.Mock()
    manager.listWorkers.side_effect = [
        {
            "workers": [
                worker("fresh"),
                worker("old", lastDateActive=(NOW - timedelta(minutes=91)).isoformat()),
                worker("quarantined", quarantineUntil=(NOW + timedelta(days=1)).isoformat()),
            ],
            "continuationToken": "next",
        },
        {
            "workers": [
                worker("fresh"),
                worker("boundary", lastDateActive=(NOW - timedelta(minutes=90)).isoformat()),
                worker("bad", lastDateActive="invalid"),
                worker("future", lastDateActive=(NOW + timedelta(minutes=1)).isoformat()),
            ],
        },
    ]
    assert controller.active_worker_count(manager, DESTINATION, NOW, 90) == 2
    assert manager.listWorkers.call_args.kwargs == {"continuationToken": "next"}


@pytest.mark.parametrize("workers,pending,running", [(0, 0, 0), (1, 1, 0), (1, 0, 1), (2, 1, 1)])
def test_never_exceeds_capacity_including_other_submitters(setup, workers, pending, running):
    instance, _ = setup
    instance.manager.listWorkers.return_value = {"workers": [worker(str(i)) for i in range(workers)]}
    instance.queue.taskQueueCounts.return_value = {"pendingTasks": pending, "claimedTasks": running}
    instance.tick()
    instance.queue.createTask.assert_not_called()


def test_rechecks_capacity_after_preflight(setup):
    instance, _ = setup
    instance.queue.taskQueueCounts.side_effect = [
        {"pendingTasks": 0, "claimedTasks": 0},
        {"pendingTasks": 1, "claimedTasks": 0},
    ]
    instance.tick()
    instance.queue.createTask.assert_not_called()
    assert "Capacity changed" in instance.message


def test_worker_disappearing_during_preflight_stops_submission(setup):
    instance, _ = setup
    instance.manager.listWorkers.side_effect = [{"workers": [worker()]}, {"workers": []}]
    instance.tick()
    instance.queue.createTask.assert_not_called()


def test_only_jobs_with_effective_scopes_are_candidates(setup):
    instance, jobs = setup
    for job in jobs:
        job.task["scopes"] = ["secrets:get:private"]
    jobs[1].task["scopes"] = []
    instance.auth.currentScopes.return_value = {"scopes": ["queue:create-task:*", "queue:scheduler-id:*"]}
    instance.tick()
    assert instance.queue.createTask.call_args.args[1]["extra"]["replay"]["sourceTaskId"] == jobs[1].task_id


def test_scope_filter_and_live_recheck(setup):
    instance, _ = setup
    instance.auth.currentScopes.return_value = {"scopes": []}
    instance.tick()
    instance.queue.createTask.assert_not_called()
    instance.auth.currentScopes.side_effect = [{"scopes": ["*"]}, {"scopes": []}]
    instance.tick()
    instance.queue.createTask.assert_not_called()
    assert "Permissions changed" in instance.message


def test_permissions_snapshot_updates_after_client_grants_change(setup):
    instance, jobs = setup
    instance.args.submissions_enabled = False
    instance.queue.options = {"credentials": {"clientId": "user/client", "accessToken": "private-token"}}
    instance.auth.currentScopes.return_value = {"scopes": []}
    instance.tick()
    data = instance.snapshot()
    assert data["permissions"]["blocked_count"] == len(jobs)
    assert data["permissions"]["client_url"].endswith("/user%2Fclient")
    assert "private-token" not in json.dumps(instance.report())
    checked = data["permissions"]["checked_at"]
    instance.auth.currentScopes.return_value = {"scopes": ["*"]}
    instance.clock = lambda: NOW + timedelta(seconds=1)
    instance.tick()
    refreshed = instance.snapshot()["permissions"]
    assert refreshed["checked_at"] != checked
    assert refreshed["blocked_count"] == 0
    assert refreshed["suggested_scopes"] == []
    instance.queue.createTask.assert_not_called()


def test_dry_run_validates_without_reserving_or_submitting(setup):
    instance, _ = setup
    instance.args.dry_run = True
    instance.tick()
    controller.check_inputs.assert_called_once()
    instance.queue.createTask.assert_not_called()
    assert not instance.state.active()
    assert "DRY RUN" in instance.message


def test_quitting_during_preflight_prevents_new_submission(setup):
    import threading

    instance, _ = setup
    stop = threading.Event()
    instance.stop_requested = stop.is_set
    controller.check_inputs.side_effect = lambda *args, **kwargs: stop.set()
    instance.tick()
    instance.queue.createTask.assert_not_called()
    assert not instance.state.active()


def test_fair_rotation_across_restart_and_rate_limit(setup):
    instance, jobs = setup
    instance.tick()
    first_id = instance.queue.createTask.call_args.args[0]
    first = instance.queue.createTask.call_args.args[1]["extra"]["replay"]["sourceTaskId"]
    instance.tick()
    assert instance.queue.createTask.call_count == 1  # local active floor despite empty API counts
    instance.queue.status.return_value = {"status": {"state": "completed"}}
    instance.tick()
    assert instance.queue.createTask.call_count == 1  # rate limit also survives restart
    instance = controller.ReplayController(
        instance.queue,
        instance.manager,
        instance.auth,
        instance.state,
        instance.args,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    instance.tick()
    second = instance.queue.createTask.call_args.args[1]["extra"]["replay"]["sourceTaskId"]
    assert second != first
    instance.tick(now=NOW + timedelta(minutes=1, seconds=1))
    assert (
        instance.state.db.execute("SELECT state FROM tasks WHERE task_id=?", (first_id,)).fetchone()[0] == "completed"
    )
    first_key = next(controller.workload_key(SOURCE, job) for job in jobs if job.task_id == first)
    assert instance.state.history(first_key)["successes"] == 1  # never counted twice


@pytest.mark.parametrize(
    "histories,expected",
    [
        ([(0, 0, 0), (0, 0, 0), (0, 0, 0)], 1),
        ([(0, 0, 0), (0, 1, 0), (0, 1, 0)], 0),
        ([(1, 0, 0), (0, 1, 0), (1, 0, 0)], 1),
        ([(0, 1, 1), (0, 1, 3), (0, 1, 2)], 0),
    ],
)
def test_shuffling_changes_ties_but_preserves_coverage_attempts_and_age_priority(setup, mocker, histories, expected):
    instance, jobs = setup
    for job, (successes, attempts, last_attempt) in zip(jobs, histories):
        key = controller.workload_key(SOURCE, job)
        instance.state.ensure_job(key, job.name)
        with instance.state.db:
            instance.state.db.execute(
                "UPDATE workloads SET successes=?, attempts=?, last_attempt=? WHERE key=?",
                (successes, attempts, last_attempt, key),
            )

    def rotate(candidates):
        candidates.append(candidates.pop(0))

    mocker.patch.object(controller.random, "shuffle", side_effect=rotate)
    instance.tick()
    assert instance.queue.createTask.call_args.args[1]["extra"]["replay"]["sourceTaskId"] == jobs[expected].task_id


def test_one_submission_per_cycle_and_no_duplicate_in_flight_type(setup):
    instance, _ = setup
    instance.args.tasks_per_worker = 5
    instance.tick()
    assert instance.queue.createTask.call_count == 1
    instance.clock = lambda: NOW + timedelta(minutes=1)
    instance.tick()
    assert instance.queue.createTask.call_count == 2
    ids = [call.args[1]["extra"]["replay"]["sourceTaskId"] for call in instance.queue.createTask.call_args_list]
    assert len(set(ids)) == 2


def test_invalid_inputs_back_off_and_allow_other_types(setup):
    instance, _ = setup
    controller.check_inputs.side_effect = [ValueError("expired artifact"), 19]
    instance.tick()
    assert instance.queue.createTask.call_count == 1
    failed = instance.state.db.execute("SELECT * FROM workloads WHERE failures=1").fetchone()
    assert failed["retry_after"] == NOW.timestamp() + 300
    assert "expired artifact" in failed["error"]


def test_failed_replay_backoff_does_not_block_unseen_types(setup):
    instance, _ = setup
    instance.tick()
    instance.clock = lambda: NOW + timedelta(minutes=1)
    instance.queue.status.return_value = {"status": {"state": "failed"}}
    instance.tick()
    assert instance.queue.createTask.call_count == 2
    failed = instance.state.db.execute("SELECT * FROM workloads WHERE failures=1").fetchone()
    assert failed["retry_after"] == (NOW + timedelta(minutes=6)).timestamp()


def test_lost_create_response_reconciles_without_duplicate(setup):
    instance, _ = setup
    instance.queue.createTask.side_effect = rest_error(500)
    with pytest.raises(taskcluster.exceptions.TaskclusterFailure):
        instance.tick()
    task_id = instance.queue.createTask.call_args.args[0]
    assert instance.state.active()[0]["state"] == "reserved"
    instance.queue.createTask.side_effect = None
    instance.queue.status.return_value = {"status": {"state": "running"}}
    instance.clock = lambda: NOW + timedelta(minutes=1)
    instance.tick()
    assert instance.queue.createTask.call_count == 1
    assert instance.state.active()[0]["task_id"] == task_id


def test_lost_request_retries_exact_id_and_definition(setup):
    instance, _ = setup
    instance.queue.createTask.side_effect = rest_error(500)
    with pytest.raises(taskcluster.exceptions.TaskclusterFailure):
        instance.tick()
    first_call = instance.queue.createTask.call_args
    instance.queue.createTask.side_effect = None
    instance.queue.status.side_effect = rest_error(404)
    instance.clock = lambda: NOW + timedelta(minutes=1)
    controller.check_inputs.side_effect = None  # mock avoids source-status network for recovery
    instance.tick()
    assert instance.queue.createTask.call_count == 2
    assert instance.queue.createTask.call_args == first_call
    assert len(instance.state.active()) == 1


def test_definitive_submission_rejection_releases_capacity(setup):
    instance, _ = setup
    instance.queue.createTask.side_effect = rest_error(403)
    with pytest.raises(taskcluster.exceptions.TaskclusterFailure):
        instance.tick()
    assert not instance.state.active()
    assert instance.state.db.execute("SELECT failures FROM workloads WHERE attempts=1").fetchone()[0] == 1


def test_api_failure_cannot_schedule_from_old_capacity(setup):
    instance, _ = setup
    instance.manager.listWorkers.side_effect = rest_error(503)
    with pytest.raises(taskcluster.exceptions.TaskclusterFailure):
        instance.tick()
    instance.queue.createTask.assert_not_called()


def test_long_stopped_controller_handles_expired_task_records(setup):
    instance, _ = setup
    instance.tick()
    instance.queue.status.side_effect = rest_error(404)
    # Past the replay deadline, a missing task cannot occupy a slot indefinitely.
    instance.clock = lambda: NOW + timedelta(days=8)
    instance.manager.listWorkers.return_value = {"workers": []}
    instance.tick()
    assert not instance.state.active()
    assert instance.queue.createTask.call_count == 1


def test_failure_backoff_doubles_and_caps(setup):
    instance, jobs = setup
    key = controller.workload_key(SOURCE, jobs[0])
    instance.state.ensure_job(key, jobs[0].name)
    delays = []
    for _ in range(7):
        with instance.state.db:
            instance.state.reject(key, "bad inputs", NOW.timestamp(), 300)
        delays.append(instance.state.history(key)["retry_after"] - NOW.timestamp())
    assert delays == [300, 600, 1200, 2400, 3600, 3600, 3600]


def test_lock_and_state_survive_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    with controller.ControllerState(path) as state:
        state.ensure_job("key", "name")
        state.reserve("key", "task", {"payload": "unchanged"}, NOW.timestamp())
        with pytest.raises(ValueError, match="Another replay controller"), controller.ControllerState(path):
            pass
    with controller.ControllerState(path) as state:
        assert state.active()[0]["task_id"] == "task"
        assert state.history("key")["attempts"] == 1


def test_render_uses_snapshot_without_cross_thread_sqlite_queries(setup):
    from concurrent.futures import ThreadPoolExecutor

    instance, _ = setup
    instance.tick()
    instance.refresh_view()
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(instance.render).result() is not None


def test_running_elapsed_uses_latest_run_start_and_recovers_after_restart(setup):
    from rich.console import Console

    instance, _ = setup
    instance.tick()
    instance.clock = lambda: NOW + timedelta(minutes=3)
    instance.queue.status.return_value = {
        "status": {
            "state": "running",
            "runs": [
                {"started": (NOW - timedelta(hours=1)).isoformat()},
                {"started": (NOW + timedelta(minutes=1)).isoformat()},
            ],
        },
    }
    instance.tick()
    instance.refresh_view()
    console = Console(width=160)
    with console.capture() as capture:
        console.print(instance.render())
    assert "00:02:00" in capture.get()
    instance.clock = lambda: NOW + timedelta(minutes=4)
    with console.capture() as capture:
        console.print(instance.render())
    assert "00:03:00" in capture.get()  # live timer advances without another API poll
    resumed = controller.ReplayController(
        instance.queue,
        instance.manager,
        instance.auth,
        instance.state,
        instance.args,
        clock=instance.clock,
    )
    resumed.tick()
    resumed.refresh_view()
    with console.capture() as capture:
        console.print(resumed.render())
    assert "00:03:00" in capture.get()


@pytest.mark.parametrize("state,expected", [("pending", "00:03:00"), ("running", "unknown")])
def test_queued_elapsed_and_missing_run_start(setup, state, expected):
    from rich.console import Console

    instance, _ = setup
    instance.tick()
    instance.clock = lambda: NOW + timedelta(minutes=3)
    instance.queue.status.return_value = {"status": {"state": state}}
    instance.tick()
    instance.refresh_view()
    console = Console(width=160)
    with console.capture() as capture:
        console.print(instance.render())
    assert expected in capture.get()
    assert ("queued" if state == "pending" else "running") in capture.get()


def test_cli_once_dry_run(mocker, tmp_path, capsys):
    mocker.patch("worker_health.replay_sessions.sessions_root", return_value=tmp_path / "sessions")
    mocker.patch(
        "sys.argv",
        [
            "replay_tc_pool.py",
            "--from-pool",
            SOURCE,
            "--queue",
            DESTINATION,
            "--dry-run",
            "--once",
            "--output",
            "log",
            "--report",
            str(tmp_path / "report.json"),
        ],
    )
    mocker.patch.object(cli, "state_path", return_value=tmp_path / "cli.sqlite3")
    mocker.patch.object(cli, "queue_client")
    mocker.patch.object(cli.taskcluster, "WorkerManager")
    mocker.patch.object(cli.taskcluster, "Auth")
    runner = mocker.patch.object(cli, "ReplayController").return_value
    runner.workers = runner.outstanding = runner.limit = 0
    runner.message = "DRY RUN"
    runner.report.return_value = {"passed": 0, "eligible": 3, "excluded": 0, "full_coverage": False}
    runner.exit_ready = False
    assert cli.main() == 0
    runner.tick.assert_called_once()
    runner.render.assert_not_called()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["exit_reason"] == "once"
    assert not report["full_coverage"]


@pytest.mark.parametrize(
    "extra",
    [["--poll-interval", "0"], ["--worker-active-minutes", "-1"], ["--failure-backoff", "3601"]],
)
def test_cli_rejects_bad_limits(mocker, extra):
    mocker.patch("sys.argv", ["replay_tc_pool.py", "--from-pool", SOURCE, "--queue", DESTINATION, *extra])
    with pytest.raises(SystemExit):
        cli.parse_args()


def cover_jobs(instance, jobs):
    for job in jobs:
        key = controller.workload_key(SOURCE, job)
        instance.state.ensure_job(key, job.name)
        with instance.state.db:
            instance.state.db.execute("UPDATE workloads SET successes=1 WHERE key=?", (key,))


def test_default_pauses_and_resumes_only_for_new_types(setup):
    instance, jobs = setup
    cover_jobs(instance, jobs[:2])
    original = jobs.pop()
    instance.tick()
    assert instance.paused and not instance.exit_ready
    assert instance.snapshot()["full_coverage"]
    instance.queue.createTask.assert_not_called()
    # A newer source task with the same name/scopes remains covered.
    jobs[0].task_id = "newer-source"
    instance.clock = lambda: NOW + timedelta(minutes=10)
    instance.tick()
    instance.queue.createTask.assert_not_called()
    jobs.append(original)
    instance.clock = lambda: NOW + timedelta(minutes=20)
    instance.tick()
    assert not instance.paused
    assert instance.queue.createTask.call_args.args[1]["extra"]["replay"]["sourceTaskId"] == original.task_id
    instance.queue.status.return_value = {"status": {"state": "completed"}}
    instance.clock = lambda: NOW + timedelta(minutes=21)
    instance.tick()
    assert instance.paused
    assert instance.queue.createTask.call_count == 1


def test_new_scope_variant_is_uncovered(setup):
    instance, jobs = setup
    cover_jobs(instance, jobs)
    jobs[0].task["scopes"].append("secrets:get:new")
    instance.tick()
    assert not instance.paused
    instance.queue.createTask.assert_called_once()


@pytest.mark.parametrize("stop", [False, True])
def test_exit_independent_of_pause_and_checks_before_submission(setup, stop):
    instance, jobs = setup
    cover_jobs(instance, jobs)
    instance.args.stop_on_full_coverage = stop
    instance.args.exit_on_full_coverage = True
    instance.tick()
    assert instance.exit_ready
    instance.queue.createTask.assert_not_called()


def test_disabling_pause_preserves_continuous_rotation(setup):
    instance, jobs = setup
    cover_jobs(instance, jobs)
    instance.args.stop_on_full_coverage = False
    instance.tick()
    assert not instance.paused and not instance.exit_ready
    instance.queue.createTask.assert_called_once()


def test_pause_does_not_rerun_covered_types_while_uncovered_type_in_backoff(setup):
    instance, jobs = setup
    cover_jobs(instance, jobs[:2])
    key = controller.workload_key(SOURCE, jobs[2])
    instance.state.ensure_job(key, jobs[2].name)
    with instance.state.db:
        instance.state.reject(key, "expired inputs", NOW.timestamp(), 300)
    instance.tick()
    instance.queue.createTask.assert_not_called()


@pytest.mark.parametrize("condition", ["empty", "denied", "unavailable", "partial"])
def test_uncertain_or_empty_coverage_does_not_exit(setup, condition):
    instance, jobs = setup
    if condition != "empty":
        cover_jobs(instance, jobs)
    instance.args.exit_on_full_coverage = True
    if condition == "empty":
        jobs.clear()
    elif condition == "denied":
        instance.auth.currentScopes.return_value = {"scopes": []}
    elif condition == "unavailable":
        controller.load_discovery.return_value.unavailable_workers = 1
    else:
        controller.load_discovery.return_value.complete = False
    instance.tick()
    assert not instance.exit_ready
    assert not instance.snapshot()["full_coverage"]


def test_report_has_evidence_and_exclusions_without_task_definitions(setup):
    instance, _ = setup
    instance.tick()
    instance.refresh_view()
    task_id = instance.state.active()[0]["task_id"]
    instance.auth.currentScopes.return_value = {"scopes": []}
    instance.clock = lambda: NOW + timedelta(minutes=1)
    instance.tick()
    data = instance.report()
    assert data["excluded"] == 3
    assert all(not row["eligible"] and row["missing_scopes"] for row in data["rows"])
    assert data["outstanding_tasks"][0]["task_id"] == task_id
    assert data["outstanding_tasks"][0]["this_invocation"]
    assert '"definition":' not in json.dumps(data)


def test_runner_exits_with_final_report_without_submitting(setup, mocker, tmp_path):
    from rich.console import Console

    instance, jobs = setup
    cover_jobs(instance, jobs)
    instance.args.exit_on_full_coverage = True
    instance.args.report = str(tmp_path / "final.json")
    instance.args.output = "log"
    instance.args.once = False
    instance.args.poll_interval = 60
    mocker.patch.object(cli, "queue_client", return_value=instance.queue)
    mocker.patch.object(cli.taskcluster, "WorkerManager", return_value=instance.manager)
    mocker.patch.object(cli.taskcluster, "Auth", return_value=instance.auth)
    mocker.patch.object(cli, "ControllerState").return_value.__enter__.return_value = instance.state
    mocker.patch.object(cli, "ReplayController", return_value=instance)
    published = []
    assert cli.run_controller(instance.args, Console(), publish=published.append) == 0
    report = json.loads((tmp_path / "final.json").read_text())
    assert report["exit_reason"] == "full_coverage"
    assert report["full_coverage"] and report["passed"] == 3
    assert published[-1]["sampled_at"] == report["sampled_at"]
    instance.queue.createTask.assert_not_called()


def test_report_failure_returns_nonzero(setup, mocker, tmp_path):
    from rich.console import Console

    instance, _ = setup
    mocker.patch.object(cli, "state_path", return_value=tmp_path / "report-failure.sqlite3")
    instance.args.report = str(tmp_path / "missing" / "report.json")
    assert cli.run_controller(instance.args, Console()) == 1


def test_cli_coverage_defaults_and_overrides(mocker):
    command = ["replay_tc_pool.py", "--from-pool", SOURCE, "--queue", DESTINATION]
    mocker.patch("sys.argv", command)
    args = cli.parse_args()
    assert args.stop_on_full_coverage and not args.exit_on_full_coverage
    mocker.patch("sys.argv", command + ["--no-stop-on-full-coverage", "--exit-on-full-coverage"])
    args = cli.parse_args()
    assert not args.stop_on_full_coverage and args.exit_on_full_coverage


def test_disappeared_types_stay_in_session_after_restart(setup):
    instance, jobs = setup
    cover_jobs(instance, jobs)
    instance.tick()
    original_key = controller.workload_key(SOURCE, jobs[0])
    jobs.pop(0)
    instance.clock = lambda: NOW + timedelta(minutes=10)
    instance.tick()
    snapshot = instance.snapshot()
    assert snapshot["passed"] == snapshot["eligible"] == 3
    historical = next(row for row in snapshot["rows"] if row["key"] == original_key)
    assert historical["coverage"] == "passed"
    assert historical["status"] == "saved source available" and not historical["source_present"]
    resumed = controller.ReplayController(
        instance.queue,
        instance.manager,
        instance.auth,
        instance.state,
        instance.args,
        clock=instance.clock,
    )
    resumed.tick()
    row = next(row for row in resumed.report()["rows"] if row["key"] == original_key)
    assert row["source_task_id"] == "0" * 22
    assert row["successes"] == 1
    instance.queue.createTask.assert_not_called()


def test_disappeared_untested_type_cannot_make_coverage_complete(setup):
    instance, jobs = setup
    instance.args.submissions_enabled = False
    instance.tick()
    cover_jobs(instance, jobs[:2])
    missing = jobs.pop()
    instance.clock = lambda: NOW + timedelta(minutes=10)
    instance.args.exit_on_full_coverage = True
    instance.tick()
    assert instance.snapshot()["eligible"] == 3
    assert not instance.exit_ready
    assert any(
        row["name"] == missing.name and row["status"] == "saved source available" for row in instance.snapshot()["rows"]
    )
    instance.queue.createTask.assert_not_called()


def test_saved_source_replays_after_restart_without_source_api(setup):
    instance, jobs = setup
    jobs[:] = jobs[:1]
    original = deepcopy(jobs[0])
    instance.args.submissions_enabled = False
    instance.tick()
    jobs.clear()
    instance.queue.task.side_effect = rest_error(404)
    instance.args.submissions_enabled = True
    resumed = controller.ReplayController(
        instance.queue,
        instance.manager,
        instance.auth,
        instance.state,
        instance.args,
        clock=lambda: NOW + timedelta(minutes=10),
    )
    resumed.tick()
    definition = instance.queue.createTask.call_args.args[1]
    assert definition["payload"]["env"] == original.task["payload"]["env"]
    assert definition["extra"]["replay"]["sourceTaskId"] == original.task_id
    instance.queue.task.assert_not_called()
    assert controller.check_inputs.call_args.kwargs["status"] == {"state": "completed"}


@pytest.mark.parametrize(
    "covered,pause,enabled,submits",
    [
        (False, True, True, True),
        (True, False, True, True),
        (True, True, True, False),
        (False, False, False, False),
    ],
)
@pytest.mark.parametrize("missing_dependency", [False, True])
def test_expired_inputs_wait_for_new_source_and_respect_controls(
    setup,
    covered,
    pause,
    enabled,
    submits,
    missing_dependency,
):
    instance, jobs = setup
    jobs[:] = jobs[:1]
    instance.args.stop_on_full_coverage = False
    controller.check_inputs.side_effect = (
        rest_error(404) if missing_dependency else controller.ExpiredReplayInputs("artifact expired")
    )
    instance.tick()
    assert instance.snapshot()["rows"][0]["status"] == "Inputs expired; waiting for newer job"
    assert instance.message == "Inputs expired; waiting for newer job"
    instance.clock = lambda: NOW + timedelta(minutes=10)
    instance.tick()
    controller.check_inputs.assert_called_once()  # The same expired source is not retried.
    if covered:
        cover_jobs(instance, jobs)
    jobs[0] = deepcopy(jobs[0])
    jobs[0].task_id = "fresh-source"
    jobs[0].completed = (NOW + timedelta(minutes=11)).isoformat()
    jobs[0].task["payload"]["env"]["FRESH_INPUT"] = "new-build"
    instance.args.stop_on_full_coverage = pause
    instance.args.submissions_enabled = enabled
    instance.clock = lambda: NOW + timedelta(minutes=20)
    controller.check_inputs.side_effect = None
    instance.tick()
    assert instance.queue.createTask.called == submits
    if submits:
        definition = instance.queue.createTask.call_args.args[1]
        assert definition["extra"]["replay"]["sourceTaskId"] == "fresh-source"
        assert definition["payload"]["env"]["FRESH_INPUT"] == "new-build"
    assert instance.state.history(controller.workload_key(SOURCE, jobs[0]))["successes"] == int(covered)


def test_older_scan_does_not_replace_newer_saved_source(setup):
    instance, jobs = setup
    instance.args.submissions_enabled = False
    instance.tick()
    original = deepcopy(jobs[0])
    jobs[0] = deepcopy(original)
    jobs[0].task_id = "older-source"
    jobs[0].completed = (NOW - timedelta(days=1)).isoformat()
    instance.clock = lambda: NOW + timedelta(minutes=10)
    instance.tick()
    key = controller.workload_key(SOURCE, original)
    assert instance.known_jobs[key].task_id == original.task_id
    assert instance.state.history(key)["source_task_id"] == original.task_id


def test_monitoring_reconciles_existing_work_but_never_submits(setup):
    instance, _ = setup
    instance.tick()
    instance.args.submissions_enabled = False
    instance.clock = lambda: NOW + timedelta(minutes=1)
    instance.queue.status.return_value = {"status": {"state": "completed"}}
    instance.tick()
    assert instance.queue.createTask.call_count == 1
    assert instance.snapshot()["passed"] == 1
    assert not instance.state.active()


def test_submissions_require_explicit_enablement(setup):
    instance, _ = setup
    del instance.args.submissions_enabled
    instance.tick()
    instance.queue.createTask.assert_not_called()
    assert not instance.snapshot()["submissions_enabled"]


def test_resume_keeps_saved_rows_visible_during_initial_scan(setup, mocker):
    instance, jobs = setup
    cover_jobs(instance, jobs)
    instance.tick()
    saved = instance.report()
    instance.args.session_info = SimpleNamespace(report=lambda: saved)
    resumed = controller.ReplayController(
        instance.queue,
        instance.manager,
        instance.auth,
        instance.state,
        instance.args,
        clock=lambda: NOW,
    )
    during_scan = []
    mocker.patch.object(controller, "load_discovery", return_value=None)

    def discover(*args, **kwargs):
        resumed.progress("Reading source worker histories")
        during_scan.append(resumed.snapshot())
        assert resumed.report()["passed"] == 3
        return Discovery(jobs, 1, 3, 3, 0)

    mocker.patch.object(controller, "discover_jobs", side_effect=discover)
    assert resumed.snapshot()["passed"] == 3
    assert len(resumed.snapshot()["rows"]) == 3
    resumed.tick()
    assert during_scan[0]["refreshing"]
    assert during_scan[0]["passed"] == 3
    assert "showing saved results" in during_scan[0]["message"]
    assert not during_scan[0]["full_coverage"]
    assert not resumed.snapshot().get("refreshing", False)
    assert resumed.snapshot()["passed"] == 3


def test_pausing_during_preflight_prevents_reservation(setup):
    instance, _ = setup
    controller.check_inputs.side_effect = lambda *args, **kwargs: setattr(instance.args, "submissions_enabled", False)
    instance.tick()
    assert not instance.state.active()
    instance.queue.createTask.assert_not_called()


def test_destination_lock_is_shared_across_session_databases(tmp_path):
    lock = tmp_path / "destination.lock"
    with (
        controller.ControllerState(tmp_path / "first.sqlite3", lock_path=lock),
        pytest.raises(ValueError, match="Another replay controller"),
        controller.ControllerState(tmp_path / "second.sqlite3", lock_path=lock),
    ):
        pass


def test_raise_success_target_runs_another_sweep_and_pauses(setup):
    instance, jobs = setup
    cover_jobs(instance, jobs)
    instance.tick()
    assert instance.paused
    instance.args.successes_per_type = 2
    instance.queue.status.return_value = {"status": {"state": "completed"}}
    for minute in range(1, 5):
        instance.clock = lambda minute=minute: NOW + timedelta(minutes=minute)
        instance.tick()
    assert instance.queue.createTask.call_count == len(jobs)
    assert instance.paused
    assert instance.report()["successes_per_type"] == 2
    assert instance.report()["passed"] == len(jobs)


def targeted_controller(instance, jobs, attempts=3, smoke=False):
    from worker_health.replay_targets import TargetedReplayController

    instance.args.task_ids = [job.task_id for job in jobs]
    instance.args.attempts_per_target = attempts
    instance.args.stop_after_success = smoke
    instance.queue.status.return_value = {"status": {"state": "completed"}}
    return TargetedReplayController(
        instance.queue,
        instance.manager,
        instance.auth,
        instance.state,
        instance.args,
        clock=instance.clock,
    )


def test_targeted_fixed_count_continues_after_success_and_survives_restart(setup):
    instance, jobs = setup
    instance = targeted_controller(instance, jobs[:1])
    for cycle in range(3):
        instance.clock = lambda cycle=cycle: NOW + timedelta(minutes=cycle)
        instance.tick()
    assert instance.queue.createTask.call_count == 3
    saved_payload = deepcopy(jobs[0].task["payload"])
    jobs[0].task["payload"] = {"changed": True}
    resumed = targeted_controller(instance, jobs[:1])
    resumed.clock = lambda: NOW + timedelta(minutes=4)
    resumed.queue.task.reset_mock()
    resumed.tick()
    assert resumed.exit_ready
    assert resumed.queue.createTask.call_count == 3
    resumed.queue.task.assert_not_called()
    assert resumed.jobs[0].task["payload"] == saved_payload
    assert resumed.report()["targets"]["maximum_submissions"] == 3
    assert len(resumed.report()["rows"][0]["tasks"]) == 3
    controller.load_discovery.assert_not_called()


def test_targeted_same_variant_keeps_both_source_ids(setup):
    instance, jobs = setup
    jobs[1].task = deepcopy(jobs[0].task)
    instance = targeted_controller(instance, jobs[:2], attempts=1)
    instance.tick()
    instance.clock = lambda: NOW + timedelta(minutes=1)
    instance.tick()
    instance.clock = lambda: NOW + timedelta(minutes=2)
    instance.tick()
    assert instance.exit_ready
    assert {call.args[1]["extra"]["replay"]["sourceTaskId"] for call in instance.queue.createTask.call_args_list} == {
        job.task_id for job in jobs[:2]
    }
    assert len(instance.report()["rows"]) == 2


def test_targeted_smoke_stops_at_first_success(setup):
    instance, jobs = setup
    instance = targeted_controller(instance, jobs[:1], smoke=True)
    instance.tick()
    instance.tick(NOW + timedelta(minutes=1))
    assert instance.exit_ready
    assert instance.queue.createTask.call_count == 1


def test_targeted_waits_for_final_result(setup):
    instance, jobs = setup
    instance = targeted_controller(instance, jobs[:1], attempts=1)
    instance.tick()
    instance.queue.status.return_value = {"status": {"state": "running"}}
    instance.tick(NOW + timedelta(minutes=1))
    assert not instance.exit_ready
    assert "waiting" in instance.message
    instance.queue.status.return_value = {"status": {"state": "failed"}}
    instance.tick(NOW + timedelta(minutes=2))
    assert instance.exit_ready
    assert instance.queue.createTask.call_count == 1


def test_targeted_uncertain_submission_reuses_id_at_cap(setup):
    instance, jobs = setup
    instance = targeted_controller(instance, jobs[:1], attempts=1)
    instance.queue.createTask.side_effect = rest_error(500)
    with pytest.raises(taskcluster.exceptions.TaskclusterRestFailure):
        instance.tick()
    instance.queue.createTask.side_effect = None
    instance.queue.status.side_effect = rest_error(404)
    instance.clock = lambda: NOW + timedelta(minutes=1)
    instance.tick()
    calls = instance.queue.createTask.call_args_list
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert instance.submission_count(instance.job_key(jobs[0])) == 1


def test_targeted_preflight_error_does_not_consume_submission_budget(setup):
    instance, jobs = setup
    instance = targeted_controller(instance, jobs[:1], attempts=1)
    controller.check_inputs.side_effect = ValueError("temporary input problem")
    instance.tick()
    assert instance.submission_count(instance.job_key(jobs[0])) == 0
    assert instance.state.history(instance.job_key(jobs[0]))["attempts"] == 0
    controller.check_inputs.side_effect = None
    instance.clock = lambda: NOW + timedelta(minutes=6)
    instance.tick()
    assert instance.queue.createTask.call_count == 1


@pytest.mark.parametrize("bad", ["pool", "level", "state"])
def test_targeted_invalid_source_stops_before_any_submission(setup, bad):
    instance, jobs = setup
    instance = targeted_controller(instance, jobs[:2])
    if bad == "pool":
        jobs[1].task["taskQueueId"] = DESTINATION
    elif bad == "level":
        jobs[1].task["payload"]["env"]["MOZ_SCM_LEVEL"] = "3"
    else:
        instance.queue.status.return_value = {"status": {"state": "failed"}}
    with pytest.raises(ValueError):
        instance.tick()
    instance.queue.createTask.assert_not_called()
    assert not instance.state.active()


def test_targeted_retains_all_runs_and_unknown_version_without_log(mocker, setup):
    instance, jobs = setup
    instance = targeted_controller(instance, jobs[:1], attempts=1)
    instance.tick()
    collect = mocker.patch("worker_health.replay_targets.collect_log_evidence")
    instance.queue.status.return_value = {
        "status": {
            "state": "failed",
            "runs": [
                {"runId": 0, "workerId": "ms017", "workerGroup": "g", "state": "exception"},
                {"runId": 1, "workerId": "ms018", "workerGroup": "g", "state": "failed"},
            ],
        },
    }
    instance.tick(NOW + timedelta(minutes=1))
    runs = instance.report()["rows"][0]["tasks"][0]["runs"]
    assert [run["worker_id"] for run in runs] == ["ms017", "ms018"]
    assert all(run["verified_worker_version"] == "unknown" for run in runs)
    assert collect.call_count == 2
    resumed = targeted_controller(instance, jobs[:1], attempts=1)
    assert resumed.report()["rows"][0]["tasks"][0]["runs"] == runs


def test_targeted_cli_preview_dry_run_then_resume_keeps_cap(mocker, setup, tmp_path, caplog):
    instance, jobs = setup
    mocker.patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)})
    instance.manager.listWorkers.return_value = {"workers": [worker(lastDateActive=datetime.now(UTC).isoformat())]}
    instance.queue.status.return_value = {"status": {"state": "completed"}}
    mocker.patch.object(cli, "queue_client", return_value=instance.queue)
    mocker.patch.object(cli.taskcluster, "WorkerManager", return_value=instance.manager)
    mocker.patch.object(cli.taskcluster, "Auth", return_value=instance.auth)
    command = [
        "replay_tc_pool.py",
        "--from-pool",
        SOURCE,
        "--queue",
        DESTINATION,
        "--task-id",
        jobs[0].task_id,
        "--attempts-per-target",
        "1",
        "--dry-run",
        "--once",
        "--output",
        "log",
    ]
    mocker.patch("sys.argv", command)
    with caplog.at_level("INFO"):
        assert cli.main() == 0
    assert "Targeted replay preview" in caplog.text
    assert "Maximum submissions: 1" in caplog.text
    instance.queue.createTask.assert_not_called()
    from worker_health.replay_sessions import list_sessions

    session = list_sessions()[0]
    assert session.report()["targets"]["attempts_per_target"] == 1
    mocker.patch(
        "sys.argv",
        [
            "replay_tc_pool.py",
            "--session",
            str(session.path),
            "--resume",
            "--submit-on-start",
            "--once",
            "--output",
            "log",
        ],
    )
    assert cli.main() == 0
    assert instance.queue.createTask.call_count == 1
    assert cli.main() == 0
    assert instance.queue.createTask.call_count == 1
    assert session.report()["exit_reason"] == "targeted_batch_finished"
