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
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import SpawnResult, SubTaskStatus, Ticket

DEFAULT_MAX_TURNS = 40
DEFAULT_MODEL = "claude-opus-5"

# Tools a sub-task agent may use. Bash is needed for tests and git, which means
# it cannot be narrowed much; permission_mode keeps edits from prompting.
DEFAULT_TOOLS = ["Bash", "Edit", "Read", "Grep"]

# What an agent that is only meant to look gets. Every planning and drafting
# session in this codebase runs on these: they read the repository to ground
# what they write, and write nothing themselves.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]

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


@dataclass(frozen=True)
class Activity:
    """One observable thing an agent did mid-session.

    Reported live, while a conversation is still running, so a caller holding a
    terminal can show that the thing is alive. Facts only: what tool, with what
    input. Rendering is the caller's business, because only the caller knows
    whether it is writing to a TTY, a log, or nothing at all.
    """

    kind: str  # "tool" or "thinking"
    tool: str = ""
    tool_input: dict[str, Any] | None = None


# The argument that best says what a call is *about*, per tool. Anything not
# listed falls back to no detail rather than dumping an arbitrary input dict to
# someone's terminal.
_ACTIVITY_DETAIL = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Bash": "command",
}


def describe_activity(activity: Activity, *, width: int = 60) -> str:
    """Render an Activity as one short line. Pure, so it can be tested."""
    if activity.kind == "thinking":
        return "thinking"
    detail = ""
    key = _ACTIVITY_DETAIL.get(activity.tool)
    if key:
        detail = str((activity.tool_input or {}).get(key) or "").strip()
        detail = " ".join(detail.split())
        if len(detail) > width:
            detail = detail[: width - 1] + "…"
    return f"{activity.tool.lower()} {detail}".strip()


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
        on_activity: Callable[[Activity], None] | None = ...,
    ) -> AgentRun: ...


def _options(options_cls, *, tools: list[str], **rest):
    """Build the SDK options every session in this codebase runs under.

    Three of these are the difference between an agent that does what its
    contract says and one that can do anything the machine can:

    `tools` is the real restriction. `allowed_tools` only says which tools skip
    the permission prompt - the model keeps the full built-in set regardless -
    so a ticket-drafting agent told it may use Read/Grep/Glob was still handed
    Bash, and used it to read around the filesystem outside the repo.

    `mcp_servers` and `strict_mcp_config` cut the session off from whatever MCP
    servers the person running it happens to have configured. Without this the
    SDK loads the user's own config, and a planning agent found itself holding
    write access to the very Linear workspace this pipeline is careful to only
    touch through a human gate. Nothing here should reach Linear except through
    `linear_client`.

    `setting_sources` keeps ~/.claude settings out for the same reason: what a
    session may do should come from this file, not from the machine it is on.

    Passed positionally rather than imported so the SDK import stays lazy, and
    constructed directly rather than filtered so an SDK too old to know these
    fields fails loudly instead of quietly running an unrestricted agent.
    """
    return options_cls(
        tools=list(tools),
        allowed_tools=list(tools),  # already the limit; also skip the prompts
        mcp_servers={},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="acceptEdits",
        **rest,
    )


def _reporter(
    on_activity: Callable[[Activity], None] | None,
) -> Callable[[Any], None]:
    """Turn a message's content blocks into Activity, and report them.

    Shared by both runners because both watch the same stream for the same
    reason. Errors from the callback are swallowed deliberately: a progress
    display must never be able to kill a session that is midway through
    creating things.
    """
    if on_activity is None:
        return lambda _blocks: None

    def report(blocks: Any) -> None:
        # Anything raised in here - the import, the block walk, the callback -
        # is a broken display, not a broken run, so it is swallowed whole.
        with contextlib.suppress(Exception):
            from claude_agent_sdk import (  # type: ignore[import-not-found]
                ThinkingBlock,
                ToolUseBlock,
            )

            for block in blocks:
                if isinstance(block, ToolUseBlock):
                    on_activity(
                        Activity(
                            kind="tool",
                            tool=block.name,
                            tool_input=dict(block.input or {}),
                        )
                    )
                elif isinstance(block, ThinkingBlock):
                    on_activity(Activity(kind="thinking"))

    return report


# -- the only place the SDK is imported --------------------------------------


