# Push Watch workflow

## Problem

The installed full-feature workflow has PR watch phases, but the installed skill does not provide a clear entrypoint for the commit, push, PR, watch, fix, and approval-to-merge loop.

## Goal

Add `push-watch` as the project-local entrypoint for shipping a verified branch to a PR and watching it until it is ready for an approved merge.

## Scope

- Install `.agent-flow/skills/push-watch/SKILL.md`.
- Install `.agent-flow/prompts/push-watch.md`.
- Install `.agent-flow/prompts/push-watch-tick.md`.
- Add CLI support for `agent-flow-kit run push-watch`.
- Add CLI support for `agent-flow-kit run push-watch-tick`.
- Keep merge as an explicit approval phase. `push-watch` may route to `merge`, but must not merge without approval.

## Behaviors

- `push-watch` refuses to run on protected branches: `main`, `master`, `develop`.
- `push-watch` records a state file under `.agent-flow/state/push-watch.json`.
- `push-watch-tick` reads PR state through GitHub CLI output and writes `artifacts/pr-watch.md`.
- `push-watch-tick` records one of: `status: pending`, `status: green`, `status: comments`, `status: ci-failed`.
- Existing `run advance` uses `artifacts/pr-watch.md` to route to fix phases or approved merge.

## Non-goals

- No unattended merge.
- No force push.
- No destructive branch cleanup.
