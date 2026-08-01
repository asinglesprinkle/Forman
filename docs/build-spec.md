# Original build spec

> Kept for provenance. This is the specification Foreman was built from, not
> documentation of what it does now. Several decisions moved during the build:
> the tool runs from inside the target repo rather than using a `work/` dir,
> branches are named `<team-key>-<number>/<title-slug>` rather than `ai/<ticket>`,
> bookkeeping lives in `.foreman/`, and Linear is reached over its GraphQL API.
> Read the README for how the shipped tool actually behaves.

This is a build specification for **Claude Code**. Read it fully, then scaffold the
project described below. It is not documentation for an existing repo; it is the
instruction set for creating one. Ask me before deviating from any decision marked
**LOCKED**.

---

## What this project is

A two-phase AI workflow that turns prose into Linear tickets, then autonomously
executes those tickets by decomposing each into local sub-tasks and running a fresh
agent per sub-task.

- **Push (write) phase:** I describe an issue in prose. An agent fills a ticket
  template and creates one or more Linear tickets.
- **Pull (execute) phase:** an orchestrator pulls tickets assigned to me,
  topo-sorts them by dependency, picks one, decomposes it into local sub-task
  READMEs (these never go to Linear), then runs sub-tasks **serially**, spawning a
  fresh agent session per sub-task. When all sub-tasks finish, it stages, commits,
  opens a PR, comments the PR link on the ticket, and sets the ticket to
  **in-review** — then STOPS for my approval.

**Key mental model:** Linear only ever sees *tickets*. Sub-tasks are a local
decomposition layer living as files on disk plus git state. Two levels of the same
topo-sort: across tickets (`blocked_by`/`blocks`) and within a ticket
(`depends_on` between sibling sub-tasks).

---

## Locked decisions

These were decided deliberately. Do not change them without asking.

- **LOCKED — Serial sub-task execution.** No parallelism, no locking. Simplest
  correct model.
- **LOCKED — Per-ticket human gate.** The pipeline stops at PR-open +
  ticket-in-review. A human approves before anything is set to Done or merged.
  Never auto-merge.
- **LOCKED — Sub-tasks are local only.** They are files (`subtask` READMEs) +
  entries in `state.json`. They are never pushed to Linear.
- **LOCKED — Fresh context per spawn.** Each sub-task runs in a brand-new agent
  session that receives only: its own sub-task README, the parent ticket, relevant
  repo paths, and read-only logs of already-completed sibling sub-tasks. Not the
  orchestrator's history.
- **LOCKED — `state.json` is the source of truth.** `manifest.md` is *rendered*
  from it on every write, never hand-edited, never parsed back.
- **LOCKED — Activity-shaped structure.** Isolate every external side effect
  (spawn, Linear API, git) behind its own function with a clean signature and a
  structured return, so this can later be ported to Temporal by moving those
  functions into `@activity.defn` wrappers with no logic rewrite. Keep the
  orchestration loop free of direct I/O.

## Decisions to confirm with me before coding

- **Spawn model:** Claude Agent SDK `query()` (Python-native, preferred) vs
  shelling out to `claude -p`. Default to the Agent SDK unless I say otherwise.
- **Integrations realness:** default to **live git, stubbed Linear** — implement a
  `LinearClient` interface with a working stub so the pipeline runs end-to-end
  without a token, and I wire the real API later. Confirm before making Linear
  calls live.

---

## Tech + environment

- Python 3.11+. Package manager: `uv` (fall back to pip if uv absent).
- Claude Agent SDK: `pip install claude-agent-sdk` (NOT the deprecated
  `claude-code-sdk`; classes are `ClaudeAgentOptions` and `query`). The Python SDK
  needs the Claude Code CLI on PATH.
- git for branch/commit/PR. Use `gh` CLI for PR creation if available; otherwise
  print the PR body and branch so I open it manually.
