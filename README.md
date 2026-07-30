# agent-flow

Project-agnostic AI workflow kit. Start with `/agent-flow` in Claude Code or Codex; the same CLI core writes the same artifacts underneath.

> **Status**: Phase 1–5 active. Scaffold, real adapters, multi-CLI fan-out, 8 profiles, Lore engine (parse / search / 4-tier compaction / auto-cite), PR-watch (gh-CLI polling with status classification). Phase 6 (optional sandboxing) deferred.

## Philosophy

1. **One trigger to remember.** `/agent-flow <task>` in Claude / Codex, backed by `agent-flow run "<task>"`. Git projects start inside the sibling folder `<repo>.worktrees/feat-<slug>/` on branch `feat/<slug>`, leaving the leader checkout untouched; installer setup is not repeated per task.
2. **Artifacts as state machine.** Each run writes state under `.agent-flow/runs/`; git-backed worktree runs keep runtime state under the repository git dir at `.git/agent-flow/worktrees/feat-<slug>/`. Context loss never loses progress.
3. **Chain is enforcement.** The next phase cannot start until the previous artifact exists. The slash trigger is only the entry point; the artifact chain blocks skipping.
4. **Stack-agnostic core, profile-specific knobs.** Workflow YAML stays generic. Profiles supply branching strategy, gate commands, review angles, vocabulary.
5. **Hosted AI contract.** Claude / Codex hosts share a single `HostedAdapter` parameterized by name; only the hint string differs. The runner is unaware of which AI is active.

## Install (in any project root)

```bash
# 1. Make the Python CLI available on PATH (until published to PyPI)
pip install -e <path-to-this-kit>

# 2. Bootstrap the project (creates .agent-flow/, injects CLAUDE.md / AGENTS.md blocks)
npx <path-to-this-kit> install

# 다른 프로젝트에 설치할 때는 --root를 쓴다. 그 디렉터리로 cd 할 필요가 없다.
npx <path-to-this-kit> install --root <project-path>
```

The order matters: `pip install -e` first, then `npx ... install`. The bootstrap markdown references the `agent-flow` binary, which step 1 makes available.

## Use

```text
# Run lifecycle from Claude / Codex
/agent-flow 유저 프로필 페이지 추가          # start
/agent-flow                                  # continue a selected worktree
/agent-flow status                           # progress for a selected worktree
/agent-flow abort                            # cancel a selected worktree run
```

```bash
# Direct CLI equivalents
agent-flow run "유저 프로필 페이지 추가"
agent-flow continue --worktree "feat-user-profile"
agent-flow status --worktree "feat-user-profile"
agent-flow abort --worktree "feat-user-profile"
agent-flow worktree list
agent-flow worktree remove --name "feat-user-profile"

# SPEC ledger (design / prd items)
# On supported Codex, Claude, and OMP hosts, reply exactly `승인` in the current
# chat. If the managed user-prompt hook is unavailable, run the path-free
# fallback from the target worktree and type `승인` in its interactive prompt.
agent-flow spec confirm
# A confirmation observed from an agent shell voids the approval.
agent-flow spec approve <spec-id> --run-dir <run-dir>

# Gates
# The workflow `gates` phase must be run with --phase all. A --phase pre-commit
# result is not accepted as QA evidence: the runner reads `produced_by.gate_phase`
# from gate-results.json.
agent-flow gates                             # default --phase pre-commit (local spot check)
agent-flow gates --phase all                 # required by the gates phase: pre-commit + pre-push (build / test)

# Skills
# `skills sync` only fetches the profile's external skill_sources.
# Profiles and workflows themselves are updated by re-running the installer.
agent-flow skills sync

# Lore (memory) management
agent-flow lore init <slug>                  # scaffold a new lore entry
agent-flow lore list                         # list active lore, sorted by weight
agent-flow lore search "<query>"             # keyword search
agent-flow lore compact                      # dry-run compaction report
agent-flow lore compact --apply              # commit compaction (archives duplicates + stale)
agent-flow lore restore <filename>           # restore an archived lore

# PR-watch
agent-flow pr-watch <number>                 # poll PR until actionable status
agent-flow pr-watch <number> --once          # single fetch (debugging)
```

