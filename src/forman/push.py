"""Push phase: prose in, Linear tickets out.

I describe an issue in prose; an agent fills the ticket template and creates one
or more tickets. This is the only phase that writes to Linear on purpose.

Splitting rule: separate tickets when concerns are independently shippable.
Whenever ordering matters, blocked_by and blocks must be populated, because the
pull phase's ticket-level topo-sort is built on exactly those fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .linear_client import LinearClient
from .models import Ticket
from .review import (
    CREATE,
    EDIT,
    FEEDBACK,
    QUIT,
    Approval,
    Question,
    Reviewer,
    TerminalReviewer,
)
from .spawn import (
    DEFAULT_MODEL,
    Activity,
    AgentRun,
    extract_last_json,
    run_agent,
    run_conversation,
)
from .topo import CycleError, topo_sort

PUSH_MAX_TURNS = 20

MAX_QUESTION_ROUNDS = 3

_TICKET_JSON_SHAPE = """\
Emit as your final message a single JSON object and nothing else:
{"tickets": [{
  "title": "imperative, under 80 chars",
  "priority": "urgent | high | medium | low",
  "labels": ["label"],
  "project": "project name or null",
  "estimate": "xs | s | m | l | xl",
  "blocked_by": [],
  "blocks": [],
  "problem": "what is broken or missing, and why it matters. 2-4 sentences.",
  "acceptance_criteria": ["observable, testable outcome"],
  "context": "files, endpoints, prior tickets, links the executor will need",
  "out_of_scope": "explicit non-goals so decomposition does not sprawl"
}]}
Every field is required except project, which may be null. Acceptance criteria \
must be observable: something a person could check without reading your mind.\
"""

_SPLIT_RULE = """\
Splitting rule: create separate tickets when the concerns are independently \
shippable. Otherwise create one. Do not invent work the person did not describe. \
Whenever ordering matters, populate blocked_by and blocks using the 1-based \
index of another ticket in your own list.\
"""

# One shot, no questions possible. Used for --yes and for non-interactive stdin.
PUSH_CONTRACT = f"""\
You turn a person's prose description of an issue into one or more Linear \
tickets. You may read the repository you are in to ground the ticket in real \
paths and names. Do not write or edit any code.

{_SPLIT_RULE}

{_TICKET_JSON_SHAPE}"""

# Interactive. The questions are the point: this ticket will be executed by
# agents with no supervision, so an ambiguity left here does not produce a vague
# pull request, it produces a confidently wrong one several sessions later.
PUSH_CONVERSATION_CONTRACT = f"""\
You are helping someone file a Linear ticket that an autonomous pipeline will \
later execute WITHOUT supervision. A later agent will read only this ticket, \
split it into sub-tasks, and write code against it. Your job is to end up with a \
ticket that can actually be executed on its own.

Before drafting, check whether you have these five things. They are what the \
pipeline needs in order to work, and nothing outside this list justifies a \
question:

1. The problem. What is broken or missing, and why it matters.
2. Acceptance criteria. Observable outcomes someone could check without reading \
the author's mind. This is what "done" will be measured against.
3. Where to look. Files, modules, endpoints, prior tickets the executor needs.
4. Out of scope. What must NOT be touched. Without this, decomposition sprawls \
and agents wander into unrelated code.
5. Whether this is one ticket or several independently shippable ones, and if \
several, what order they have to happen in.

Answer as many of these as you can yourself by reading the repository you are \
in. Always prefer looking over asking: a question you could have settled by \
opening a file is a question you should not ask. Do not write or edit any code.

Then ask ONLY for the gaps that remain, all in one message rather than one \
question at a time. Keep it short and concrete. If the person's description \
already covers everything, ask nothing at all and draft immediately. You have at \
most {MAX_QUESTION_ROUNDS} rounds of questions; after that, draft with what you \
have and record the remaining uncertainty under "Out of scope" or in the \
context section.

