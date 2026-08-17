from worker_health import status


def test_get_idle_hosts_preserves_requested_order(mocker):
    worker_status = object.__new__(status.Status)
    mocker.patch.object(worker_status, "get_hosts_running_jobs", return_value=["worker-2"])

    assert worker_status.get_idle_hosts(["worker-3", "worker-2", "worker-1"]) == ["worker-3", "worker-1"]


def test_wait_for_idle_hosts_refreshes_job_status(mocker):
    worker_status = object.__new__(status.Status)
    get_busy = mocker.patch.object(
        worker_status,
        "get_hosts_running_jobs",
        side_effect=[["worker-1", "worker-2"], ["worker-2"]],
    )
    sleep = mocker.patch("worker_health.status.time.sleep")

    assert worker_status.wait_for_idle_hosts(["worker-1", "worker-2"], sleep_time=1, show_indicator=False) == [
        "worker-1",
    ]
    assert get_busy.call_count == 2
    sleep.assert_called_once_with(1)


def test_wait_until_no_jobs_running_refreshes_job_status(mocker):
    worker_status = object.__new__(status.Status)
    get_busy = mocker.patch.object(
        worker_status,
        "get_hosts_running_jobs",
        side_effect=[["worker-1"], []],
    )
    sleep = mocker.patch("worker_health.status.time.sleep")

    worker_status.wait_until_no_jobs_running(["worker-1"], sleep_seconds=1, show_indicator=False)

    assert get_busy.call_count == 2
    sleep.assert_called_once_with(1)
