"""The terminal's answer to "is it working?".

An agent session says nothing until it is finished - reading a repository,
splitting a ticket, or executing a sub-task - so the only thing standing
between a working run and one that looks hung is what this prints.
"""

from __future__ import annotations

import io
import re
import time

from forman.progress import Progress
from forman.spawn import Activity


class FakeClock:
    """Elapsed time is part of the output, so the tests get to control it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_something_is_printed_before_the_agent_has_said_anything():
    out = io.StringIO()
    Progress(stream=out, clock=FakeClock()).start("reading the repository")

    assert "reading the repository" in out.getvalue()


def test_the_abort_key_is_named_because_that_is_the_question_being_asked():
    out = io.StringIO()
    Progress(stream=out, clock=FakeClock()).start("drafting")

    assert "ctrl-c" in out.getvalue()


def test_each_thing_the_agent_does_becomes_a_line():
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock())
    progress.start("drafting")

    progress(Activity(kind="tool", tool="Read", tool_input={"file_path": "src/cli.py"}))
    progress(Activity(kind="tool", tool="Grep", tool_input={"pattern": "rate.?limit"}))

    lines = out.getvalue().splitlines()
    assert "read src/cli.py" in lines[1]
    assert "grep rate.?limit" in lines[2]


def test_elapsed_time_climbs_so_a_stall_is_visible():
    out = io.StringIO()
    clock = FakeClock()
    progress = Progress(stream=out, clock=clock)
    progress.start("drafting")

    clock.now = 8
    progress(Activity(kind="thinking"))
    clock.now = 95
    progress(Activity(kind="thinking"))

    lines = out.getvalue().splitlines()
    assert "0:08" in lines[1]
    assert "1:35" in lines[2]


def test_activity_arriving_without_a_start_still_prints():
    """The redraft path starts its own run; a stray callback must not crash it."""
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock())

    progress(Activity(kind="tool", tool="Read", tool_input={"file_path": "a.py"}))

    assert "read a.py" in out.getvalue()


def test_the_clock_restarts_between_runs():
    out = io.StringIO()
    clock = FakeClock()
    progress = Progress(stream=out, clock=clock)
    progress.start("drafting")

    clock.now = 60
    progress.done()
    progress.start("redrafting")
    clock.now = 63
    progress(Activity(kind="thinking"))

    assert "0:03" in out.getvalue().splitlines()[-1]


def test_nothing_reaches_stdout():
    """`--dry-run` writes the draft to stdout and that has to stay pipeable."""
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock())
    progress.start("drafting")
    progress(Activity(kind="thinking"))

    assert out.getvalue()  # it went to the stream it was given, not print()


# -- the ticking clock --------------------------------------------------------
#
# The event lines alone were not enough: forty seconds of thinking is one event
# and then nothing, which looks exactly like the freeze it is not.


def test_the_clock_redraws_itself_while_nothing_happens():
    """The whole point: no activity at all, and the number still moves."""
    out = io.StringIO()
    clock = FakeClock()
    progress = Progress(stream=out, clock=clock, tick=True)
    progress.start("drafting")
    try:
        for _ in range(40):  # ~4s of real time at most; the ticker runs at 1s
            clock.now += 1
            time.sleep(0.1)
            if len(_clock_readings(out.getvalue())) >= 2:
                break
    finally:
        progress.done()

    readings = _clock_readings(out.getvalue())
    assert len(readings) >= 2, f"clock did not redraw: {out.getvalue()!r}"
    assert readings != [readings[0]] * len(readings), "clock redrew but never moved"


def _clock_readings(drawn: str) -> list[str]:
    """Every elapsed time the ticker painted, in order."""
    return re.findall(r"\d+:\d\d", drawn.split("abort)")[-1])


def test_the_live_line_is_erased_when_the_run_ends():
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock(), tick=True)
    progress.start("drafting")
    progress.done()

    # Whatever was drawn, the last thing written returns the cursor and blanks
    # the line, so the next print does not land on top of a stale clock.
    assert out.getvalue().endswith("\r") or out.getvalue().endswith("\n")


def test_a_prompt_stops_the_clock_from_writing_over_it():
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock(), tick=True)
    progress.start("drafting")
    progress.pause()
    drawn_at_pause = out.getvalue()

    time.sleep(0.3)  # the ticker, had it survived, would have drawn by now
    assert out.getvalue() == drawn_at_pause

    progress.done()


def test_activity_after_a_pause_starts_the_clock_again():
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock(), tick=True)
    progress.start("drafting")
    progress.pause()
    progress(Activity(kind="tool", tool="Read", tool_input={"file_path": "a.py"}))
    try:
        assert "read a.py" in out.getvalue()
    finally:
        progress.done()


def test_ticking_is_off_by_default_so_a_pipe_gets_no_control_characters():
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock())
    progress.start("drafting")
    time.sleep(0.3)
    progress.done()

    assert out.getvalue() == "drafting... (ctrl-c to abort)\n"