- My machine is NixOS. Prefer a `flake.nix` dev shell if straightforward; a
  `pyproject.toml` + venv is an acceptable fallback. No em dashes in any generated
  file or output — use hyphens.

---

## Repo layout to create

```
foreman/
  README.md                  # a NORMAL repo readme (not this spec) — usage, setup
  pyproject.toml
  flake.nix                  # optional dev shell
  .gitignore
  src/pipeline/
    __init__.py
    orchestrator.py          # the serial loop; NO direct I/O, calls the layers below
    spawn.py                 # spawn_agent(): wraps Agent SDK query(); structured return
    linear_client.py         # LinearClient protocol + StubLinearClient + (later) real impl
    git_ops.py               # branch/stage/commit/PR helpers
    state.py                 # read/write state.json, render manifest.md
    decompose.py             # ticket -> sub-task READMEs + seeded state
    push.py                  # prose -> ticket template -> Linear create (stub for now)
    models.py                # dataclasses: Ticket, SubTask, SpawnResult, enums
    templates/
      ticket.md              # push-side ticket template (below)
      subtask.md             # decomposition sub-task template (below)
  work/                      # runtime working dirs, one per ticket; gitignored
  tests/
    test_state.py
    test_topo_sort.py
    test_orchestrator_stub.py   # full loop against stubbed spawn + stubbed Linear
```

---

## State schema (`state.json`, one per ticket under `work/<TICKET>/`)

```json
{
  "ticket": "TICKET-123",
  "status": "in_progress",
  "branch": "ai/TICKET-123",
  "pulled_at": "<iso8601>",
  "subtasks": [
    {
      "id": "TICKET-123.01",
      "goal": "short imperative",
      "status": "pending",
      "depends_on": [],
      "blocked_reason": null,
      "log": null,
      "started_at": null,
      "finished_at": null
    }
  ]
}
```

Status vocab (both levels): `pending -> in_progress -> done`, plus `blocked` (needs
`blocked_reason`) and `failed`. Ticket-level also has `pulled`, `in_progress`,
`in_review` (terminal pre-gate). `manifest.md` is rendered from this: checkbox per
sub-task, `waiting on X` when deps unmet, warning marker + reason when blocked,
cross marker when failed.

---

## Orchestrator loop (serial, per-ticket gate)

Implement exactly this control flow. Keep it I/O-free — every external call goes
through the layer modules so the whole loop is Temporal-portable later.

1. **Select ticket.** `linear.list_assigned()`, filter to those whose every
   `blocked_by` dep is Done, topo-sort the ready set, pick head. Tie-break
   (priority, then unblock-count) may use a cheap model — fine to hardcode a simple
   sort for v1.
2. **Decompose.** Create `work/<ticket>/`, `git checkout -b ai/<ticket>`,
   `decompose()` writes sub-task READMEs and seeds `state.json`. Render manifest.
3. **Execute serially.** While any sub-task is `pending`: pick the next pending one
   whose `depends_on` are all Done. If none exists, remaining work is blocked —
   break. Mark it `in_progress`, render manifest, `spawn_agent()`. On the result:
   `done` (record log), `blocked` (record reason), or `failed` (record error).
   Render manifest after each. **Commit after each `done` sub-task** so a mid-run
   failure leaves a clean, inspectable tree.
4. **Finalize only if all sub-tasks Done.** Stage, commit, open PR (body templated
   from ticket + accumulated sub-task logs), comment PR URL on the ticket, set
   ticket `in_review`. **STOP.** This is the gate. Otherwise: comment a summary of
   blocked/failed sub-tasks and leave the ticket `in_progress` for me.

**Note on blocking:** with a correct topo-sort, a sub-task can only run when its
deps are Done, so a later sibling can never retroactively unblock an earlier one.
Blocking is therefore terminal for the run and there is NO retry pass. A blocked
sub-task means an *external* blocker (a decision only I can make, a missing
credential, an unfinished upstream ticket). I resolve it and re-run; the re-run
skips already-Done sub-tasks. Do not build a retry/max-iterations loop.

