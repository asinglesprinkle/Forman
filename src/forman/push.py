"""Push phase: prose in, Linear tickets out.

I describe an issue in prose; an agent fills the ticket template and creates one
or more tickets. This is the only phase that writes to Linear on purpose.

Splitting rule: separate tickets when concerns are independently shippable.
Whenever ordering matters, blocked_by and blocks must be populated, because the
pull phase's ticket-level topo-sort is built on exactly those fields.

Those fields take two kinds of reference: a 1-based index into the batch being
drafted, and the identifier of a ticket that already exists. The second kind is
what lets a push depend on work filed an hour ago, and the drafting agent is
given the open backlog so it can name one. Everything here refuses loudly rather
than dropping a reference it cannot resolve: an ordering edge that goes missing
between the draft and Linear is invisible until the pull phase works the wrong
ticket first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

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
    READ_ONLY_TOOLS,
    Activity,
    AgentRun,
    extract_last_json,
    run_agent,
    run_conversation,
)
from .topo import CycleError, topo_sort

PUSH_MAX_TURNS = 20

MAX_QUESTION_ROUNDS = 3

# How many open tickets to show the drafting agent. Enough to cover a project's
# worth of in-flight work without turning the prompt into a backlog dump.
BACKLOG_LIMIT = 40

# `TEAM-42`. Anything matching this in blocked_by/blocks is a ticket that already
# exists, as opposed to a 1-based index into the batch being drafted.
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")

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
shippable. Otherwise create one. Do not invent work the person did not describe.

Whenever ordering matters, populate blocked_by and blocks. There are two ways to \
name a ticket there, and you may mix them freely:

- a ticket in your own list, by its 1-based index: 2
- a ticket that already exists, by its identifier: TEAM-42

Ordering recorded anywhere else is invisible to the pipeline that executes these \
tickets. A note in the context section saying "a sibling slice owns the database \
containers", or a number in the title, will not stop the executor from picking \
this ticket up first. If one piece of work genuinely cannot start until another \
has landed, it has to appear in blocked_by.\
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
several, what order they have to happen in. Any open tickets that already exist \
are listed for you below; if this work has to wait on one of them, that belongs \
in blocked_by, not in prose.

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


def _index_ref(ref: str, count: int) -> int | None:
    """A 1-based index into the batch being pushed, as a 0-based position."""
    text = str(ref).strip()
    if not text.isdigit():
        return None
    pos = int(text) - 1
    return pos if 0 <= pos < count else None


def _identifier_ref(ref: str) -> str | None:
    """A reference to a ticket that already exists, normalised."""
    text = str(ref).strip()
    return text.upper() if _IDENTIFIER.match(text) else None


def ref_problems(tickets: list[Ticket]) -> list[str]:
    """Every ordering reference that cannot mean anything, in plain words.

    Silence here used to be the entire bug. A blocked_by naming a real ticket
    was dropped by a list comprehension that only understood indices, so the
    ordering the drafting agent had worked out evaporated between the draft and
    Linear — and the pull phase, reading a board with no relations on it, then
    picked whatever it liked.
    """
    count = len(tickets)
    problems: list[str] = []
    for pos, ticket in enumerate(tickets, start=1):
        for field_name in ("blocked_by", "blocks"):
            for ref in getattr(ticket, field_name):
                text = str(ref).strip()
                if not text:
                    continue
                index = _index_ref(text, count)
                if index is not None:
                    if index == pos - 1:
                        problems.append(
                            f"ticket {pos} ({ticket.title!r}) lists itself in "
                            f"{field_name}"
                        )
                    continue
                if _identifier_ref(text):
                    continue
                problems.append(
                    f"ticket {pos} ({ticket.title!r}) has {field_name} {text!r}, "
                    f"which is neither an index into this batch (1-{count}) nor "
                    "a ticket identifier like TEAM-42"
                )
    return problems


def validate_refs(tickets: list[Ticket]) -> None:
    """Refuse to create a batch whose ordering cannot be recorded.

    Fatal and before creation on purpose: a bad reference should cost a
    re-draft, not leave a half-ordered backlog behind.
    """
    problems = ref_problems(tickets)
    if problems:
        raise PushError("; ".join(problems))


def _resolve_refs(
    refs: list[str], identifier_for: dict[int, str], count: int
) -> list[str]:
    """Rewrite a ref list into real identifiers, keeping order and dropping
    duplicates.

    Indices resolve through `identifier_for`; anything that was already an
    identifier is carried straight through, which is the whole point — a batch
    is allowed to depend on work pushed weeks ago.
    """
    out: list[str] = []
    for ref in refs:
        text = str(ref).strip()
        index = _index_ref(text, count)
        if index is not None:
            if index in identifier_for:
                out.append(identifier_for[index])
            continue
        known = _identifier_ref(text)
        if known:
            out.append(known)

    seen: set[str] = set()
    return [ref for ref in out if not (ref in seen or seen.add(ref))]


def _creation_order(tickets: list[dict]) -> list[int]:
    """Create blockers before the things they block, so index references can be
    rewritten into real identifiers as we go.

    Only index references constrain this order. A reference to a ticket that
    already exists imposes nothing: it was created long before this batch
    started.
    """
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
                estimate=(
                    str(fields["estimate"]).lower() if fields.get("estimate") else None
                ),
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


def _record_relations(
    tickets: list[Ticket],
    linear: LinearClient,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Write every ordering edge to Linear, once each, in both notations.

    `blocks` used to be decoration: only `blocked_by` was ever sent, so a batch
    that expressed its ordering the other way round — the natural way for a
    foundational ticket, which knows what it enables before those tickets have
    been written — recorded nothing at all.

    This runs after every ticket exists rather than during creation, so a
    `blocks` pointing forward at a sibling still to be created is recordable.
    """
    edges: list[tuple[str, str]] = []
    for ticket in tickets:
        edges += [(blocker, ticket.identifier) for blocker in ticket.blocked_by]
        edges += [(ticket.identifier, blocked) for blocked in ticket.blocks]

    seen: set[tuple[str, str]] = set()
    for blocker, blocked in edges:
        if not blocker or not blocked or blocker == blocked:
            continue
        if (blocker, blocked) in seen:
            continue
        seen.add((blocker, blocked))
        try:
            linear.relate_blocks(blocker, blocked)
        except Exception as exc:  # noqa: BLE001 - the backend's error, whatever it is
            # The tickets exist by now, so a refused relation cannot undo the
            # push. It must not pass unnoticed either: a lost edge is lost
            # ordering, and the pull phase will cheerfully work the wrong
            # ticket next with no sign anything went missing.
            if warn:
                warn(f"could not record that {blocker} blocks {blocked}: {exc}")


