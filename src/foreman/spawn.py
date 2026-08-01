"""The agent spawn boundary.

LOCKED: fresh context per spawn. One `query()` call is one brand-new session
with no memory of any other call, which IS the fresh-context requirement. The
session receives only: its own sub-task README, the parent ticket, relevant repo
paths, and read-only logs of already-completed siblings. Never the
orchestrator's history.

Everything the SDK touches is confined to `run_agent`. `spawn_agent` above it is
pure prompt-building and result-parsing, so the interesting logic can be unit
tested with a fake runner and no model calls at all.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import SpawnResult, SubTaskStatus, Ticket

DEFAULT_MAX_TURNS = 40
DEFAULT_MODEL = "claude-opus-5"

# Tools a sub-task agent may use. Bash is needed for tests and git, which means
# it cannot be narrowed much; permission_mode keeps edits from prompting.
DEFAULT_TOOLS = ["Bash", "Edit", "Read", "Grep"]

SPAWN_CONTRACT = """\
You are executing ONE sub-task defined in the provided README. Do only what that \
README specifies; its parent ticket and sibling logs are context, not a to-do \
list. When finished, append a concise summary of what you did (and any decisions) \
under the README's `## Execution log` heading - append only, never edit the brief \
above it. Then emit as your final message a single JSON object and nothing else: \
{"status": "done" | "blocked" | "failed", "summary": "...", "blocked_reason": null | "..."}. \
Use `blocked` only for external blockers you cannot resolve (a decision only the \
human can make, a missing credential, an unfinished upstream dependency).\
"""


@dataclass
class AgentRun:
    """Raw outcome of one SDK session, before any pipeline meaning is attached."""

    text: str = ""
    session_id: str | None = None
    total_cost_usd: float | None = None
    error: str | None = None
    turn_limit_hit: bool = False


class AgentRunner(Protocol):
    """The seam. Tests pass a fake; production passes `run_agent`."""

    def __call__(
        self,
        *,
        prompt: str,
        system_prompt: str,
        cwd: str | Path,
        allowed_tools: list[str] | None = ...,
        max_turns: int = ...,
        model: str | None = ...,
        mcp_servers: dict[str, Any] | None = ...,
    ) -> AgentRun: ...


# -- the only place the SDK is imported --------------------------------------


def run_agent(
    *,
    prompt: str,
    system_prompt: str,
    cwd: str | Path,
    allowed_tools: list[str] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str | None = DEFAULT_MODEL,
    mcp_servers: dict[str, Any] | None = None,
) -> AgentRun:
    """Run one fresh Claude Agent SDK session and collect its output.

    Imported lazily so the rest of the pipeline (and the whole test suite) runs
    without claude-agent-sdk installed.
    """
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )
    except ImportError as exc:  # pragma: no cover - environment dependent
        return AgentRun(error=f"claude-agent-sdk is not installed: {exc}")

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=list(allowed_tools or DEFAULT_TOOLS),
        permission_mode="acceptEdits",
        max_turns=max_turns,
        cwd=str(cwd),
        model=model,
        **({"mcp_servers": mcp_servers} if mcp_servers else {}),
    )

    async def _collect() -> AgentRun:
        run = AgentRun()
        chunks: list[str] = []
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                    continue
                # The terminal message carries session and cost metadata. Field
                # names have moved across SDK versions, so read defensively
                # rather than depending on a shape we cannot pin here.
                for attr in ("session_id", "total_cost_usd"):
                    value = getattr(message, attr, None)
                    if value is not None:
                        setattr(run, attr, value)
                subtype = getattr(message, "subtype", None)
                terminal = getattr(message, "terminal_reason", None)
                if terminal == "max_turns" or subtype == "error_max_turns":
                    run.turn_limit_hit = True
                if isinstance(subtype, str) and subtype.endswith("error"):
                    run.error = f"agent session ended with subtype={subtype}"
        except Exception as exc:  # SDK or infra failure: a `failed` result
            run.error = f"{type(exc).__name__}: {exc}"
        run.text = "\n".join(chunks).strip()
        return run

    return asyncio.run(_collect())


# -- prompt building and result parsing (pure) -------------------------------


