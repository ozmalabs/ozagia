"""`ozagia.harness.progress`: launch status lines before the TUI appears."""
from __future__ import annotations

import io
import time

from ozagia.harness import progress


def test_spinner_plain_stream_prints_each_message_once():
    out = io.StringIO()
    spinner = progress.Spinner(stream=out, enabled=False)
    spinner.step("graph: making the snapshot current")
    spinner.note("graph: built (13706 nodes) in 7.3 s")
    spinner.close()
    assert out.getvalue() == ("graph: making the snapshot current\n"
                              "graph: built (13706 nodes) in 7.3 s\n")


def test_spinner_terminal_rewrites_one_line_then_keeps_the_result():
    out = io.StringIO()
    spinner = progress.Spinner(stream=out, enabled=True, interval=0.005)
    spinner.step("graph: making the snapshot current")
    deadline = time.monotonic() + 2.0
    while "\r" not in out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.005)                       # let the ticker paint a frame
    spinner.note("graph: fresh (3 nodes) in 0.1 s")
    text = out.getvalue()
    assert text.startswith("graph: making the snapshot current |")
    assert "\rgraph: making the snapshot current " in text
    assert text.endswith("\r\x1b[Kgraph: fresh (3 nodes) in 0.1 s\n")
    assert text.count("\n") == 1                # the ticking line never broke


def test_spinner_close_clears_an_unfinished_line_and_is_idempotent():
    out = io.StringIO()
    spinner = progress.Spinner(stream=out, enabled=True, interval=60)
    spinner.step("graph daemon: starting")
    spinner.close()
    after = out.getvalue()
    assert after.endswith("\r\x1b[K")
    spinner.close()
    assert out.getvalue() == after


def test_spinner_cuts_a_long_phase_to_one_terminal_row(monkeypatch):
    monkeypatch.setenv("COLUMNS", "30")
    out = io.StringIO()
    spinner = progress.Spinner(stream=out, enabled=True, interval=60)
    spinner.step("graph: making the snapshot current for /a/long/path (build=auto)")
    spinner.close()
    shown = out.getvalue().lstrip("\r").split(" |")[0]
    assert shown.startswith("graph: making the snapshot")
    assert shown.endswith("…") and len(shown) <= 30


def test_spinner_follows_isatty_when_not_told():
    class Stream(io.StringIO):
        def __init__(self, tty: bool) -> None:
            super().__init__()
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    assert progress.Spinner(stream=Stream(True)).enabled is True
    assert progress.Spinner(stream=Stream(False)).enabled is False


def test_a_broken_stream_does_not_break_the_launch():
    class Broken(io.StringIO):
        def write(self, _text):                 # type: ignore[override]
            raise OSError("stderr is gone")

    spinner = progress.Spinner(stream=Broken(), enabled=True, interval=0.005)
    spinner.step("graph: checking")
    spinner.note("graph: fresh")
    spinner.close()
    assert spinner.enabled is False


def test_null_progress_says_nothing():
    progress.NULL_PROGRESS.step("graph: checking")
    progress.NULL_PROGRESS.note("graph: fresh")
    progress.NULL_PROGRESS.close()