{_SPLIT_RULE}

While you still have questions, write them as plain prose and nothing else. \
Emit no JSON until you are ready to draft, because the JSON is the signal that \
you are done asking.

{_TICKET_JSON_SHAPE}"""


class PushError(RuntimeError):
    """The push agent produced something unusable."""


def render_ticket_body(fields: dict) -> str:
    """Render the ticket template body. Frontmatter lives on the Linear issue
    itself, so only the prose sections go into the description."""
    criteria = fields.get("acceptance_criteria") or []
    lines = [
        "## Problem",
        str(fields.get("problem") or "").strip() or "(not described)",
        "",
        "## Acceptance criteria",
    ]
    lines += [f"- [ ] {c}" for c in criteria] or ["- [ ] (none given)"]
    lines += [
        "",
        "## Context / pointers",
        str(fields.get("context") or "").strip() or "(none)",
        "",
        "## Out of scope",
        str(fields.get("out_of_scope") or "").strip() or "(none stated)",
    ]
    return "\n".join(lines)


def parse_push(text: str) -> list[dict]:
    payload = extract_last_json(text)
    if not isinstance(payload, dict) or "tickets" not in payload:
        raise PushError("push agent emitted no `tickets` object")
    tickets = payload.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        raise PushError("push agent returned an empty ticket list")
    for i, t in enumerate(tickets, start=1):
        if not isinstance(t, dict) or not str(t.get("title", "")).strip():
            raise PushError(f"ticket {i} has no title")
    return tickets


def _creation_order(tickets: list[dict]) -> list[int]:
    """Create blockers before the things they block, so index references can be
    rewritten into real identifiers as we go."""
    count = len(tickets)
    graph: dict[str, list[str]] = {}
    for i, t in enumerate(tickets):
        deps = set()
        for ref in t.get("blocked_by") or []:
            ref = str(ref).strip()
            if ref.isdigit() and 0 <= int(ref) - 1 < count and int(ref) - 1 != i:
                deps.add(str(int(ref) - 1))
        graph[str(i)] = sorted(deps, key=int)
    try:
        return [int(x) for x in topo_sort(graph)]
    except CycleError as exc:
        raise PushError(
            f"push agent produced circular ticket dependencies: {exc.remaining}"
        ) from exc


def to_tickets(raw: list[dict]) -> list[Ticket]:
    """Build Ticket objects with index references left intact."""
    out = []
    for fields in raw:
        out.append(
            Ticket(
                identifier="",
                title=str(fields["title"]).strip(),
                description=render_ticket_body(fields),
                priority=str(fields.get("priority") or "medium").lower(),
                labels=[str(x) for x in fields.get("labels") or []],
                project=fields.get("project") or None,
                estimate=(str(fields["estimate"]).lower() if fields.get("estimate") else None),
                blocked_by=[str(x) for x in fields.get("blocked_by") or []],
                blocks=[str(x) for x in fields.get("blocks") or []],
            )
        )
    return out


# -- draft rendering and round-tripping --------------------------------------

TICKET_SEPARATOR = "<!-- forman:ticket -->"


def _fmt_list(values: list[str]) -> str:
    return "[" + ", ".join(values) + "]"


def render_ticket_markdown(ticket: Ticket) -> str:
    """Render a draft in the ticket template's own shape.

    This is what gets shown before anything is created, and what `$EDITOR`
    opens. It round-trips through parse_ticket_markdown.
    """
    return "\n".join(
        [
            "---",
            f"title: {ticket.title}",
            f"priority: {ticket.priority}",
            f"labels: {_fmt_list(ticket.labels)}",
            f"project: {ticket.project or 'null'}",
            f"estimate: {ticket.estimate or 'null'}",
            f"blocked_by: {_fmt_list(ticket.blocked_by)}",
            f"blocks: {_fmt_list(ticket.blocks)}",
            "---",
            "",
            ticket.description.strip(),
            "",
        ]
    )


def _parse_list(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_ticket_markdown(text: str) -> Ticket:
    """Read a draft back after a human has edited it.

    Deliberately forgiving: someone editing their own ticket in vim should not
    have to get YAML exactly right for their work to survive.
    """
    body = text.strip()
    fields: dict[str, str] = {}

    if body.startswith("---"):
        _, _, rest = body.partition("---")
        front, sep, remainder = rest.partition("\n---")
        if sep:
            body = remainder.strip()
            for line in front.splitlines():
                key, colon, value = line.partition(":")
                if colon:
                    fields[key.strip().lower()] = value.strip()

    title = fields.get("title", "").strip()
    if not title:
        raise PushError("the edited ticket has no title")

    def optional(name: str) -> str | None:
        value = fields.get(name, "").strip()
        return None if value.lower() in ("", "null", "none") else value

    return Ticket(
        identifier="",
        title=title,
        description=body,
        priority=(optional("priority") or "medium").lower(),
        labels=_parse_list(fields.get("labels", "")),
        project=optional("project"),
        estimate=(optional("estimate") or "").lower() or None,
        blocked_by=_parse_list(fields.get("blocked_by", "")),
        blocks=_parse_list(fields.get("blocks", "")),
    )


def render_drafts(tickets: list[Ticket]) -> str:
    return f"\n{TICKET_SEPARATOR}\n\n".join(render_ticket_markdown(t) for t in tickets)


def parse_drafts(text: str) -> list[Ticket]:
    chunks = [c.strip() for c in text.split(TICKET_SEPARATOR)]
    return [parse_ticket_markdown(c) for c in chunks if c.strip()]


# -- creation ----------------------------------------------------------------


def create_tickets(tickets: list[Ticket], linear: LinearClient) -> list[Ticket]:
    """Create tickets blockers-first, rewriting index references as we go."""
    order = _creation_order([{"blocked_by": t.blocked_by} for t in tickets])
    identifier_for: dict[int, str] = {}

    for pos in order:
        ticket = tickets[pos]
        ticket.blocked_by = [
            identifier_for[int(ref) - 1]
            for ref in ticket.blocked_by
            if str(ref).isdigit() and (int(ref) - 1) in identifier_for
        ]
        made = linear.create(ticket)
        identifier_for[pos] = made.identifier

    # `blocks` mirrors `blocked_by`; resolve it now every ticket has an id, so
    # the objects we hand back are self-consistent.
    for ticket in tickets:
        ticket.blocks = [
            identifier_for[int(ref) - 1]
            for ref in ticket.blocks
            if str(ref).isdigit() and (int(ref) - 1) in identifier_for
        ]

    return [tickets[pos] for pos in sorted(identifier_for)]


def push(
    *,
    prose: str,
    linear: LinearClient,
    cwd: str | Path = ".",
    runner: Callable[..., AgentRun] = run_agent,
    model: str | None = DEFAULT_MODEL,
    dry_run: bool = False,
) -> list[Ticket]:
    """Turn prose into tickets, and create them unless dry_run is set."""
    run = runner(
        prompt=f"Turn this into Linear tickets:\n\n{prose.strip()}",
        system_prompt=PUSH_CONTRACT,
        cwd=cwd,
        allowed_tools=["Read", "Grep", "Glob"],
        max_turns=PUSH_MAX_TURNS,
        model=model,
    )
    if run.error:
        raise PushError(f"push session failed: {run.error}")

    tickets = to_tickets(parse_push(run.text))
    if dry_run:
        return tickets
    return create_tickets(tickets, linear)


# -- interactive -------------------------------------------------------------


class Aborted(RuntimeError):
    """The human walked away. Nothing was created."""


def push_interactive(
    *,
    prose: str,
    linear: LinearClient,
    ask: Callable[[str], str] | None = None,
    show: Callable[[str], None] | None = None,
    reviewer: Reviewer | None = None,
    edit: Callable[[str], str] | None = None,
    cwd: str | Path = ".",
    conversation: Callable[..., AgentRun] = run_conversation,
    model: str | None = DEFAULT_MODEL,
    on_activity: Callable[[Activity], None] | None = None,
) -> list[Ticket]:
    """Talk it through, show the draft, then create only once told to.

    `pull` stops at a human gate before anything leaves the machine. Until this
    existed, `push` did not: one line of prose became real tickets in a real
    workspace, unseen. This closes that asymmetry.

    All the I/O is injected, so the whole flow is testable without a terminal,
    a model, or an account.

    The human side is a `Reviewer`. Passing `ask` and `show` instead builds a
    `TerminalReviewer` from them, which is what every caller did before the port
    existed and does exactly what it did then.

    `on_activity` is passed straight to the conversation, so a caller driving
    this in a loop (Red, one slice at a time) can show that the agent is working
    rather than leaving a slice heading on screen above a silent terminal.
    """
    if reviewer is None:
        if ask is None or show is None:
            raise TypeError("push_interactive needs either reviewer, or both ask and show")
        reviewer = TerminalReviewer(ask=ask, show=show)

    rounds = 0

    def respond(agent_text: str) -> str | None:
        nonlocal rounds
        payload = extract_last_json(agent_text)
        if isinstance(payload, dict) and "tickets" in payload:
            return None  # the JSON is the signal that it is done asking
        rounds += 1
        if rounds > MAX_QUESTION_ROUNDS:
            return "That is enough questions. Draft the ticket with what you have."
        return reviewer.answer(Question(text=agent_text, round=rounds))

    run = conversation(
        system_prompt=PUSH_CONVERSATION_CONTRACT,
        opening=f"Here is what I want to achieve:\n\n{prose.strip()}",
        respond=respond,
        cwd=cwd,
        allowed_tools=["Read", "Grep", "Glob"],
        max_rounds=MAX_QUESTION_ROUNDS + 2,
        model=model,
        on_activity=on_activity,
    )
    if run.error:
        raise PushError(f"push session failed: {run.error}")

    tickets = to_tickets(parse_push(run.text))

    while True:
        decision = reviewer.decide(
            Approval(tickets=tickets, rendered=render_drafts(tickets))
        )

        if decision.action == CREATE:
            return create_tickets(tickets, linear)

        if decision.action == QUIT:
            raise Aborted("nothing created")

        if decision.action == EDIT:
            if edit is None:
                reviewer.show("No editor available. Type feedback instead.")
                continue
            try:
                tickets = parse_drafts(edit(render_drafts(tickets)))
            except PushError as exc:
                reviewer.show(f"Could not read that back: {exc}. Nothing changed.")
            continue

        if decision.action != FEEDBACK:
            raise PushError(f"reviewer returned an unknown action: {decision.action!r}")

        # Feedback. Redraft with it, still without creating.
        run = conversation(
            system_prompt=PUSH_CONVERSATION_CONTRACT,
            opening=(
                f"Here is what I want to achieve:\n\n{prose.strip()}\n\n"
                f"You drafted:\n\n{render_drafts(tickets)}\n\n"
                f"Revise it: {decision.feedback}\n\n"
                "Redraft now; do not ask more questions."
            ),
            respond=lambda _text: None,
            cwd=cwd,
            allowed_tools=["Read", "Grep", "Glob"],
            max_rounds=1,
            model=model,
            on_activity=on_activity,
        )
        if run.error:
            raise PushError(f"redraft failed: {run.error}")
        tickets = to_tickets(parse_push(run.text))


def summarize(tickets: list[Ticket]) -> str:
    return json.dumps(
        [
            {
                "identifier": t.identifier,
                "title": t.title,
                "priority": t.priority,
                "blocked_by": t.blocked_by,
            }
            for t in tickets
        ],
        indent=2,
    )
