# Failures and wandering — beauty-store, full-feature, 2026-10-20

Paths are relative to this directory: `logs/log-NN.txt` are the runner outputs of each `agent-flow continue`, `run/` is a
copy of `.agent-flow/runs/20261020-205937/`. The kit is `main@16c774a`.

Ordered by impact.

## F1 — multi-review cannot run on Linux: the run stops at phase 13/24 (blocker)

- Phase: multi-review.
- Evidence: `run/multi-review-generalist-claude.md` and `-codex.md`: `status: ERROR`, `returncode: -1`, `duration: 0.00s`,
  `no verified provider write-sandbox backend for this operating system`. Source:
  `src/agent_flow/providers/subprocess.py:474` (`verify_provider_sandbox_backend` raises unless `sys.platform == "darwin"`);
  documented at `docs/USAGE.md:675`. `logs/log-19.txt`, `logs/log-20.txt` exit 2.
- Effect: no reviewer can ever start on Linux, so `full-feature` cannot reach architecture-review, gates, commit. Per the
  brief I stopped here instead of bypassing it. The `commit`/`push-pr` stop point was never reached.
- Nothing warned earlier: `agent-flow status` (host connection block), the run header and `run-start` all passed;
  the platform check fires 17 min into the run after 12 phases of work.
- Reproducible: yes, 3/3 attempts, deterministic on any non-macOS host.
- phase_eval / regression idea: start a `full-feature` (or any workflow with a `multi_review` phase) run on
  `sys.platform == "linux"` and expect a run-start (or `status`) blocker naming the missing sandbox backend, before the
  first phase.

## F2 — the multi-review failure is printed out of order and `status` then points the host at the wrong action

- Phase: multi-review.
- Evidence: `logs/log-19.txt`/`log-20.txt`: the reason (`required reviewer subprocesses failed closed: …`) is the
  first line, above the `▶ resuming` header (stderr unbuffered, stdout buffered), and no `status:` block or
  `next_command` follows — the run just exits 2. A reader scanning the tail sees only `[run] multi-review` and exit 2.
  Afterwards `agent-flow status` reports `status: awaiting_host`, `reason: missing_phase_artifact`,
  `required_artifact: …/multi-review.md`, i.e. it invites the controller to write the aggregate review it must not
  author ("never substitute controller-session work for a failed child process").
- Reproducible: yes.
- Idea: on reviewer fail-closed, print the structured `status: blocked` block with a reviewer-specific reason
  (e.g. `reviewer_sandbox_unavailable`) and make `status` report the same reason instead of `missing_phase_artifact`.

## F3 — a provider that cannot authenticate is still assigned half the reviewer jobs

- Phase: multi-review (and run header).
- Evidence: `codex login status` → `Not logged in`; `agent-flow provider list` → `codex-session available`; run header
  `available : claude, codex`; `run/review-metrics-*.json` `assigned_job_count: 12` (6 angles × 2 providers).
- Effect here: masked by F1, but on macOS the 6 codex jobs would fail at login, and with only one working provider the
  "2+ independent reviewers" rule could not be met. Availability is decided by binary presence only.
- Reproducible: yes.
- Idea: provider availability fixture with an installed but unauthenticated CLI; expect `unavailable (not logged in)`
  and the reviewer pool to exclude it, or a run-start warning.

## F4 — `skill_scope_grew` fires with an unchanged skill set, names only opaque ids, and moves the artifact away

- Phases: red (`logs/log-12.txt`), green (`logs/log-15.txt`).
- Evidence: `scope grew: document:32f0254d…, document:5392bb15…` (red) and `document:81caf8c7…, document:ff3aa4e0…`
  (green). `run/meta.json:skill_scope.names` is the same 9 skills before and after, and `agent-flow skills prompt --phase
  red|green` lists the same 9 bodies with the same SHA-256. The ids come from `NormativeDocument.identity`
  (`src/agent_flow/core/skill_resolver.py:148`), which hashes the *routes* too, so creating the phase's first
  `.tsx`/`.ts` files adds changed-file routes to the same bodies and yields new ids. No command maps an id back to a path.
  The runner renamed the written artifact to `red-norms-1.md` / `green-norms-1.md` (`run/`), so the artifact had to be
  restored by hand.
