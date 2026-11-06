import json
import stat

from worker_health import replay_cache
from worker_health.replay_picker import Discovery, ReplayJob


POOL = "releng-hardware/gecko-t-linux-talos-2404"


def test_history_cache_round_trip_expiration_and_permissions(mocker, tmp_path):
    path = tmp_path / "history.json"
    mocker.patch.object(replay_cache, "cache_path", return_value=path)
    clock = mocker.patch.object(replay_cache.time, "time", return_value=1000.0)
    job = ReplayJob("task-id", {"metadata": {"name": "talos"}}, "2026-11-05T00:00:00Z", 123, "host")
    discovery = Discovery([job], 120, 2400, 600, 0)
    replay_cache.save_discovery(POOL, discovery)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    clock.return_value = 1100.0
    loaded = replay_cache.load_discovery(POOL, 10)
    assert loaded.jobs == [job]
    assert loaded.cached_at == 1000.0
    assert replay_cache.load_discovery("other/pool", 10) is None
    assert replay_cache.load_discovery(POOL, 0) is None
    clock.return_value = 1600.0
    assert replay_cache.load_discovery(POOL, 10) is None


def test_corrupt_missing_and_future_caches_are_ignored(mocker, tmp_path):
    path = tmp_path / "history.json"
    mocker.patch.object(replay_cache, "cache_path", return_value=path)
    assert replay_cache.load_discovery(POOL, 10) is None
    path.write_text("invalid json")
    assert replay_cache.load_discovery(POOL, 10) is None
    path.write_text(json.dumps({"version": 1, "pool": POOL, "saved_at": 99999999999, "discovery": {}}))
    assert replay_cache.load_discovery(POOL, 10) is None


def test_cache_write_failure_is_nonfatal(mocker):
    # Fail while creating the directory, after choosing a valid cache path.
    mocker.patch.object(replay_cache, "cache_path", return_value=mocker.Mock())
    replay_cache.cache_path.return_value.parent.mkdir.side_effect = PermissionError("denied")
    replay_cache.save_discovery(POOL, Discovery([], 0, 0, 0, 0))
