[한국어](ko/GETTING-STARTED.md)

# Getting started

This page is the path from an empty terminal to one finished change. Every command and flag is
catalogued in [USAGE.md](USAGE.md), so this page links there instead of repeating it. Why the
tool is shaped this way is in [README.md](../README.md).

## What you are agreeing to

Installing this tool moves the routing decision out of the agent and into the runner. The runner
decides which phase comes next, the agent follows the printed `next_command`, and neither you nor
the agent skips a phase to get to the end faster. Whether a phase actually did its work is judged
from the command-execution log the hooks recorded, not from the agent's report — a fix with no
test command in that phase is rejected no matter what the agent wrote down. So a run is a
sequence of phases you have to walk, including the ones you think you do not need. That friction
is the product, not a defect; the rest of this page says where it costs you.

## Prerequisites

- **Homebrew.** The install path below is a tap, so Homebrew has to exist first.
- **git, and a git repository.** `agent-flow run` refuses to start outside a repository, because
  every run works in an isolated worktree and there is nothing to isolate without git.
- **At least one host CLI — Claude Code or Codex CLI.** agent-flow does not call a model itself.
  It prints the phase prompt and reads what the host CLI wrote, so with no host CLI there is
  nothing to drive the phases.
- **`node`.** The formula installs it as a runtime dependency, so there is nothing to do by hand.
  Project install and every managed hook run `bin/agent-flow-kit.mjs`.
- **`gh`, only for the PR phases.** `agent-flow pr-watch` calls the `gh` CLI and inherits the
  authentication you already have. agent-flow manages no token of its own.

Install **both** Claude Code and Codex CLI if you care about the review phases. Phases marked
`multi_review` — `review` in the small workflows, `multi-review`, `architecture-review`, and
`final-review` in the large ones — run review angles only on the installed Claude and Codex CLIs,
as confined subprocesses. How the angles are spread across the providers depends on the phase:
`final-review` distributes every angle to both providers, while every other `multi_review` phase
runs every angle on one primary provider, plus any additional provider that was selected.

- **Both installed** — `final-review`'s verdict comes from two independent processes on two
  independent providers, and the other `multi_review` phases get a second provider on top of the
  primary one. That is the point of having the phase at all: the session that wrote the code
  passes its own code, so it is not allowed to review it.
- **Only one installed** — every angle still runs as an independent subprocess, but one provider
  covers all of them. You keep process independence and lose provider independence.
- **Neither installed** — the phase closes as a failure. The controller session cannot record the
  review verdict in its place, so the run stops there.

## Install

```bash
brew tap chonamdoo/agent-flow https://github.com/chonamdoo/agent-flow
brew install chonamdoo/agent-flow/agent-flow
```

The tap takes the two-argument form with the URL spelled out, because the repository is not named
`homebrew-*`. Install by the fully qualified name: since Homebrew 6.0 a third-party tap needs
explicit trust, and the qualified name grants it for this one formula.

Then install the workflow into a project:

```bash
agent-flow .
```

A bare path is the install command — `agent-flow <dir>` installs the project assets into that
directory, so there is no need to `cd` first. Installer flags pass straight through:

```bash
agent-flow . --profile android --skills tdd,code-review
```

Two limits on this command. It runs **once per project**; a new session is not a reason to run it
again. And it runs **in the leader checkout only** — running it inside a linked worktree is
blocked, because the installed assets belong to the repository, not to one run.

## Your first run

Use `bugfix`. It has 5 phases — `reproduce`, `implement-fix`, `review`, `qa`, `handoff` — which
makes it the cheapest workflow that still contains a real review and a real verification step.

