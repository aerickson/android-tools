import curses

import pytest
import taskcluster

from worker_health.replay_picker import (
    Discovery,
    ReplayJob,
    _picker_screen,
    discover_jobs,
    filtered_jobs,
    level_one_problem,
    missing_scopes,
)


POOL = "releng-hardware/gecko-t-linux-talos-2404"


def task(name="talos", level="1", scopes=None):
    return {
        "taskQueueId": POOL,
        "created": "2026-11-05T00:00:00.000Z",
        "metadata": {"name": name},
        "payload": {"env": {"MOZ_SCM_LEVEL": level}},
        "scopes": scopes or [],
    }


def status(task_id, state="completed", resolved="2026-11-05T01:10:00.000Z"):
    return {
        "taskId": task_id,
        "status": {
            "state": state,
            "runs": [{"state": state, "workerId": "host", "started": "2026-11-05T01:00:00.000Z", "resolved": resolved}],
        },
    }


def job(task_id="a", name="talos"):
    return ReplayJob(task_id, task(name), "2026-11-05T01:10:00.000Z", 600, "host")


@pytest.mark.parametrize("level", ["2", "3", "4", "unknown"])
def test_non_level_one_rejected(level):
    assert level_one_problem(task(level=level))


@pytest.mark.parametrize(
    "scope",
    [
        "generic-worker:cache:gecko-level-3-pip",
        "secrets:get:project/perftest/gecko/level-3/login",
        "secrets:get:*",
        "generic-worker:cache:gecko-level-*",
        "*",
    ],
)
def test_level_one_environment_cannot_hide_other_levels(scope):
    assert level_one_problem(task(scopes=[scope]))


def test_unknown_level_rejected_but_level_one_resource_marker_accepted():
    source = task()
    source["payload"]["env"] = {}
    assert level_one_problem(source)
    source["scopes"] = ["generic-worker:cache:gecko-level-1-pip"]
    assert level_one_problem(source) is None
    source["payload"]["mounts"] = [{"cacheName": "gecko-level-3-pip"}]
    assert level_one_problem(source)


def test_scope_checks_include_submission_and_accept_higher_priority_grant():
    source = task(scopes=["generic-worker:cache:gecko-level-1-pip"])
    granted = [
        "generic-worker:cache:gecko-level-1-*",
        "queue:create-task:project:none",
        "queue:scheduler-id:-",
        f"queue:create-task:high:{POOL}",
    ]
    assert missing_scopes(source, POOL, granted) == []
    assert missing_scopes(source, "other/pool", granted) == ["queue:create-task:lowest:other/pool"]
    assert missing_scopes(source, POOL, ["queue:create-task:*", "queue:scheduler-id:*"]) == source["scopes"]


def test_discovery_collects_all_worker_histories_and_batches_unique_ids(mocker):
    manager, queue = mocker.Mock(), mocker.Mock()
    manager.listWorkers.side_effect = [
        {
            "workers": [{"workerGroup": "g", "workerId": "one", "latestTask": {"taskId": "a"}}],
            "continuationToken": "next",
        },
        {"workers": [{"workerGroup": "g", "workerId": "two"}]},
    ]
    manager.getWorker.side_effect = lambda p, t, g, w: {
        "recentTasks": [{"taskId": value} for value in (["a", "b", "l3"] if w == "one" else ["b", "pending", "other"])],
    }
    queue.statuses.return_value = {
        "statuses": [
            status("a"),
            status("b", resolved="2026-11-05T01:20:00.000Z"),
            status("l3"),
            status("pending", "running"),
            status("other"),
        ],
    }
    other = task()
    other["taskQueueId"] = "other/pool"
    queue.tasks.return_value = {
        "tasks": [
            {"taskId": "a", "task": task()},
            {"taskId": "b", "task": task()},
            {"taskId": "l3", "task": task(level="3")},
            {"taskId": "other", "task": other},
        ],
    }
    result = discover_jobs(queue, manager, POOL)
    assert result.workers == 2
    assert result.task_ids == 5
    assert result.eligible_tasks == 2
    assert len(result.jobs) == 1
    assert result.jobs[0].task_id == "b"
    assert result.jobs[0].observed == 2
    assert result.jobs[0].seconds == 1200
    assert queue.statuses.call_args.args[0]["taskIds"] == ["a", "b", "l3", "other", "pending"]
    assert "pending" not in queue.tasks.call_args.args[0]["taskIds"]
    manager.listWorkers.assert_called_with("releng-hardware", "gecko-t-linux-talos-2404", continuationToken="next")