def run_agent(
    *,
    prompt: str,
    system_prompt: str,
    cwd: str | Path,
    allowed_tools: list[str] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str | None = DEFAULT_MODEL,
    on_activity: Callable[[Activity], None] | None = None,
) -> AgentRun:
    """Run one fresh Claude Agent SDK session and collect its output.

    Imported lazily so the rest of the pipeline (and the whole test suite) runs
    without claude-agent-sdk installed.

    `on_activity` is called as the agent works. A sub-task session is the
    longest-running thing here by far - it edits, runs tests, and commits - and
    it says nothing at all until it is finished.
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

    options = _options(
        ClaudeAgentOptions,
        tools=allowed_tools or DEFAULT_TOOLS,
        system_prompt=system_prompt,
        max_turns=max_turns,
        cwd=str(cwd),
        model=model,
    )

    report = _reporter(on_activity)

    async def _collect() -> AgentRun:
        run = AgentRun()
        chunks: list[str] = []
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    report(message.content)
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
        # The catch has to be blind. Whatever the SDK or the infrastructure
        # under it raises is the same thing to a caller: a run that failed,
        # reported as one rather than as a traceback out of the orchestrator.
        except Exception as exc:  # noqa: BLE001 - see comment above
            run.error = f"{type(exc).__name__}: {exc}"
        run.text = "\n".join(chunks).strip()
        return run

    return asyncio.run(_collect())


# -- multi-turn --------------------------------------------------------------


def run_conversation(
    *,
    system_prompt: str,
    opening: str,
    respond: Callable[[str], str | None],
    cwd: str | Path,
    allowed_tools: list[str] | None = None,
    max_rounds: int = 6,
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str | None = DEFAULT_MODEL,
    on_activity: Callable[[Activity], None] | None = None,
) -> AgentRun:
    """Hold one multi-turn conversation and return its final message.

    The only stateful agent usage in the codebase. Everything else is one-shot
    on purpose; this exists because writing a good ticket is a dialogue, and the
    ticket is the last thing a human sees before the pipeline runs unsupervised.

    `respond` receives each agent message and returns the human's reply, or None
    to end the conversation. Keeping that a callback is what lets the whole
    thing be tested with canned answers and no SDK.

    `on_activity` is called as the agent works, before it has said anything. The
    first round can run for minutes while the agent reads a repository, and a
    caller with a terminal needs some way to tell that from a hang.
    """
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            TextBlock,
        )
    except ImportError as exc:  # pragma: no cover - environment dependent
        return AgentRun(error=f"claude-agent-sdk is not installed: {exc}")

    options = _options(
        ClaudeAgentOptions,
        tools=allowed_tools or READ_ONLY_TOOLS,
        system_prompt=system_prompt,
        max_turns=max_turns,
        cwd=str(cwd),
        model=model,
    )

    report = _reporter(on_activity)

    async def _talk() -> AgentRun:
        run = AgentRun()
        text = ""
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(opening)
                for _ in range(max_rounds):
                    chunks: list[str] = []
                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            report(message.content)
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    chunks.append(block.text)
                            continue
                        for attr in ("session_id", "total_cost_usd"):
                            value = getattr(message, attr, None)
                            if value is not None:
                                setattr(run, attr, value)
                    text = "\n".join(chunks).strip()

                    reply = respond(text)
                    if reply is None:
                        break
                    await client.query(reply)
                else:
                    run.turn_limit_hit = True
        # Blind for the same reason as the one-shot runner above: any SDK or
        # infrastructure failure is reported as a failed run, not raised.
        except Exception as exc:  # noqa: BLE001 - see comment above
            run.error = f"{type(exc).__name__}: {exc}"
        run.text = text
        return run

    return asyncio.run(_talk())


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
            (
                "The working tree may already contain partial changes from it. Read "
                "the current state of the relevant files before editing anything, "
                "keep whatever is already correct, and finish the job rather than "
                "starting over. Check the execution log in your README for what the "
                "previous attempt recorded."
            ),
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
        (
            "Do only what your sub-task README specifies. End with the JSON object "
            "described in your instructions and nothing else."
        ),
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


# Failures that an immediate retry cannot fix. A usage limit does not clear in
# the seconds between two attempts, and a bad credential never clears on its
# own, so retrying either one only spends the second attempt to learn nothing.
_PERMANENT_FAILURE_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "429",
    "limit reached",
    "too many requests",
    "insufficient credit",
    "credit balance",
    "authentication",
    "unauthorized",
    "invalid api key",
    "permission denied",
)


def is_retryable(error: str | None) -> bool:
    """Whether trying the exact same thing again could plausibly work.

    Pattern matching on error text is crude, and it only catches failures that
    describe themselves. An SDK error that says nothing useful still gets a
    retry, which is the safe direction to be wrong in.
    """
    if not error:
        return True
    lowered = error.lower()
    return not any(marker in lowered for marker in _PERMANENT_FAILURE_MARKERS)


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
        return SpawnResult(
            status=SubTaskStatus.FAILED.value,
            error=run.error,
            retryable=is_retryable(run.error),
            **meta,
        )
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
        error = str(reason or summary or "agent reported failure")
        return SpawnResult(
            status=status,
            summary=summary,
            error=error,
            retryable=is_retryable(error),
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
    on_activity: Callable[[Activity], None] | None = None,
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
        on_activity=on_activity,
    )
    return result_from_run(run)
