# beauty-store × full-feature — first practical run (2026-10-20)

First end-to-end attempt of the `beauty-store` UI task (draft PR #397, branch `eval/ui-task-beauty-store@76f6e3c`) with
Agent Flow's `full-feature` workflow, from an empty Next.js project.

## Result

- **Run: stopped at `multi-review` (phase 13 of 24).** No reviewer subprocess can start on Linux: the provider write
  sandbox only has a macOS backend and fails closed elsewhere (failures.md F1). `architecture-review`, `gates`, `commit`
  and everything after were not reached; no fix-loop round ran.
- **Build score (official scorer, `--repeat 3`): 19/20 checks, 8/9 Spec Items** (`score.md`, `score.json`). Two more
  scoring sessions gave 18/20 and 19/20. All misses are two checks that race hydration in the scorer (failures.md F14);
  with a scratch scorer copy that waits for `networkidle` and pauses the clock at a future time, the same build scores
  20/20 twice. Reference clone: 20/20 in both sessions.
  That scorer change is now in `evals/ui_task_runner.mjs` (`goto`/`reload` wait for `networkidle`, `PAUSE_LEAD_MS`).
- Wall clock: run 18 min 17 s (20:59:36 → 21:17:53 UTC); see `timeline.md`.

## Setup

- Kit under test: this repository, `main@16c774a`. Task files: `eval/ui-task-beauty-store@76f6e3c`.
- Project `~/store` (outside the repo): `git init -b main`; `next@15.5`, `react@19`, `react-dom@19`, `typescript@5`,
  `@types/{node,react,react-dom}`, `vitest`, `jsdom`, `@testing-library/{react,jest-dom}`, `@vitejs/plugin-react`; scripts
  `dev`, `build`, `start`, `lint` (`tsc --noEmit`), `test` (`vitest run`); minimal `app/layout.tsx` + `app/page.tsx`.
  `docs/source-prd.md` = task `prd.md` with the "Purpose:" bullet replaced by "Acceptance: a browser test scores the build
  through the test-id contract below; any framework works." (its closing "## Acceptance" section, which names
  `acceptance.json`, was left as is; the build never looked for that file). `fixtures/catalog.json` copied. No
  `acceptance.json` or `reference/` in the project.
- Install: `node bin/agent-flow-kit.mjs install --root ~/store --profile nextjs --architecture-mode stack` → ok.
- `agent-flow status` host connection: install ok; claude `registered=ok trusted=unknown level=hook_unproven`; codex
  `registered=ok trusted=no level=runner_only`; execution not observed. Hooks were not active in the controller session,
  so every test/build/lint ran as `agent-flow run-command -- <argv>` (status later showed `commands=18`).
- Providers: `claude-session` usable; `codex-session` binary present but `codex login status` = Not logged in (still
  assigned reviewer jobs — failures.md F3); `omp-session` unavailable. Effective reviewer subprocesses: 0 (F1).
- Controller: one Claude Code session acting as host and, at pauses, as the user.

## Pauses and answers

| Pause | Phase | Answer given (as the user) |
|---|---|---|
| 1 | domain-grill (round 1, 5 questions; `logs/pause-01-domain-grill.md`) | "`docs/source-prd.md` is the settled requirement; every decision in it is final; do not change its constants, texts or test ids." → approved |
| 2 | prd read-back (11 SPEC items, 25 design values; `logs/pause-02-prd.md`) | same answer → approved |

No other question reached the user.

## What was built

FSD layout (`shared/`, `entities/catalog`, `features/{cart,search,shipping-prefs,listing}`, `widgets/*`, `app/*` routes),
11 Vitest files (one per SPEC, 50 tests, all green), `npm run lint` and `npm run build` green. The store project itself is
not pushed.

## Files

- `failures.md` — every failure or detour, with evidence, reproducibility and a phase_eval idea.
- `timeline.md` — phases, times, reviewer subprocess counts, prompt bytes.
- `score.md`, `score.json` — official scorer output (first scoring session).
- `run/` — copy of `.agent-flow/runs/20261020-205937/` (transitions, events, phase artifacts, reviewer outputs, metrics,
  the `*-norms-1.md` artifacts the runner moved aside; prompt payloads omitted).
- `logs/` — `01-run.txt` (initial `agent-flow run`), `log-NN.txt` (each `agent-flow continue`, in order), pause records.

## How to rerun

```bash
# from the repository root (the task is on main since PR #397)
R=$(pwd); T=$R/evals/ui-tasks/beauty-store
# project as in Setup, then:
node bin/agent-flow-kit.mjs install --root ~/store --profile nextjs --architecture-mode stack
cd ~/store && .agent-flow/bin/agent-flow run "Implement the Store in docs/source-prd.md: all five screens and the header, every Spec Item (SPEC-1..SPEC-9) and the exact data-testid contract, reading fixtures/catalog.json" --workflow full-feature
# follow each next_command; on Linux expect the stop at multi-review (F1). Use macOS to get further.
# score from the run's worktree:
npm run build && npx next start -p 3417 &
python3 $R/evals/ui_task.py run $T --url http://127.0.0.1:3417 --repeat 3 --summary score.md --json score.json
python3 $R/evals/ui_task.py run $T --static $T/reference --repeat 3
```

Playwright resolves from `/opt/node-tools/node_modules/playwright`; Chromium from `/opt/pw-browsers`.
