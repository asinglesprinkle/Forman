"""Ticket -> local sub-tasks.

LOCKED: sub-tasks are local only. They are files under `.foreman/<TICKET>/` plus
entries in state.json, and they are never pushed to Linear. Linear only ever
sees tickets.

The split itself is a model call, isolated behind the same runner seam as
`spawn_agent`, so the orchestration loop stays free of I/O and the tests can
replay a canned decomposition instead of paying for a real one. Sub-tasks are
numbered in dependency order, so `.01` can never depend on `.02`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .models import SubTask, SubTaskSpec, Ticket
from .spawn import DEFAULT_MODEL, AgentRun, extract_last_json, run_agent
from .state import StateStore
from .topo import CycleError, topo_sort

DECOMPOSE_MAX_TURNS = 30

DECOMPOSE_CONTRACT = """\
You are decomposing ONE ticket into local sub-tasks for an autonomous coding \
pipeline. Read the ticket and inspect the repository you are in to ground the \
split in what the code actually looks like. Do not write or edit any code; this \
is a planning step only.

Rules:
- Each sub-task must be independently executable by a fresh agent that sees only \
its own brief, the parent ticket, and the logs of completed siblings.
- Prefer the smallest number of sub-tasks that still leaves each one coherent. \
Between 1 and 8. A small ticket is allowed to be a single sub-task.
- Sub-tasks run SERIALLY, in the order you give, subject to depends_on.
- Use depends_on only for real ordering constraints, referring to the 1-based \
index of an earlier sub-task in your list.
- Stay inside the ticket's scope. Respect anything the ticket marks out of scope.

Emit as your final message a single JSON object and nothing else:
{"subtasks": [{"goal": "short imperative", "depends_on": [1], \
"definition_of_done": ["observable signal"], "files": ["path/one.py"], \
"test_plan": "how to verify", "notes": "gotchas"}]}
Only "goal" is required. Omit optional fields, or leave them empty, when you \
have nothing real to say - an empty section is worse than no section.\
"""


class DecompositionError(RuntimeError):
    """The decomposer produced something unusable. Treated as a run failure;
    the ticket is left alone for the human."""


# -- pure helpers ------------------------------------------------------------


def build_decompose_prompt(ticket: Ticket, repo_paths: list[str] | None = None) -> str:
    parts = [
        "# Ticket to decompose",
        "",
        f"{ticket.identifier}: {ticket.title}",
        "",
        ticket.description.strip() or "(no description)",
        "",
        f"- priority: {ticket.priority}",
        f"- labels: {', '.join(ticket.labels) if ticket.labels else '(none)'}",
        f"- estimate: {ticket.estimate or '(none)'}",
    ]
    if repo_paths:
        parts += ["", "# Paths worth looking at first", ""]
        parts += [f"- {p}" for p in repo_paths]
    parts += [
        "",
        "Decompose this ticket. End with the JSON object described in your "
        "instructions and nothing else.",
    ]
    return "\n".join(parts)


def parse_decomposition(text: str) -> list[SubTaskSpec]:
    """Read the decomposer's JSON into specs, with index-based dependencies."""
    payload = extract_last_json(text)
    if not isinstance(payload, dict) or "subtasks" not in payload:
        raise DecompositionError("decomposer emitted no `subtasks` object")

    raw = payload.get("subtasks")
    if not isinstance(raw, list) or not raw:
        raise DecompositionError("decomposer returned an empty sub-task list")

    specs: list[SubTaskSpec] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise DecompositionError(f"sub-task {i} is not an object")
        goal = str(item.get("goal", "")).strip()
        if not goal:
            raise DecompositionError(f"sub-task {i} has no goal")
        specs.append(
            SubTaskSpec(
                goal=goal,
                depends_on=[str(d) for d in item.get("depends_on") or []],
                definition_of_done=[
                    str(x) for x in item.get("definition_of_done") or [] if str(x).strip()
                ],
                files=[str(x) for x in item.get("files") or [] if str(x).strip()],
                test_plan=str(item.get("test_plan") or "").strip(),
                notes=str(item.get("notes") or "").strip(),
            )
        )
    return specs


