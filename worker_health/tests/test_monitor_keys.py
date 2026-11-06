import os
import pty
import sys
import termios
import threading

import pytest

from worker_health import monitor_keys


@pytest.mark.parametrize("key", [b"q", b"Q", b"\x1b"])
def test_quit_key_stops_watching_and_restores_terminal(mocker, key):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    notified = threading.Event()
    mocker.patch.object(monitor_keys.os, "kill", side_effect=lambda *_: notified.set())
    try:
        with os.fdopen(os.dup(slave)) as stdin:
            mocker.patch.object(sys, "stdin", stdin)
            with pytest.raises(monitor_keys.MonitoringStopped):
                with monitor_keys.MonitorKeys(enabled=True):
                    os.write(master, key)
                    assert notified.wait(timeout=2)
                    raise KeyboardInterrupt
            restored = termios.tcgetattr(slave)
            # BSD may set PENDIN while reprocessing input after restoring canonical mode.
            restored[3] &= ~getattr(termios, "PENDIN", 0)
            original[3] &= ~getattr(termios, "PENDIN", 0)
            assert restored == original
    finally:
        os.close(master)
        os.close(slave)


def test_redirected_stdin_is_not_read(mocker):
    mocker.patch.object(sys.stdin, "isatty", return_value=False)
    with monitor_keys.MonitorKeys(enabled=True) as keys:
        assert keys.thread is None


def test_plain_ctrl_c_is_not_treated_as_q(mocker):
    with pytest.raises(KeyboardInterrupt):
        with monitor_keys.MonitorKeys(enabled=False):
            raise KeyboardInterrupt
