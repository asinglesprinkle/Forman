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
from .spawn import DEFAULT_MODEL, AgentRun, extract_last_json, run_agent
from .topo import CycleError, topo_sort

PUSH_MAX_TURNS = 20

PUSH_CONTRACT = """\
You turn a person's prose description of an issue into one or more Linear \
tickets. You may read the repository you are in to ground the ticket in real \
paths and names. Do not write or edit any code.

Splitting rule: create separate tickets when the concerns are independently \
shippable. Otherwise create one. Do not invent work the person did not describe.
Whenever ordering matters, populate blocked_by and blocks using the 1-based \
index of another ticket in your own list.

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

    raw = parse_push(run.text)
    tickets = to_tickets(raw)
    if dry_run:
        return tickets

    order = _creation_order(raw)
    identifier_for: dict[int, str] = {}
    created: list[Ticket] = []

    for pos in order:
        ticket = tickets[pos]
        ticket.blocked_by = [
            identifier_for[int(ref) - 1]
            for ref in ticket.blocked_by
            if str(ref).isdigit() and (int(ref) - 1) in identifier_for
        ]
        made = linear.create(ticket)
        identifier_for[pos] = made.identifier
        created.append(made)

    # `blocks` is the mirror of `blocked_by`; resolve it now that every ticket
    # has an identifier, purely so the returned objects are self-consistent.
    for pos, ticket in enumerate(tickets):
        ticket.blocks = [
            identifier_for[int(ref) - 1]
            for ref in ticket.blocks
            if str(ref).isdigit() and (int(ref) - 1) in identifier_for
        ]

    return [tickets[pos] for pos in sorted(identifier_for)]


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