## Layout

```
agent-flow-new/
├── bin/agent-flow-install.mjs        # npx installer (project-local bootstrap)
├── src/agent_flow/                   # Python orchestrator
│   ├── cli.py                        # `agent-flow run|continue|status|abort`
│   ├── runner.py                     # phase loop + auto-advance + profile injection
│   ├── artifact.py                   # .agent-flow/runs/<id>/ (atomic, safe)
│   ├── cli_detect.py                 # detect installed AI CLIs on PATH
│   ├── multi_review.py               # distribute review angles across CLIs
│   ├── subprocess_pool.py            # async parallel subprocess with timeout/drain
│   ├── adapters/                     # base + auto + hosted (one class) + generic
│   ├── workflows/default.yaml        # single full-cycle workflow (정본 한 벌)
│   └── profiles/{generic,nextjs,node,python,react-native,android,ios,spring}.yaml
├── templates/_shared/review/         # AI-facing review-angle prompts
├── skills/agent-flow/                # `/agent-flow` common entry skill
├── skills/ddd-architecture/          # bundled DDD/Clean Arch expert skill
├── bootstrap/                        # CLAUDE.md / AGENTS.md templates
└── tests/
```

## Workflow (`default.yaml`)

```
design → slice-plan → ═══ pause ═══
       → worktree → implement (TDD red→green→refactor inside, per slice)
       → final-review (multi_review: true — fans out across installed CLIs)
       → fix-loop → commit → push-pr → pr-watch → merge → cleanup
```

The AI collapses or expands phases per task. A 1-line fix may produce a single-question interview, a one-paragraph design, and a one-slice plan; a feature deepens every phase.

## Profiles

Each `profiles/<stack>.yaml` declares: `branching`, `gates`, `review_angles`, `artifacts`, `vocabulary`, `commit_convention`, `pr`. See `profiles/_schema.yaml`. The runner parses the active profile (resolved from `.agent-flow/kit.json:profile` or `AGENT_FLOW_PROFILE`) and injects it into every phase prompt — the host AI sees real data, not "look it up somewhere".

## Multi-CLI fan-out

For phases marked `multi_review: true` (currently `final-review`), the runner distributes review angles round-robin across installed CLIs:

- **3 CLIs detected** → 1 angle per CLI (most diverse review)
- **2 CLIs** → round-robin (host CLI last)
- **1 CLI** → all angles on that CLI (host adapter handles in-host parallelism — Claude `Task` tool, etc.)
- **0 CLIs** → host AI handles all angles itself

Override with `AGENT_FLOW_REVIEWERS="claude,codex"`. Per-angle artifacts (`final-review-<angle>.md`) survive partial timeouts — one slow CLI does not block siblings.

## Honesty notes

- **Self-application gap**: this kit ships a `ddd-architecture` skill prescribing DDD + Clean Architecture + Google Repository Pattern for user code. The kit's own Python source is procedural with one abstraction (`Adapter`). That's appropriate for a small CLI tool, but worth acknowledging — the kit doesn't dogfood every principle it prescribes.
- **PR watch uses `gh`, not the API directly**. agent-flow shells out to `gh pr view --json`, inheriting whatever auth the user already has. No agent-flow-side token management. If `gh` isn't installed or authenticated, the watcher surfaces a clear error.

## Roadmap

- **Phase 4** (active): Lore engine — Constraint/Rejected/Directive index, fingerprint dedup, 4-tier compaction (dedup/stale-drop/weight-decay/cluster reports), auto-cite into design phase.
- **Phase 5** (active): PR-watch — gh-CLI polling, status classification (green / has_comments / ci_failed / pending / merged / closed), exponential backoff with jitter.
- **Phase 6** (deferred): optional sandboxing if user demand emerges (currently out of scope).
