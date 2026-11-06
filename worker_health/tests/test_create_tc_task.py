import json
import logging

import pytest

import create_tc_task
from create_tc_task import build_payload, default_bash_command


def test_default_bash_command_uses_timeout_minus_buffer():
    assert default_bash_command(70) == 'for ((i=1;i<=60;i++)); do echo "$i"; sleep 1; done'


def test_build_docker_worker_payload():
    payload = build_payload(
        "docker-worker",
        "echo hello",
        90,
        {"FOO": "bar"},
        "ubuntu:24.04",
    )

    assert payload == {
        "image": "ubuntu:24.04",
        "command": ["/bin/bash", "-lc", "echo hello"],
        "maxRunTime": 90,
        "env": {"FOO": "bar"},
    }


def test_build_python_script_command_passes_shell_style_arguments(tmp_path):
    script = tmp_path / "benchmark.py"
    script.write_text("#!/usr/bin/env python3\nprint('ok')\n")

    command = create_tc_task._build_command_from_script(
        script,
        "--configuration bitbar-docker-with-robustcheckout --output-dir 'clone results'",
    )

    assert command.startswith(
        "python3 - --configuration bitbar-docker-with-robustcheckout --output-dir 'clone results' << 'SCRIPT_EOF'",
    )
    assert "print('ok')" in command


def test_build_shell_script_command_rejects_arguments(tmp_path):
    script = tmp_path / "benchmark.sh"
    script.write_text("#!/bin/bash\necho ok\n")

    try:
        create_tc_task._build_command_from_script(script, "--configuration test")
    except ValueError as exc:
        assert str(exc) == "--script-args is supported only for non-shell --script-file inputs"
    else:
        raise AssertionError("expected shell script arguments to be rejected")


def test_wait_for_tasks_reports_result_and_worker(mocker):
    tcclient = object.__new__(create_tc_task.TCClient)
    tcclient.queue_object = mocker.Mock()
    tcclient.queue_object.status.return_value = {
        "status": {
            "state": "completed",
            "runs": [
                {
                    "state": "completed",
                    "reasonResolved": "completed",
                    "workerId": "worker-194",
                },
            ],
        },
    }

    details = tcclient.wait_for_tasks(["task-1"], poll_interval=0.01)

    assert details == {
        "task-1": {
            "state": "completed",
            "result": "completed",
            "worker_id": "worker-194",
        },
    }


def task_status(state, worker=None, reason=None):
    run = {"state": state}
    if worker:
        run["workerId"] = worker
    if reason:
        run["reasonResolved"] = reason
    return {"status": {"state": state, "runs": [run] if worker or reason else []}}


def test_wait_retries_errors_and_reports_worker_changes(mocker):
    client = object.__new__(create_tc_task.TCClient)
    client.queue_object = mocker.Mock()
    client.queue_object.status.side_effect = [
        RuntimeError("temporary API failure"),
        task_status("running", "worker-a"),
        task_status("running", "worker-a"),
        task_status("running", "worker-b"),
        task_status("completed", "worker-b", "completed"),
    ]
    sleep = mocker.patch.object(create_tc_task.time, "sleep")
    updates = []

    def progress(states, final, changed):
        updates.append((states["task-1"].copy(), final.copy(), changed.copy()))

    result = client.wait_for_tasks(["task-1"], 0.1, progress)

    assert [update[0]["worker_id"] for update in updates] == ["worker-a", "worker-b", "worker-b"]
    assert all(update[2] == ["task-1"] for update in updates)
    assert updates[-1][1] == result
    assert sleep.call_count == 4


@pytest.mark.parametrize("state", ["completed", "failed", "exception"])
def test_wait_terminal_state_without_runs(mocker, state):
    client = object.__new__(create_tc_task.TCClient)
    client.queue_object = mocker.Mock()
    client.queue_object.status.return_value = task_status(state)

    assert client.wait_for_tasks(["task-1"], 0.1) == {
        "task-1": {"state": state, "result": state, "worker_id": "-"},
    }


@pytest.fixture
def cli_client(mocker):
    client = object.__new__(create_tc_task.TCClient)
    client.queue_object = mocker.Mock()
    client.queue_object.status.return_value = task_status("completed", "worker-1", "completed")
    mocker.patch.object(client, "create_task", side_effect=["task-1", "task-2"])
    mocker.patch.object(create_tc_task, "TCClient", return_value=client)
    mocker.patch.object(create_tc_task, "is_interactive_terminal", return_value=False)
    mocker.patch.object(create_tc_task.time, "sleep")
    return client


