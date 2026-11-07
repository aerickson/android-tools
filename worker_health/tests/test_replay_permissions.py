from types import SimpleNamespace

from worker_health.replay_permissions import CLIENTS_URL, permission_summary
from worker_health.replay_report import ReplayReport


def test_only_missing_grants_are_suggested_and_known_prefixes_are_grouped():
    cache_a = "generic-worker:cache:gecko-level-1-pip-a"
    cache_b = "generic-worker:cache:gecko-level-1-uv-b"
    unfamiliar = "secrets:get:project/other/level-1/exact-secret"
    rows = [
        {"name": "Talos", "missing_scopes": [cache_a, unfamiliar]},
        {"name": "Browsertime", "missing_scopes": [cache_a, cache_b]},
        {"name": "Covered permissions", "missing_scopes": []},
    ]
    data = permission_summary(rows, "pool/destination", "user/name|client")
    assert data["blocked_count"] == 2
    assert data["missing_scopes"] == sorted([cache_a, cache_b, unfamiliar])
    assert data["suggested_scopes"] == ["generic-worker:cache:gecko-level-1-*", unfamiliar]
    assert data["workloads"][1]["missing_scopes"] == [cache_a, cache_b]
    assert data["client_url"] == CLIENTS_URL + "/user%2Fname%7Cclient"
    assert "queue:create-task:lowest:pool/destination" in data["recommended_scopes"]
    assert not any(scope.startswith("queue:create-task:") for scope in data["suggested_scopes"])


def test_known_secret_subtrees_and_artifacts_are_separate_grants():
    scopes = [
        "secrets:get:project/perftest/gecko/level-1/perftest-login",
        "secrets:get:project/releng/gecko/build/level-1/conditioned-profiles",
        "queue:get-artifact:project/gecko/talos-pdfs/*",
        "secrets:get:project/perftest/gecko/level-2/exact",
    ]
    data = permission_summary([{"name": "job", "missing_scopes": scopes}], "pool/dest")
    assert data["suggested_scopes"] == sorted(
        [
            "secrets:get:project/perftest/gecko/level-1/*",
            "secrets:get:project/releng/gecko/build/level-1/*",
            "queue:get-artifact:project/gecko/*",
            scopes[-1],
        ],
    )


def test_report_exports_needed_scopes_and_clears_stale_suggestions(tmp_path):
    args = SimpleNamespace(from_pool="source/pool", queue="dest/pool", report=str(tmp_path / "report.json"))
    report = ReplayReport(args)
    needed = permission_summary(
        [
            {"name": "job", "missing_scopes": ["generic-worker:cache:gecko-level-1-uv"]},
        ],
        args.queue,
    )
    report.write({"permissions": needed})
    path = tmp_path / "suggested-scopes.txt"
    assert path.read_text() == "generic-worker:cache:gecko-level-1-*\n"
    report.write({"permissions": permission_summary([], args.queue)})
    assert path.read_text() == ""
