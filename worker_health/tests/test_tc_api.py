import pytest

from worker_health import tc_api


def test_get_tc_workers_collects_all_pages(mocker):
    first_worker = {
        "workerId": "a55-01",
        "lastDateActive": "2026-09-08T01:00:00.000Z",
    }
    second_worker = {
        "workerId": "a55-02",
        "lastDateActive": "2026-09-08T02:00:00.000Z",
    }

    def list_workers(provisioner, worker_type, **kwargs):
        assert provisioner == "proj-autophone"
        assert worker_type == "gecko-t-bitbar-gw-perf-a55"
        kwargs["paginationHandler"]({"workers": [first_worker]})
        kwargs["paginationHandler"]({"workers": [second_worker]})

    list_workers_mock = mocker.patch.object(
        tc_api.worker_manager,
        "listWorkers",
        side_effect=list_workers,
    )

    workers = tc_api.get_tc_workers(
        "proj-autophone",
        "gecko-t-bitbar-gw-perf-a55",
    )

    assert workers == [first_worker, second_worker]
    assert list_workers_mock.call_args.kwargs["paginationLimit"] == 1000


def test_get_tc_workers_wraps_taskcluster_errors(mocker):
    mocker.patch.object(
        tc_api.worker_manager,
        "listWorkers",
        side_effect=RuntimeError("service unavailable"),
    )

    with pytest.raises(
        tc_api.TaskclusterWorkerError,
        match=(
            "Error getting workers from Taskcluster REST API for "
            "proj-autophone/gecko-t-bitbar-gw-perf-a55: service unavailable"
        ),
    ):
        tc_api.get_tc_workers(
            "proj-autophone",
            "gecko-t-bitbar-gw-perf-a55",
        )


def test_get_tc_workers_rejects_malformed_response(mocker):
    def list_workers(_provisioner, _worker_type, **kwargs):
        kwargs["paginationHandler"]({})

    mocker.patch.object(
        tc_api.worker_manager,
        "listWorkers",
        side_effect=list_workers,
    )

    with pytest.raises(tc_api.TaskclusterWorkerError, match="does not contain a workers list"):
        tc_api.get_tc_workers(
            "proj-autophone",
            "gecko-t-bitbar-gw-perf-a55",
        )