def run_cli(mocker, *options):
    mocker.patch("sys.argv", ["create_tc_task.py", "-q", "test/pool", "-c", "2", "--wait", *options])
    create_tc_task.main()


def test_cli_json_stdout_is_parseable_and_ordered(mocker, cli_client, capsys):
    run_cli(mocker, "--output", "json")
    captured = capsys.readouterr()
    tasks = json.loads(captured.out)["tasks"]
    assert tasks == [
        {
            "task_id": task_id,
            "state": "completed",
            "result": "completed",
            "worker_id": "worker-1",
            "url": f"https://firefox-ci-tc.services.mozilla.com/tasks/{task_id}",
        }
        for task_id in ("task-1", "task-2")
    ]


@pytest.mark.parametrize("interactive", [False, True])
def test_cli_table_shows_results_and_summary(mocker, cli_client, capsys, interactive):
    mocker.patch.object(create_tc_task, "is_interactive_terminal", return_value=interactive)
    run_cli(mocker)
    output = capsys.readouterr().out
    assert "Task results" in output
    assert "task-1" in output
    assert "task-2" in output
    assert "worker-1" in output
    assert "Summary: 2 completed, 0 failed, 0 exception" in output


def test_cli_log_reports_transitions_and_summary(mocker, cli_client, caplog, capsys):
    cli_client.queue_object.status.side_effect = [
        task_status("pending"),
        task_status("running", "worker-2"),
        task_status("completed", "worker-1", "completed"),
        task_status("completed", "worker-2", "completed"),
    ]
    with caplog.at_level(logging.INFO):
        run_cli(mocker, "--output", "log")
    assert "pending" in caplog.text
    assert "running" in caplog.text
    assert "worker=worker-2" in caplog.text
    assert "Summary: 2 completed, 0 failed, 0 exception" in caplog.text
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("state, reason", [("failed", "failed"), ("exception", "worker-shutdown")])
def test_cli_unsuccessful_tasks_return_failure_with_results(mocker, cli_client, capsys, state, reason):
    cli_client.queue_object.status.return_value = task_status(state, "worker-1", reason)
    with pytest.raises(SystemExit) as exc:
        run_cli(mocker, "--output", "json")
    assert exc.value.code == 1
    tasks = json.loads(capsys.readouterr().out)["tasks"]
    assert len(tasks) == 2
    assert all(task["result"] == reason and task["state"] == state for task in tasks)


@pytest.mark.parametrize("created", [[None, None], ["task-1", None]])
def test_cli_submission_failures_return_failure(mocker, cli_client, capsys, created):
    cli_client.create_task.side_effect = created
    with pytest.raises(SystemExit) as exc:
        run_cli(mocker, "--output", "json")
    assert exc.value.code == 1
    tasks = json.loads(capsys.readouterr().out)["tasks"]
    assert len(tasks) == sum(task_id is not None for task_id in created)


def test_cli_dry_run_does_not_poll(mocker, cli_client, capsys):
    cli_client.create_task.side_effect = [None, None]
    run_cli(mocker, "--output", "json", "--dry-run")
    assert json.loads(capsys.readouterr().out) == {"tasks": []}
    cli_client.queue_object.status.assert_not_called()


def test_task_links_and_status_colors():
    task_id = create_tc_task._task_id_text("task-1")
    assert task_id.spans[0].style == "link https://firefox-ci-tc.services.mozilla.com/tasks/task-1"
    assert create_tc_task._task_state_text("failed").style == "red"


@pytest.mark.parametrize("seconds, expected", [(0, "00:00:00"), (65, "00:01:05"), (3661, "01:01:01")])
def test_elapsed_display(seconds, expected):
    assert create_tc_task.format_elapsed(seconds) == expected


def test_live_timer_refreshes_without_state_changes(mocker, cli_client):
    clock = mocker.patch.object(create_tc_task.time, "monotonic", return_value=100.0)
    mocker.patch.object(create_tc_task, "is_interactive_terminal", return_value=True)
    live = mocker.patch.object(create_tc_task, "Live")
    titles = []

    def wait(task_ids, poll_interval, progress_callback):
        render = live.call_args.kwargs["get_renderable"]
        titles.append(render().title)
        clock.return_value = 165.0
        titles.append(render().title)
        return {task_id: {"state": "completed", "result": "completed", "worker_id": "host"} for task_id in task_ids}

    cli_client.wait_for_tasks = wait
    create_tc_task.wait_for_task_results(cli_client, ["task-1"])
    assert "Elapsed 00:00:00" in titles[0]
    assert "Elapsed 00:01:05" in titles[1]
    clock.return_value = 200.0
    assert live.call_args.kwargs["get_renderable"]().caption == "Elapsed 00:01:05"


