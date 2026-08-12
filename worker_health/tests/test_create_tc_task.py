import create_tc_task
from create_tc_task import build_payload, default_bash_command


def test_default_bash_command_uses_timeout_minus_buffer():
    assert default_bash_command(70) == 'for ((i=1;i<=60;i++)); do echo "$i"; sleep 1; done'


def test_build_docker_worker_payload():
    payload = build_payload(
        "docker-worker",
        "echo hello",
        90,
        {"FOO": "bar"},
        "ubuntu:24.04",
    )

    assert payload == {
        "image": "ubuntu:24.04",
        "command": ["/bin/bash", "-lc", "echo hello"],
        "maxRunTime": 90,
        "env": {"FOO": "bar"},
    }


def test_build_python_script_command_passes_shell_style_arguments(tmp_path):
    script = tmp_path / "benchmark.py"
    script.write_text("#!/usr/bin/env python3\nprint('ok')\n")

    command = create_tc_task._build_command_from_script(
        script,
        "--configuration bitbar-docker-with-robustcheckout --output-dir 'clone results'",
    )

    assert command.startswith(
        "python3 - --configuration bitbar-docker-with-robustcheckout --output-dir 'clone results' << 'SCRIPT_EOF'",
    )
    assert "print('ok')" in command


def test_build_shell_script_command_rejects_arguments(tmp_path):
    script = tmp_path / "benchmark.sh"
    script.write_text("#!/bin/bash\necho ok\n")

    try:
        create_tc_task._build_command_from_script(script, "--configuration test")
    except ValueError as exc:
        assert str(exc) == "--script-args is supported only for non-shell --script-file inputs"
    else:
        raise AssertionError("expected shell script arguments to be rejected")
