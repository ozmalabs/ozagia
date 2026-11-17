"""Status output while `ozagia agent` gets the TUI's graph ready.

The TUI cannot appear until the snapshot is current and the daemon answers.
A first launch builds the snapshot and a later one refreshes it; on a large
repository that takes seconds, and the terminal would show nothing meanwhile.
`Spinner` writes one line per phase to stderr and ticks a frame while the
phase runs, so a long build does not look like a hang. Finished phases stay
as plain lines in the scrollback above the TUI.

A stream that is not a terminal (a log, a pipe, the test runner) gets each
message once, without frames or carriage returns. `NULL_PROGRESS` is the sink
for callers that did not ask for output: `GraphSession` and `run_agent` in the
eval drivers and in tests.
"""
from __future__ import annotations

import shutil
import sys
import threading
from typing import Protocol, TextIO

FRAMES = "|/-\\"
INTERVAL_S = 0.1


class Progress(Protocol):
    """Where a launch reports its phases (`GraphSession`, `run_agent`)."""

    def step(self, message: str) -> None:
        """Start a phase: show `message`, then tick until the next call."""

    def note(self, message: str) -> None:
        """Write a finished line of its own."""

    def close(self) -> None:
        """Stop ticking and clear an unfinished line."""


class Spinner:
    """A `Progress` on a console stream.

    `enabled=None` follows `stream.isatty()`. An enabled stream rewrites one
    ticking line (a frame after the message), cut to the terminal width so it
    stays on one row; a disabled one writes each message once as a plain
    line. `close` is safe to call twice and on any path, and a closed or
    broken stream never breaks the launch.
    """

    def __init__(self, stream: TextIO | None = None, *, enabled: bool | None = None,
                 interval: float = INTERVAL_S) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = bool(self.stream.isatty()) if enabled is None else enabled
        self.interval = interval
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._open = False

    def step(self, message: str) -> None:
        self._end()
        shown = self._fit(message)
        with self._lock:
            self._open = self.enabled
            self._write(f"{shown} {FRAMES[0]}" if self.enabled else f"{message}\n")
        if not self.enabled:
            return
        stop = threading.Event()
        thread = threading.Thread(target=self._spin, args=(shown, stop),
                                  name="ozagia-progress", daemon=True)
        self._stop, self._thread = stop, thread
        thread.start()

    def note(self, message: str) -> None:
        self._end()
        with self._lock:
            self._write(f"{message}\n")

    def close(self) -> None:
        self._end()

    def _fit(self, message: str) -> str:
        """The message on one row: a wrapped ticker would smear its frames."""
        try:
            width = shutil.get_terminal_size((80, 24)).columns
        except OSError:
            width = 80
        if len(message) + 2 <= width:
            return message
        return message[:max(1, width - 4)] + "…"

    def _spin(self, message: str, stop: threading.Event) -> None:
        frame = 0
        while not stop.wait(self.interval):
            frame = (frame + 1) % len(FRAMES)
            with self._lock:
                if not self._open:
                    return
                self._write(f"\r{message} {FRAMES[frame]}")

    def _end(self) -> None:
        thread, stop = self._thread, self._stop
        self._thread = self._stop = None
        if thread is not None and stop is not None:
            stop.set()
            thread.join(timeout=1.0)
        with self._lock:
            if self._open:
                self._open = False
                self._write("\r\x1b[K")

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or broken stream must not break a launch.
            self.enabled = False
            self._open = False


class _NoProgress:
    """A `Progress` that says nothing (the library default)."""

    def step(self, message: str) -> None:
        pass

    def note(self, message: str) -> None:
        pass

    def close(self) -> None:
        pass


NULL_PROGRESS: Progress = _NoProgress()

__all__ = ["FRAMES", "INTERVAL_S", "NULL_PROGRESS", "Progress", "Spinner"]
