import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from worker_health.replay_report import ReplayReport


def test_default_report_unique_under_tmp():
    paths = []
    try:
        for _ in range(2):
            args = SimpleNamespace(report=None, from_pool="source/pool", queue="target/pool")
            report = ReplayReport(args)
            paths.append(report.path)
            assert report.path.parent == Path("/tmp")
            assert args.report == str(report.path)
            assert not json.loads(report.path.read_text())["full_coverage"]
        assert paths[0] != paths[1]
    finally:
        for path in paths:
            path.unlink()


def test_explicit_report_updates_atomically(tmp_path, mocker):
    path = tmp_path / "coverage.json"
    report = ReplayReport(SimpleNamespace(report=str(path), from_pool="a/b", queue="c/d"))
    report.write({"full_coverage": True, "passed": 2})
    data = json.loads(path.read_text())
    assert data["exit_reason"] is None
    assert data["destination_pool"] == "c/d"
    mocker.patch.object(Path, "replace", side_effect=OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        report.write({"full_coverage": False})
    assert json.loads(path.read_text()) == data
    assert list(tmp_path.iterdir()) == [path]


def test_finalized_report_is_not_overwritten_by_late_api_work(tmp_path):
    report = ReplayReport(SimpleNamespace(report=str(tmp_path / "report.json"), from_pool="a/b", queue="c/d"))
    report.write({"full_coverage": False, "passed": 2})
    report.finish("manual_stop")
    report.write({"full_coverage": True, "passed": 3}, exit_reason="full_coverage")
    data = json.loads(report.path.read_text())
    assert data["exit_reason"] == "manual_stop"
    assert data["passed"] == 2 and not data["full_coverage"]
