"""The human gate in the push phase, as a port.

`push_interactive` has always stopped at a human. Until this existed, the shape
of that stop was two callables and two prompt strings, so the only way to tell
"the agent is asking you something" apart from "these drafts are ready" was to
match the literals `"> "` and `"[c]reate ..."`. That is fine for a terminal and
hostile to anything embedding this. Both moments are now typed.

Nothing here decides anything. A Reviewer is asked; the loop in push.py acts on
what comes back. The free-text parsing that used to sit inline in that loop now
lives in TerminalReviewer, which is where free text actually is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .models import Ticket


@dataclass
class Question:
    """The push agent wants something from the human before it can draft.

    `text` is the agent's own words, unedited. An implementation may add to it
    but must not put its own question in its place: the point of the round trip
    is that the person sees what was actually asked.
    """

    text: str
    round: int = 1  # 1-based, capped by push.MAX_QUESTION_ROUNDS


@dataclass
class Approval:
    """Drafts are ready and nothing has been created yet.

    `rendered` is `render_drafts(tickets)`, carried along so an implementation
    that only wants to display the drafts does not have to import push.py to do
    it.
    """

    tickets: list[Ticket] = field(default_factory=list)
    rendered: str = ""


@dataclass
class Decision:
    """What the human decided about a set of drafts.

    A closed set, so the loop in push.py branches on an enum-shaped value
    instead of lowercasing whatever was typed:

      create   - file them.
      edit     - open them in an editor and come back.
      quit     - walk away; nothing is created.
      feedback - redraft with `feedback`, still without creating.
    """

    action: str
    feedback: str = ""


CREATE = "create"
EDIT = "edit"
QUIT = "quit"
FEEDBACK = "feedback"


@runtime_checkable
class Reviewer(Protocol):
    """Every question the push phase puts to a human. Nothing else is allowed."""

    def show(self, text: str) -> None:
        """One-way output. Progress, drafts, explanations."""
        ...

    def answer(self, question: Question) -> str:
        """The agent asked something. Return what to send back."""
        ...

    def decide(self, approval: Approval) -> Decision:
        """The drafts are ready. Return what happens to them."""
        ...


class TerminalReviewer:
    """What the CLI has always done, now behind the port.

    Behaviour is byte-identical to the inline version: the same two prompts, the
    same forgiving synonyms, the same treatment of an empty line as assent.
    """

    def __init__(
        self,
        ask: Callable[[str], str] = input,
        show: Callable[[str], None] = print,
    ) -> None:
        self._ask = ask
        self._show = show

    def show(self, text: str) -> None:
        self._show(text)

    def answer(self, question: Question) -> str:
        self._show(question.text)
        return self._ask("> ")

    def decide(self, approval: Approval) -> Decision:
        self._show("")
        self._show(approval.rendered)
        answer = self._ask(
            f"[c]reate {len(approval.tickets)} ticket(s), [e]dit, [q]uit, "
            "or type feedback to redraft: "
        ).strip()
        return parse_decision(answer)


def parse_decision(answer: str) -> Decision:
    """Read a typed line as a decision.

    An empty line means create: someone who has read the drafts and pressed
    enter has agreed with them. Anything unrecognised is feedback rather than an
    error, because the alternative is scolding a person for describing what they
    want in their own words.
    """
    stripped = answer.strip()
    lowered = stripped.lower()
    if not stripped or lowered in ("c", "create", "y", "yes"):
        return Decision(CREATE)
    if lowered in ("q", "quit", "n", "no"):
        return Decision(QUIT)
    if lowered in ("e", "edit"):
        return Decision(EDIT)
    return Decision(FEEDBACK, stripped)
