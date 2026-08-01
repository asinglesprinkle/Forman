![Foreman](assets/foreman.jpg)

# Foreman

Turn prose into Linear tickets, then let agents work them until a pull request
is waiting for you.

---

Foreman is a local, single-user tool that closes the loop between a ticket and a
pull request. You describe a problem in plain language; it files the ticket. Later
it picks that ticket up, breaks it into sub-tasks, runs a fresh agent on each one,
and stops when there is a PR for you to review.

It never merges anything and never marks a ticket done. Every run ends at a human.

## How it works

**Push.** `foreman push` talks it through with you first. It checks your
description against what the pipeline actually needs to run, asks only for the
gaps, then shows you the drafted ticket. Nothing is filed until you say so.

**Pull.** `foreman pull` picks a ticket, decomposes it into local sub-tasks, and
runs them one at a time in fresh agent sessions. It commits after each finished
sub-task, then opens a PR, comments the link on the ticket, moves the ticket to
in-review, and stops.

Linear only ever sees tickets. Sub-tasks are local: files on disk plus git state.

## Install

Needs Python 3.11+, git, and the [Claude Code CLI](https://claude.com/claude-code)
on your PATH. `gh` is optional; without it Foreman prints the PR body for you to
open by hand.

```sh
git clone https://github.com/you/foreman && cd foreman
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then set up your Linear credentials:

```sh
foreman init
```

It asks for a personal API key (get one at
[linear.app/settings/api](https://linear.app/settings/api)), checks it against
the API before saving anything, picks a team if you have more than one, and
tells you whether your board has a workflow state it can use for the review
gate. Input is hidden, and the file it writes is `0600`.

It saves to `~/.config/foreman/.env` on purpose. Your API key belongs to *you*,
but Foreman runs inside whichever repo you point it at, so a key saved in one
repo is invisible from every other one.

## Quick start

Run it from **inside the repo you want it to work on**. The current directory is
the codebase; more than one board just means more than one directory.

```sh
foreman doctor                    # read-only: what can Foreman see?
foreman push                      # talk through a ticket, then file it
foreman pull                      # work the next ready ticket
foreman pull --ticket ABC-42      # or work a specific one
foreman status                    # what Foreman has in flight here
```

### Writing a ticket

`foreman push` is a short conversation, not a one-shot command:

```
$ foreman push
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
and redrafts. `foreman push "prose" --yes` skips all of it and files in one
shot, which is also what happens automatically when stdin is not a terminal.

`doctor` is the one to reach for when something looks wrong. It prints who you
authenticated as, your teams and workflow states, which state it will use for
the gate, and the tickets currently assigned to you.

## What a run actually does

1. Picks a ticket assigned to you whose blockers are all done, highest priority
   first, then whichever unblocks the most other work.
2. Checks out the default branch (detected, never assumed to be `main`), pulls,
   and **aborts if your tree is dirty**. It will not stash your work.
3. Cuts `<team-key>-<number>/<title-slug>`, so `ABC-42` titled "Add rate limiting"
   becomes `abc-42/add-rate-limiting`.
4. Decomposes the ticket into sub-task briefs under `.foreman/<TICKET>/`.
5. Runs them serially, committing after each one, so a mid-run failure leaves a
   clean, readable tree.
6. Opens the PR, comments it on the ticket, sets the ticket to in-review, stops.

If something blocks, it comments what got in the way and leaves the ticket alone.
Fix the blocker, run again, and it skips whatever already finished.

## Configuration

Set in the environment, a `.env` in the target repo, or `~/.config/foreman/.env`.
Nearest wins; the shell always beats a file.

| Variable | Required | What it does |
|---|---|---|
| `LINEAR_API_KEY` | yes | Personal API key from Linear settings |
| `LINEAR_TEAM_KEY` | no | Which team to create issues on, if more than one is visible |
| `LINEAR_REVIEW_STATE` | no | Exact workflow state for the gate. Any state containing "review" is matched by default |
| `LINEAR_USER` | no | Act as someone else. Unset, identity comes from the API key, which cannot drift when a name changes |

Foreman keeps its bookkeeping in `.foreman/` inside the target repo and adds that
plus `.env` to `.git/info/exclude`, so neither can be committed by accident.

## Design decisions

- **Serial execution.** No parallelism, no locking. The simplest correct model.
- **One human gate per ticket**, at PR-open. Never auto-merge, never mark done.
- **Sub-tasks stay local.** Linear sees tickets, nothing else.
- **Fresh context per sub-task.** Each agent gets its own brief, the parent
  ticket, relevant paths, and read-only logs of finished siblings. Never the
  orchestrator's history.
- **`state.json` is the source of truth.** `manifest.md` is rendered from it and
  never parsed back.
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

`docs/build-spec.md` is the original specification, kept for provenance.

## License

[MIT](LICENSE)
