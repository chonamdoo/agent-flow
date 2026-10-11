# Adversarial review: Agent Flow domain-grill / human-confirmation proposals

You are an independent adversarial reviewer. Another reviewer gets the same materials; you will not see each other's output. This is an advisory review, not an Agent Flow workflow phase or approval. Do not modify any file, do not run Agent Flow lifecycle commands (`agent-flow run/start/continue/approve`), do not install anything.

## Materials (read them; verify claims against source)
- `controller-proposals.md` (this directory): the controller's proposal "B" for domain-grill and the controller's review of the pane investigation, including a recommended order.
- `pane-first-answer.md`: an investigator agent's bug report (observed runs where domain-grill passed with zero questions; a Codex run self-approved the PRD pause with `--approve <hash>`).
- `pane-final-answer.md`: the same investigator's comparison with the upstream mattpocock grilling skill and similar OSS, plus a minimal design (remove approve token from next_command, runner sub-state questions_open → awaiting_confirmation, human-turn ledger via UserPromptSubmit, fail-closed with `start --no-grill`).
- Optional deep evidence (large): `/Users/namdoo/.omp/agent/sessions/-Developer-Aiproject-agent-workflow/2026-10-08T23-47-16-498Z_01a11de9-fd92-75f5-8f62-d0d70e6e443c/local/` (research-*.md, adv-*.md).
- Repository (read-only): `/Users/namdoo/Developer/Aiproject/agent-workflow`. Key files: `src/agent_flow/workflows/full-feature.yaml`, `src/agent_flow/workflows/default.yaml`, `src/agent_flow/runner.py`, `src/agent_flow/artifact.py`, `src/agent_flow/core/host_write_boundary.py`, `src/agent_flow/core/hook_integrity.py`, `lib/installer-shared.mjs`, `lib/omp-hooks-extension.mjs`, `skills/agent-flow/SKILL.md`, `skills/grilling/SKILL.md`, `skills/grill-with-docs/SKILL.md`, `skills/domain-modeling/SKILL.md`, `AGENTS.md`, `maintainer/verification-policy.md`, `maintainer/2026-10-09-plugin-evolution-discussion.md`.

Every claim in the materials may be wrong, overstated, or incomplete — including the controller's. Do not assume any option is preferred. Rejecting a proposal, or concluding that nothing should change, is an acceptable outcome. Do not invent proposals to fill space.

## Non-negotiable product constraints (from the owner)
1. Agent Flow is a system that enforces workflows on AI agents. Existing enforcement strengths must not weaken.
2. Top priority: it must work across Claude Code, Codex and OMP (and other agents). Host parity means the same lifecycle/approval/evidence/protection outcomes for supported operations, not identical UI.
3. Python runner is the sole routing authority; workflow YAML is the single source of phase order/routes; runs pin their workflow definition.
4. Host write boundary keeps exactly two pre-blocks (literal protected path; irreversible command reaching it). No command write-target tables. Lifecycle exemption stays narrow ({continue, run, start, status}). Recovery paths (status/abort/lifecycle) must stay open to avoid deadlock. The post-tool leader tripwire hook stays.
5. No new workflows, no phase deletion, no lightweight bypass unless explicitly justified as non-weakening.
6. multi-review needs >=2 real Claude/Codex reviewer subprocesses; OMP is controller only.
7. Hooks are not a security boundary against a determined same-OS-user actor; the assurance target is cooperative AI agents, host-mediated actions, and lifecycle.
8. User-facing rule in skills/agent-flow/SKILL.md:49: never require an exact phrase or ask the user to enter a terminal command.
9. Owner decision already made: do not add an installed-runtime/YAML digest check at run start.
10. Owner's motivating question: domain interviewing does not have to happen live at the start of every run; interviews could be done and organized beforehand, then development starts.

## What to do
1. Verify the load-bearing factual claims against repository source with file:line evidence. Mark each as confirmed / wrong / unverified. Prioritize: approve token in next_command; no pause after domain-grill; prefilled marker values in the prompt; disable-model-invocation; skill-read gate absent for domain-grill; whether chat-approval flow depends on the token; existing human-input hooks; whether any runtime path already checks user participation; how SPEC confirmation and pause approvals work.
2. Adversarially attack each proposal (B; token removal; human-turn ledger; sub-state; fail-closed + --no-grill; cheap prompt/pause_after changes; recommended order). For each: does it actually fix the observed failure? does it weaken enforcement? does it break multi-host parity or the generic/hookless path? deadlock risk? conflicts with constraints above? hidden costs?
3. Identify missed scenarios or better alternatives, only if justified by evidence.
4. Output only justified proposals. For each proposal give: problem it solves (with evidence), exact change, impact range (files/subsystems/hosts/tests/evals), pros, cons/risks, what it preserves, acceptance evidence, and dependency order. If a controller proposal should be dropped, say so with reasons. If you have no proposal for an item, say "no proposal".

## Output format
Korean Markdown, concise, maximum ~1800 Korean words:
- 결론 (3-6 lines)
- 사실 검증 표 (claim | 판정 | 근거 file:line)
- 제안별 적대적 평가 (keep/modify/drop + 이유)
- 최종 제안 (각 제안: 해결하는 문제, 변경, 영향 범위, 장점, 단점·위험, 보존되는 것, 수용 증거, 선후관계)
- 제안하지 않는 것과 이유
- 확인 못 한 것
Label your own judgment as [판단] and unverified items as [미확인].
