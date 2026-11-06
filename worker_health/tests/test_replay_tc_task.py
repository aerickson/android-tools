import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re

import pytest
from rich.console import Console

import replay_tc_task as replay
from worker_health.replay_picker import Discovery, ReplayJob


SOURCE_ID = "ICHaZTMDTx2YY-fM3qlVXA"
SOURCE_QUEUE = "releng-hardware/gecko-t-linux-talos-2404"
DESTINATION = "releng-hardware/gecko-t-linux-talos-2404-tc-v115"
CLONE_IDS = ["D7ndxwWdTgmvJWSE40Lizw", "J6DlI0cBTPGzScK1grfKgw"]
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


@pytest.fixture
def source():
    # Adapt the captured level-3 payload to a level-1 source for replay-policy tests.
    task = json.loads(
        (Path(__file__).parent / "fixtures/replay_browsertime_task.json").read_text().replace("level-3", "level-1"),
    )
    task["payload"]["env"]["MOZ_SCM_LEVEL"] = "1"
    return task


@pytest.fixture
def queue(mocker, source):
    queue = mocker.Mock()
    queue.options = {
        "rootUrl": replay.ROOT_URL,
        "credentials": {"clientId": "test-client", "accessToken": "not-a-real-token"},
    }
    queue.task.return_value = source
    queue.status.return_value = {"status": {"state": "completed"}}
    inventories = {}
    for task_id, run_id, name in replay.artifact_references(source["payload"]):
        inventories.setdefault(task_id, []).append(
            {"name": name, "expires": "2099-01-01T00:00:00Z", "storageType": "object"},
        )
    queue.listLatestArtifacts.side_effect = lambda task_id, **kwargs: {"artifacts": inventories[task_id]}
    mocker.patch.object(replay, "queue_client", return_value=queue)
    mocker.patch.object(replay, "remember_submission")
    mocker.patch.object(replay, "remember_results")
    mocker.patch.object(replay, "target_coverage", return_value=None)
    mocker.patch.object(replay, "gen_task_id", side_effect=CLONE_IDS)
    return queue


def run_cli(mocker, *options):
    mocker.patch("sys.argv", ["replay_tc_task.py", SOURCE_ID, *options])
    return replay.main()


@pytest.mark.parametrize("suffix", ["", "/", "/runs/0", "/runs/3?foo=bar"])
def test_task_url_argument(suffix):
    assert replay.task_id_argument(f"{replay.ROOT_URL}/tasks/{SOURCE_ID}{suffix}") == SOURCE_ID
    assert replay.task_id_argument(SOURCE_ID) == SOURCE_ID


@pytest.mark.parametrize(
    "value",
    ["short", f"https://example.com/tasks/{SOURCE_ID}", f"{replay.ROOT_URL}/tasks/{SOURCE_ID}/logs"],
)
def test_invalid_task_argument(value):
    with pytest.raises(argparse.ArgumentTypeError):
        replay.task_id_argument(value)


def test_worker_pool_url_argument():
    assert (
        replay.queue_argument(
            f"{replay.ROOT_URL}/provisioners/releng-hardware/worker-types/gecko-t-linux-talos-2404-tc-v115?sortBy=Last%20Active&sortDirection=desc",
        )
        == DESTINATION
    )
    assert replay.queue_argument(SOURCE_QUEUE) == SOURCE_QUEUE


@pytest.mark.parametrize(
    "value",
    ["missing-slash", "a/b/c", "a/UPPERCASE", "https://example.com/provisioners/a/worker-types/b"],
)
def test_invalid_worker_pool(value):
    with pytest.raises(argparse.ArgumentTypeError):
        replay.queue_argument(value)


