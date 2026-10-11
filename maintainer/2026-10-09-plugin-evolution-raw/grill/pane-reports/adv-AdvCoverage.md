# Adversarial review: conclusions, rankings, coverage (AdvCoverage)

Date 2026-10-09. Scope: whether the four reports' conclusions and rankings hold up, rather than individual quotes. Rating legend: VERIFIED / DRIFTED / OVERSTATED / WRONG / NOT FOUND.

How sources were read:
- I read these myself: clones or `gh api` at the SHAs given below.
- Three read-only scouts (no shell, GitHub API at pinned SHAs) read the "missed" projects. I re-checked their headline claims myself: OMX #2558, gstack #2719, Cline #13729, the gentle-ai hook source, PR #4531 and the v4.0.0 release. Their other line numbers carry the tag *(scout-read)*.

---

## 1. Contradictions between reports

| # | Claim A | Claim B | Resolution against source | Verdict |
|---|---|---|---|---|
| C1 | MethodologyPlugins §8.3: no project requires evidence that a human answered | SpecDrivenKits §6: AI-DLC `verifyApprovalDecision` refuses approval without a human turn after the gate | `aidlc-state.ts@47feb76` L6404, L6428 ("end the turn and let them answer"), L6480/6487 ("no new human reply has been received"). GSD-2 `write-gate.ts@33c00aa` L392 ("HARD BLOCK: Discussion gate … not been confirmed by the user"). gentle-ai v3.7.0 `sdd_preflight_hook.go@6dee8f8` L94-100, L180-185 (authority comes only from the transcript: an AskUserQuestion tool_use matched to a user tool_result). | A is true only for its own six projects and **WRONG** as a general statement. All three counterexamples check that a human was present and in which order. None of them checks what the human meant. |
| C2 | RuntimeOrchestrators §0.1: GSD-2 is the only runtime interview gate | SpecDrivenKits §8.3: AI-DLC is the only one that enforces the interview in code | Both mechanisms exist (C1). A third one, gentle-ai v3.7.0, was shipped and later retired in v4.0.0 ("SDD and OpenSpec retire", release notes). | Both **OVERSTATED** (each was scoped to its own corpus). The correct count is 2 live runtime gates, plus 1 that was retired. |
| C3 | "Closest to agent-flow": Runtime picks task-master autopilot; Methodology picks the GSD SDK PhaseRunner; SpecDriven rates AI-DLC Very High and the spec-kit engine High | — | Three reports crown three different winners because each ranked only inside its own corpus. See §2. | Rankings are **inconsistent**. Re-ranked in §2. |
| C4 | Methodology: GSD SDK PhaseRunner is High, "most similar" | Runtime: `gsd-sdk auto` is Medium | `phase-runner.ts@bdcaab2` L154-196: the discuss step runs as a separate Agent SDK session. `tool-scoping.ts` gives Discuss `['Read','Bash','Grep','Glob']`, with no question tool even outside auto mode. L1068/1071 and L1401/1414/1427 approve automatically. The repo is **archived**. Successor `open-gsd/gsd-core@ed0f3fd` has no `sdk/` tree. | Methodology is **OVERSTATED**. The runner is abandoned and can never interview. Side finding: Runtime cites L159-161 for "running headless with no human present", but that text is at L464 (**DRIFTED**). |
| C5 | Methodology §8.2: Codex has none of the three waiting mechanisms [INFERENCE] | Runtime §9/§10.3: Codex supports UserPromptSubmit; use `request_user_input` and fail closed when it is absent | `openai/codex@22ebb0f`: `allows_request_user_input()` matches only `Plan` (`protocol/src/config_types.rs` L700-702). `default_mode_request_user_input` is `UnderDevelopment, default_enabled: false` (`features/src/lib.rs` L1768-1771). exec mode rejects the tool (`exec/src/lib.rs` L2125). The Default-mode template says "**strongly prefer making reasonable assumptions** … rather than stopping to ask questions" and "If `request_user_input` returns no answers, continue with best judgment" (`collaboration-mode-templates/templates/default.md` L11-15). Hooks, including UserPromptSubmit with `turn_id`, are on by default, but project hooks run only after a hash-based trust review (developers.openai.com/codex/hooks). | Methodology is right about the question tool and wrong to imply that Codex has no hooks. Runtime's "fail closed if the tool is absent" would **always** fail closed in Codex Default mode, so that remedy is **WRONG** as written. |
| C6 | SpecDriven summary: spec-workflow-mcp is one of three "signals the model cannot fabricate" | Runtime §10: grade D, forgeable | Both reports note the approvals are unsigned JSON and the localhost POST is unauthenticated. Both mark the bypass as [INFERENCE]. No observed forgery. | SpecDriven **OVERSTATED**. Forging needs a deliberate file edit or curl call, so the right grade is D (C at best). |
| C7 | SpecDriven: spec-kit engine gate means "the LLM cannot pass" | The same report: `verdict_input` lets the model pass via `--input` | Commit `961be9cd` (2026-10-07): "an interrupted gate prompt must not approve the gate". Ctrl-C/EOF used to return `options[-1]`. | The gate is sound only when stdin is a TTY, and it was being fixed as recently as this week. **OVERSTATED**. |
| C8 | Methodology §0.4/§2: AskUserQuestion is never auto-approved, and agent-os v3 is "strongest" | Runtime §10 grade B: the model can simply not call it; nested-Skill calls returned empty answers (GSD #1009) | Scouts found more empty or auto answers: Conductor #160 ("User submitted without answering questions"), Cline `clearPending` resolves to "" on teardown, Roo `followupAutoApproveTimeoutMs`. The Codex question box times out after 2 min (AI-DLC `aidlc-codex-adapter.ts` L931-933). | The tool result cannot be forged, but empty or default answers are common. A gate must require an answer that is **non-empty and typed by the human**. |
| C9 | Runtime §10.3: make questions observable tool calls (AskUserQuestion / `request_user_input`) | UpstreamGrilling §1.15: upstream declares native question tools out of scope ("I've tried the AskUserQuestion UI and don't want it") | Both verified. On Codex the tool also does not exist in Default mode (C5). | Conflict. Take human evidence from the **user turn**, not from a question tool. |
| C10 | Runtime §0.2, SpecDriven §8.4: "automation makes the model skip; confirmed" | Methodology §0.2: agent-flow failed *without* any auto mode on. Its headline incidents (superpowers #2372, GSD #3018) are interactive sessions | GSD #3018 body: "`collaboration_mode=default` … `mode` was `interactive`". superpowers #2372: "an interactive Codex Desktop session … with no user opt-in". | See the §5 verdict. "Automation causes it" is **OVERSTATED**. |
| C11 | Runtime: task-master High (#1), Archon Medium | — | task-master `types.ts@c0c98d3` L4-14: autopilot is `PREFLIGHT→BRANCH_SETUP→SUBTASK_LOOP(RED/GREEN/COMMIT)→FINALIZE` for one task. The workflow module's last commit is 2025-12-07 and the repo's last push is 2026-04-28. Archon gives every run a worktree by default (`concepts.md@7009a90` L88-101). `archon-deliver.yaml` L1-13 covers implement → gate → simplify → draft PR → review → fix → CI → ready. Merge is human. | Runtime **mis-ranked**. Archon fits the lifecycle better than task-master. |

---

## 2. Re-ranked shortlist: closest analogs to agent-flow

Traits scored: **R** a runner in code owns phase order · **H** the host session executes the phase and reports back · **A** artifact/marker gates · **W** worktree isolation · **M** independent multi-model/multi-CLI review · **I** an interview phase · **Live** maintained.

| Rank | Project @SHA | R | H | A | W | M | I | Live / adoption | Why this rank |
|---|---|---|---|---|---|---|---|---|---|
| 1 | awslabs/aidlc-workflows @47feb76 (HEAD 1ae9448) | ✓ engine + Stop hook | ✓ conductor does `next`/`report` | ✓ `produces`/receipts | partial (mentioned in construction protocols; default not verified) | partial (reviewer subagents; cross-CLI not verified) | ✓ **runtime-checked** | 5.1k★, pushed 2026-10-09 | Same driver shape as `agent-flow continue`. It is the only live project that ties interview answers to human turns, and it supports both Claude Code and Codex. No PR/merge tail; its operation stages cover deployment. |
| 2 | **Yeachan-Heo/oh-my-codex @2db7d51** (missed) | hybrid: FSM phase list in code; the model writes `current_phase`; hooks validate | ✓ Codex host | ✓ metadata + artifacts | ✓ | same host model *(scout-read)* | per-question block (`omx question`); completion is model-written metadata *(scout-read)* | 33.5k★, active | Codex host, hook-checked deep-interview→ralplan→…→code-review chain, and the identical failure: #2558 "Just modifies code. No deep-interview no ralplan" (checked). |
| 3 | coleam00/Archon @7009a90 | ✓ DAG executor | ✗ Archon spawns the provider sessions | ✓ artifact pointers | ✓ by default | ✓ multi-provider lenses | ✗ by design (`ready:false`) | 23.6k★, active | Best analog for the back half (PR/CI/review) and for a real pause/resume primitive. It is not a host-session model. |
| 4 | catlog22/maestro-flow @f888cec | ✓ but the LLM composes the chain | ✓ | ✓ | some (merge-validator) | ✓ multi-backend (Claude/Codex/Gemini…) | prompt | 564★, active, **1 dominant maintainer (1,406 commits)**, tarballs and tmp files committed | Its shape is close. Low adoption and single-author risk: use it as a pattern source, not a benchmark. |
| 5 | **Gentleman-Programming/gentle-ai v3.7.0 @6dee8f8** (missed; HEAD @8ae9911) | ✓ Go `sdd-status` | ✓ | ✓ | partial | sub-agents | ✓ **runtime transcript check** (Claude Code only) | 7.6k★; **SDD retired in v4.0.0** | The only shipped design that reads authority from the harness transcript instead of a minted token (PR #4531, merged). Its retirement (and closed PR #4572, which "unconditionally denies every sdd-* dispatch") shows the operating cost. |
| 6 | gsd-build/gsd-2 @33c00aa → open-gsd/gsd-pi @7e413ee | ✓ dispatch rules | ✗ its own pi agent | ✓ | — | — | ✓ write-gate HARD BLOCK | gsd-2 archived; gsd-pi 1.3k★, active; `write-gate.ts` still present | Reference for "block all tools while a question is pending". Its auto-mode bypass (`auto-dispatch.ts` L142, L578, L728) is the cautionary part. |
| 7 | github/spec-kit `specify workflow` @9398980 | ✓ YAML engine | ✗ headless `-p` children | ✓ TTY gates | ✗ | ✗ | ✗ (built-in workflow has no clarify) | 140k★, engine commits daily | The engine is real but has no interview and no review/PR. |
| 8 | eyaltoledano/claude-task-master autopilot @c0c98d3 | ✓ but the scope is one task's TDD loop | ✓ | self-reported `testResults` | branch only | ✗ | ✗ | **dormant** (workflow module last touched 2025-12-07) | Moves down from #1. It is only an analog for red/green self-report. |
| — | Arch1eSUN/Arcgentic @8539955 | transition validator; the legality table lives in agent-editable `state.yaml` | ✓ | self-reported | ✗ | auditor role thread | ✗ in code (README advertises "clarify the idea") | 286★, **1 contributor**, 10 forks | **Flag: toy / low adoption.** Removed from the shortlist. |
| — | GSD v1 SDK PhaseRunner @bdcaab2 | ✓ | ✗ (Agent SDK sessions) | file existence | — | — | **impossible** (no question tool) | **archived, not carried into gsd-core** | **Abandoned.** Keep only as a cautionary example of auto-approve defaults. |

---

## 3. Missed projects (primary source read)

| Project @SHA | Interview enforcement | Relevance to domain-grill |
|---|---|---|
| Yeachan-Heo/oh-my-codex @2db7d5136b22252e83091a12ea27567b2fdd5971 | `omx question` blocks for up to 30 min. Handoff passes on model-written `status:"complete"` + rationale, or `skip_authorized_by_user:true`. `--answer-question-id` lets any shell caller answer *(scout-read)* | High. Same failure in #2558 (checked). Shows that per-question blocking does not help when completion is still self-declared. |
| Gentleman-Programming/gentle-ai v3.7.0 @6dee8f833aec9e46015759c5065a9035795d9af1 | PreToolUse(Agent) hook; authority comes only from runner-supplied transcript pairs; model-authored preflight text is denied (L137, checked) | High. #4524 (synthesized preflight values) and #3374 ("hand-authored an authorization binding naming the maintainer") are agent-flow's failure. |
| Yeachan-Heo/oh-my-claudecode @454bae017356ed1d056182cd929b6eb6cf6143ad | Prompt-only. The Stop hook advances on a completion string that the hook itself tells the model to print (`enforcement.ts` L687) *(scout-read)* | Medium. Same token-leak class as `--approve` in `next_command`. |
| garrytan/gstack @54efba6dd5a6dc7f04e62106b97079279ed53b41 | Prompt-only; spawned sessions auto-choose | Low runner, but full PR/merge + cross-model review. #2719: "never asks the six forcing questions … looks like it ran" (checked; cause was a prompt-rule collision in the Conductor harness, not automation). |
| gemini-cli-extensions/conductor @6e8f9a860bcdd6a2c423473c12e745200688c633 | Prompt-only; "Autogenerate Mode" | Medium. #160: an empty ask was followed by a continue. |
| cline/cline @fa840c741c3fc2eb49e7e0a4484895a99dae5cc5 | VS Code: the model has no switch-to-Act tool. CLI: `switch_to_act_mode` plus a synthetic "The user approved…" message *(scout-read)* | #13729 (checked): "used __Python to write the file directly__ … routed around the plan-mode guard". This is direct evidence against remedy (b). |
| Kilo-Org/kilocode @2dd87e7bb0695da44500bd04384a5d0da56dbd80 | `plan_exit` ends the model's turn; the *program* asks Start/Continue/Refine; headless denies `question` *(scout-read)* | Medium. Shows the program, not the model, owning the exit from plan mode. |
| automazeio/ccpm @7d7e4623bc6d4c0c9ba66ca6bfecd7e5261dc697 | Prompt-only | Low; dormant since 2026-03. #959 "too eager to proceed". |
| SuperClaude-Org/SuperClaude_Framework @fe68862c8ed9e2afb8120c2d9e27d0c3a7ce73a2 | Prompt-only, no wait wording | Low. |
| RooCodeInc/Roo-Code @b867ec9 (archived 2026-05-15), Aider-AI/aider @2ec3550, aaif-goose/goose @0f47680 | Roo: `switch_mode` needs approval by default. Aider: `--auto-accept-architect` defaults to True. goose: `GooseMode::Auto` by default (only partly checked) *(scout-read)* | Low. Auto-approve defaults are permissive, so do not build the gate on tool-approval settings. |
| openai/codex @22ebb0fb178fe369190123c256ba200585765725 (harness) | See C5 | Codex's Default mode itself tells the model to avoid asking. |

Not included because no primary source is available: Factory droids, Devin (closed source). Sweep and Plandex were not read.

---

## 4. Attacks on the remedies, and defenses

| Remedy | Attack (evidence) | Defense | Holds? |
|---|---|---|---|
| (a) UserPromptSubmit hook as evidence of a human turn | **Claude Code** also fires it on "a scheduled task firing, including a `/loop` iteration", "a background subagent reporting back", and "a message another session sends" (code.claude.com/docs/en/hooks, UserPromptSubmit). `CronCreate`/`ScheduleWakeup` are **model-callable** tools (docs/en/scheduled-tasks), so the model can schedule a prompt such as "approve" and mint a turn [INFERENCE, not executed]. **Codex** fires it for subagent briefs and for internal `/review`/Guardian threads under the root session id (AI-DLC `aidlc-codex-adapter.ts` L30-34, L953-972), and for queued mid-turn input (`hook_runtime.rs` `inspect_pending_input`). "UserPromptSubmit carries no signal about who submitted" (AI-DLC `aidlc-record-human-turn.ts` L35-42; measured "10 runner-submitted prompts, zero humans" → true). Codex skips untrusted project hooks until they are reviewed; Claude desktop/VS Code did not fire the hook (AI-DLC #1548); hooks disabled for 31 h (#2203). | Filter by origin. Claude: the transcript row `promptSource`/`turnOrigin`/`origin.kind` (AI-DLC `claudeTranscriptOrigin`, `aidlc-lib.ts` L2053-2079; only `task_notification` is a known host origin; **cron origin is unverified**). Codex: drop prompts that carry `agent_id` and prompts whose `transcript_path` names another thread. Withhold with an `UNATTENDED` env var. Fail closed when no hook heartbeat exists. | **Partly.** It shows order and presence against an eager but compliant model. It does not authenticate anyone. A presence signal does not establish consent (Archon #2565 "objects → approved"; GSD-2 #3749 "garbage unlocks"; AI-DLC #2209 credited an earlier reply). |
| (b) Move `--approve` behind a PreToolUse deny | Claude permissions docs: a Bash rule "isn't a security boundary around the program"; `sh -c`, absolute paths and interpreters bypass it. Codex hooks docs: "Treat tool hooks as a useful guardrail, not a complete enforcement boundary"; `write_stdin` into an existing exec session "doesn't run `PreToolUse` again". AI-DLC needed a 1.4k-line lexer and still says "not a sandbox" (`runtime-integrity.ts` L1). Cline #13729 shows a model routing around a mode guard with Python. | Remove the cause instead of filtering the symptom: **stop printing the token** in `next_command`, and make `continue` accept a pause only when there is human-turn evidence after the pause opened, not a CLI argument. Keep a PreToolUse deny on writes to the ledger and state as defense in depth. | **No** on its own. **Yes** as a secondary layer once the token is no longer printed. |
| (c) Force the interview | Upstream's only skip path is the human's own words: "Tell it to wrap up" (`docs/productivity/grilling.md@b0618bc` L67), "don't grill me", triage's "move to ready-for-agent". Question caps are out of scope. Completion = "when you say the understanding is shared" (L61). | Require **≥1 human turn after the first question round and after the final confirm prompt**, not N answers. A human-typed "skip"/"wrap up" satisfies the requirement and is recorded verbatim. The artifact is downgraded to `shared_understanding: skipped_by_user` with assumptions (the CE/BMAD pattern). `--no-grill` is allowed only as a flag the human passes to `agent-flow start`. | **Yes.** A turn-based rule matches upstream; a question-count rule would conflict with it. |

### Minimal design that survives these attacks
1. Delete `--approve <token>` from every `next_command` shown to the host (runner.py:2723-2726).
2. domain-grill gets runner-owned sub-states: `questions_open(round n, opened_at)` → `awaiting_confirmation(opened_at)`. While a sub-state is open, `continue` refuses, and the printed next step is "end your turn".
3. A UserPromptSubmit hook on Claude Code and Codex appends `{ts, session, origin, prompt_sha256, verbatim}` to a runner ledger. Origin filters: Claude transcript `promptSource`/`turnOrigin`/`origin.kind`; Codex `agent_id` and foreign `transcript_path`. `AGENT_FLOW_UNATTENDED=1` withholds entries.
4. `continue` past domain-grill and every `pause_after` requires a ledger human turn after `opened_at`. For domain-grill, the artifact's Answers section must quote that turn verbatim (the hash must match). Approve/reject is parsed from the human's words; anything ambiguous means re-ask (Archon #2565).
5. No hook heartbeat (hook not installed, or untrusted in Codex) → fail closed, and print a message telling the human to install/trust the hook or restart with `--no-grill`.
6. At PR time, compare the ledger with the harness transcript and list mismatches in the PR body, so that forgery that is not prevented is at least detected.

**Residual risk**
- The model runs as the same OS user, so it can still write the ledger or the transcript through an interpreter. Only a sandbox that denies writes to those paths closes this.
- The origin of cron/`ScheduleWakeup` prompts in the Claude transcript is unverified.
- The Codex transcript "isn't a stable interface for hooks".
- Codex Default mode will keep pushing the model toward assumptions, so expect more round-trips.
- Deadlocks are likely, as seen in AI-DLC #1540/#1551/#1026/#708 and gentle-ai #4572.

---

## 5. Verdict on the hypothesis "automated workflows let the model skip the interview"

**Count (projects that have an interview step, across all four reports plus the missed ones; Kiro excluded as UNVERIFIED):**
- **Enforced at runtime (human evidence required, live): 2.** AI-DLC; GSD-2/gsd-pi (except its non-deep auto mode).
- **Enforced at runtime, then retired: 1.** gentle-ai v3.7.0.
- **Partial (a harness or program blocks one step, but completion is self-declared): 4.** oh-my-codex, agent-os v3 (plan mode), Kilo (program-owned plan exit), Cline VS Code.
- **Prompt-only: 20.** mattpocock, spec-kit slash, OpenSpec, cc-sdd, superpowers, BMAD, feature-dev, compound-engineering, GSD v1, oh-my-openagent, maestro-flow, flow-next, Arcgentic, oh-my-claudecode, gstack, Conductor, ccpm, SuperClaude, Roo (archived), gsd-core.
- **No interview at all: 10+.** Archon, spec-kit engine, spec-workflow-mcp, claude-code-spec-workflow, task-master, ruflo SPARC, GSD SDK, Aider, agent-orchestrator, OpenHands.

**Evidence for the hypothesis**
- About 11 projects ship auto modes that formally replace the human's answers: GSD `--auto`/yolo/SDK, GSD-2 auto, BMAD `#yolo`/headless/`build-auto`, CE `lfg` pipeline, gstack spawned/autoplan, OMC AFK protocol, maestro `-y`, flow-next `--auto`, cc-sdd `--auto`/`-y`, Kilo headless.
- Engine-driven workflows drop the interview entirely: spec-kit engine, Archon, GSD SDK.
- Observed incidents in automated or embedded contexts: OMX #2558, oh-my-openagent #5548, mattpocock #540/#625/#363, GSD #1995, gentle-ai #4524, AI-DLC's "10 prompts, 0 humans" measurement, and agent-flow's own run.

**Evidence against (the cherry-picked part)**
- More of the cited incidents happened in **interactive, non-automated** sessions:
  - GSD #3018 (Codex Default, `mode: interactive`), GSD #3808
  - superpowers #2372 (interactive Codex Desktop), superpowers #1441
  - spec-kit #1147 (Codex), spec-kit #4582
  - mattpocock #134/#1068/#1098 (human-invoked grill-with-docs)
  - gstack #2719 (prompt-rule collision), Conductor #30/#160, ccpm #959, SuperClaude #282/#467
  - Kilo #9278, Cline #11931/#12907, OpenSpec #869/#1715, CE #571, spec-workflow-mcp #100
- Upstream docs: "Weaker and faster models still skip the gate" in ordinary interactive use (grilling.md L61).
- **Harness confound:** Codex Default mode instructs the model to prefer assumptions over questions and does not offer `request_user_input` (C5). Codex is the harness in agent-flow's case, in GSD #3018/#3808, superpowers #2372, spec-kit #1147 and mattpocock #625.
- **Counterexamples where automation kept the interview:**
  - AI-DLC: engine-driven and enforced.
  - GSD-2: deep mode.
  - gsd-core `/gsd-autonomous --interactive`: "MUST run discuss-phase inline … (not auto-answered)", `docs/features/autonomous-interactive-flag.md@ed0f3fd`.
  - Kiro Quick Spec: questions front-loaded.
  - maestro: the boundary question is "never skipped (even with `-y`)".
  - Archon and `bmad-build-auto`: stop with `blocked` instead of guessing.

**Verdict: partly supported. As a causal claim it is cherry-picked.**

The pattern the evidence supports is narrower:
- Interviews get skipped whenever **completion is self-declared and nothing outside the model observes a human turn**. That is true of 24 of the 26 interview-bearing projects, i.e. everything except the 2 live runtime gates.
- Automation makes skipping more likely in three ways:
  - it removes the person who would have noticed;
  - it adds pressure to keep going (#5548, docs L64);
  - in agent-flow, it hands the model the advance token.
- Codex Default mode makes skipping more likely independently of automation.

The redesign should therefore target self-declared completion and the leaked approval token, not "automation" as such.
