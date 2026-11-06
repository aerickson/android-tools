"""Single-key exit from live monitoring, with terminal settings restored on exit."""

import os
import select
import signal
import sys
import termios
import threading
import tty


class MonitoringStopped(Exception):
    """The user stopped watching; submitted jobs continue running."""


class MonitorKeys:
    def __init__(self, enabled):
        self.enabled = enabled and sys.stdin.isatty()
        self.stopped = threading.Event()
        self.quit_requested = threading.Event()
        self.thread = None
        self.original = None

    def __enter__(self):
        if self.enabled:
            self.fd = sys.stdin.fileno()
            self.original = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.thread = threading.Thread(target=self._listen, daemon=True)
            self.thread.start()
        return self

    def _listen(self):
        while not self.stopped.is_set():
            if select.select([self.fd], [], [], 0.1)[0]:
                key = os.read(self.fd, 1)
                if not key:
                    return
                if key in (b"q", b"Q", b"\x1b"):
                    self.quit_requested.set()
                    os.kill(os.getpid(), signal.SIGINT)
                    return

    def __exit__(self, exc_type, exc, traceback):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=0.3)
        if self.original is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)
        if exc_type is KeyboardInterrupt and self.quit_requested.is_set():
            raise MonitoringStopped from None
