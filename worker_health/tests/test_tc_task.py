import json
from copy import deepcopy

import pytest
import requests
import tc_task

TASK = "ICHaZTMDTx2YY-fM3qlVXA"
OTHER = "D7ndxwWdTgmvJWSE40Lizw"


@pytest.fixture
def queue(mocker):
    queue = mocker.Mock()
    queue.options = {}
    queue.status.return_value = {
        "status": {
            "state": "completed",
            "workerType": "test-pool",
            "runs": [
                {
                    "runId": 0,
                    "state": "failed",
                    "reasonResolved": "failed",
                    "workerId": "old-worker",
                    "started": "2026-11-09T10:00:00Z",
                    "resolved": "2026-11-09T10:00:12Z",
                },
                {"runId": 1, "state": "completed", "workerId": "new-worker"},
            ],
        },
    }
    mocker.patch.object(tc_task, "get_queue", return_value=queue)
    return queue


def test_selected_run_summary_and_log(queue, mocker, capsys):
    fetch = mocker.patch.object(tc_task, "fetch_log", return_value="hello")
    assert tc_task.main([TASK, "--run", "0", "--logs", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]
    assert (result["state"], result["workerId"], result["duration"]) == ("failed", "old-worker", "12s")
    assert result["started"] == "2026-11-09T10:00:00Z"
    assert result["reasonResolved"] == "failed"
    fetch.assert_called_once_with(queue, TASK, 0, tc_task.DEFAULT_LOG)


def test_latest_run_and_single_result_array(queue, capsys):
    assert tc_task.main([TASK, "--json"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 1
    assert results[0]["runId"] == 1
    assert results[0]["workerId"] == "new-worker"


def test_no_runs_summary(queue, capsys):
    queue.status.return_value = {"status": {"state": "unscheduled", "runs": []}}
    assert tc_task.main([TASK, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]
    assert result["runId"] is None
    assert result["state"] == "unscheduled"
    assert result["totalRuns"] == 0


def test_no_runs_logs_fail_cleanly(queue, mocker, capsys):
    queue.status.return_value = {"status": {"state": "pending", "runs": []}}
    fetch = mocker.patch.object(tc_task, "fetch_log")
    assert tc_task.main([TASK, "--logs", "--json"]) == 1
    output = capsys.readouterr()
    assert "no runs" in json.loads(output.out)[0]["log_error"]
    assert "no runs" in output.err
    fetch.assert_not_called()


def test_invalid_run(queue, capsys):
    assert tc_task.main([TASK, "--run", "5", "--json"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    assert "available runs: 0, 1" in output.err


def test_run_ids_are_not_list_indices(queue):
    status = deepcopy(queue.status.return_value)
    status["status"]["runs"][0]["runId"] = 7
    assert tc_task.summarize_task(TASK, status)["runId"] == 7
    assert tc_task.summarize_task(TASK, status, 7)["workerId"] == "old-worker"


@pytest.mark.parametrize("partial", [False, True])
def test_lookup_failures(queue, capsys, partial):
    queue.status.side_effect = [
        tc_task.taskcluster.exceptions.TaskclusterFailure("lookup failed"),
        queue.status.return_value,
    ]
    args = [TASK, OTHER, "--json"] if partial else [TASK, "--json"]
    assert tc_task.main(args) == 1
    output = capsys.readouterr()
    results = json.loads(output.out)
    assert [row["taskId"] for row in results] == ([OTHER] if partial else [])
    assert "lookup failed" in output.err


def test_log_failure_keeps_summary(queue, mocker, capsys):
    mocker.patch.object(tc_task, "fetch_log", side_effect=RuntimeError("log unavailable"))
    assert tc_task.main([TASK, "--logs", "--json"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)[0]["log_error"] == "log unavailable"
    assert "log unavailable" in output.err


@pytest.mark.parametrize("options", [["--grep", "["], ["--run", "-1"], ["bad-id"]])
def test_bad_arguments_fail_before_network(queue, options):
    args = options if options == ["bad-id"] else [TASK, *options]
    with pytest.raises(SystemExit) as exc:
        tc_task.main(args)
    assert exc.value.code == 2
    tc_task.get_queue.assert_not_called()
    queue.status.assert_not_called()


@pytest.mark.parametrize(
    "regex, expected",
    [("ERROR|FAIL", "ERROR: broken\nFAIL: test"), ("", "ok\nERROR: broken\nFAIL: test")],
)
def test_grep_implies_logs(queue, mocker, capsys, regex, expected):
    mocker.patch.object(tc_task, "fetch_log", return_value="ok\nERROR: broken\nFAIL: test\n")
    assert tc_task.main([TASK, "--grep", regex, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["log"] == expected


@pytest.mark.parametrize("suffix, run", [("", None), ("/runs/0", 0), ("/runs/1/logs", 1), ("/artifacts", None)])
def test_task_urls(suffix, run):
    assert tc_task.task_reference(f"{tc_task.TC_ROOT_URL}/tasks/{TASK}{suffix}") == (TASK, run)


@pytest.mark.parametrize(
    "url",
    [
        f"https://example.com/tasks/{TASK}",
        f"http://firefox-ci-tc.services.mozilla.com/tasks/{TASK}",
        f"{tc_task.TC_ROOT_URL}/tasks/{TASK}/runs/-1",
        f"{tc_task.TC_ROOT_URL}/tasks/{TASK}/invalid",
    ],
)
def test_invalid_task_urls(url):
    with pytest.raises(tc_task.argparse.ArgumentTypeError):
        tc_task.task_reference(url)


@pytest.mark.parametrize("override, expected", [([], 0), (["--run", "1"], 1)])
def test_url_run_selection(queue, capsys, override, expected):
    assert tc_task.main([f"{tc_task.TC_ROOT_URL}/tasks/{TASK}/runs/0", *override, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["runId"] == expected


def test_auth_and_custom_artifact(queue, mocker):
    fetch = mocker.patch.object(tc_task, "fetch_log", return_value="private log")
    assert tc_task.main([TASK, "--auth", "--artifact", "private/log.txt"]) == 0
    tc_task.get_queue.assert_called_once_with(True)
    fetch.assert_called_once_with(queue, TASK, 1, "private/log.txt")


def test_anonymous_does_not_read_token(mocker):
    read = mocker.patch.object(tc_task.Path, "read_text", side_effect=AssertionError("Should not read token"))
    factory = mocker.patch.object(tc_task.taskcluster, "Queue")
    tc_task.get_queue()
    assert "credentials" not in factory.call_args.args[0]
    read.assert_not_called()


def test_load_credentials(mocker, tmp_path):
    mocker.patch.object(tc_task.Path, "home", return_value=tmp_path)
    (tmp_path / ".tc_token").write_text(json.dumps({"clientId": "test-client", "accessToken": "test-token"}))
    factory = mocker.patch.object(tc_task.taskcluster, "Queue")
    tc_task.get_queue(True)
    assert factory.call_args.args[0]["credentials"] == {"clientId": "test-client", "accessToken": "test-token"}


@pytest.mark.parametrize("content", [None, "not json", "null", "[]", "{}", '{"clientId": "", "accessToken": 123}'])
def test_invalid_credentials(mocker, tmp_path, content):
    mocker.patch.object(tc_task.Path, "home", return_value=tmp_path)
    if content is not None:
        (tmp_path / ".tc_token").write_text(content)
    with pytest.raises(ValueError, match="Cannot read valid Taskcluster credentials"):
        tc_task.get_queue(True)


def test_credential_failure_json(queue, capsys):
    tc_task.get_queue.side_effect = ValueError("Cannot read valid Taskcluster credentials from ~/.tc_token")
    assert tc_task.main([TASK, "--auth", "--json"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    assert "~/.tc_token" in output.err


@pytest.mark.parametrize("authenticated", [False, True])
def test_artifact_url_signing(mocker, authenticated):
    queue = mocker.Mock()
    queue.options = {"credentials": {"clientId": "client", "accessToken": "token"}} if authenticated else {}
    get = mocker.patch.object(tc_task.requests, "get")
    response = get.return_value.__enter__.return_value
    response.text = "log contents"
    assert tc_task.fetch_log(queue, TASK, 0) == "log contents"
    builder = queue.buildSignedUrl if authenticated else queue.buildUrl
    builder.assert_called_once_with("getArtifact", TASK, "0", tc_task.DEFAULT_LOG)
    get.assert_called_once_with(builder.return_value, allow_redirects=True, timeout=60)


@pytest.mark.parametrize("status", [None, 403])
def test_artifact_errors_do_not_expose_signed_urls(mocker, status):
    queue = mocker.Mock()
    queue.options = {"credentials": {"clientId": "client", "accessToken": "token"}}
    response = None
    if status is not None:
        response = requests.Response()
        response.status_code = status
    mocker.patch.object(
        tc_task.requests,
        "get",
        side_effect=requests.HTTPError("https://example.com/?bewit=SECRET", response=response),
    )
    with pytest.raises(RuntimeError) as exc:
        tc_task.fetch_log(queue, TASK, 0)
    assert "SECRET" not in str(exc.value)
    assert ("HTTP 403" if status else "HTTPError") in str(exc.value)
