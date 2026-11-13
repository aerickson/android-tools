import json
import pytest
import replay_tc_pool as cli
import requests
from worker_health import replay_targets as targets
from worker_health.replay_sessions import create_session
from worker_health.replay_session_info import current_invocation

SOURCE = "releng-hardware/source"
DESTINATION = "releng-hardware/destination"
TASK = "A" * 22


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def arguments(mocker, *extra):
    mocker.patch("sys.argv", ["replay_tc_pool.py", *extra])
    return cli.parse_args()


def test_selection_file_deduplicates_and_persists_independently(mocker, tmp_path):
    broad = create_session(SOURCE, DESTINATION)
    ids = tmp_path / "ids.txt"
    ids.write_text(f"{TASK}\n\n{'B' * 22}\n{TASK}\n")
    args = arguments(
        mocker,
        "--from-pool",
        SOURCE,
        "--queue",
        DESTINATION,
        "--task-id",
        TASK,
        "--task-id-file",
        str(ids),
        "--attempts-per-target",
        "3",
    )
    selected = cli.select_session(args, False)
    assert selected.path != broad.path
    assert not args.submissions_enabled
    assert args.task_ids == [TASK, "B" * 22]
    config = selected.configuration()["targets"]
    assert config["attempts_per_target"] == 3
    assert not config["stop_after_success"]
    ids.unlink()
    args = arguments(mocker, "--session", str(selected.path), "--resume")
    cli.select_session(args, False)
    assert args.task_ids == config["task_ids"]
    assert args.attempts_per_target == 3
    assert not args.submissions_enabled
    command = current_invocation(args, {})
    assert "--successes-per-type" not in command
    assert "--no-stop-on-full-coverage" not in command
    args = arguments(mocker, "--from-pool", SOURCE, "--queue", DESTINATION)
    assert cli.select_session(args, False).path == broad.path


@pytest.mark.parametrize(
    "extra",
    [
        ["--task-id", TASK],
        ["--task-id", TASK, "--attempts-per-target", "0"],
        ["--attempts-per-target", "3"],
        ["--stop-after-success"],
        ["--task-id", "invalid", "--attempts-per-target", "3"],
        ["--task-id", TASK, "--attempts-per-target", "3", "--resume"],
        ["--task-id", TASK, "--attempts-per-target", "3", "--successes-per-type", "1"],
    ],
)
def test_invalid_selection(mocker, extra):
    with pytest.raises(SystemExit):
        arguments(mocker, "--from-pool", SOURCE, "--queue", DESTINATION, *extra)


def test_invalid_file_fails_before_creating_session(mocker, tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("not a task\n")
    with pytest.raises(SystemExit):
        arguments(
            mocker,
            "--from-pool",
            SOURCE,
            "--queue",
            DESTINATION,
            "--task-id-file",
            str(path),
            "--attempts-per-target",
            "3",
        )


def test_log_evidence_keeps_version_source_and_failure_lines(mocker):
    queue = mocker.Mock()
    queue.buildUrl.return_value = "https://example.test/log"
    response = mocker.MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [
        b"generic-worker v99\n[taskcluster] Worker Image Version: v115\nTEST-UNEXPECTED-FAIL | Error decoding image\n",
    ]
    mocker.patch.object(targets.requests, "get", return_value=response)
    run = targets.execution_evidence(TASK, {"runs": [{"runId": 0, "state": "failed", "workerId": "ms017"}]})["runs"][0]
    targets.collect_log_evidence(queue, TASK, run)
    assert run["verified_worker_version"] == "v115"
    assert run["expected_worker_version"] == "unknown"
    assert run["worker_version_source"]["artifact"] == "https://example.test/log"
    assert run["failure_signature"] == ["TEST-UNEXPECTED-FAIL | Error decoding image"]


def test_missing_log_keeps_evidence_unknown(mocker):
    queue = mocker.Mock()
    queue.buildUrl.return_value = "https://example.test/log"
    mocker.patch.object(targets.requests, "get", side_effect=requests.RequestException("sensitive URL"))
    run = targets.execution_evidence(TASK, {"runs": [{"runId": 0, "state": "failed"}]})["runs"][0]
    targets.collect_log_evidence(queue, TASK, run)
    assert run["failure_signature"] == "unknown"
    assert run["verified_worker_version"] == "unknown"
    assert run["log_evidence_status"] == "unavailable"
    assert "sensitive" not in json.dumps(run)


def test_empty_file_cannot_fall_back_to_broad_sweep(mocker, tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("\n")
    with pytest.raises(SystemExit):
        arguments(
            mocker,
            "--from-pool",
            SOURCE,
            "--queue",
            DESTINATION,
            "--task-id-file",
            str(path),
            "--submit-on-start",
        )


def test_generic_worker_version_and_pool_name_are_not_verified_image(mocker):
    queue = mocker.Mock()
    queue.buildUrl.return_value = "https://example.test/log"
    response = mocker.MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"[taskcluster] Worker Type (pool-v115)\ngeneric-worker version v99\n"]
    mocker.patch.object(targets.requests, "get", return_value=response)
    run = targets.execution_evidence(TASK, {"runs": [{"runId": 0, "state": "completed"}]})["runs"][0]
    targets.collect_log_evidence(queue, TASK, run)
    assert run["verified_worker_version"] == "unknown"


def test_log_sampling_is_bounded_and_marked(mocker):
    queue = mocker.Mock()
    queue.buildUrl.return_value = "https://example.test/log"
    response = mocker.MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"x" * 16, b"TEST-UNEXPECTED outside sample"]
    mocker.patch.object(targets.requests, "get", return_value=response)
    mocker.patch.object(targets, "LOG_LIMIT", 10)
    run = targets.execution_evidence(TASK, {"runs": [{"runId": 0, "state": "failed"}]})["runs"][0]
    targets.collect_log_evidence(queue, TASK, run)
    assert run["log_truncated"]
    assert run["failure_signature"] == "unknown"
