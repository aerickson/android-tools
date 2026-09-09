import taskcluster

TC_ROOT_URL = "https://firefox-ci-tc.services.mozilla.com"
WORKERS_PAGE_LIMIT = 1000


class TaskclusterWorkerError(RuntimeError):
    """Raised when Taskcluster worker data cannot be collected."""


worker_manager = taskcluster.WorkerManager({"rootUrl": TC_ROOT_URL})


def get_tc_workers(provisioner, worker_type):
    """Return every active worker in a Taskcluster worker pool via REST."""
    workers = []

    def collect_page(response):
        page_workers = response.get("workers")
        if not isinstance(page_workers, list):
            raise TaskclusterWorkerError("Taskcluster response does not contain a workers list")
        workers.extend(page_workers)

    try:
        worker_manager.listWorkers(
            provisioner,
            worker_type,
            paginationLimit=WORKERS_PAGE_LIMIT,
            paginationHandler=collect_page,
        )
    except Exception as exc:
        raise TaskclusterWorkerError(
            f"Error getting workers from Taskcluster REST API for {provisioner}/{worker_type}: {exc}",
        ) from exc

    return workers