def test_scope_variants_are_separate_workloads(mocker):
    manager, queue = mocker.Mock(), mocker.Mock()
    manager.listWorkers.return_value = {"workers": [{"workerGroup": "g", "workerId": "one"}]}
    manager.getWorker.return_value = {"recentTasks": [{"taskId": "a"}, {"taskId": "b"}]}
    queue.statuses.return_value = {"statuses": [status("a"), status("b")]}
    queue.tasks.return_value = {
        "tasks": [
            {"taskId": "a", "task": task()},
            {"taskId": "b", "task": task(scopes=["secrets:get:project/perftest/gecko/level-1/login"])},
        ],
    }
    assert len(discover_jobs(queue, manager, POOL).jobs) == 2


def test_expired_worker_history_keeps_latest_task_and_reports_partial_results(mocker, caplog):
    manager, queue = mocker.Mock(), mocker.Mock()
    manager.listWorkers.return_value = {
        "workers": [{"workerGroup": "g", "workerId": "gone", "latestTask": {"taskId": "a"}}],
    }
    manager.getWorker.side_effect = taskcluster.exceptions.TaskclusterRestFailure(
        "Worker not found.\n\n---\n* method: getWorker\n* statusCode: 404",
        None,
        status_code=404,
    )
    queue.statuses.return_value = {"statuses": []}
    result = discover_jobs(queue, manager, POOL)
    assert result.unavailable_workers == 1
    assert result.task_ids == 1
    assert result.jobs == []
    assert caplog.messages == ["Skipping history for g/gone: worker no longer available (HTTP 404)"]


def test_empty_pool_does_not_fetch_tasks(mocker):
    manager, queue = mocker.Mock(), mocker.Mock()
    manager.listWorkers.return_value = {"workers": []}
    assert discover_jobs(queue, manager, POOL).jobs == []
    queue.tasks.assert_not_called()
    queue.statuses.assert_not_called()


def test_batch_pagination_and_multiple_batches(mocker):
    from worker_health.replay_picker import batch_results

    method = mocker.Mock(
        side_effect=[{"tasks": ["first"], "continuationToken": "next"}, {"tasks": ["second"]}, {"tasks": ["third"]}],
    )
    assert list(batch_results(method, list(range(101)), "tasks")) == ["first", "second", "third"]
    assert method.call_args_list[1].kwargs == {"continuationToken": "next"}
    assert method.call_args_list[2].args[0]["taskIds"] == [100]


def test_picker_filtering_navigation_and_cancellation(mocker):
    jobs = [job("a", "Talos PDF"), job("b", "Speedometer")]
    assert filtered_jobs(jobs, "PDF HOST") == [jobs[0]]
    assert filtered_jobs(jobs, "missing") == []
    discovery = Discovery(jobs, 2, 40, 4, 0)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (24, 120)
    screen.get_wch.side_effect = [curses.KEY_DOWN, "\n"]
    assert _picker_screen(screen, discovery, POOL, POOL, "") == [jobs[1]]
    screen.get_wch.side_effect = ["p", "d", "f", "\n"]
    assert _picker_screen(screen, discovery, POOL, POOL, "") == [jobs[0]]
    screen.get_wch.side_effect = ["\x1b"]
    assert _picker_screen(screen, discovery, POOL, POOL, "") is None


def test_picker_handles_resize_and_no_search_results(mocker):
    discovery = Discovery([job()], 1, 1, 1, 0)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (10, 40)
    screen.getch.return_value = 27
    assert _picker_screen(screen, discovery, POOL, POOL, "") is None
    screen.getmaxyx.return_value = (24, 120)
    screen.get_wch.side_effect = ["\n", "\x1b"]
    assert _picker_screen(screen, discovery, POOL, POOL, "nomatch") is None


def test_picker_disables_missing_permissions_but_keeps_jobs_inspectable(mocker):
    jobs = [job("a", "Needs login"), job("b", "Public job")]
    jobs[0].task["scopes"] = ["secrets:get:project/releng/gecko/build/level-1/conditioned-profiles"]
    discovery = Discovery(jobs, 2, 40, 4, 0)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 160)
    screen.get_wch.side_effect = ["\n", curses.KEY_DOWN, "\n"]
    granted = ["queue:create-task:*", "queue:scheduler-id:*"]
    assert _picker_screen(screen, discovery, POOL, POOL, "", granted) == [jobs[1]]
    text = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert "conditioned-profiles" in text
    assert "Selection disabled" in text


