from create_tc_task import (
    BITBAR_DASHBOARD_URL,
    BITBAR_SCRIPTVARS_PATH,
    prepend_bitbar_dashboard_link,
)


def test_prepends_bitbar_dashboard_link():
    command = prepend_bitbar_dashboard_link("proj-autophone/gecko-t-bitbar-gw-test-2", "echo hello")

    assert command.endswith("; echo hello")
    assert f'echo "Bitbar test run: {BITBAR_DASHBOARD_URL}/' in command
    assert command.count(BITBAR_SCRIPTVARS_PATH) == 1
    assert 'values[f"TESTDROID_{key}_ID"]' in command
    assert '("PROJECT", "BUILD", "RUN")' in command


def test_does_not_prepend_link_for_non_bitbar_queue():
    assert prepend_bitbar_dashboard_link("proj-autophone/gecko-t-moonshot", "echo hello") == "echo hello"
