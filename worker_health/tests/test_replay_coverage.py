import sqlite3

import pytest

from worker_health import replay_coverage as coverage


POOL = "releng-hardware/gecko-t-linux-talos-2404-tc-v115"


def task(name="talos", replay=False):
    return {
        "taskQueueId": POOL,
        "metadata": {"name": ("Replay: " if replay else "") + name},
        "payload": {"env": {"MOZ_SCM_LEVEL": "1"}},
        "extra": {"replay": {"sourceTaskId": "source"}} if replay else {},
    }


def status(state):
    return {"state": state, "runs": [{"state": state, "workerId": "host", "resolved": "2026-11-05T03:00:00Z"}]}


@pytest.fixture
def ledger(mocker, tmp_path):
    mocker.patch.object(coverage, "coverage_path", return_value=tmp_path / "coverage.sqlite3")
    return tmp_path / "coverage.sqlite3"


def test_native_and_replay_jobs_match_without_merging_distinct_names():
    assert coverage.workload_name(task("talos", replay=True)) == "talos"
    assert coverage.workload_name(task("talos")) == "talos"
    assert coverage.workload_name(task("talos-swr")) != "talos"


def test_coverage_records_completion_and_caches_scan(mocker, ledger):
    queue, manager = mocker.Mock(), mocker.Mock()
    ids = mocker.patch.object(coverage, "pool_task_ids", return_value=({"passed", "failed", "active"}, 3, 0))
    queue.statuses.return_value = {
        "statuses": [
            {"taskId": "passed", "status": status("completed")},
            {"taskId": "failed", "status": status("failed")},
            {"taskId": "active", "status": status("running")},
        ],
    }
    queue.tasks.return_value = {
        "tasks": [
            {"taskId": "passed", "task": task(replay=True)},
            {"taskId": "failed", "task": task("pdf")},
            {"taskId": "active", "task": task("speedometer")},
        ],
    }
    snapshot = coverage.target_coverage(queue, manager, POOL)
    assert snapshot.badge(task()) == "★ 1"
    assert snapshot.badge(task("pdf")) == "failed"
    assert snapshot.badge(task("speedometer")) == "running"
    assert snapshot.badge(task("other")) == "unseen"
    assert "passed" in snapshot.details(task())[1]
    assert coverage.target_coverage(queue, manager, POOL).cached
    assert ids.call_count == 1
    coverage.target_coverage(queue, manager, POOL, refresh=True)
    assert ids.call_count == 2
    assert coverage.target_coverage(queue, manager, POOL).passed(task()) == 1  # Refresh does not double-count.


def test_success_persists_after_worker_history_rolls_over(mocker, ledger):
    coverage.remember_submission(POOL, "copy", task())
    coverage.remember_results(POOL, {"copy": {"state": "completed", "worker_id": "host"}})
    mocker.patch.object(coverage, "pool_task_ids", return_value=(set(), 1, 0))
    queue = mocker.Mock()
    snapshot = coverage.target_coverage(queue, mocker.Mock(), POOL, refresh=True)
    assert snapshot.passed(task()) == 1
    queue.statuses.assert_not_called()
    assert coverage.target_coverage(queue, mocker.Mock(), "other/pool", refresh=True).passed(task()) == 0


def test_pending_submission_is_rechecked_even_outside_worker_history(mocker, ledger):
    coverage.remember_submission(POOL, "copy", task())
    mocker.patch.object(coverage, "pool_task_ids", return_value=(set(), 0, 0))
    queue = mocker.Mock()
    queue.statuses.return_value = {"statuses": [{"taskId": "copy", "status": status("completed")}]}
    queue.tasks.return_value = {"tasks": [{"taskId": "copy", "task": task(replay=True)}]}
    assert coverage.target_coverage(queue, mocker.Mock(), POOL).passed(task()) == 1
    queue.statuses.assert_called_once_with({"taskIds": ["copy"]})


def test_submission_does_not_count_as_a_success_and_invalidates_cache(mocker, ledger):
    with coverage.database() as db:
        db.execute("INSERT INTO scans VALUES (?, ?, 1)", (POOL, 99999999999))
    coverage.remember_submission(POOL, "copy", task())
    with coverage.database() as db:
        snapshot = coverage.snapshot(db, POOL, True)
        assert snapshot.badge(task()) == "queued"
        assert db.execute("SELECT * FROM scans").fetchone() is None


def test_missing_or_partial_target_history_is_unknown_not_unseen(mocker, ledger):
    import taskcluster

    mocker.patch.object(coverage, "pool_task_ids", side_effect=taskcluster.exceptions.TaskclusterFailure("unavailable"))
    snapshot = coverage.target_coverage(mocker.Mock(), mocker.Mock(), POOL)
    assert snapshot.badge(task()) == "unknown"
    assert not snapshot.complete


def test_level_three_and_wrong_pool_tasks_do_not_count(mocker, ledger):
    mocker.patch.object(coverage, "pool_task_ids", return_value=({"l3", "other"}, 1, 0))
    l3, other = task(), task()
    l3["payload"]["env"]["MOZ_SCM_LEVEL"] = "3"
    other["taskQueueId"] = "other/pool"
    queue = mocker.Mock()
    queue.statuses.return_value = {
        "statuses": [{"taskId": value, "status": status("completed")} for value in ("l3", "other")],
    }
    queue.tasks.return_value = {"tasks": [{"taskId": "l3", "task": l3}, {"taskId": "other", "task": other}]}
    assert coverage.target_coverage(queue, mocker.Mock(), POOL).passed(task()) == 0


def test_local_storage_failure_does_not_block_replay(mocker):
    mocker.patch.object(coverage, "database", side_effect=sqlite3.OperationalError("unavailable"))
    coverage.remember_submission(POOL, "copy", task())
    coverage.remember_results(POOL, {})
    assert coverage.target_coverage(mocker.Mock(), mocker.Mock(), POOL).badge(task()) == "unknown"