def create_tickets(
    tickets: list[Ticket],
    linear: LinearClient,
    warn: Callable[[str], None] | None = None,
) -> list[Ticket]:
    """Create tickets blockers-first, then record the ordering between them."""
    validate_refs(tickets)
    count = len(tickets)
    order = _creation_order([{"blocked_by": t.blocked_by} for t in tickets])
    identifier_for: dict[int, str] = {}

    for pos in order:
        ticket = tickets[pos]
        # Every index this ticket names is already in `identifier_for`, because
        # `order` put its blockers first. Identifier references pass through.
        ticket.blocked_by = _resolve_refs(ticket.blocked_by, identifier_for, count)
        made = linear.create(ticket)
        identifier_for[pos] = made.identifier

    # `blocks` points forward, so it can only be resolved once every ticket in
    # the batch has an identifier.
    for ticket in tickets:
        ticket.blocks = _resolve_refs(ticket.blocks, identifier_for, count)

    _record_relations(tickets, linear, warn)
    return [tickets[pos] for pos in sorted(identifier_for)]


def _natural_key(identifier: str) -> tuple[str, int]:
    prefix, _, number = identifier.rpartition("-")
    return (prefix, int(number)) if number.isdigit() else (identifier, 0)


def backlog_digest(linear: LinearClient) -> str:
    """The open backlog, rendered for the drafting agent.

    Without this, an agent cannot reference work that already exists, so a
    ticket that depends on one pushed an hour ago records the dependency as
    English in its context section and nowhere the pipeline can act on. The
    agent has always been able to read the repository; this lets it read the
    board too.

    A backlog it cannot fetch is not worth failing a draft over.
    """
    try:
        tickets = [
            t for t in linear.list_assigned() if t.identifier and not t.is_done()
        ]
    except Exception:  # noqa: BLE001 - drafting is more useful than the listing
        return ""
    if not tickets:
        return ""

    tickets.sort(key=lambda t: _natural_key(t.identifier))
    rows = [f"{t.identifier}  [{t.status}]  {t.title}" for t in tickets[:BACKLOG_LIMIT]]
    return (
        "These tickets already exist and are still open. If what you are "
        "drafting cannot start until one of them has landed, put that "
        "identifier in blocked_by:\n\n" + "\n".join(rows)
    )


