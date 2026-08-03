"""state.json read/write plus manifest.md rendering.

LOCKED: state.json is the source of truth. manifest.md is rendered from it on
every write, is never hand-edited, and is never parsed back. If the two ever
disagree, state.json wins and the next save fixes the manifest.

Layout, inside the TARGET repo:

    .forman/
      <TICKET>/
        state.json
        manifest.md
        <TICKET>.01.md      <- sub-task README
        <TICKET>.02.md
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import SubTask, SubTaskStatus, TicketState

FORMAN_DIR = ".forman"
STATE_FILE = "state.json"
MANIFEST_FILE = "manifest.md"

# Everything the spawned agent appends goes below this line in a sub-task
# README. The brief above it is never edited.
EXECUTION_LOG_HEADING = "## Execution log"


class StateStore:
    """All filesystem access for pipeline bookkeeping.

    This is an activity boundary: the orchestrator calls these methods and never
    touches a path itself.
    """

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    # -- paths ---------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.repo_root / FORMAN_DIR

    def dir_for(self, ticket: str) -> Path:
        return self.root / ticket

    def state_path(self, ticket: str) -> Path:
        return self.dir_for(ticket) / STATE_FILE

    def manifest_path(self, ticket: str) -> Path:
        return self.dir_for(ticket) / MANIFEST_FILE

    def subtask_path(self, ticket: str, subtask_id: str) -> Path:
        return self.dir_for(ticket) / f"{subtask_id}.md"

    # -- lifecycle -----------------------------------------------------------

    def exists(self, ticket: str) -> bool:
        return self.state_path(ticket).is_file()

    def init(self, ticket: str) -> Path:
        d = self.dir_for(ticket)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def load(self, ticket: str) -> TicketState:
        raw = json.loads(self.state_path(ticket).read_text(encoding="utf-8"))
        return state_from_dict(raw)

    def save(self, state: TicketState) -> None:
        """Write state.json, then re-render manifest.md. Always in that order."""
        self.init(state.ticket)
        self.state_path(state.ticket).write_text(
            json.dumps(state_to_dict(state), indent=2) + "\n", encoding="utf-8"
        )
        self.manifest_path(state.ticket).write_text(
            render_manifest(state), encoding="utf-8"
        )

    def write_subtask_readme(self, ticket: str, subtask_id: str, body: str) -> Path:
        self.init(ticket)
        path = self.subtask_path(ticket, subtask_id)
        path.write_text(body, encoding="utf-8")
        return path

    def read_subtask_readme(self, ticket: str, subtask_id: str) -> str:
        return self.subtask_path(ticket, subtask_id).read_text(encoding="utf-8")

    def append_execution_log(self, ticket: str, subtask_id: str, text: str) -> None:
        """Append to a sub-task README below the execution-log heading.

        The spawned agent is asked to do this itself. We also do it from the
        orchestrator side so a failed or blocked run still leaves a record, and
        so the file is right even when an agent ignores the instruction.
        """
        path = self.subtask_path(ticket, subtask_id)
        if not path.is_file():
            return
        body = path.read_text(encoding="utf-8").rstrip("\n")
        if EXECUTION_LOG_HEADING not in body:
            body += f"\n\n{EXECUTION_LOG_HEADING}\n"
        path.write_text(body + "\n\n" + text.strip() + "\n", encoding="utf-8")

    def tickets(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / STATE_FILE).is_file())


# -- serialization -----------------------------------------------------------


def state_to_dict(state: TicketState) -> dict:
    d = asdict(state)
    d["subtasks"] = [asdict(st) for st in state.subtasks]
    return d


def state_from_dict(raw: dict) -> TicketState:
    subtasks = [SubTask(**st) for st in raw.get("subtasks", [])]
    fields = {k: v for k, v in raw.items() if k != "subtasks"}
    return TicketState(subtasks=subtasks, **fields)


# -- manifest rendering ------------------------------------------------------

_MARKERS = {
    SubTaskStatus.DONE.value: "[x]",
    SubTaskStatus.IN_PROGRESS.value: "[~]",
    SubTaskStatus.PENDING.value: "[ ]",
    SubTaskStatus.BLOCKED.value: "[!]",
    SubTaskStatus.FAILED.value: "[X]",
}


def total_cost_usd(state: TicketState) -> float:
    """What the ticket has cost so far, in USD.

    The single place the sum is computed. render_manifest and the orchestrator's
    RunReport both call this, so the manifest and the report can never disagree.

    A sub-task with no recorded cost counts as zero: that is either a sub-task
    that has not run yet, or one written by a Forman old enough not to have
    captured the figure. Neither is worth refusing to add up. Rounded because
    summing floats otherwise produces things like 0.30000000000000004.
    """
    return round(sum(st.cost_usd or 0.0 for st in state.subtasks), 6)


def format_cost(value: float | None) -> str:
    """USD to four places. Four, not two, so a few-cent session is not $0.00."""
    return f"${value or 0.0:.4f}"


def render_manifest(state: TicketState) -> str:
    """Render state.json as a human-readable checklist.

    Rendered, never parsed. Safe to open mid-run to see where things stand.
    """
    done = state.done_ids()
    lines: list[str] = [
        f"# {state.ticket}" + (f": {state.title}" if state.title else ""),
        "",
        f"- status: `{state.status}`",
        f"- branch: `{state.branch}`" if state.branch else "- branch: (not created)",
        f"- pulled at: {state.pulled_at}"
        if state.pulled_at
        else "- pulled at: (unknown)",
    ]
    if state.pr_url:
        lines.append(f"- pull request: {state.pr_url}")
    lines += [
        "",
        "<!-- Rendered from state.json on every write. Do not edit by hand. -->",
        "",
        "## Sub-tasks",
        "",
    ]

    if not state.subtasks:
        lines.append("_(not decomposed yet)_")
    for st in state.subtasks:
        marker = _MARKERS.get(st.status, "[ ]")
        note = ""
        if st.status == SubTaskStatus.BLOCKED.value:
            note = f"  (blocked: {st.blocked_reason or 'no reason recorded'})"
        elif st.status == SubTaskStatus.FAILED.value:
            note = f"  (failed: {st.blocked_reason or 'no reason recorded'})"
        elif st.status == SubTaskStatus.IN_PROGRESS.value:
            note = "  (in progress)"
        elif st.status == SubTaskStatus.PENDING.value:
            unmet = [d for d in st.depends_on if d not in done]
            if unmet:
                note = f"  (waiting on {', '.join(unmet)})"
        cost = f"  {format_cost(st.cost_usd)}" if st.cost_usd is not None else ""
        lines.append(f"- {marker} `{st.id}` {st.goal}{note}{cost}")

    lines += [
        "",
        f"**Total: {format_cost(total_cost_usd(state))}**",
        "",
    ]
    return "\n".join(lines)


# -- queries the orchestrator needs -----------------------------------------


def reset_for_resume(
    state: TicketState, include_blocked: bool = True
) -> list[tuple[str, str]]:
    """Put failed (and by default blocked) sub-tasks back to pending.

    Nothing in the loop does this on its own, and that is deliberate: a failed
    or blocked sub-task means a human has to look. Resuming is that human
    saying they have looked. Returns what changed, so the caller can report it.

    Done sub-tasks are never touched, which is what makes a re-run skip work
    that already succeeded.
    """
    wanted = {SubTaskStatus.FAILED.value}
    if include_blocked:
        wanted.add(SubTaskStatus.BLOCKED.value)

    changed: list[tuple[str, str]] = []
    for st in state.subtasks:
        if st.status in wanted:
            changed.append((st.id, st.status))
            st.status = SubTaskStatus.PENDING.value
            st.blocked_reason = None
            st.started_at = None
            st.finished_at = None
    return changed


def next_ready_subtask(state: TicketState) -> SubTask | None:
    """The next pending sub-task whose dependencies are all done.

    Returns None when nothing can run. With a correct topo-sort that means the
    remaining work is blocked, which is terminal for this run: a later sibling
    can never retroactively unblock an earlier one. There is deliberately no
    retry pass.
    """
    done = state.done_ids()
    for st in state.subtasks:
        if st.status != SubTaskStatus.PENDING.value:
            continue
        if all(d in done for d in st.depends_on):
            return st
    return None
