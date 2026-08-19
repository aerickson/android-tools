import importlib.util
import json
import os
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "ct_scripts" / "hg_clone_network_check.py"
SPEC = importlib.util.spec_from_file_location("hg_clone_network_check", SCRIPT_PATH)
hg_clone_network_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hg_clone_network_check)


def test_run_phase_collects_periodic_telemetry(tmp_path):
    result = hg_clone_network_check.run_phase(
        "sample",
        [sys.executable, "-c", "import time; time.sleep(0.03)"],
        str(tmp_path),
        os.environ.copy(),
        telemetry_interval=0.01,
        monitored_path=str(tmp_path),
    )

    telemetry = result["telemetry"]
    assert telemetry["enabled"] is True
    assert len(telemetry["samples"]) >= 2
    assert all("elapsed_seconds" in sample for sample in telemetry["samples"])
    assert all("storage" in sample for sample in telemetry["samples"])
    with (tmp_path / telemetry["artifact"]).open() as artifact:
        persisted = json.load(artifact)
    assert persisted["samples"] == telemetry["samples"]


def test_hgrc_environment_uses_absolute_path(tmp_path):
    environment = hg_clone_network_check.hgrc_environment(tmp_path / "benchmark.hgrc")

    assert environment["HGRCPATH"] == str(tmp_path / "benchmark.hgrc")
    assert os.path.isabs(environment["HGRCPATH"])