- Effect: two extra blocks (~4 min) whose instruction ("Read them") cannot be followed.
- Reproducible: yes, deterministic whenever an implementation phase creates the first files of a new type after its
  prompt was rendered.
- phase_eval idea: red-phase case on an empty seed where the author creates `tests/*.test.tsx`; expect no
  `skill_scope_grew` when the set of required bodies (path + sha) is unchanged, or, if it does fire, the message lists
  file paths.

## F5 — completion markers the prompt never listed, and a value it allowed is rejected

- Phases: red (`logs/log-13.txt`), refactor (`logs/log-17.txt`); green avoided it only because I pre-filled them.
- Evidence: red prompt "Required marker lines" (`logs/log-11.txt`) lists 11 markers and allows
  `project-local-skills: checked|n/a`. After submission the runner demanded `skill-availability: pass|degraded`,
  `skill-use-evidence: verified|unavailable`, `architecture-contract: applied`, `project-local-skills: checked`,
  `project-local-skills-used: <all 9 bundled skills>`, `project-local-skill-docs: applied` — none of which were in the
  list, and `n/a` was refused. The bundled kit skills are called "project-local" in the demanded marker. Refactor then
  required `code-review` in that list (it was in the read plan but not mentioned in the phase prompt).
- Reproducible: yes.
- phase_eval idea: red author case that writes exactly the prompt's "Required marker lines" with `project-local-skills:
  n/a`; expect the runner to accept it (or the prompt to list every marker the gate checks).

## F6 — `spec-items:` marker format is not stated in the prd prompt

- Phase: prd (`logs/log-04.txt`).
- Evidence: the prompt only says to record `spec-items: none` for an instruction-free task. I wrote `spec-items: 11`;
  the block reads `spec-items: expected SPEC-1, …, SPEC-11 (declared 11)` — the expected form (comma-separated ids) is
  only derivable from the error. Source `src/agent_flow/core/design_value_check.py:86`.
- Reproducible: yes.
- phase_eval idea: prd author case with 3 SPEC items; score whether the first submission passes the marker check.

## F7 — slice-plan gets no architecture skill; the FSD contract first appears in ddd-design

- Phases: slice-plan → ddd-design.
- Evidence: the slice-plan prompt (`logs/log-05.txt`) has no "Selected architecture contract" section and no required
  skills, yet asks to record "boundary scope in the selected architecture contract's own terms"; it never names the contract
  body. My plan used `lib/` + `components/` (`run/artifacts/slice-plan.before-ddd-design.md`). ddd-design then required
  `react-fsd-architecture` (FSD layers), so the plan's `- Files:` lists — which "route implementation skills" — were
  wrong. I rewrote them in place after plan-review had approved the plan; the runner did not notice the change to an
  approved upstream artifact.
- Reproducible: yes (any stack-mode nextjs run).
- phase_eval idea: slice-plan case in stack mode; oracle checks that `Files:` paths follow FSD layers.

## F8 — header says `architecture: default` while the run uses `stack`

- All phases. Every runner header prints `▶ architecture: default`; the phase envelopes say `Mode: stack`, and
  `run/review-metrics-*.json` has `"architecture_mode": "stack"`. Cosmetic, but it made me check whether
  `--architecture-mode stack` had been ignored.
- Reproducible: yes.

## F9 — worktree/branch slug ignores `max_slug_length: 60`

- Phase: run start / worktree.
- Evidence: branch `feat/implement-the-store-in-docs-source-prd.md-all-five-screens-and-the-header-every-spec-item-spec-1-spec-9-and-the-exact-data-testid-contract-reading-fixtures-catalog.json`
  (≈170 chars, keeps `.md`/`.json` dots); profile `branching.naming.max_slug_length: 60` (`run/artifacts/worktree.md`).
  Every `next_command` repeats it, which bloats every runner output.
- Reproducible: yes.
- Idea: unit case on the slug builder with a long task containing file names; expect ≤ 60 chars, no dots.

## F10 — the nextjs worktree has no `node_modules`

- Phase: worktree.
- Evidence: fresh worktree had no `node_modules` (`run/artifacts/worktree.md`); the nextjs profile leaves
  `worktree_setup.link_node_modules` off and the CLI only copies `project.yaml`. `npm test`/`build` gates would fail until the
  host ran `npm ci` (which the worktree prompt half-forbids: "do not … run install").
- Reproducible: yes.
- Idea: nextjs fixture run; expect either `link_node_modules: true` for the profile or the worktree prompt to name the
  dependency install as part of setup verification.

## F11 — plan-review is a self-review

- Phase: plan-review. The same controller session that wrote slice-plan.md writes `verdict: approve`
  (`run/artifacts/plan-review.md`). No independent reviewer, so the F7 layout mismatch passed review.
- Reproducible: by design.

## F12 — workflow shape conflicts with the `tdd` skill

- Phases: red/green/refactor. The red prompt asks for failing tests for all slices in one phase; the `tdd` skill it
  requires names exactly that "horizontal slicing" as an anti-pattern. The resulting red was an import-failure red
  (11 suites fail to resolve modules, 0 assertions run — `run/artifacts/red.log`), which the runner accepted.
  The refactor phase also requires `code-review`, whose process is to spawn parallel sub-agents, while the host hint says
  not to use in-session sub-agents, and `tdd` says refactoring belongs to review.
- Reproducible: yes.

## F13 — `fix_round: 1` without any fix loop

- `run/transitions.jsonl` lines 3 (`prd → slice-plan`) and 10 (`green → refactor`) carry `fix_round: 1`; all others 0;
  fix-loop never ran. Both phases had one prior `validation_failed` block (prd markers, green scope-grew), so the counter
  seems to count re-submissions, not fix-loop rounds. Unexplained; worth a look before using `fix_round` as a metric.

## F14 — build vs scorer: interactions before hydration (intermittent, 1–2 checks)

- Phase: none of the workflow phases exercised the built app in a browser; found at scoring.
- Evidence: official scorer, three sessions of `--repeat 3`: 19/20, 18/20, 19/20.
  - `search-debounce` (SPEC-1) fails 1/3, 2/3, 2/3 runs at step 1 `{"pause": true}`:
    `clock.pauseAt: Error: Cannot fast-forward to the past`.
  - `listing-page-size-empty` (SPEC-4) failed 2/3 runs in one session: page size 48 selected but 24 cards.
  - Reference clone: 20/20 twice.
  - Scratch copy of the scorer with `goto` waiting for `networkidle` and `pause` at `now + 1000`: this build 20/20 twice,
    reference 20/20.
- Cause: `evals/ui_task_runner.mjs` `goto` waits for `domcontentloaded`, so on a server-rendered Next.js page the
  scorer selects/types before React hydrates and the change is lost (a real defect for fast users, invisible to jsdom
  unit tests); and `pause` does `pauseAt(await evaluate(Date.now()))` on a still-running clock, which races when hydration
  keeps the main thread busy. The reference is static HTML with no hydration, so it never hits either race.
  Attribution: evaluator flake (both races) plus an agent-flow gap — no phase or gate in the nextjs profile runs the
  touched routes in a browser, although `react-development-guide` asks to "verify no hydration or boundary errors in the
  touched route".
- Reproducible: intermittent (seen in every scoring session).
- Ideas: (scorer) wait for hydration or `networkidle` in `goto` and pause at a future time; (phase_eval) web verification
  case: a Next.js page whose `<select>` is changed before hydration, oracle = browser check that the choice survives.

## Not observed

- Test-id or text mismatches with the PRD: none — every check that ran found its ids and texts.
- Missing timers or persistence: none (200 ms debounce, 6 s carousel, localStorage for cart/recent/location/currency all
  present and scored).
- Reviewer disagreements or invalid verdicts: not measurable — no reviewer ran (F1).
- Repeated fix loops: none (fix-loop not reached).
