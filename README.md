# agent-flow

Project-agnostic AI workflow kit for Claude Code, Codex, and OMP. Each host follows the installed project snapshot, shared workflow YAML, hooks, and artifact contract; the Node `agent-flow` CLI is the public transition writer.

> **Status**: Phase 1–5 active. Scaffold, real adapters, multi-CLI fan-out, 10 profiles, Lore engine (parse / search / 4-tier compaction / auto-cite), PR-watch (gh-CLI polling with status classification). Phase 6 (optional sandboxing) deferred.

## Philosophy

1. **One trigger to remember.** `/agent-flow <task>` in Claude / Codex / OMP, backed by the pinned project launcher `./.agent-flow/bin/agent-flow run "<task>"`. Git projects start inside `.agent-flow/worktrees/feat-<slug>/` on branch `feat/<slug>`; installer setup is not repeated per task.
2. **Artifacts as state machine.** Each run writes state under `.agent-flow/runs/`; git-backed worktree runs keep runtime state under the repository git dir at `.git/agent-flow/worktrees/feat-<slug>/`. Context loss never loses progress.
3. **Chain is enforcement.** The next phase cannot start until the previous artifact exists. The slash trigger is only the entry point; the artifact chain blocks skipping.
4. **Stack-agnostic core, profile-specific knobs.** Workflow YAML stays generic. Profiles supply branching strategy, gate commands, review angles, vocabulary.
5. **Hosted AI contract.** Claude / Codex / OMP consume the same installed prompts, indexed skills, and hook policy. Host-specific adapter hints may differ, but phase order and artifact validation do not.

## Install (in any project root)

```bash
# Bootstrap the project (creates .agent-flow/, injects CLAUDE.md / AGENTS.md blocks)
npx <path-to-this-kit> install
```

The pinned project launcher `./.agent-flow/bin/agent-flow` is the public transition writer. It always resolves the Node runtime installed under the same `.agent-flow/` snapshot; `agent-flow-python` remains a compatibility and library CLI with the same worktree safety contract.

## Use

```text
# Run lifecycle from Claude / Codex / OMP
/agent-flow 유저 프로필 페이지 추가          # start
/agent-flow                                  # continue a selected worktree
/agent-flow status                           # progress for a selected worktree
```

```bash
# Direct CLI equivalents
./.agent-flow/bin/agent-flow run "유저 프로필 페이지 추가"
./.agent-flow/bin/agent-flow status                            # active run and exact next_command
./.agent-flow/bin/agent-flow run next                          # print current phase prompt
./.agent-flow/bin/agent-flow run advance                       # validate artifact and transition

# PR-watch
./.agent-flow/bin/agent-flow run push-watch                    # start PR status watch on feature branch
./.agent-flow/bin/agent-flow run push-watch-tick               # poll PR checks and review state once
```

## Layout

```
agent-flow-new/
├── bin/agent-flow-install.mjs        # npx installer (project-local bootstrap)
├── src/agent_flow/                   # Python orchestrator
│   ├── cli.py                        # `agent-flow-python` compatibility/library CLI
│   ├── runner.py                     # phase loop + auto-advance + profile injection
│   ├── artifact.py                   # .agent-flow/runs/<id>/ (atomic, safe)
│   ├── cli_detect.py                 # detect installed AI CLIs on PATH
│   ├── multi_review.py               # distribute review angles across CLIs
│   ├── subprocess_pool.py            # async parallel subprocess with timeout/drain
│   └── adapters/                     # base + auto + hosted (one class) + generic
├── workflows/default.yaml            # single full-cycle workflow
├── profiles/{generic,nextjs,node,typescript,react,react-native,python,android,ios,spring}.yaml
├── templates/_shared/review/         # AI-facing review-angle prompts
├── skills/agent-flow/                # `/agent-flow` common entry skill
├── skills/ddd-architecture/          # bundled DDD/Clean Arch expert skill
├── bootstrap/                        # CLAUDE.md / AGENTS.md templates
└── tests/
```

## Workflows

```text
default:
design → slice-plan → worktree → implement → comment-authoring
→ final-review → gates ↔ fix-loop → comment-authoring → final-review → gates
→ commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge → cleanup

full-feature:
domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design
→ worktree → run-start → red → green → refactor → comment-authoring
→ multi-review → architecture-review → gates ↔ fix-loop → comment-authoring
→ multi-review → architecture-review → gates → commit → push-pr
→ pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
```

`slice-plan` pauses for explicit approval. Its status prints an `--approve-pause` command; repeating the plain advance remains blocked. Review or gate failures route through the declared fix loop; phases are not silently collapsed or skipped.

Lore auto-citation reads an immutable, run-pinned snapshot for the duration of a run.

## Profiles

Each `profiles/<stack>.yaml` declares: `branching`, `gates`, `review_angles`, `artifacts`, `vocabulary`, `commit_convention`, `pr`. See `profiles/_schema.yaml`. Installed projects pin `primary_profile`, the selected profile union, and `auto|explicit` provenance in `.agent-flow/kit.json`; runtime environment overrides cannot change that snapshot. The runner injects the installed snapshot into every phase prompt — the host AI sees real data, not "look it up somewhere".

Install auto-detects one primary profile; repeat `--profile <id>` for an explicit monorepo union. The installer pins the selected common/profile skills into `.agent-flow/skills/index.json`. Runtime prompts load only the touched profile/conditional guide union. Project-local skills without activation metadata remain installed and host-discoverable as `on-demand`. Explicit `always` skills default to code/review phases unless `workflowPhases` narrows them; `conditional` skills become mandatory in any matching workflow phase, including design/Figma work, only when their task/path selectors also match.

## Review hosts

For a phase marked `multi_review: true`, the active host must run at least two independent in-host sub-agents in parallel. Each reviewer records `reviewer-source: sub-agent`, and any `request-changes` verdict blocks approval.

Claude, Codex, or OMP providers other than the active host may add optional review evidence when available. They do not replace the two required active-host sub-agents, and an unavailable optional provider is not by itself a completion blocker.

## Honesty notes

- **Self-application gap**: this kit ships a `ddd-architecture` skill prescribing DDD + Clean Architecture + Google Repository Pattern for user code. The kit's own Python source is procedural with one abstraction (`Adapter`). That's appropriate for a small CLI tool, but worth acknowledging — the kit doesn't dogfood every principle it prescribes.
- **PR watch uses `gh`, not the API directly**. agent-flow shells out to `gh pr view --json`, inheriting whatever auth the user already has. No agent-flow-side token management. If `gh` isn't installed or authenticated, the watcher surfaces a clear error.

## Roadmap

- **Phase 4** (active): Lore engine — Constraint/Rejected/Directive index, fingerprint dedup, 4-tier compaction (dedup/stale-drop/weight-decay/cluster reports), auto-cite into design phase.
- **Phase 5** (active): PR-watch — gh-CLI polling, status classification (green / has_comments / ci_failed / pending / merged / closed), exponential backoff with jitter.
- **Phase 6** (deferred): optional sandboxing if user demand emerges (currently out of scope).