def test_clone_preserves_workload_and_isolates_reporting(source, mocker):
    original = deepcopy(source)
    mocker.patch.object(replay.getpass, "getuser", return_value="operator")
    clone = replay.clone_task(source, SOURCE_ID, CLONE_IDS[0], DESTINATION, NOW)

    assert source == original
    assert clone["taskQueueId"] == DESTINATION
    assert "workerType" not in clone and "provisionerId" not in clone
    assert clone["taskGroupId"] == CLONE_IDS[0]
    assert clone["schedulerId"] == "-"
    assert clone["projectId"] == "none"
    assert clone["routes"] == []
    assert "treeherder" not in clone["extra"]
    assert "treeherder-platform" not in clone["extra"]
    assert clone["payload"]["env"]["GECKO_HEAD_REV"] == source["payload"]["env"]["GECKO_HEAD_REV"]
    assert clone["payload"]["env"]["MOZ_SOURCE_CHANGESET"] == source["payload"]["env"]["MOZ_SOURCE_CHANGESET"]
    assert "tags" not in clone
    assert clone["extra"] == {"replay": {"sourceTaskId": SOURCE_ID, "sourceTaskQueueId": SOURCE_QUEUE}}
    assert clone["priority"] == "lowest"
    assert clone["retries"] == 0
    assert clone["metadata"]["owner"] == "operator@mozilla.com"
    assert SOURCE_ID in clone["metadata"]["description"]
    assert source["metadata"]["name"] in clone["metadata"]["description"]
    assert clone["scopes"] == source["scopes"]
    assert clone["dependencies"] == source["dependencies"]
    assert clone["requires"] == source["requires"]
    expected_payload = deepcopy(source["payload"])
    for artifact in expected_payload["artifacts"]:
        artifact["expires"] = clone["expires"]
    assert clone["payload"] == expected_payload
    assert clone["payload"]["maxRunTime"] == 4000
    assert replay.parse_timestamp(clone["created"]) == NOW
    assert replay.parse_timestamp(clone["deadline"]) == NOW + timedelta(days=1)
    assert replay.parse_timestamp(clone["expires"]) == NOW + timedelta(days=7)


def test_legacy_queue_and_docker_artifacts(source):
    source.pop("taskQueueId")
    source["payload"] = {
        "image": "ubuntu:24.04",
        "command": ["echo", "ok"],
        "artifacts": {"public/out": {"type": "directory", "path": "out", "expires": "2020-01-01T00:00:00Z"}},
    }
    assert replay.source_queue(source) == SOURCE_QUEUE
    clone = replay.clone_task(source, SOURCE_ID, CLONE_IDS[0], SOURCE_QUEUE, NOW)
    assert clone["payload"]["artifacts"]["public/out"]["expires"] == clone["expires"]


def test_real_job_artifact_references_include_decision_task_and_build(source):
    references = replay.artifact_references(source["payload"])
    assert len(references) == 19
    assert ("eJbNe9liQb6Bg0JkftiBnA", None, "public/run-task-hg") in references
    assert ("dLLPSJ3BSuGnsYyc65uowg", None, "public/build/mozharness.zip") in references
    assert ("dLLPSJ3BSuGnsYyc65uowg", None, "public/build/target.test_packages.json") in references
    assert ("Z6UKyPL2T16SHy0XdhNOmQ", None, "public/build/browsertime.tar.zst") in references


def test_task_image_and_run_specific_url_references():
    payload = {
        "image": {"type": "task-image", "taskId": SOURCE_ID, "path": "public/image.tar.zst"},
        "env": {
            "INPUT": f"{replay.ROOT_URL}/api/queue/v1/task/{SOURCE_ID}/runs/0/artifacts/public/file%20name.zip?download=1",
        },
    }
    assert replay.artifact_references(payload) == {
        (SOURCE_ID, None, "public/image.tar.zst"),
        (SOURCE_ID, 0, "public/file name.zip"),
    }


def test_preflight_checks_real_job_inputs(queue, source):
    assert replay.check_inputs(queue, source, SOURCE_ID, NOW) == 19
    assert queue.status.call_count == 12
    assert queue.listLatestArtifacts.call_count == 12


@pytest.mark.parametrize("state", ["pending", "running", "failed", "exception"])
def test_source_must_be_completed(queue, source, state):
    queue.status.return_value = {"status": {"state": state}}
    with pytest.raises(ValueError, match="Source task must be completed"):
        replay.check_inputs(queue, source, SOURCE_ID, NOW)