def build_subtask_prompt(
    *,
    subtask_readme: str,
    parent_ticket: Ticket,
    repo_paths: list[str],
    sibling_logs: list[tuple[str, str]],
    attempt: int = 1,
    previous_error: str | None = None,
) -> str:
    """Assemble the entire context a sub-task agent is allowed to see."""
    parts: list[str] = []
    if attempt > 1:
        # The working tree is deliberately left as the failed attempt left it.
        # A turn-limit failure often means most of the work is already correct,
        # and finishing it beats redoing it. Say so plainly, because an agent
        # that assumes a clean slate will duplicate edits.
        parts += [
            f"# Retry (attempt {attempt})",
            "",
            "A previous attempt at this exact sub-task did not finish"
            + (f": {previous_error}" if previous_error else ".")
            + "",
            "",
            "The working tree may already contain partial changes from it. Read "
            "the current state of the relevant files before editing anything, "
            "keep whatever is already correct, and finish the job rather than "
            "starting over. Check the execution log in your README for what the "
            "previous attempt recorded.",
            "",
        ]
    parts += [
        "# Your sub-task",
        "",
        subtask_readme.strip(),
        "",
        "# Parent ticket (context only, not a to-do list)",
        "",
        f"{parent_ticket.identifier}: {parent_ticket.title}",
        "",
        parent_ticket.description.strip() or "(no description)",
    ]
    if repo_paths:
        parts += ["", "# Relevant repo paths", ""]
        parts += [f"- {p}" for p in repo_paths]
    if sibling_logs:
        parts += [
            "",
            "# Completed sibling sub-tasks (read-only, for context)",
            "",
        ]
        for sid, log in sibling_logs:
            parts += [f"## {sid}", (log or "").strip() or "(no log recorded)", ""]
    parts += [
        "",
        "Do only what your sub-task README specifies. End with the JSON object "
        "described in your instructions and nothing else.",
    ]
    return "\n".join(parts)


def extract_last_json(text: str) -> dict | None:
    """Pull the final JSON object out of an agent's last message.

    Agents wrap output in prose or code fences no matter how firmly you ask them
    not to, so scan for the last well-formed object that looks like our contract
    rather than trying to parse the whole message.
    """
    decoder = json.JSONDecoder()
    found: list[dict] = []
    index = 0
    while True:
        index = text.find("{", index)
        if index == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        index = end
    for obj in reversed(found):
        if "status" in obj:
            return obj
    return found[-1] if found else None


def result_from_run(run: AgentRun) -> SpawnResult:
    """Turn a raw session into a pipeline outcome.

    An SDK error or a turn-limit hit is `failed`, which the orchestrator will
    retry once. A clean run reporting it cannot proceed is `blocked`, a normal
    business outcome that is never retried. Missing or malformed JSON is
    `failed`, with the raw text captured so the human can see what the agent
    actually said.
    """
    meta = {
        "session_id": run.session_id,
        "total_cost_usd": run.total_cost_usd,
        "raw": run.text or None,
    }
    if run.error:
        return SpawnResult(status=SubTaskStatus.FAILED.value, error=run.error, **meta)
    if run.turn_limit_hit:
        return SpawnResult(
            status=SubTaskStatus.FAILED.value,
            error="agent hit its turn limit before finishing",
            **meta,
        )

    payload = extract_last_json(run.text)
    if payload is None:
        return SpawnResult(
            status=SubTaskStatus.FAILED.value,
            error="agent emitted no JSON result object",
            **meta,
        )

    status = str(payload.get("status", "")).strip().lower()
    summary = str(payload.get("summary", "") or "")
    reason = payload.get("blocked_reason")

    if status == SubTaskStatus.DONE.value:
        return SpawnResult(status=status, summary=summary, **meta)
    if status == SubTaskStatus.BLOCKED.value:
        return SpawnResult(
            status=status,
            summary=summary,
            blocked_reason=str(reason or "agent reported a blocker but gave no reason"),
            **meta,
        )
    if status == SubTaskStatus.FAILED.value:
        return SpawnResult(
            status=status,
            summary=summary,
            error=str(reason or summary or "agent reported failure"),
            **meta,
        )
    return SpawnResult(
        status=SubTaskStatus.FAILED.value,
        error=f"agent returned an unrecognized status: {status!r}",
        **meta,
    )


# -- the activity ------------------------------------------------------------


def spawn_agent(
    *,
    subtask_readme: str,
    parent_ticket: Ticket,
    repo_paths: list[str],
    sibling_logs: list[tuple[str, str]],
    cwd: str | Path,
    attempt: int = 1,
    previous_error: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str | None = DEFAULT_MODEL,
    runner: AgentRunner | Callable[..., AgentRun] = run_agent,
) -> SpawnResult:
    """Run one sub-task in a fresh agent session and report the outcome."""
    prompt = build_subtask_prompt(
        subtask_readme=subtask_readme,
        parent_ticket=parent_ticket,
        repo_paths=repo_paths,
        sibling_logs=sibling_logs,
        attempt=attempt,
        previous_error=previous_error,
    )
    run = runner(
        prompt=prompt,
        system_prompt=SPAWN_CONTRACT,
        cwd=cwd,
        allowed_tools=DEFAULT_TOOLS,
        max_turns=max_turns,
        model=model,
    )
    return result_from_run(run)