def _opening(lead: str, prose: str, linear: LinearClient) -> str:
    parts = [f"{lead}\n\n{prose.strip()}"]
    digest = backlog_digest(linear)
    if digest:
        parts.append(digest)
    return "\n\n".join(parts)


def push(
    *,
    prose: str,
    linear: LinearClient,
    cwd: str | Path = ".",
    runner: Callable[..., AgentRun] = run_agent,
    model: str | None = DEFAULT_MODEL,
    dry_run: bool = False,
    warn: Callable[[str], None] | None = None,
) -> list[Ticket]:
    """Turn prose into tickets, and create them unless dry_run is set."""
    run = runner(
        prompt=_opening("Turn this into Linear tickets:", prose, linear),
        system_prompt=PUSH_CONTRACT,
        cwd=cwd,
        allowed_tools=READ_ONLY_TOOLS,
        max_turns=PUSH_MAX_TURNS,
        model=model,
    )
    if run.error:
        raise PushError(f"push session failed: {run.error}")

    tickets = to_tickets(parse_push(run.text))
    if dry_run:
        validate_refs(tickets)  # say so now, not on the run that does create them
        return tickets
    return create_tickets(tickets, linear, warn)


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
    warn: Callable[[str], None] | None = None,
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
            raise TypeError(
                "push_interactive needs either reviewer, or both ask and show"
            )
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
        opening=_opening("Here is what I want to achieve:", prose, linear),
        respond=respond,
        cwd=cwd,
        allowed_tools=READ_ONLY_TOOLS,
        max_rounds=MAX_QUESTION_ROUNDS + 2,
        model=model,
        on_activity=on_activity,
    )
    if run.error:
        raise PushError(f"push session failed: {run.error}")

    tickets = to_tickets(parse_push(run.text))

    while True:
        # Shown rather than raised: the reviewer can edit the draft or ask for a
        # redraft, both of which fix this. create_tickets still refuses if they
        # approve it anyway.
        for problem in ref_problems(tickets):
            reviewer.show(f"Ordering problem: {problem}")

        decision = reviewer.decide(
            Approval(tickets=tickets, rendered=render_drafts(tickets))
        )

        if decision.action == CREATE:
            return create_tickets(tickets, linear, warn)

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
                _opening("Here is what I want to achieve:", prose, linear) + "\n\n"
                f"You drafted:\n\n{render_drafts(tickets)}\n\n"
                f"Revise it: {decision.feedback}\n\n"
                "Redraft now; do not ask more questions."
            ),
            respond=lambda _text: None,
            cwd=cwd,
            allowed_tools=READ_ONLY_TOOLS,
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
                "blocks": t.blocks,
            }
            for t in tickets
        ],
        indent=2,
    )