Issue these commands from inside a Claude Code or Codex session, not from a bare shell: agent-flow
prints the phase prompt and waits, and the host CLI in that session is what reads the prompt, does
the phase's work, and writes `required_artifact`. Inside a session the same steps have a
slash-command form — `/agent-flow <task>` to start, `/agent-flow` to continue. Run the binary in a
plain terminal instead and you are left at `status: awaiting_host` with nothing that can write the
artifact. See [Running](USAGE.md#running) in USAGE.md.

```bash
agent-flow run "fix the login timeout on slow networks" --workflow bugfix --worktree fix-login-timeout
```

The first line of output is the workspace:

```text
worktree: fix-login-timeout /Users/you/.agent-flow/worktrees/<repo-id>/fix-login-timeout
```

Omit `--worktree` and the name is derived from the task text instead. The default location is
`~/.agent-flow/worktrees/<repo-id>/<name>`, outside the project folder on purpose: put the
worktree inside the project and an IDE left open on the leader reacts to worktree activity,
touches the leader's caches, and the leader tripwire reports that as contamination — which blocks
the remaining phases. **All work happens in that worktree, never in the leader checkout.** The
leader is what the tripwire watches, so editing there is what stops your own run.

The run then prints the first phase prompt and stops:

```text
═══ phase 'reproduce' awaits host AI. Write artifact → `agent-flow continue --root /path/to/project --worktree fix-login-timeout`. ═══
status: awaiting_host
run: bugfix/<run-id>
current_phase: reproduce
reason: missing_phase_artifact
required_artifact: /.../reproduce.md
next_command: agent-flow continue --root /path/to/project --worktree fix-login-timeout
```

Read it as four facts. `current_phase` is where the run is. `reason` is why it stopped.
`required_artifact` is the exact file the phase owes. `next_command` is the only command that
moves the run — it is the runner's decision, not a suggestion, and it carries the `--root` and
`--worktree` you must not retype from memory.

So the loop is: let the host CLI do the phase's work and write `required_artifact`, then run the
printed command.

```bash
agent-flow continue --root /path/to/project --worktree fix-login-timeout
```

Check where you are at any point without advancing anything:

```bash
agent-flow status --root /path/to/project --worktree fix-login-timeout
```

Walk that loop five times and `bugfix` is done. The last output looks like this:

```text
status: complete
reason: workflow_complete
report: /.../RUN_REPORT.md
next_command: none
```

`next_command: none` is the end of the run. `RUN_REPORT.md` collects the phase artifacts;
`agent-flow report --run-dir <run-dir>` rewrites it later. To stop a run you no longer want:

```bash
agent-flow abort --root /path/to/project --worktree fix-login-timeout --yes
```

## Picking a workflow

Pick by the size of the work. The source of truth is `src/agent_flow/workflows/<name>.yaml`.

| Workflow | Phases | Right size for |
|---|---|---|
| `review` | 3 | reading an existing change; no code is written |
| `bugfix` | 5 | one reproducible bug, with a regression test that failed first |
| `development` | 6 | one concern, where you already know the implementation path |
| `default` | 15 | a change that goes through PR and merge |
| `full-feature` | 24 | a feature that starts from requirements, PRD, and DDD modeling |

Say it plainly: `default` on a small change costs more in phase waiting than the change itself.
Fifteen phases each want an artifact, and a two-line fix does not have fifteen phases' worth of
decisions to record. Reach for `bugfix` or `development` and move up only when the change really
has a PR, a review, and a merge in it.

One thing the small workflows do not give you: `review`, `bugfix`, and `development` have no
`design` or `prd` phase, so they carry no SPEC ledger. The guarantee that every instruction you
gave survives a compacted conversation exists only in `default` and `full-feature`.

## When a phase blocks you

Three things actually stop a run. In all three, `agent-flow status` is the command that says
which one it is — read `reason` first.

**A missing artifact.** `reason: missing_phase_artifact`, with `required_artifact` naming the
file. The phase produced no document, so there is nothing to judge. Write that exact path and run
`next_command`. Writing a different filename does not count; the runner looks only where the
workflow said.

**A missing `## Completion Gate` marker.** `reason: missing_completion_markers`, and status
prints the list:

```text
missing_completion_markers: ["regression-test:", "red-observed:"]
```

The artifact exists but does not answer what the phase demanded. Add those exact marker lines to
the `## Completion Gate` block at the **end** of the artifact, then run `next_command`. Marker
values are cross-checked against the body, so a value that contradicts what the document says is
rejected the same as a missing one — filling in a plausible value to get past the gate does not
work.

**A reviewer verdict of `request-changes`.** The run does not stop; it routes backwards. In
`bugfix` the `review` phase sends `request-changes` back to `implement-fix`, so `current_phase`
becomes `implement-fix` again and the whole implement/review pair runs a second time. A single
`request-changes` from a single reviewer makes the overall verdict `request-changes`, however many
reviewers approved. To find out what was asked for, read `review.md` in the run directory — the
directory holding `required_artifact` — or search it:

```bash
agent-flow query "request-changes" --run-dir <run-dir>
```

The way out is to fix the findings and let the review run again. There is no override; you cannot
edit the verdict into an approval, because the artifact you would be editing is the evidence.

## Keeping the tool current

`run`, `start`, `status`, and `continue` check for a newer release at most once a day and print
one line to stderr when there is one. The check is capped at 1.5 seconds and caches its result —
including a failure — so a blocked network costs one attempt per day, not one per command.

```bash
agent-flow update
```

That asks immediately, bypassing the cache, and prints the installed version, the latest release,
and the upgrade command for how this kit was installed. For a Homebrew install that is
`brew upgrade chonamdoo/agent-flow/agent-flow`. It never upgrades anything itself, so nothing
changes under you while a run is open.

```bash
AGENT_FLOW_NO_UPDATE_CHECK=1
```

That turns off the automatic check. `agent-flow update` ignores the switch, because asking
directly is not the same as being asked.

After upgrading the kit, run `agent-flow .` again in each project. The assets copied into a
project are a copy, and upgrading the kit does not touch them. These are two separate warnings
with two separate fixes: the kit is behind, fixed by upgrading; the project's copy no longer
matches the kit, fixed by installing again.

## What this is not

**It is not a speed-up.** Nothing here promises less time. It promises that when a phase says a
test ran, a test ran. More phases mean more waiting, and on a small change that trade is a bad
one — that is why the small workflows exist.

**The reviewers cost money.** Review phases run reviewer sub-agents in parallel as separate
processes. Running two reviewers doubles the token cost of that phase, and the large workflows
have several such phases. There is no way to get the independence without paying for it; a
reviewer that shares the session that wrote the code is not a reviewer.

**A passed gate is not a correct change.** The gates prove the declared checks ran and reported
what the artifact claims. They do not prove the change is the right one. Read the diff.

**It is still a personal tool.** The verification criteria are one person's criteria applied to
one person's projects. Four things needed for a team to trust it are unsolved and written down in
[Open problems](../README.md#open-problems). Read those before rolling it out to anyone else, and
see [TEAM-ADOPTION.md](TEAM-ADOPTION.md) for the path that has been worked out so far.