def test_unusable_dependency(queue, source):
    queue.status.side_effect = [{"status": {"state": "completed"}}, {"status": {"state": "failed"}}]
    with pytest.raises(ValueError, match="Dependency .* is failed"):
        replay.check_inputs(queue, source, SOURCE_ID, NOW)


def test_all_resolved_dependencies_can_have_failed(queue, source):
    source["requires"] = "all-resolved"
    queue.status.side_effect = [{"status": {"state": "completed"}}] + [{"status": {"state": "failed"}}] * 11
    assert replay.check_inputs(queue, source, SOURCE_ID, NOW) == 19


@pytest.mark.parametrize(
    "artifacts",
    [
        [],
        [{"name": "public/input", "expires": "2020-01-01T00:00:00Z"}],
        [{"name": "public/input", "expires": "2099-01-01T00:00:00Z", "storageType": "error"}],
    ],
)
def test_unusable_artifact_fails_preflight(queue, source, artifacts):
    source["dependencies"] = []
    source["payload"] = {"mounts": [{"content": {"taskId": SOURCE_ID, "artifact": "public/input"}}]}
    queue.listLatestArtifacts.side_effect = None
    queue.listLatestArtifacts.return_value = {"artifacts": artifacts}
    with pytest.raises(ValueError, match="Required artifact is missing, expired, or an error"):
        replay.check_inputs(queue, source, SOURCE_ID, NOW)


def test_artifact_pagination_and_explicit_run(queue, source):
    source["dependencies"] = []
    source["payload"] = {"command": [f"{replay.ROOT_URL}/api/queue/v1/task/{SOURCE_ID}/runs/0/artifacts/public/input"]}
    queue.listArtifacts.side_effect = [
        {"artifacts": [], "continuationToken": "next"},
        {"artifacts": [{"name": "public/input", "expires": "2099-01-01T00:00:00Z"}]},
    ]
    assert replay.check_inputs(queue, source, SOURCE_ID, NOW) == 1
    queue.listArtifacts.assert_any_call(SOURCE_ID, 0, continuationToken="next")


def test_dry_run_is_json_and_never_submits(mocker, queue, capsys, caplog):
    with caplog.at_level(logging.INFO):
        assert run_cli(mocker, "-q", DESTINATION, "-c", "2", "--dry-run") == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["tasks_submitted"] == 0
    assert "DRY RUN: checking inputs" in caplog.text
    assert "DRY RUN complete: no tasks submitted" in caplog.text
    assert plan["source_queue"] == SOURCE_QUEUE
    assert plan["destination_queue"] == DESTINATION
    assert plan["artifact_references_checked"] == 19
    assert [entry["task_id"] for entry in plan["tasks"]] == CLONE_IDS
    assert all(entry["task"]["taskGroupId"] == CLONE_IDS[0] for entry in plan["tasks"])
    queue.createTask.assert_not_called()
    replay.queue_client.assert_called_once_with(authenticated=False)


def test_dry_run_terminal_preview_is_dim_and_preserves_json(mocker, queue, capsys):
    mocker.patch.object(replay, "Console", side_effect=lambda **kwargs: Console(force_terminal=True, **kwargs))
    assert run_cli(mocker, "--dry-run") == 0
    output = capsys.readouterr().out
    assert "\x1b[2m" in output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    assert json.loads(plain)["dry_run"] is True


def test_submit_defaults_to_original_pool(mocker, queue, capsys):
    assert run_cli(mocker, "--output", "json") == 0
    task_id, definition = queue.createTask.call_args.args
    assert task_id == CLONE_IDS[0]
    assert definition["taskQueueId"] == SOURCE_QUEUE
    assert json.loads(capsys.readouterr().out)["tasks"][0]["task_id"] == task_id