def _dependency_index(ref: str, count: int) -> int | None:
    """Resolve a depends_on entry to a 0-based position.

    Accepts a 1-based index ("2", 2) or a trailing sub-task number ("TEAM-42.02").
    Anything unresolvable is dropped rather than failing the run: a hallucinated
    reference should not cost the whole decomposition.
    """
    ref = str(ref).strip()
    if ref.isdigit():
        pos = int(ref) - 1
        return pos if 0 <= pos < count else None
    tail = ref.rsplit(".", 1)[-1]
    if tail.isdigit():
        pos = int(tail) - 1
        return pos if 0 <= pos < count else None
    return None


def order_and_number(
    ticket_id: str, specs: list[SubTaskSpec]
) -> list[tuple[SubTask, SubTaskSpec]]:
    """Topo-sort the specs, then number them `<TICKET>.01`, `.02`, ...

    Numbering after sorting is what guarantees that reading the manifest top to
    bottom is also a legal execution order. Each sub-task is returned paired
    with the spec it came from, so the caller can render the brief without
    matching on goal text.
    """
    count = len(specs)
    graph: dict[str, list[str]] = {}
    for i, spec in enumerate(specs):
        deps = {
            str(pos)
            for pos in (_dependency_index(d, count) for d in spec.depends_on)
            if pos is not None and pos != i
        }
        graph[str(i)] = sorted(deps, key=int)

    try:
        order = [int(x) for x in topo_sort(graph)]
    except CycleError as exc:
        raise DecompositionError(
            f"decomposer produced a circular dependency among sub-tasks {exc.remaining}"
        ) from exc

    id_for = {pos: f"{ticket_id}.{n:02d}" for n, pos in enumerate(order, start=1)}
    return [
        (
            SubTask(
                id=id_for[pos],
                goal=specs[pos].goal,
                depends_on=[id_for[int(d)] for d in graph[str(pos)]],
            ),
            specs[pos],
        )
        for pos in order
    ]


def render_subtask_readme(subtask: SubTask, parent: Ticket, spec: SubTaskSpec) -> str:
    """Render the sub-task brief. Optional sections are omitted unless real."""
    lines = [
        "---",
        f"subtask_id: {subtask.id}",
        f"parent: {parent.identifier}",
        "status: pending",
        f"depends_on: [{', '.join(subtask.depends_on)}]",
        "---",
        "",
        "## Goal",
        spec.goal,
        "",
        "## Definition of done",
    ]
    if spec.definition_of_done:
        lines += [f"- [ ] {item}" for item in spec.definition_of_done]
    else:
        lines.append(f"- [ ] {spec.goal}")

    if spec.files:
        lines += ["", "## Likely files / touchpoints"]
        lines += [f"- {p}" for p in spec.files]
    if spec.test_plan:
        lines += ["", "## Test plan", spec.test_plan]
    if spec.notes:
        lines += ["", "## Notes for executor", spec.notes]

    lines += [
        "",
        "---",
        "## Execution log",
        "<!-- spawn appends below this line; never edits above it -->",
        "",
    ]
    return "\n".join(lines)


# -- the activity ------------------------------------------------------------


def decompose(
    *,
    ticket: Ticket,
    store: StateStore,
    cwd: str | Path,
    repo_paths: list[str] | None = None,
    runner: Callable[..., AgentRun] = run_agent,
    model: str | None = DEFAULT_MODEL,
) -> list[SubTask]:
    """Split a ticket, write one README per sub-task, return the seeded list.

    The caller owns state.json; this writes only the sub-task briefs.
    """
    run = runner(
        prompt=build_decompose_prompt(ticket, repo_paths),
        system_prompt=DECOMPOSE_CONTRACT,
        cwd=cwd,
        allowed_tools=["Read", "Grep", "Glob"],  # planning only, no writes
        max_turns=DECOMPOSE_MAX_TURNS,
        model=model,
    )
    if run.error:
        raise DecompositionError(f"decomposer session failed: {run.error}")

    specs = parse_decomposition(run.text)
    paired = order_and_number(ticket.identifier, specs)

    for subtask, spec in paired:
        store.write_subtask_readme(
            ticket.identifier,
            subtask.id,
            render_subtask_readme(subtask, ticket, spec),
        )
    return [subtask for subtask, _ in paired]
