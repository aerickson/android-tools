import os
import shutil

# import pytest
from pytest import fixture

from worker_health import runner


@fixture
def datadir(tmpdir, request):
    """
    Fixture responsible for searching a folder with the same name of test
    module and, if available, moving all contents to a temporary directory so
    tests can use them freely.
    """
    filename = request.module.__file__
    test_dir, _ = os.path.splitext(filename)

    if os.path.isdir(test_dir):
        shutil.copytree(test_dir, str(tmpdir), dirs_exist_ok=True)

    return tmpdir


# @pytest.mark.skip(reason="temp")
def test_get_host_counts_general(datadir):
    confdir = datadir.join("general")
    r = runner.Runner.from_resume(confdir, test_mode=True)
    assert r.get_host_counts() == {"skipped": 0, "completed": 3, "failed": 0, "remaining": 0, "total": 3, "to_skip": 1}


# @pytest.mark.skip(reason="temp")
def test_get_host_counts_general2(datadir):
    confdir = datadir.join("general2")
    r = runner.Runner.from_resume(confdir, test_mode=True)
    assert r.get_host_counts() == {"skipped": 1, "completed": 1, "failed": 1, "remaining": 4, "total": 7, "to_skip": 2}


def test_load_config(datadir):
    confdir = datadir.join("general")
    r = runner.Runner.from_resume(confdir, test_mode=True)
    assert r.provisioner == "releng-hardware"
    assert r.worker_type == "gecko-t-osx-1015-r8"
    assert r.command == "ssh SR_HOST.SR_FQDN echo monkey"
    assert r.hosts_to_skip == ["macmini-r8-9"]
    assert r.fqdn_postfix == "test.releng.mdc1.mozilla.com"


def test_load_config2(datadir):
    confdir = datadir.join("general2")
    r = runner.Runner.from_resume(confdir, test_mode=True)
    assert r.provisioner == "releng-hardware999"
    assert r.worker_type == "gecko-t-banana"
    assert r.command == "ssh SR_HOST.SR_FQDN echo FUN FUN FUN"
    assert r.hosts_to_skip == ["macmini-r8-222", "macmini-r8-391"]
    assert r.fqdn_postfix == "test.releng.mdc1.mozilla.comzzz"