def test_cross_pool_submit_waits_with_shared_output(mocker, queue, capsys):
    assert run_cli(mocker, "-q", DESTINATION, "-c", "2", "--wait", "--output", "json") == 0
    assert queue.createTask.call_count == 2
    assert all(call.args[1]["taskQueueId"] == DESTINATION for call in queue.createTask.call_args_list)
    tasks = json.loads(capsys.readouterr().out)["tasks"]
    assert [task["task_id"] for task in tasks] == CLONE_IDS
    assert all(task["state"] == "completed" for task in tasks)


def test_partial_submission_failure_still_waits_for_created_tasks(mocker, queue, capsys):
    queue.createTask.side_effect = [RuntimeError("denied"), {}]
    assert run_cli(mocker, "-c", "2", "--wait", "--output", "json") == 1
    tasks = json.loads(capsys.readouterr().out)["tasks"]
    assert [task["task_id"] for task in tasks] == [CLONE_IDS[1]]


def test_all_submissions_fail_with_empty_json_results(mocker, queue, capsys):
    queue.createTask.side_effect = RuntimeError("denied")
    assert run_cli(mocker, "-c", "2", "--wait", "--output", "json") == 1
    assert json.loads(capsys.readouterr().out) == {"tasks": []}
    assert queue.status.call_count == 12  # Preflight only; there are no clones to poll.


@pytest.mark.parametrize("state", ["failed", "exception"])
def test_replayed_task_failure_returns_nonzero(mocker, queue, capsys, state):
    queue.status.side_effect = lambda task_id: {
        "status": {"state": state if task_id in CLONE_IDS else "completed"},
    }
    assert run_cli(mocker, "--wait", "--output", "json") == 1
    assert json.loads(capsys.readouterr().out)["tasks"][0]["state"] == state