def test_json_wait_reports_elapsed_seconds(mocker, cli_client, capsys):
    mocker.patch.object(create_tc_task.time, "monotonic", side_effect=[100.0, 165.5])
    create_tc_task.wait_for_task_results(cli_client, ["task-1"], output="json")
    assert json.loads(capsys.readouterr().out)["elapsed_seconds"] == 65.5


@pytest.mark.parametrize(
    "state, started, now, expected",
    [
        ("pending", None, 1000, "~00:08:00 once running"),
        ("running", "1970-01-01T00:16:40Z", 1060, "~00:07:00 left"),
        ("running", "1970-01-01T00:16:40Z", 1500, "over estimate by 00:00:20"),
        ("running", None, 1000, "awaiting start time"),
        ("completed", None, 1000, "finished"),
    ],
)
def test_eta_uses_actual_run_start(state, started, now, expected):
    assert create_tc_task.eta_label({"state": state, "started": started}, 480, now) == expected


def test_batch_progress_uses_each_tasks_runtime():
    from rich.console import Console

    states = {task_id: {"state": "pending", "worker_id": "-"} for task_id in ("fast", "slow", "unknown")}
    table = create_tc_task.render_task_progress(
        list(states),
        states,
        expected_runtime={"fast": 60, "slow": 600, "unknown": None},
    )
    console = Console(width=160)
    with console.capture() as capture:
        console.print(table)
    rows = capture.get().splitlines()
    assert any("fast" in row and "00:01:00 once running" in row for row in rows)
    assert any("slow" in row and "00:10:00 once running" in row for row in rows)
    assert any("unknown" in row and "unknown" in row.split("unknown", 1)[1] for row in rows)
    assert "Total estimated task time: ~00:11:00" in table.caption
    assert "1 missing estimates" in table.caption


@pytest.mark.parametrize(
    "task_ids, estimates, expected",
    [
        (["a", "b"], {"a": 60, "b": 600, "unsubmitted": 900}, "~00:11:00"),
        (["a", "b", "c"], 60, "~00:03:00"),
        (["a", "b"], {"a": 0, "b": 0}, "~00:00:00"),
        (["a", "b"], {"a": None}, "unknown (2 missing estimates)"),
    ],
)
def test_batch_total_counts_only_submitted_tasks(task_ids, estimates, expected):
    assert expected in create_tc_task.total_runtime_label(task_ids, estimates)


def test_total_omitted_without_estimates_or_for_single_task():
    assert create_tc_task.total_runtime_label(["a", "b"], None) is None
    assert create_tc_task.total_runtime_label(["a"], 60) is None


def test_batch_log_and_json_use_per_task_estimates(mocker, caplog, capsys):
    client = mocker.Mock()
    states = {task_id: {"state": "pending", "result": "-", "worker_id": "-"} for task_id in ("fast", "slow")}

    def wait(task_ids, poll_interval, progress_callback):
        if progress_callback:
            progress_callback(states, {}, task_ids)
        return {task_id: {**details, "state": "completed"} for task_id, details in states.items()}

    client.wait_for_tasks.side_effect = wait
    estimates = {"fast": 60, "slow": 600}
    with caplog.at_level(logging.INFO):
        create_tc_task.wait_for_task_results(client, list(states), output="log", expected_runtime=estimates)
    assert any("task=fast" in line and "00:01:00 once running" in line for line in caplog.messages)
    assert any("task=slow" in line and "00:10:00 once running" in line for line in caplog.messages)
    assert "Total estimated task time: ~00:11:00" in caplog.text
    create_tc_task.wait_for_task_results(client, list(states), output="json", expected_runtime=estimates)
    assert json.loads(capsys.readouterr().out)["expected_runtime_seconds"] == estimates


def test_monitor_provides_latest_run_start_for_eta(mocker):
    client = create_tc_task.TaskMonitor(mocker.Mock())
    client.queue_object.status.return_value = {
        "status": {
            "state": "completed",
            "runs": [
                {"started": "2026-11-05T01:00:00Z", "workerId": "old"},
                {"started": "2026-11-05T02:00:00Z", "workerId": "new"},
            ],
        },
    }
    updates = mocker.Mock()
    client.wait_for_tasks(["task-1"], 1, updates)
    assert updates.call_args.args[0]["task-1"]["started"] == "2026-11-05T02:00:00Z"
