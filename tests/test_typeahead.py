"""Keystrokes pressed at a terminal that looks frozen must not count as answers.

An empty line at the review gate means create. The terminal queues whatever is
typed while an agent works and hands it to the next read, so without a guard an
impatient enter during a silent minute would approve drafts nobody had seen -
defeating the gate that `push_interactive` exists to provide.

This drives a real pty. The behaviour under test belongs to the terminal driver
and disappears under any fake, which is why it is here rather than beside the
pure-logic tests.
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import textwrap
import time

# The child imports the real prompt used for the gate, waits the way an agent
# session would, and reports what it actually received.
CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    from forman.cli import _ask_after_agent
    time.sleep(1.0)                       # the agent, working, saying nothing
    got = _ask_after_agent("[c]reate, [e]dit, [q]uit, or feedback: ")
    sys.stderr.write("GOT[" + got + "]\\n")
    sys.stderr.flush()
    """
)

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _gate_receives(*, impatient: bytes, then: bytes) -> str:
    """Type `impatient` while the agent works, `then` once the prompt is up.

    Both writes happen either way, so the test terminates whether or not the
    guard works: if type-ahead leaks through, the gate answers with it and
    `then` is simply left unread.
    """
    main, worker = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(src=SRC)],
        stdin=worker,
        stdout=worker,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    os.close(worker)
    try:
        time.sleep(0.2)
        os.write(main, impatient)  # frozen-looking terminal, so: enter, enter
        time.sleep(1.4)            # the prompt appears at ~1.0s
        os.write(main, then)       # and now a deliberate answer
        err = proc.stderr.read().decode() if proc.stderr else ""
        proc.wait(timeout=15)
    finally:
        os.close(main)
    for line in err.splitlines():
        if line.startswith("GOT["):
            return line[len("GOT[") : -1]
    raise AssertionError(f"the gate never answered: {err!r}")


def test_enter_pressed_while_the_agent_works_does_not_reach_the_gate():
    got = _gate_receives(impatient=b"\r\r", then=b"q\r")

    # "" is the dangerous one: at the gate an empty line means create.
    assert got != "", "type-ahead was accepted as assent at the gate"
    assert got == "q", f"the gate should have read the deliberate answer, got {got!r}"


def test_an_answer_typed_at_the_prompt_is_still_kept():
    """The guard drops what came too early, not what the person meant."""
    got = _gate_receives(impatient=b"", then=b"split the second ticket\r")

    assert got == "split the second ticket"
