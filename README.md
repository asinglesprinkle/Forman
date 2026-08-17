![Forman](assets/forman.jpg)

# Forman

Turn prose into Linear tickets, then let agents work them until a pull request
is waiting for you.

---

Forman is a local, single-user tool that closes the loop between a ticket and a
pull request. You describe a problem in plain language; it files the ticket. Later
it picks that ticket up, breaks it into sub-tasks, runs a fresh agent on each one,
and stops when there is a PR for you to review.

It never merges anything and never marks a ticket done. Every run ends at a human.

## How it works

**Push.** `forman push` talks it through with you first. It checks your
description against what the pipeline actually needs to run, asks only for the
gaps, then shows you the drafted ticket. Nothing is filed until you say so.

**Pull.** `forman pull` picks a ticket it created, moves it to in-progress,
decomposes it into local sub-tasks, and runs them one at a time in fresh agent
sessions. It commits after each finished sub-task, then opens a PR, comments the
link on the ticket, moves the ticket to in-review, and stops.

Linear only ever sees tickets. Sub-tasks are local: files on disk plus git state.

## Install

Needs Python 3.11+, git, and the [Claude Code CLI](https://claude.com/claude-code)
on your PATH. `gh` is optional; without it Forman prints the PR body for you to
open by hand.

```sh
git clone https://github.com/asinglesprinkle/Forman && cd Forman
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then set up your Linear credentials:

```sh
forman init
```

It asks for a personal API key (get one at
[linear.app/settings/api](https://linear.app/settings/api)), checks it against
the API before saving anything, picks a team if you have more than one, and
tells you whether your board has a workflow state it can use for the review
gate. Input is hidden, and the file it writes is `0600`.

### Where your key lives

Forman runs agents that execute code inside whichever repo you point it at, so
where a credential sits is a real decision, not a detail. The key is looked up
in one order, nearest first:

1. `LINEAR_API_KEY` in the environment
2. `.env` in the target repo
3. `~/.config/forman/.env`

`forman init` writes (3), because your Linear key belongs to *you* and not to
any one project: set it once and every repo you run from picks it up. Use (2)
when a particular repo needs a *different* key — a work board and a personal
one under separate accounts, say.

That second case is the isolating one. Lookup only ever considers the target
repo and your user config, never a sibling checkout, so **a key saved in one
repo is invisible from every other one**. Forman running in `~/work/api` will
not load the credential sitting in `~/side-project/.env`, no matter how the two
are arranged on disk — there is no search path to walk, no parent-directory
climb, and no cross-repo cache. A work key stays on the work board.

The user-level key at (3) is deliberately the opposite: shared by every repo,
which is the entire point of putting it there. Two honest limits go with that.
It is written `0600`, so it is readable only by your account — but agents
Forman spawns run *as you*, so a repo with no `.env` of its own falls back to it
rather than failing. And per-repo isolation is about which key Forman *loads*,
not a sandbox: it does not stop code in your tree from reading files elsewhere
on the machine. If a board must be unreachable from a given repo, give that repo
its own `.env` with a narrower key and keep the broad one out of `~/.config`.

Neither file can be committed by accident: Forman adds `.env` and `.forman/` to
`.git/info/exclude` in the target repo — its own tooling's business, rather than
an uninvited edit to the project's tracked `.gitignore`.

## Quick start

Run it from **inside the repo you want it to work on**. The current directory is
the codebase; more than one board just means more than one directory.

```sh
forman doctor                    # read-only: what can Forman see?
forman push                      # talk through a ticket, then file it
forman pull                      # work the next ready ticket Forman filed
forman pull --ticket ABC-42      # or work a specific one, whoever filed it
forman pull --any                # or consider your whole assigned backlog
forman status                    # what Forman has in flight here
```

### Which tickets `pull` will start

By default, only ones Forman filed itself. `forman push` labels everything it
creates `forman`, and `forman pull` will only *select* tickets carrying that
label.

The point is narrow: a project manager who files twenty tickets on you while
you are mid-run should not be able to start an unsupervised coding session. It
is a speed bump, not a wall, and there are three ways past it:

- **add the `forman` label** to a ticket in Linear — that opts it in for good.
- **`--ticket ABC-42`** works anything by name. Naming a ticket is a deliberate
  act, so the label does not apply; the run says in its notes that it went
  ahead anyway.
- **`--any`** drops the filter for one run and considers your whole assigned
  backlog, which is what every version before this did.

Rename the label with `FORMAN_LABEL`. It is created on your team the first time
`forman push` needs it. Tickets are still always *stamped* on creation even
under `--any`, so a run started that way does not file work a later default run
would refuse to see.

An unlabelled ticket still counts as a blocker: if a `forman` ticket is blocked
by somebody else's open ticket, it stays put. The filter narrows what gets
worked, not what counts as a dependency.

`forman doctor` marks assigned tickets with `*` when they carry the label, so
"why did it say there was nothing to do?" is one command away.

### Writing a ticket

`forman push` is a short conversation, not a one-shot command:

```
$ forman push
What do you want to achieve? (one line is fine)
> the auth client keeps dropping sessions on long requests

Two things I want to pin down:
- Is this the token refresh path specifically, or any 401 mid-request?
- Should the fix cover the mobile client, or just the web SDK?

> just web, and yes it's refresh

---
title: Refresh auth tokens before they expire mid-request
priority: high
labels: [auth]
...

[c]reate 1 ticket(s), [e]dit, [q]uit, or type feedback to redraft:
```

The questions come from what the pipeline needs in order to run unsupervised:
the problem, checkable acceptance criteria, where to look, what is out of
scope, and whether it is really one ticket. The agent reads your repo to answer
what it can, and asks only about the rest, so it stops as soon as it can fill
the template rather than after some fixed number of questions.

The ticket is the last thing a human sees before agents start writing code
against it, so ambiguity left here does not produce a vague pull request. It
produces a confidently wrong one, several sessions later. That is why this step
is a conversation.

`e` opens the draft in `$EDITOR`, anything else you type is treated as feedback
and redrafts. `forman push "prose" --yes` skips all of it and files in one
shot, which is also what happens automatically when stdin is not a terminal.

Ordering is part of the draft, in `blocked_by` and `blocks`. A ticket in the
same push is named by its 1-based index; a ticket that already exists is named
by its identifier, and the drafting agent is shown your open backlog so it can
do that. Both directions are recorded, so it does not matter which end of the
edge the draft states it from. A reference that resolves to neither is refused
before anything is created — ordering that only exists as prose in the context
section is invisible to `pull`, which reads the relations and nothing else.

`doctor` is the one to reach for when something looks wrong. It prints who you
authenticated as, your teams and workflow states, which states it will use for
in-progress and for the gate, and the tickets currently assigned to you —
marking the ones `forman pull` is willing to start.

## What a run actually does

1. Picks a ticket assigned to you, carrying the `forman` label, whose blockers
   are all done — highest priority first, then whichever unblocks the most
   other work.
2. Checks out the default branch (detected, never assumed to be `main`), pulls,
   and **aborts if your tree is dirty**. It will not stash your work.
3. Cuts `<team-key>-<number>/<title-slug>`, so `ABC-42` titled "Add rate limiting"
   becomes `abc-42/add-rate-limiting`.
4. Moves the ticket to in-progress, so the board is honest for the minutes and
   hours the run takes.
5. Decomposes the ticket into sub-task briefs under `.forman/<TICKET>/`.
6. Runs them serially, committing after each one, so a mid-run failure leaves a
   clean, readable tree.
7. Opens the PR, comments it on the ticket, sets the ticket to in-review, stops.

The in-progress move is a courtesy to everyone not watching your terminal, and
never fatal: a board with no matching workflow state gets a note in the run
output, not a failed run. Forman matches an exact name, then a substring, then
the leftmost column in Linear's "started" group that is not a review state —
so `Doing`, `WIP` and `Building` all work without configuration. Name it
explicitly with `LINEAR_PROGRESS_STATE` if yours is stranger than that.

A halted run leaves the ticket in-progress rather than putting it back. It is
still your work in flight, and the column is the signal that somebody should
come and look.

If something blocks, it comments what got in the way and leaves the ticket alone.
Fix the blocker, run again, and it skips whatever already finished.

## State on disk

Everything Forman knows about a ticket is one directory in the target repo:

```
.forman/
  ABC-42/
    state.json        <- the source of truth
    manifest.md       <- rendered from state.json on every write, never parsed back
    ABC-42.01.md      <- sub-task brief, plus the execution log appended under it
    ABC-42.02.md
    ABC-42.03.md
```

`state.json` is the whole resumption story. Nothing is inferred from git, from
Linear, or from the manifest:

```json
{
  "ticket": "ABC-42",
  "title": "Add rate limiting to the public API",
  "status": "in_progress",
  "branch": "abc-42/add-rate-limiting",
  "pulled_at": "2026-02-11T15:04:07+00:00",
  "pr_url": null,
  "subtasks": [
    {
      "id": "ABC-42.01",
      "goal": "Add a token-bucket limiter behind the existing middleware port",
      "status": "done",
      "depends_on": [],
      "blocked_reason": null,
      "log": null,
      "started_at": "2026-02-11T15:04:12+00:00",
      "finished_at": "2026-02-11T15:11:48+00:00",
      "session_id": "0f3c8a1e-...",
      "cost_usd": 0.4131
    },
    {
      "id": "ABC-42.02",
      "goal": "Wire the limiter into the public routes",
      "status": "in_progress",
      "depends_on": ["ABC-42.01"],
      "blocked_reason": null,
      "log": null,
      "started_at": "2026-02-11T15:11:50+00:00",
      "finished_at": null,
      "session_id": null,
      "cost_usd": null
    }
  ]
}
```

Ticket `status` is one of `pulled`, `in_progress`, `in_review`, `blocked`,
`failed`, `done`; a sub-task is `pending`, `in_progress`, `done`, `blocked`, or
`failed`, and a `blocked` one always carries a `blocked_reason`. Those two
vocabularies are why a re-run is cheap: the next sub-task to run is the first
`pending` one whose `depends_on` are all `done`, which is a pure function of
this file. Sub-tasks already `done` are never touched, so fixing a blocker and
running again resumes rather than restarts.

The manifest is for you, not for the program:

```markdown
# ABC-42: Add rate limiting to the public API

- status: `in_progress`
- branch: `abc-42/add-rate-limiting`
- pulled at: 2026-02-11T15:04:07+00:00

<!-- Rendered from state.json on every write. Do not edit by hand. -->

## Sub-tasks

- [x] `ABC-42.01` Add a token-bucket limiter behind the existing middleware port  $0.4131
- [~] `ABC-42.02` Wire the limiter into the public routes  (in progress)
- [ ] `ABC-42.03` Document the limits in the API reference  (waiting on ABC-42.02)

**Total: $0.4131**
```

Safe to open mid-run. Safe to delete, too — the next write regenerates it. If
the two ever disagree, `state.json` wins and saving fixes the manifest, which is
the only reason a rendered file can be trusted at all.

## Configuration

Set in the environment, a `.env` in the target repo, or `~/.config/forman/.env`.
Nearest wins; the shell always beats a file.

| Variable | Required | What it does |
|---|---|---|
| `LINEAR_API_KEY` | yes | Personal API key from Linear settings |
| `LINEAR_TEAM_KEY` | no | Which team to create issues on, if more than one is visible |
| `LINEAR_REVIEW_STATE` | no | Exact workflow state for the gate. Any state containing "review" is matched by default |
| `LINEAR_PROGRESS_STATE` | no | Exact workflow state for a run in flight. Linear's "started" group is used by default |
| `LINEAR_USER` | no | Act as someone else. Unset, identity comes from the API key, which cannot drift when a name changes |
| `FORMAN_LABEL` | no | The provenance label, `forman` by default. [What it gates](#which-tickets-pull-will-start) |

Which file wins, and why the per-repo one isolates a key from every other
checkout, is [above](#where-your-key-lives).

## Design decisions

- **Serial execution.** No parallelism, no locking. The simplest correct model.
- **One human gate per ticket**, at PR-open. Never auto-merge, never mark done.
- **Only its own work by default.** A ticket Forman did not file does not start
  a run on its own. [Why, and how to override it](#which-tickets-pull-will-start).
- **Sub-tasks stay local.** Linear sees tickets, nothing else.
- **Fresh context per sub-task.** Each agent gets its own brief, the parent
  ticket, relevant paths, and read-only logs of finished siblings. Never the
  orchestrator's history.
- **`state.json` is the source of truth.** `manifest.md` is rendered from it and
  never parsed back. [What that looks like](#state-on-disk).
- **Credentials are per-user by default, per-repo when you need isolation.**
  [Where your key lives](#where-your-key-lives).
- **The orchestration loop performs no I/O.** Every side effect sits behind a
  port, which is what lets the whole pipeline be tested without a network, a
  repo, or an API key.

## Status and limits

Alpha. Proven end to end against real Linear, real git, and real agents, but it
has not been through much mileage.

- Built for one person working their own tickets on one machine. There is no
  locking and no concurrency story, by design.
- Considers at most **25 open assigned tickets**, because Linear rejects queries
  above a complexity budget. More than that needs real pagination.
- A `failed` sub-task is retried once, since turn limits and dropped
  connections are usually transient. A `blocked` one never is: an external
  blocker will not resolve itself, so retrying only burns tokens.
- `--linear stub` runs the whole pipeline offline against a JSON file, with no
  account and no network. Useful for trying it out.
- Decomposition has been exercised on small tickets. How it handles a ticket
  that genuinely needs five interdependent sub-tasks is not yet known.

## Development

```sh
pip install -e ".[dev]"
pytest
```

The suite runs the entire pipeline against a stubbed spawn and a stubbed Linear,
asserting state transitions, commit points, and that runs halt at the gate. No
network, no git, no API key, no model calls.

## License

[MIT](LICENSE)