def test_preflight_failure_submits_nothing(mocker, queue, capsys):
    queue.status.return_value = {"status": {"state": "running"}}
    assert run_cli(mocker, "--output", "json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["tasks"] == []
    assert "Source task must be completed" in result["error"]
    queue.createTask.assert_not_called()


@pytest.mark.parametrize("option", ["--count", "--poll-interval"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_nonpositive_arguments_fail_before_network(mocker, queue, option, value):
    with pytest.raises(SystemExit) as exc:
        run_cli(mocker, option, value)
    assert exc.value.code == 2
    replay.queue_client.assert_not_called()


def test_public_preview_without_token(mocker, tmp_path):
    mocker.patch.object(replay.Path, "home", return_value=tmp_path)
    factory = mocker.patch.object(replay.taskcluster, "Queue")
    replay.queue_client(authenticated=False)
    assert "credentials" not in factory.call_args.args[0]
    with pytest.raises(ValueError, match="Cannot read Taskcluster credentials"):
        replay.queue_client(authenticated=True)


def run_picker(mocker, source, **kwargs):
    job = ReplayJob(SOURCE_ID, source, "2026-11-05T01:00:00Z", 600, "host")
    mocker.patch.object(replay, "picker_terminal", return_value=True)
    mocker.patch.object(replay.taskcluster, "WorkerManager")
    mocker.patch.object(replay.taskcluster, "Auth").return_value.currentScopes.return_value = {"scopes": ["*"]}
    mocker.patch.object(replay, "discover_jobs", return_value=Discovery([job], 120, 2400, 80, 0))
    mocker.patch.object(replay, "load_discovery", return_value=None)
    mocker.patch.object(replay, "save_discovery")
    mocker.patch.object(replay, "pick_job", return_value=[job])
    mocker.patch.object(replay.Confirm, "ask", return_value=kwargs.get("confirm", False))
    mocker.patch(
        "sys.argv",
        ["replay_tc_task.py", "--from-pool", SOURCE_QUEUE, "--output", "json", *kwargs.get("options", [])],
    )
    return job


def test_picker_submit_requires_confirmation(mocker, queue, source, capsys):
    run_picker(mocker, source, confirm=True, options=["-q", DESTINATION, "-c", "2"])
    assert replay.main() == 0
    assert queue.createTask.call_count == 2
    assert all(call.args[1]["taskQueueId"] == DESTINATION for call in queue.createTask.call_args_list)
    replay.Confirm.ask.assert_called_once()
    assert len(json.loads(capsys.readouterr().out)["tasks"]) == 2
    replay.remember_submission.assert_any_call(DESTINATION, CLONE_IDS[0], source)
    replay.remember_results.assert_called_once()


def test_target_coverage_is_only_fetched_for_copy_mode(mocker, queue, source):
    run_picker(mocker, source)
    assert replay.main() == 0
    replay.target_coverage.assert_not_called()


def test_copy_mode_passes_target_coverage_to_picker(mocker, queue, source):
    run_picker(mocker, source, options=["-q", DESTINATION, "--refresh"])
    sentinel = mocker.Mock()
    replay.target_coverage.return_value = sentinel
    assert replay.main() == 0
    assert replay.target_coverage.call_args.args[2] == DESTINATION
    assert replay.target_coverage.call_args.kwargs["refresh"] is True
    assert replay.pick_job.call_args.kwargs["coverage"] is sentinel


def test_picker_declining_confirmation_submits_nothing(mocker, queue, source):
    run_picker(mocker, source)
    assert replay.main() == 0
    queue.createTask.assert_not_called()


def test_picker_escape_exits_before_preflight(mocker, queue, source):
    run_picker(mocker, source)
    replay.pick_job.return_value = None
    assert replay.main() == 0
    queue.task.assert_not_called()
    queue.createTask.assert_not_called()


def test_picker_missing_scopes_prevents_confirmation_and_submission(mocker, queue, source, capsys, caplog):
    run_picker(mocker, source, confirm=True)
    replay.taskcluster.Auth.return_value.currentScopes.return_value = {"scopes": []}
    with caplog.at_level(logging.INFO):
        assert replay.main() == 1
    assert "Client lacks required replay scopes" in json.loads(capsys.readouterr().out)["error"]
    assert f"Selected job: {source['metadata']['name']}" in caplog.text
    assert f"Selected task: {replay.ROOT_URL}/tasks/{SOURCE_ID}" in caplog.text
    assert caplog.text.index("Selected job:") < caplog.text.index("Client lacks required replay scopes")
    replay.Confirm.ask.assert_not_called()
    queue.createTask.assert_not_called()


def test_picker_dry_run_previews_without_confirmation(mocker, queue, source, capsys):
    run_picker(mocker, source, options=["--dry-run"])
    queue.options.pop("credentials")
    assert replay.main() == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    replay.Confirm.ask.assert_not_called()
    replay.taskcluster.Auth.assert_not_called()
    queue.createTask.assert_not_called()


def test_picker_empty_history(mocker, queue, source, capsys):
    run_picker(mocker, source)
    replay.discover_jobs.return_value = Discovery([], 0, 0, 0, 0)
    assert replay.main() == 1
    assert "No completed level-1 jobs" in json.loads(capsys.readouterr().out)["error"]
    replay.pick_job.assert_not_called()
    queue.createTask.assert_not_called()


def test_picker_reuses_history_cache_but_checks_permissions_live(mocker, queue, source):
    run_picker(mocker, source)
    discovery = replay.discover_jobs.return_value
    replay.load_discovery.return_value = discovery
    assert replay.main() == 0
    replay.discover_jobs.assert_not_called()
    replay.taskcluster.Auth.return_value.currentScopes.assert_called()
    replay.save_discovery.assert_not_called()


def test_picker_explicit_refresh_bypasses_cache(mocker, queue, source):
    run_picker(mocker, source, options=["--refresh"])
    assert replay.main() == 0
    replay.load_discovery.assert_not_called()
    replay.discover_jobs.assert_called_once()


def test_picker_ctrl_r_refreshes_history_and_client_scopes(mocker, queue, source):
    from worker_health.replay_picker import RefreshPicker

    job = run_picker(mocker, source)
    replay.pick_job.side_effect = [RefreshPicker("pdf"), [job]]
    assert replay.main() == 0
    assert replay.discover_jobs.call_count == 2
    assert replay.pick_job.call_args.args[3] == "pdf"
    assert replay.taskcluster.Auth.return_value.currentScopes.call_count >= 2


def test_picker_refresh_restores_present_selections_and_reports_missing(mocker, queue, source, caplog):
    from worker_health.replay_picker import RefreshPicker

    job = run_picker(mocker, source)
    replay.pick_job.side_effect = [RefreshPicker("pdf", (SOURCE_ID, "disappeared")), [job]]
    assert replay.main() == 0
    assert replay.pick_job.call_args.kwargs["selected_ids"] == (SOURCE_ID,)
    assert "Selected task disappeared" in caplog.text
    assert "disappeared" in caplog.text


def select_two_jobs(mocker, source, **kwargs):
    first = run_picker(mocker, source, **kwargs)
    second_source = deepcopy(source)
    second_source["metadata"]["name"] = "Second workload"
    second = ReplayJob("B" * 22, second_source, first.completed, 300, "other-host")
    replay.pick_job.return_value = [first, second]
    return first, second


def test_multi_job_count_confirmation_and_shared_monitoring(mocker, queue, source, capsys):
    jobs = select_two_jobs(mocker, source, confirm=True, options=["-q", DESTINATION, "-c", "2"])
    clone_ids = [*CLONE_IDS, "C" * 22, "D" * 22]
    replay.gen_task_id.side_effect = clone_ids
    sources = {job.task_id: job.task for job in jobs}
    queue.task.side_effect = sources.__getitem__
    monitor = mocker.patch.object(
        replay,
        "wait_for_task_results",
        return_value={task_id: {"state": "completed"} for task_id in clone_ids},
    )
    assert replay.main() == 0
    replay.Confirm.ask.assert_called_once()
    assert "2 workload(s), 4 replay task(s)" in capsys.readouterr().err
    assert monitor.call_args.args[1] == clone_ids
    assert queue.createTask.call_count == 4
    definitions = [call.args[1] for call in queue.createTask.call_args_list]
    assert [task["extra"]["replay"]["sourceTaskId"] for task in definitions] == [jobs[0].task_id] * 2 + [
        jobs[1].task_id,
    ] * 2
    assert all(task["taskGroupId"] == clone_ids[0] for task in definitions)
    replay.remember_submission.assert_any_call(DESTINATION, clone_ids[-1], jobs[1].task)


@pytest.mark.parametrize("failure", ["inputs", "scopes", "pool"])
def test_multi_job_validation_failure_submits_nothing(mocker, queue, source, capsys, failure):
    jobs = select_two_jobs(mocker, source, confirm=True)
    queue.task.side_effect = lambda task_id: jobs[1].task if task_id == jobs[1].task_id else source
    if failure == "inputs":
        check = mocker.patch.object(replay, "check_inputs", side_effect=[19, ValueError("expired artifact")])
    elif failure == "scopes":
        mocker.patch.object(replay, "missing_scopes", side_effect=[[], ["secrets:get:private"]])
    else:
        jobs[1].task["taskQueueId"] = DESTINATION
    assert replay.main() == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert "Second workload" in error
    assert jobs[1].task_id in error
    if failure == "inputs":
        assert check.call_count == 2
    replay.Confirm.ask.assert_not_called()
    queue.createTask.assert_not_called()


def test_multi_job_dry_run_contains_all_sources(mocker, queue, source, capsys):
    jobs = select_two_jobs(mocker, source, options=["--dry-run", "-q", DESTINATION])
    queue.task.side_effect = lambda task_id: jobs[1].task if task_id == jobs[1].task_id else source
    assert replay.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert [item["source_task_id"] for item in plan["sources"]] == [job.task_id for job in jobs]
    assert len(plan["tasks"]) == 2
    assert all(item["destination_queue"] == DESTINATION for item in plan["sources"])
    replay.Confirm.ask.assert_not_called()
    queue.createTask.assert_not_called()


def test_multi_job_partial_failure_monitors_created_task_with_own_runtime(mocker, queue, source):
    jobs = select_two_jobs(mocker, source, confirm=True)
    queue.task.side_effect = lambda task_id: jobs[1].task if task_id == jobs[1].task_id else source
    check = mocker.patch.object(replay, "check_inputs", return_value=19)
    queue.status.side_effect = [
        {
            "status": {
                "state": "completed",
                "runs": [{"state": "completed", "started": "2026-11-05T01:00:00Z", "resolved": end}],
            },
        }
        for end in ["2026-11-05T01:10:00Z", "2026-11-05T01:05:00Z"]
    ]
    queue.createTask.side_effect = [RuntimeError("denied"), {}]
    monitor = mocker.patch.object(replay, "wait_for_task_results", return_value={CLONE_IDS[1]: {"state": "completed"}})
    assert replay.main() == 1
    assert check.call_count == 2
    assert monitor.call_args.args[1] == [CLONE_IDS[1]]
    assert monitor.call_args.kwargs["expected_runtime"] == {CLONE_IDS[1]: 300}


def test_picker_no_wait_returns_immediately_after_submission(mocker, queue, source, capsys):
    run_picker(mocker, source, confirm=True, options=["--no-wait"])
    wait = mocker.patch.object(replay, "wait_for_task_results")
    assert replay.main() == 0
    assert json.loads(capsys.readouterr().out)["tasks"][0]["task_id"] == CLONE_IDS[0]
    wait.assert_not_called()


def test_picker_defaults_to_wait(mocker, queue, source, capsys):
    run_picker(mocker, source, confirm=True)
    assert replay.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert "elapsed_seconds" in result
    assert result["tasks"][0]["state"] == "completed"


def test_ctrl_c_leaves_submitted_jobs_running(mocker, queue, source):
    run_picker(mocker, source, confirm=True)
    mocker.patch.object(replay, "wait_for_task_results", side_effect=KeyboardInterrupt)
    assert replay.main() == 130
    queue.createTask.assert_called_once()
    queue.cancelTask.assert_not_called()


def test_q_leaves_submitted_jobs_running_and_returns_success(mocker, queue, source):
    run_picker(mocker, source, confirm=True)
    mocker.patch.object(replay, "wait_for_task_results", side_effect=replay.MonitoringStopped)
    assert replay.main() == 0
    queue.createTask.assert_called_once()
    queue.cancelTask.assert_not_called()


def test_latest_completed_source_runtime_is_used(mocker, queue, source):
    run_picker(mocker, source, confirm=True)
    queue.status.return_value = {
        "status": {
            "state": "completed",
            "runs": [
                {"state": "completed", "started": "2026-11-05T01:00:00Z", "resolved": "2026-11-05T01:10:00Z"},
                {"state": "completed", "started": "2026-11-05T02:00:00Z", "resolved": "2026-11-05T02:05:00Z"},
            ],
        },
    }
    monitor = mocker.patch.object(replay, "wait_for_task_results", return_value={CLONE_IDS[0]: {"state": "completed"}})
    assert replay.main() == 0
    assert monitor.call_args.kwargs["expected_runtime"] == 300


def test_explicit_level_three_source_rejected(mocker, queue, source, capsys):
    source["payload"]["env"]["MOZ_SCM_LEVEL"] = "3"
    assert run_cli(mocker, "--output", "json") == 1
    assert "level-1 source" in json.loads(capsys.readouterr().out)["error"]
    queue.createTask.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [[], [SOURCE_ID, "--from-pool", SOURCE_QUEUE], [SOURCE_ID, "--filter", "pdf"], ["--from-pool", SOURCE_QUEUE]],
)
def test_invalid_source_modes_fail_before_network(mocker, queue, options):
    mocker.patch.object(replay, "picker_terminal", return_value=False)
    mocker.patch("sys.argv", ["replay_tc_task.py", *options])
    with pytest.raises(SystemExit) as exc:
        replay.main()
    assert exc.value.code == 2
    replay.queue_client.assert_not_called()