def test_picker_allows_preview_without_missing_permissions(mocker):
    discovery = Discovery([job()], 1, 20, 1, 0)
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (24, 120)
    screen.get_wch.return_value = "\n"
    assert _picker_screen(screen, discovery, POOL, POOL, "", [], preview=True) == [discovery.jobs[0]]


def test_picker_ctrl_r_returns_refresh_request_with_filter(mocker):
    from worker_health.replay_picker import RefreshPicker

    screen = mocker.Mock()
    screen.getmaxyx.return_value = (24, 120)
    screen.get_wch.return_value = "\x12"
    assert _picker_screen(screen, Discovery([job()], 1, 1, 1, 0), POOL, POOL, "pdf") == RefreshPicker("pdf")


def test_picker_selects_across_filters_and_submits_when_no_rows_match(mocker):
    jobs = [job("a", "Talos PDF"), job("b", "Speedometer")]
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 160)
    screen.get_wch.side_effect = ["\t", "s", "p", "e", "\t", "z", "\n"]
    assert _picker_screen(screen, Discovery(jobs, 2, 40, 4, 0), POOL, POOL, "") == jobs
    text = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert "2 selected" in text
    assert "[x]" in text


def test_picker_toggle_off_and_spaces_still_filter(mocker):
    jobs = [job("a", "Talos PDF"), job("b", "Speedometer")]
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 160)
    screen.get_wch.side_effect = ["\t", "\t", "t", "a", "l", "o", "s", " ", "p", "\n"]
    assert _picker_screen(screen, Discovery(jobs, 2, 40, 4, 0), POOL, POOL, "") == [jobs[0]]
    text = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert "Filter: talos p" in text
    assert "0 selected" in text


def test_picker_refresh_carries_selection(mocker):
    from worker_health.replay_picker import RefreshPicker

    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 160)
    screen.get_wch.side_effect = ["\t", "p", "\x12"]
    assert _picker_screen(screen, Discovery([job("a")], 1, 1, 1, 0), POOL, POOL, "") == RefreshPicker("p", ("a",))


def test_picker_rechecks_permissions_on_restored_hidden_selections(mocker):
    jobs = [job("a", "Private"), job("b", "Public")]
    jobs[0].task["scopes"] = ["secrets:get:private"]
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 160)
    # Enter must reject a hidden selected job whose permissions changed on refresh.
    screen.get_wch.side_effect = ["\n", "\x1b"]
    granted = ["queue:create-task:*", "queue:scheduler-id:*"]
    assert (
        _picker_screen(
            screen,
            Discovery(jobs, 2, 40, 4, 0),
            POOL,
            POOL,
            "Public",
            granted,
            selected_ids=("a",),
        )
        is None
    )
    text = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert "Selection disabled" in text


def test_picker_cannot_mark_denied_jobs_but_can_remove_restored_selection(mocker):
    jobs = [job("a", "Private"), job("b", "Public")]
    jobs[0].task["scopes"] = ["secrets:get:private"]
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 160)
    granted = ["queue:create-task:*", "queue:scheduler-id:*"]
    for selected_ids in [(), ("a",)]:
        screen.get_wch.side_effect = ["\t", curses.KEY_DOWN, "\t", "\n"]
        assert _picker_screen(
            screen,
            Discovery(jobs, 2, 40, 4, 0),
            POOL,
            POOL,
            "",
            granted,
            selected_ids=selected_ids,
        ) == [jobs[1]]


def test_copy_picker_shows_target_badges_and_filters_unseen_jobs(mocker):
    from worker_health.replay_coverage import TargetCoverage

    jobs = [job("a", "talos"), job("b", "pdf")]
    coverage = TargetCoverage(
        {
            "talos": [
                {
                    "passed": 1,
                    "state": "completed",
                    "task_id": "copy",
                    "worker": "target-host",
                    "observed": "2026-11-05T00:00:00Z",
                },
            ],
        },
    )
    assert filtered_jobs(jobs, "passed", coverage) == [jobs[0]]
    assert filtered_jobs(jobs, "unseen", coverage) == [jobs[1]]
    screen = mocker.Mock()
    screen.getmaxyx.return_value = (30, 180)
    screen.get_wch.return_value = "\x1b"
    assert _picker_screen(screen, Discovery(jobs, 2, 40, 4, 0), POOL, "other/pool", "", coverage=coverage) is None
    text = "\n".join(call.args[2] for call in screen.addnstr.call_args_list)
    assert "★ 1" in text
    assert "unseen" in text
    assert "Target: 1/2 job types succeeded" in text
    assert "Last target: copy" in text
