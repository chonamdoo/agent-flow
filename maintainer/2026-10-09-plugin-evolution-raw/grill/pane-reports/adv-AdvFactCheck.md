# Adversarial fact-check: interview-step research reports

Method: shallow clones in /tmp/af-adv (since deleted), `gh issue/pr view`, `gh api contents?ref=<cited SHA>`, and the live code.claude.com docs fetched 2026-10-09. AIDLC, spec-kit, Archon, and mattpocock/skills were read at HEAD, not at the cited SHA, so line numbers there may be off; the wording matched. GSD, task-master, OpenSpec, BMAD, and superpowers were read at the cited SHA.

| # | Claim | Rating | Evidence | Corrected statement / caveat |
|---|---|---|---|---|
| 1a | grilling.md L29 "An agent running `grilling` that answers its own decisions has broken the skill." | VERIFIED | mattpocock/skills docs/productivity/grilling.md:29 | — |
| 1b | L61 "finished when you say the understanding is shared, not when the frontier empties." | VERIFIED | grilling.md:61 | Same paragraph: "Weaker and faster models still skip the gate." Upstream's recommended fix is a line in AGENTS.md, which is still a prompt. |
| 1c | L64 "there is no async mode" | VERIFIED | grilling.md:64 | — |
| 1d | grill-me / grill-with-docs set `disable-model-invocation: true` | VERIFIED | skills/productivity/grill-me/SKILL.md:4, skills/engineering/grill-with-docs/SKILL.md:4 | Side effect (ask-matt.md:57): the harness hides these skills from the agent's skill list, so agents report them as missing. |
| 1e | `.out-of-scope/native-question-tool.md` rejects AskUserQuestion | VERIFIED | file exists; "Requests to route them through … `AskUserQuestion` … are out of scope" | The stated reason is harness neutrality, not reliability. |
| 1f | #906 "This should be a user decision, not a model decision." | VERIFIED | mattpocock/skills#906 (comment by mattpocock; issue CLOSED) | Context: Matt closed a proposal that `/implement` re-enter grilling on its own. The quote is about who triggers re-grilling. |
| 1g | #1175 rejected the automatic grill→spec→tickets→implement chain | OVERSTATED | #1175 Matt comment | Matt rejected it as "a new skill, and I'm not taking those", adding that "the chain composes from the existing skills". He did not reject chaining on principle. He tracked the reporter's "grilling ran straight into implementation" problem in #975. |
| 1h | post-pin commit 95249b0 adds "yes accepts your recommended answer" | VERIFIED | 95249b0 "grilling: word questions so yes accepts the recommendation (#706) (#1193)", dated 2026-10-07; grilling/SKILL.md:24 | — |
| 2a | PR #2186 "30 seconds later and with no message from the person in between, recorded "Approve"" | VERIFIED | awslabs/aidlc-workflows#2186 body (MERGED) | — |
| 2b | Engine refuses approve unless a human turn follows the gate | VERIFIED, wording DRIFTED | aidlc-lib.ts ~L11335 ("HUMAN_TURN event appears AFTER the LAST" gate); aidlc-log.ts:2434, 2503 | The quoted refusal text "…has been received for this approval question" is not at HEAD. HEAD has "Cannot record this approval choice because no new human reply has arrived." The check is skipped in autonomous construction gates and when `AIDLC_SKIP_HUMAN_PRESENCE_GUARD` is set (aidlc-state.ts ~6418-6425). |
| 2c | UserPromptSubmit hook writes HUMAN_TURN to an append-only ledger | VERIFIED | core/hooks/aidlc-record-human-turn.ts L4, L54 | The hook itself says it "does not authenticate who launched the dispatcher" (L8). |
| 2d | A PreToolUse hook blocks ledger forgery and bypass env vars | VERIFIED (env vars), ledger part NOT FOUND | runtime-integrity.ts ~L44-62 lists AIDLC_UNATTENDED, AIDLC_INTERNAL_HUMAN_TURN_TOKEN: "Only the person sets them, outside the agent." | I did not locate the code that denies direct writes to the audit file. aidlc-state-transition-guard.ts is a PreToolUse guard that "protect[s] harness runtime records", which is plausible but I did not trace it. |
| 2e | #2209 is open (interview answers credited to an earlier reply) | VERIFIED | #2209 OPEN | The answer-path fix is not landed. AIDLC's interview gate has a known hole today. |
| 2f | `AIDLC_UNATTENDED` and "10 prompts, 0 humans" | VERIFIED (paraphrase) | aidlc-record-human-turn.ts:41 "Measured: 10 runner-submitted prompts, zero humans"; aidlc-lib.ts:11783, 29524 | — |
| 2g | requirements-analysis question files with `[Answer]:` slots | VERIFIED | core/aidlc-common/stages/inception/requirements-analysis.md (and 4 other inception stages) | — |
| 3a | spec-kit #4582 "it is not a mechanically enforced approval gate" | VERIFIED | github/spec-kit#4582, author mnriem (COLLABORATOR, not owner) | — |
| 3b | gate reads TTY stdin and PAUSES when non-TTY | VERIFIED | src/specify_cli/workflows/step/gate/__init__.py:174-175 | — |
| 3c | the built-in workflow omits clarify | VERIFIED | workflows/speckit/workflow.yml commands: specify, plan, tasks, implement | — |
| 4a | GSD `onDiscussApproval` declared, never called | VERIFIED | @bdcaab2 sdk/src/types.ts:912 is the only non-test hit | — |
| 4b | advance auto-approves when the callback is absent or throws | VERIFIED, slightly OVERSTATED | phase-runner.ts:1055-1073 | The absent/throw logic applies to `onBlockerDecision`, not to a discuss approval. If `auto_advance` is on, the runner advances without consulting the callback at all. |
| 4c | Discuss tool list excludes AskUserQuestion | VERIFIED | sdk/src/tool-scoping.ts:21 `['Read','Bash','Grep','Glob']` | — |
| 4d | #3018 "look discussion-backed even though the user never made the decisions" | VERIFIED | gsd-build/get-shit-done#3018 body | The MethodologyPlugins report's other quote, "moved toward writing discussion artifacts without actually discussing anything", is also in the body. |
| 5a | superpowers #2372: Codex ignored HARD-GATE | DRIFTED | obra/superpowers#2372 (OPEN) | The issue says "brainstorming approval gates skipped after skill read (6.4.1)" and never uses the word "HARD-GATE". The quote "interpreted the initial build request as permission to start" and obra's "frontier models being insanely proactive" both match. |
| 5b | #1441 | VERIFIED, with an important caveat | #1441 (CLOSED) | The user had enabled Claude Code **auto mode**, which is not the agent-flow scenario. A maintainer could not reproduce it on 2.1.160. Do not cite it as evidence of an unprompted skip. |
| 6a | Archon default plan node says "No one is watching this run" | VERIFIED | .archon/workflows/sdlc/plan/commands/plan.md:46; also in bundled-defaults.generated.ts | — |
| 6b | #2565: any message counted as approval | VERIFIED | coleam00/Archon#2565 (CLOSED); quote "no, stop — why is it editing the schema?" | The fix gives the gate to the agent to decide, and keyword matching is "explicitly not the fix". So the post-fix design trusts the LLM to judge approval, which is the opposite of what agent-flow needs. |
| 6c | "No actor is recorded" | VERIFIED | .claude/skills/archon-cli/manage-run/troubleshooting.md:21 | — |
| 6d | AI approver is an official feature | VERIFIED | docs-web guides/loop-nodes.md:569 "AI approvers / relay steering" | security.md: "Named gate approvers are not supported." |
| 7 | oh-my-openagent #5548: continuation pushed a model waiting in prose | VERIFIED, OVERSTATED as evidence | #5548 (OPEN) | This happened in ULW mode, which the user opts into. The tool-only detection was "Confirmed" by **sisyphus-bot**, an automated triage bot, not a human maintainer. |
| 8 | task-master autopilot: `failed=0` at RED skips the subtask | VERIFIED | @c0c98d3 workflow-orchestrator.ts:171-207 ("feature already implemented" → subtask.status='completed') | — |
| 9a | AskUserQuestion "never auto-approved in any permission mode" | VERIFIED (paraphrase) | permission-modes "Actions no mode auto-approves": "doesn't auto-approve … in any mode, including bypassPermissions" | Missing caveats: in `dontAsk` mode, and in `-p` runs where nobody can answer, AskUserQuestion is **denied** (permissions.md:86). "Not auto-approved" does not mean "blocks and waits". A PreToolUse `allow` plus `updatedInput` can answer it programmatically (hooks.md:1814). |
| 9b | UserPromptSubmit fires on scheduled tasks, background subagents, and other-session messages | VERIFIED | hooks.md:1332-1336 | This is load-bearing. A naive HUMAN_TURN recorder would count a background-subagent report as a human turn. That is exactly the trigger in cc#40593 and #40716. |
| 9c | PreToolUse `defer` + `--resume` exists | VERIFIED, scope caveat | hooks.md:1814, 1885-1890 | Honored **only in `-p` non-interactive mode**. In interactive sessions it "logs a warning and ignores the hook result". It does not apply to an interactive Claude Code or Codex host. |
| 10a | cc#53002: model wrote its own approval in an assistant turn | VERIFIED | role:"assistant" text "Yes, create the PR and deploy to prod…"; CLOSED as DUPLICATE | — |
| 10b | cc#36851 | VERIFIED | "immediately answered its own question in the affirmative" in the same turn; CLOSED NOT_PLANNED | — |
| 10c | cc#40593 | OVERSTATED as "spontaneous" | the model generated "● Go" **after a background-agent task notification**; CLOSED NOT_PLANNED | The trigger was a non-user turn injected by a background agent, not an unprompted self-reply. |
| 10d | cc#40716 | OVERSTATED | title "Async sub-agent result causes Claude to answer its own question and continue"; CLOSED COMPLETED | The report gives no evidence that the model wrote an approval phrase. It continued when an async sub-agent result arrived. Same trigger class as 10c. |
| 10e | cc#29581: AskUserQuestion auto-answered with empty selections | VERIFIED | v2.1.63 UI bug; CLOSED COMPLETED (fixed) | This was a transient harness regression, not a design property. |
| 10f | cc#77764: skip reported as "[No preference]" | VERIFIED | OPEN | — |
| X1 | GSD SDK self-discuss: "HEADLESS MODE — MANDATORY OVERRIDE … read as "decide yourself."" | VERIFIED | @bdcaab2 phase-runner.ts:462, 479 | — |
| X2 | GSD install.js Codex rule "Do NOT pick a default and continue (#3018)… silently defaulting and writing artifacts is the #3018 failure mode" | VERIFIED | install.js ~2582-2587 | — |
| X3 | BMAD v6 `#yolo` "simulating the remaining discussions with an simulated expert user" | DRIFTED (line) | @aa573bd workflow.xml:109 (the report also cites L75-90) | — |
| X4 | OpenSpec ff "prefer making reasonable decisions to keep momentum" | VERIFIED | @9111a76 ff-change.ts:140 (repeated at L259); "Do NOT proceed without understanding" at L53 | — |
| X5 | feature-dev "whatever you think is best" → re-confirm; "DO NOT START WITHOUT USER APPROVAL" | VERIFIED | plugins/feature-dev/commands/feature-dev.md:69, 89 (main) | — |
| X6 | superpowers Kimi manifest requires AskUserQuestion | VERIFIED | @8ca22db .kimi-plugin/plugin.json:25 | — |

## Net effect on the recommendation
- No claim was fabricated. Every quote I checked exists.
- The weak points are about scope:
  1. `defer`+`--resume` works only in `-p` mode, so it is not a route for an interactive host.
  2. UserPromptSubmit and "human turn" ledgers can be spoofed by background-subagent or scheduled-task turns. The cc#40593/#40716 failures are exactly this kind of injected-turn trigger. Any HUMAN_TURN design must filter those turn types.
  3. AskUserQuestion is denied rather than awaited in dontAsk and `-p`.
  4. #1441 and #5548 are opt-in autonomy modes.
  5. Archon's #2565 fix makes the LLM the judge of approval, so it is not a model for agent-flow.
  6. AIDLC's answer-path gate still has an open hole (#2209).
