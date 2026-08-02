"""What a terminal shows while an agent works, and what it refuses to read.

Both CLIs need the same two things, for the same reason: an agent session says
nothing until it is finished, and a person watching a silent terminal cannot
tell that from a hang. They press keys at it, and those keys land in the next
prompt. So this owns the live display and the guard on the input queue, and
both `forman` and `red` use it rather than each growing its own.

Nothing here is imported by the pipeline. It is terminal presentation, kept out
of the modules that do the work, which is why they take `on_activity` as a
callback and never know what draws it.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from .spawn import Activity, describe_activity

# Imported for its import side effect, not for anything it exports: this is what
# makes `input()` use GNU readline instead of the terminal driver's canonical
# mode. The driver erases with backspace-space-backspace, which cannot cross a
# row boundary, so editing a line that has wrapped stops dead at the start of
# the current visual row. This module owns the prompts now, so the import lives
# beside them rather than in whichever CLI happened to be imported first.
#
# Do not "clean up" this import. Not available on every build, hence the guard.
try:  # pragma: no cover - presence is a property of the interpreter build
    import readline  # noqa: F401
except ImportError:  # pragma: no cover
    pass


class Progress:
    """The live line under a running agent.

    Printing one line per thing the agent does is not enough on its own. A long
    stretch of thinking is a single event followed by a minute of nothing, which
    looks exactly like the freeze it is not. So a ticking clock runs alongside,
    redrawing in place: if the number is moving, it is alive.

    Writes to stderr on purpose. Both CLIs put real output on stdout, and that
    has to stay pipeable.
    """

    def __init__(
        self,
        stream=None,
        clock: Callable[[], float] = time.monotonic,
        tick: bool = False,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._clock = clock
        self._started: float | None = None
        self._label = ""
        self._width = 0  # how much of the live line needs erasing
        self._tick = tick
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self, what: str) -> None:
        with self._lock:
            self._started = self._clock()
            self._label = "thinking"
            self._line(f"{what}... (ctrl-c to abort)", live=False)
        _remember(self)
        self._start_ticking()

    def __call__(self, activity: Activity) -> None:
        """Called from inside the agent session, once per thing it does."""
        if self._started is None:
            self.start("working")
        with self._lock:
            self._label = describe_activity(activity)
            self._line(f"  {self._elapsed()}  {self._label}", live=False)
        self._start_ticking()  # resumes after a prompt paused it

    def done(self) -> None:
        self._stop_ticking()
        with self._lock:
            self._erase()
            self._started = None
        _forget(self)

    def pause(self) -> None:
        """Take the live line down because something is about to read input.

        An agent asks its questions from inside the session, so a prompt can
        appear while the clock is still ticking. Two writers on one line makes
        an unreadable mess, and the prompt is the one that matters. Ticking
        resumes by itself on the next thing the agent does.
        """
        self._stop_ticking()
        with self._lock:
            self._erase()

    # -- the live line --------------------------------------------------------

    def _start_ticking(self) -> None:
        if not self._tick or self._thread is not None:
            return
        self._stop = threading.Event()

        def run() -> None:
            # Daemon: a display must never be the reason the process will not
            # exit, least of all on the ctrl-c this same line advertises.
            while self._stop is not None and not self._stop.wait(1.0):
                with self._lock:
                    if self._started is None:
                        return
                    self._line(f"  {self._elapsed()}  {self._label}", live=True)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _stop_ticking(self) -> None:
        if self._stop is not None:
            self._stop.set()
        thread, self._thread, self._stop = self._thread, None, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _erase(self) -> None:
        if self._width:
            print("\r" + " " * self._width + "\r", end="", file=self._stream, flush=True)
            self._width = 0

    def _line(self, text: str, *, live: bool) -> None:
        """Draw `text`, replacing whatever the ticker last left on the line.

        `live` lines are transient and get overwritten in place; the others are
        history and stay, so what the agent did scrolls up the terminal.
        """
        self._erase()
        if live:
            # Stays on the line so the next tick can overwrite it.
            print(f"\r{text}", end="", file=self._stream, flush=True)
            self._width = len(text)
        else:
            # _erase already put the cursor at column 0; a \r here would only
            # confuse anything reading this back, since it counts as a line
            # break to splitlines.
            print(text, file=self._stream, flush=True)
            self._width = 0

    def _elapsed(self) -> str:
        # `is None`, not a truth test: a monotonic clock can legitimately start
        # at 0.0, and treating that as unset pins the display to 0:00 forever.
        started = self._started if self._started is not None else self._clock()
        seconds = int(self._clock() - started)
        return f"{seconds // 60}:{seconds % 60:02d}"


def for_terminal() -> Progress | None:
    """Only narrate to a human watching a terminal.

    Piped or redirected, return nothing: the callers treat None as "stay quiet",
    and nothing downstream of a pipe asked for a running commentary or for
    control characters in its input.
    """
    return Progress(tick=True) if sys.stderr.isatty() else None


# -- the one running display ---------------------------------------------------

_ACTIVE: Progress | None = None


def _remember(progress: Progress) -> None:
    """Track the running display so a prompt can quiet it.

    A module-level handle, because the prompts that need to interrupt it are
    buried inside an agent session several layers down, and threading a display
    object through those signatures would put presentation in places that have
    no business knowing about a terminal.
    """
    global _ACTIVE
    _ACTIVE = progress


def _forget(progress: Progress) -> None:
    global _ACTIVE
    if _ACTIVE is progress:
        _ACTIVE = None


def quiet_for_prompt() -> None:
    if _ACTIVE is not None:
        _ACTIVE.pause()


# -- the input queue -----------------------------------------------------------


def drop_typeahead() -> None:
    """Throw away anything typed before this prompt was on screen.

    The terminal queues keystrokes while an agent works and hands them to the
    next read. That is fine for a shell and dangerous here: the next read is
    usually a gate, and an empty line at a gate means create. Someone pressing
    enter at a terminal that looks frozen would be agreeing to a draft that did
    not exist yet.

    Only fresh keystrokes count at a gate, so the queue is dropped. Not on a
    pipe: there the queue is the input, and discarding it would eat the answer.
    """
    try:
        if not sys.stdin.isatty():
            return
        import termios

        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:  # pragma: no cover - no tty, or a platform without termios
        pass


def ask_after_agent(prompt: str) -> str:
    """Read an answer to something an agent asked, or to a gate it reached.

    Both arrive after a stretch of silence long enough to make a person press
    keys at it, which is exactly the input that must not be treated as an
    answer. Prompts that follow nothing do not go through here: there is nothing
    queued, and flushing would only discard deliberate type-ahead.
    """
    quiet_for_prompt()
    drop_typeahead()
    return input(prompt)
