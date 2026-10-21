# Timeline — beauty-store, full-feature, 2026-10-20

All times UTC. Phase start/end come from `run/events.jsonl` (first and last event carrying that phase); run transitions are in
`run/transitions.jsonl`.

| Phase | Start | End | Wall | Blocks (reason) |
|---|---|---|---:|---|
| setup (empty Next.js project, `npm install`, commit) | 20:55 | 20:59:00 | ~4 min | — |
| install (`agent-flow-kit install --profile nextjs --architecture-mode stack`) | 20:59:09 | 20:59:12 | 3 s | — |
| domain-grill | 20:59:37 | 21:00:34 | 57 s | `phase_approval_required` (pause 1) |
| product-brief | 21:00:34 | 21:00:51 | 17 s | — |
| prd | 21:00:51 | 21:02:28 | 97 s | `missing_completion_markers` (spec-items format), `phase_approval_required` (pause 2) |
| slice-plan | 21:02:28 | 21:03:05 | 37 s | — |
| plan-review | 21:03:05 | 21:03:20 | 15 s | — (host self-review, `verdict: approve`) |
| ddd-design | 21:03:21 | 21:04:36 | 76 s | — |
| worktree | 21:04:36 | 21:05:30 | 54 s | — (`npm ci` in the worktree) |
| run-start | 21:05:30 | 21:05:44 | 14 s | — |
| red | 21:05:44 | 21:11:00 | 316 s | `skill_scope_grew`, `missing_completion_markers` |
| green | 21:11:00 | 21:15:52 | 292 s | `skill_scope_grew` |
| refactor | 21:15:52 | 21:16:33 | 41 s | `missing_completion_markers` (`code-review` in skills list) |
| comment-authoring | 21:16:33 | 21:16:55 | 22 s | — |
| multi-review | 21:16:55 | 21:17:53 (stopped) | — | reviewer subprocesses fail closed, exit 2 ×3 |
| architecture-review … handoff | not reached | | | |
| scoring (build, `next start`, scorer ×3 sessions, reference ×2, attribution runs) | 21:18 | 21:27 | ~9 min | — |

- Run wall clock (run start → stop at multi-review): 20:59:36 → 21:17:53 = **18 min 17 s**. Phases reached: 13 of 24.
- Time on runner blocks that needed investigation rather than work: red ≈ 3 min (`skill_scope_grew` with opaque ids, then
  markers the prompt never listed), green ≈ 1 min (same `skill_scope_grew`), multi-review ≈ 1 min to find the swallowed reason.
- Fix-loop rounds: **0** (fix-loop was never reached). `transitions.jsonl` nonetheless records `fix_round: 1` on
  `prd → slice-plan` and `green → refactor` (see failures.md F13).

## Reviewer subprocesses

| Attempt | Assigned jobs | Observed jobs | Executed | Outcome |
|---|---:|---:|---:|---|
| 21:16:55 | 12 (6 angles × claude, codex) | 2 (generalist ×2) | 0 | `error`: no verified provider write-sandbox backend |
| 21:17:28 | 12 | 2 | 0 | same |
| 21:17:4x (traceback run) | 12 | 2 | 0 | same |

Angles assigned: generalist, types, architecture-design, clean-architecture (legacy id, judged by the selected FSD
contract), render-stability, a11y. Reviewer prompt: 22,863 UTF-8 bytes each (`run/review-metrics-*.json`).

## Tokens and cost

- No reviewer subprocess ran, so there is no reviewer token or cost record. `review-metrics-*.json` exposes only prompt
  bytes, durations and stdout/stderr bytes (all 0).
- Rendered phase prompts (`prompt_rendered` events): 12 controller prompts, 144,830 bytes; 6 reviewer base prompts of
  17,969 bytes each (3 attempts × 2 providers); total 252,644 bytes.
- The controller session's own token use is not exposed by any run artifact.
