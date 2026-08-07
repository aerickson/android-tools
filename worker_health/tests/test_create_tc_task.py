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