---

## Spawn contract (`spawn.py`)

`spawn_agent(subtask_readme, parent_ticket, repo_paths, sibling_logs) -> SpawnResult`

- Use Agent SDK `query()` — one call = one fresh session (no memory across calls),
  which IS the fresh-context requirement.
- `ClaudeAgentOptions`: `system_prompt=` the SPAWN CONTRACT below;
  `allowed_tools=["Bash","Edit","Read","Grep"]` (scope Bash to git where you can);
  `permission_mode="acceptEdits"`; `max_turns` set to a sane cap (e.g. 40);
  `cwd="work/<ticket>"`.
- Distinguish failure kinds: an SDK/infra error or turn-limit hit is a **failed**
  result (a Temporal port would auto-retry these). A clean run where the agent
  reports it cannot proceed is a **blocked** result (a normal business outcome).
- Capture `session_id` and `total_cost_usd` from the result messages for logging.

**SPAWN CONTRACT (inject as system_prompt):**
> You are executing ONE sub-task defined in the provided README. Do only what that
> README specifies; its parent ticket and sibling logs are context, not a to-do
> list. When finished, append a concise summary of what you did (and any decisions)
> under the README's `## Execution log` heading — append only, never edit the brief
> above it. Then emit as your final message a single JSON object and nothing else:
> `{"status": "done" | "blocked" | "failed", "summary": "...", "blocked_reason": null | "..."}`.
> Use `blocked` only for external blockers you cannot resolve (a decision only the
> human can make, a missing credential, an unfinished upstream dependency).

Parse that final JSON to build the `SpawnResult`. If the JSON is missing or
malformed, treat it as `failed` with the raw text captured.

---

## Templates to write verbatim into `src/pipeline/templates/`

### `ticket.md` (push side output — schema in frontmatter, prose in body)

```markdown
---
title: <imperative, <80 chars>
priority: <urgent | high | medium | low>
labels: [<label>, <label>]
project: <project name or null>
estimate: <xs | s | m | l | xl>
blocked_by: []
blocks: []
---

## Problem
<what's broken or missing, and why it matters. 2-4 sentences.>

## Acceptance criteria
- [ ] <observable, testable outcome>

## Context / pointers
<files, endpoints, prior tickets, links the executor will need>

## Out of scope
<explicit non-goals so decomposition doesn't sprawl>
```

Push rules: split into separate tickets when concerns are independently shippable;
always populate `blocked_by`/`blocks` when ordering matters.

### `subtask.md` (decomposition output — loose; optional sections stay omitted unless real)

```markdown
---
subtask_id: <TICKET-ID.NN>
parent: <TICKET-ID>
status: pending
depends_on: []
---

## Goal
<one sentence: what this sub-task delivers>

## Definition of done
- [ ] <concrete completion signal>

## Likely files / touchpoints        (optional — include only if real)
<paths the executor will probably work in>

## Test plan                          (optional)
<how to verify>

## Notes for executor                 (optional)
<gotchas, constraints, links to sibling outputs>

---
## Execution log
<!-- spawn appends below this line; never edits above it -->
```

---

## Build order

1. `models.py` + `state.py` + manifest render, with `test_state.py`.
2. Topo-sort (ticket-level and sub-task-level share one helper) + `test_topo_sort.py`.
3. `linear_client.py` stub + `decompose.py` + `git_ops.py`.
4. `spawn.py` against the Agent SDK.
5. `orchestrator.py` wiring it together, I/O-free.
6. `test_orchestrator_stub.py`: run the whole loop with a stubbed spawn (returns
   canned `done` results) and stubbed Linear, asserting state transitions, commit
   points, and that the run halts at `in_review`. This must pass before any live
   integration.

Deliver a working `test_orchestrator_stub.py` run as the definition of done for v1.
Confirm the two open decisions (spawn model, integration realness) with me before
step 4.
