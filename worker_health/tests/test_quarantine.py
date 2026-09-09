from unittest.mock import MagicMock

from worker_health.quarantine import Quarantine


def make_quarantine():
    instance = Quarantine.__new__(Quarantine)
    instance.tc_queue = MagicMock()
    instance.tc_worker_manager = MagicMock()
    return instance


def test_get_quarantined_workers_structured_uses_rest_and_collects_all_pages():
    instance = make_quarantine()
    first_worker = {
        "workerGroup": "bitbar",
        "workerId": "a55-01",
        "quarantineUntil": "2027-01-01T00:00:00.000Z",
    }
    second_worker = {
        "workerGroup": "bitbar",
        "workerId": "a55-02",
        "quarantineUntil": "2027-01-02T00:00:00.000Z",
    }

    def list_workers(_provisioner, _worker_type, **kwargs):
        kwargs["paginationHandler"]({"workers": [first_worker]})
        kwargs["paginationHandler"]({"workers": [second_worker]})

    instance.tc_queue.listWorkers.side_effect = list_workers
    instance.tc_worker_manager.getWorker.side_effect = [
        {"quarantineDetails": [{"quarantineInfo": "first reason"}]},
        {"quarantineDetails": [{"quarantineInfo": "second reason"}]},
    ]

    result = instance.get_quarantined_workers_structured(
        "proj-autophone",
        "gecko-t-bitbar-gw-perf-a55",
    )

    assert result == {
        "quarantined_workers": ["a55-01", "a55-02"],
        "quarantine_info": {
            "a55-01": [{"quarantineInfo": "first reason"}],
            "a55-02": [{"quarantineInfo": "second reason"}],
        },
    }
    assert instance.tc_worker_manager.getWorker.call_count == 2
    instance.tc_worker_manager.getWorker.assert_any_call(
        "proj-autophone",
        "gecko-t-bitbar-gw-perf-a55",
        "bitbar",
        "a55-01",
    )


def test_get_quarantined_workers_structured_can_skip_details():
    instance = make_quarantine()

    def list_workers(_provisioner, _worker_type, **kwargs):
        kwargs["paginationHandler"](
            {
                "workers": [
                    {
                        "workerGroup": "bitbar",
                        "workerId": "a55-01",
                    },
                ],
            },
        )

    instance.tc_queue.listWorkers.side_effect = list_workers

    result = instance.get_quarantined_workers_structured(
        "proj-autophone",
        "gecko-t-bitbar-gw-perf-a55",
        skip_details=True,
    )

    assert result["quarantine_info"] == {"a55-01": []}
    instance.tc_worker_manager.getWorker.assert_not_called()
