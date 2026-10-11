# Runtime 주도 오케스트레이터 비교: interview(요구사항 확인)와 승인 단계를 누가 강제하는가

작성일 2026-10-09 · 담당 `RuntimeOrchestrators`

**조사 방법**
- Archon, oh-my-openagent, ruflo, claude-task-master, humanlayer, langgraph, OpenHands SDK는 직접 `git clone --depth 1` 해서 읽었다. 읽은 SHA는 각 섹션 표에 적었다.
- GSD 계열, spec-workflow-mcp, Arcgentic, flow-next, maestro-flow, agent-orchestrator는 하위 scout가 GitHub API로 SHA를 고정해 읽었다(셸 없이). Python 파일의 줄 번호는 몇 줄 어긋날 수 있다(≈ 표시). 인용문은 원문 그대로다.
- 이슈는 `gh issue list --search`로 찾았다.

**용어**
- **enforced**: runtime 코드가 강제한다.
- **prompt-only**: 모델에게 지시만 한다.
- **self-reported**: 모델이 직접 쓴 값을 runtime이 그대로 믿는다.

---

## 0. 요약

**1. 거의 모든 프로젝트에서 interview는 prompt-only다.**
- 질문 단계를 runtime이 막아 두는 곳은 GSD-2(gsd-pi)의 depth_verification write-gate 하나뿐이다.
- 그 GSD-2도 non-deep auto 모드에서는 runtime이 스스로 gate를 통과 처리한다(§7).
- 나머지는 interview가 아예 없거나(Archon SDLC pack, task-master, ruflo SPARC, spec-workflow-mcp), 있어도 prompt 지시다(oh-my-openagent Prometheus, GSD v1, flow-next, maestro-flow).

**2. 자동화 모드는 설계상 interview를 건너뛴다.** maintainer의 의심은 1차 자료로 확인된다.

| 프로젝트 | 건너뛰는 방식 | 근거 |
|---|---|---|
| Archon `archon-plan` | 질문 대신 `ready:false`로 끝낸다 | "No one is watching this run" |
| Prometheus | UNCLEAR로 분류하면 질문하지 않는다 | "do NOT ask the user extra questions" |
| GSD v1 | `--auto`/YOLO 모드 | "Claude picks recommended option for every question; no AskUserQuestion" |
| GSD SDK runner | 모델이 스스로 결정 | "running headless with no human present… Make all decisions autonomously" |
| GSD-2 | auto 모드에서 gate 자동 통과 | `markDepthVerified` |
| maestro-flow | `-y` | "code exploration replaces human answers" |

**3. 계속 진행시키는 장치가 interview를 망가뜨린 사례가 문서로 남아 있다.**
- oh-my-openagent #5548: todo-continuation이 사람의 답을 기다리던 모델을 "Proceed without asking for permission"으로 다시 밀었다. 결과는 "the model … convince[s] itself to make decisions on behalf of the user"였다.
- runtime은 `question` 도구 호출만 "대기 중"으로 인식하고, 산문으로 쓴 질문은 인식하지 못했다.

**4. 사람을 기다리는 동안 runtime이 실제로 멈추는 곳**
- Archon: DB `status='paused'` + approve/reject/respond 채널.
- LangGraph: `interrupt()` → `Command(resume=...)`.
- HumanLayer: 외부 승인 API polling, hld MCP `request_approval` 블로킹.
- OpenHands: `WAITING_FOR_CONFIRMATION`.
- GSD-2: tool hook이 다른 모든 도구를 HARD BLOCK.
- spec-workflow-mcp: 대시보드 승인. 단 모델이 polling해야 한다.

**5. 그래도 "사람이 승인했다"는 사실을 인증하는 곳은 거의 없다.**

| 프로젝트 | 모델이 직접 승인할 수 있는 경로 | 근거 |
|---|---|---|
| Archon | 공식 기능인 "AI approvers"로 승인 가능. CLI 승인 시 actor가 남지 않는다 | "No actor is recorded" |
| spec-workflow-mcp | 승인 기록이 서명 없는 JSON 파일이고, 대시보드 API에 인증이 없다 | [INFERENCE] |
| maestro-flow | `run decide`의 `--actor`가 임의 값이다 | |
| Arcgentic | `--skip-gates` | |
| task-master | `testResults`가 self-reported다 | |

- agent-flow의 `--approve <hash>` 자가 통과는 이 계열에서 흔한 결함이다.

**6. agent-flow와 가장 가까운 프로젝트:** ① claude-task-master autopilot, ② Arcgentic, ③ maestro-flow (§11).
- fix 설계의 참고 모델로는 Archon(실제 pause/resume, gate 해석을 agent에 맡김)과 GSD-2(질문 대기 중 tool HARD BLOCK)가 가장 유용하다.

---

## 1. coleam00/Archon

| 항목 | 값 |
|---|---|
| repo | https://github.com/coleam00/Archon (default branch `dev`) |
| SHA | `7009a90a089adb16a545141dc162b9aaa988b4dd` (2026-10-08) |
| license / maturity | MIT · ★23,646 · 2026-10-08 push |
| 구조 | TypeScript/Bun 모노레포. 예전 Python 버전(task mgmt + RAG)은 `archive/v1-task-management-rag` 브랜치에 있다. 현재는 YAML DAG workflow 엔진이다 |

### Driver: runtime
- **DAG executor가 노드 순서를 정한다.** 각 AI 노드는 Archon이 띄운 별도 provider 세션(Claude SDK, Codex app-server 등)에서 실행된다.
  - "Nodes in the same dependency layer run in parallel" ([concepts.md L30](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/docs-web/src/content/docs/getting-started/concepts.md#L30))
  - 노드 타입: `command/prompt/bash/loop/approval/cancel` ([L38-L45](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/docs-web/src/content/docs/getting-started/concepts.md#L38-L45))
- **host 세션(Claude Code + `archon-cli` skill)은 노드를 직접 실행하지 않는다.**
  - host는 `archon workflow run/approve/...` CLI를 부르는 조작자다.
  - agent-flow처럼 "host가 phase를 수행하고 continue를 부르는" 구조가 아니다.

### Phase artifact와 gate
- **artifact pointer를 machine-check한다.** 가리키는 파일이 없으면 노드를 거부한다.
  - "refers to a file that does not exist under this run's artifacts directory" ([artifact-pointer.ts L142](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/workflows/src/artifact-pointer.ts#L142))
- **결과 필드는 `output_format`(JSON schema)으로 받는다.**
  - 예: SDLC plan 노드의 `ready/summary/report` ([plan.md L56-L71](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/.archon/workflows/sdlc/plan/commands/plan.md#L56-L71))
  - 결과 *내용*(`ready:true`가 맞는지)은 모델이 self-report한다.

### Interview / clarification
**기본 SDLC pack에는 interview 단계가 없다.**
- 기본 배포 pack은 `sdlc` 하나다 ([bundle-index.json](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/workflows/src/defaults/bundle-index.json)).
- plan 노드는 "질문 없이 실행 가능한 계획"을 쓰도록 지시받는다.
  - "Turn the decided intent into a plan a competent implementer could execute without asking questions." ([plan.md L3](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/.archon/workflows/sdlc/plan/commands/plan.md#L3))
  - "No one is watching this run: the plan file and your declared fields are all that survive it." ([L46](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/.archon/workflows/sdlc/plan/commands/plan.md#L46))
- 의도가 모자라면 질문하지 않고 `ready:false`로 정상 종료한다.
  - "False when missing intent blocked it" ([L58](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/.archon/workflows/sdlc/plan/commands/plan.md#L58))
  - "Blocked is a valid outcome, not a failure … beats a plan built on guesses" ([archon-plan.yaml L27-L36](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/.archon/workflows/sdlc/plan/archon-plan.yaml#L27-L36))
  - 즉 "모르면 멈추고 명시한다"가 기본 정책이다. 질문 대신 *정직한 차단*을 택한다.

**interview를 만들 수 있는 runtime 원시 기능은 있다: `interactive: true` loop.**
- 매 iteration 뒤 executor가 run을 pause하고, 사용자 피드백을 `$LOOP_USER_INPUT`으로 다음 iteration에 주입한다.
  - "Set `interactive: true` to pause the loop between iterations and wait for human input." ([loop-nodes.md L500-L517](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/docs-web/src/content/docs/guides/loop-nodes.md#L500-L517))
- 첫 iteration이 완료 신호를 내도 기본적으로 먼저 gate에 선다.
  - "By default an interactive loop **always gates first** … the human confirms before the node completes." ([L547-L549](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/docs-web/src/content/docs/guides/loop-nodes.md#L547-L549))
  - `signal_completes: true`를 주면 gate 없이 끝낼 수 있다. 이것이 명시적 skip 경로다.
- README 예시: `interactive: true  # Pauses and waits for human input` ([README L64-L69](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/README.md#L64-L69)).

**대기 방식: runtime interrupt/resume (enforced)**
- pause 시 run 상태를 DB에 `paused`로 쓰고, approval context를 metadata에 저장한다.
  - [dag-executor.ts L6738-L6755](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/workflows/src/dag-executor.ts#L6738-L6755)
  - [L6830-L6843](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/workflows/src/dag-executor.ts#L6830-L6843)
  - SQL `SET status = 'paused' … WHERE id = $1 AND status = 'running'` ([db/workflows.ts L1660-L1671](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/core/src/db/workflows.ts#L1660-L1671))
- 노드를 실행하던 agent 세션은 이 시점에 이미 끝나 있다. 실행 중인 모델이 자기 gate를 넘길 방법은 없다.
  - [INFERENCE] 노드 agent에게는 `manage_run` 도구가 주어지지 않는다. 이 도구는 chat orchestrator에만 붙는다(아래).

### Approval node
- `approval:` 노드는 사람이 결정할 때까지 run을 pause한다.
  - Pause → Notify → Wait → Approve/Reject 절차 ([approval-nodes.md L44-L67](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/docs-web/src/content/docs/guides/approval-nodes.md#L44-L67))
  - schema: "Gate node schema … pauses the workflow for human review" ([dag-node.ts L652-L682](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/workflows/src/schemas/dag-node.ts#L652-L682))
- 승인 경로: `/workflow approve <id>` 슬래시 명령, CLI `archon workflow approve`, Web 버튼, REST `POST /api/workflows/runs/<id>/approve`, Slack 버튼, 그리고 chat agent의 `manage_run` 도구.

### 모델이 스스로 승인할 수 있는가: 부분적으로 그렇다
- **chat agent:** 사용자 메시지로 시작된 턴 안에서 `manage_run approve`를 호출한다. `confirm:true`가 필요하다.
  - 코드 주석이 이 확인 절차가 사람 확인이 아니라고 스스로 밝힌다: "Without confirm the tool returns a preview and asks the agent to check with the user first: a model-visible two-step that creates an audit point … since there is no mid-turn UI-confirm primitive to block on." ([manage-run-tool.ts L69-L80](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/core/src/orchestrator/manage-run-tool.ts#L69-L80))
  - 도구는 사용자 메시지로 시작된 orchestrator 턴에만 주어지고, 한 턴에 gate 하나만 풀 수 있다 ([orchestrator-agent.ts L2574-L2602](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/core/src/orchestrator/orchestrator-agent.ts#L2574-L2602)).
- **native tool이 없는 provider(Codex/OpenCode/Copilot):** agent가 `archon workflow approve|reject <run-id>` CLI를 직접 실행한다.
  - "Providers without native tool support (Codex, OpenCode, Copilot) resolve the gate by running `archon workflow approve|reject <run-id>` for you instead." ([approval-nodes.md L180-L193](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/docs-web/src/content/docs/guides/approval-nodes.md#L180-L193))
- **AI approver가 공식 기능이다.**
  - "**AI approvers / relay steering.** An orchestrating agent can steer another run's gate … then finalize with `archon workflow approve <id> --json`" ([loop-nodes.md L569-L575](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/packages/docs-web/src/content/docs/guides/loop-nodes.md#L569-L575))
- **결정을 누가 내렸는지 인증하지 않는다.**
  - "No actor is recorded. Written by the process that approved or rejected, which is often not the one running the workflow" ([troubleshooting.md L21](https://github.com/coleam00/Archon/blob/7009a90a089adb16a545141dc162b9aaa988b4dd/.claude/skills/archon-cli/manage-run/troubleshooting.md#L21))
- **결론**
  - 실행 중인 노드 모델은 자기 gate를 못 푼다.
  - 그러나 host/chat 모델은 CLI 한 줄로 gate를 풀 수 있다.
  - agent-flow의 `--approve <hash>`와 같은 결함이 있다. 다만 "사용자 메시지가 있는 턴에서만 도구 제공"이라는 부분 완화가 있다.

### 관련 이슈
- **#2565** "fix(orchestrator): a plain message at an approval gate always approves, even when it objects" (closed 2026-08-17)
  - "Typing *"no, stop — why is it editing the schema?"* at a gate: 1. resolves the gate as `approved` … 4. continues doing the thing the user objected to."
  - 해결책: gate를 agent 컨텍스트로 넘기고, agent가 `manage_run`으로 명시적으로 approve/reject하게 했다. 애매하면 agent가 사용자에게 되묻는다. 키워드 매칭은 명시적으로 기각했다.
- **#2707** "design(workflows): one pause primitive — structured gate output, interactive/unattended classes, three input channels retired" (open)
  - pause 원시 기능 통합, workflow 단위 `interactive`/unattended 분류, background launch 거부 등.
- **#3953** "fix(web): a gate with declared decisions offers Continue, and the engine accepts its undeclared "approved"" (open)

### agent-flow와의 유사도: **Medium**
- 같은 점: runner(YAML DAG)가 순서를 소유하고 artifact gate가 있다.
- 다른 점: phase를 host 세션이 아니라 Archon이 띄운 별도 세션이 수행한다. 사람 gate는 DB pause를 쓰는 실제 interrupt다.

---

## 2. code-yeongyu/oh-my-openagent (구 oh-my-opencode)

| 항목 | 값 |
|---|---|
| repo | https://github.com/code-yeongyu/oh-my-openagent (`oh-my-opencode`에서 redirect) |
| SHA | `a47fe9860f266b89354ed072e4a9e3b72e63604e` (2026-10-09) |
| license / maturity | NOASSERTION (custom LICENSE.md) · ★69,896 · 2026-10-08 push |

### Driver: LLM (prompt) + OpenCode plugin hook
- **Sisyphus/Atlas가 메인 orchestrator 에이전트다.**
- **Prometheus는 planner 에이전트다.**
  - 프롬프트는 "planning consultant"이고, 첫 행동으로 `ulw-plan` skill을 로드하라고 지시한다 ([prometheus/default.md L1-L5](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/prompts-core/prompts/prometheus/default.md#L1-L5)).
- **phase 순서를 정하는 runner는 없다.**
  - 실행 전환은 사용자가 별도 세션에서 `/ulw-execute`를 실행할 때 일어난다. 이 hook이 plan을 고르고 boulder 상태를 초기화한다 ([ulw-execute/AGENTS.md](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/omo-opencode/src/hooks/ulw-execute/AGENTS.md)).

### Gate (enforced vs prompt)
**enforced: Prometheus의 쓰기 범위**
- `tool.execute.before` hook이 `.omo/*.md` 밖으로의 Write/Edit를 throw로 막는다.
  - "Prometheus is a planning agent. File operations restricted to .omo/*.md plan files only. … implementation starts only when the user runs /ulw-execute." ([prometheus-md-only/hook.ts L40-L62](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/omo-opencode/src/hooks/prometheus-md-only/hook.ts#L40-L62))

**enforced (반대 방향): 계속 굴리기**
- todo-continuation-enforcer는 `session.idle` 때 미완료 todo가 있으면 continuation 프롬프트를 주입한다.
  - "forces Sisyphus to keep rolling when incomplete todos remain" ([AGENTS.md L7-L22](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/omo-opencode/src/hooks/todo-continuation-enforcer/AGENTS.md#L7-L22))
- Prometheus는 제외 대상이다: `DEFAULT_SKIP_AGENTS = ["prometheus", "compaction", "plan"]` ([constants.ts L5](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/omo-opencode/src/hooks/todo-continuation-enforcer/constants.ts#L5)).
- 미답 질문 감지는 *도구 호출*만 본다: `QUESTION_TOOL_NAMES = new Set(["question", "ask_user_question", "askuserquestion"])` ([pending-question-detection.ts L21-L60](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/omo-opencode/src/hooks/todo-continuation-enforcer/pending-question-detection.ts#L21-L60))
- 미답 질문이 있으면 continuation을 건너뛴다 ([idle-event.ts L103-L106](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/omo-opencode/src/hooks/todo-continuation-enforcer/idle-event.ts#L103-L106)).

### Interview / clarification: prompt-only, 의도적 skip 경로 있음
- 파일: [`packages/shared-skills/skills/ulw-plan/SKILL.md`](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/shared-skills/skills/ulw-plan/SKILL.md)
- **질문 여부는 모델이 판정하는 intent routing으로 정한다** ([L44-L47](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/shared-skills/skills/ulw-plan/SKILL.md#L44-L47)):

  | 분류 | 지시 |
  |---|---|
  | CLEAR | "ask the surviving forks with WHY" |
  | UNCLEAR | "Asking would offload your own job onto the user. … adopt and ANNOUNCE best-practice defaults, do NOT ask the user extra questions" |
  | 사용자가 명시 요청("interview me") | CLEAR로 강제하고 모든 fork를 질문 |

  - 즉 모호할수록 질문하지 *않는* 설계다. 모델 스스로 UNCLEAR로 분류하면 interview가 사라진다.
  - 완화책은 prompt-only다: "ON THE FENCE … treat it as CLEAR and ask exactly ONE question. A user wrongly silenced is worse than one extra question."
- **승인 gate도 prompt-only이고, 대기 방식은 턴 종료다.**
  - "present a short brief once, then **wait for the user's explicit okay**" ([L81-L83](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/shared-skills/skills/ulw-plan/SKILL.md#L81-L83))
  - "Brief presented and `status: awaiting-approval` recorded: wait." ([L98](https://github.com/code-yeongyu/oh-my-openagent/blob/a47fe9860f266b89354ed072e4a9e3b72e63604e/packages/shared-skills/skills/ulw-plan/SKILL.md#L98))
  - `status: awaiting-approval`는 모델이 draft 파일에 쓰는 self-reported 마커다. runtime은 이 마커를 검증하지 않는다.
- **자가 인증:** 가능하다. 다만 Prometheus는 `.omo` 밖에 쓸 수 없으므로 피해는 "plan을 미리 쓰는 것"에 그친다.
  - 실행은 사용자가 입력하는 `/ulw-execute`로만 시작된다. 이 부분이 사실상의 사람 gate다.
  - [INFERENCE] 모델이 슬래시 명령을 스스로 입력할 수는 없다.

### 관련 이슈
- **#5548** (open) "[Bug]: ULW mode: forced continuation conflicts with user-input pauses, causing model to decide autonomously"
  - 본문: "the model may have stopped intentionally to ask the user for input … This behavior ultimately leads the model to convince itself to make decisions on behalf of the user."
  - bot 확인 코멘트: "checks for question TOOLS only. If the model stops to ask the user a question in natural language … forces continuation with 'Proceed without asking for permission'."
- **#5850** (open) "ulw planner sometimes uses native OpenCode plan mode and leaves the parent waiting"
  - "If I choose "Yes", the planning subagent starts implementing the plan itself."
- **#1052** (closed) "Sisyphus is insane … System Instructions and Hooks Need Refinement"
  - "Sisyphus … ignores the very clear and strongly worded AGENTS.md instructions". prompt-only 규칙이 무시된 사례다.

### agent-flow와의 유사도: **Low–Medium**
- phase runner가 없다. 강제는 tool hook(쓰기 범위)과 continuation hook뿐이다.
- 다만 #5548은 agent-flow에 바로 적용되는 교훈이다. **runtime이 "대기 중"을 인식하려면 대기가 산문이 아니라 관찰 가능한 이벤트(도구 호출)여야 한다.**

---

## 3. ruvnet/ruflo (구 claude-flow)

| 항목 | 값 |
|---|---|
| repo | https://github.com/ruvnet/ruflo (`claude-flow`에서 redirect) |
| SHA | `5f709e36799274be6bb68fff69090bdbc8621220` (2026-10-08) |
| license / maturity | MIT · ★74,141 · 2026-10-08 push |

### Driver
**SPARC: LLM이 순서를 정한다 (prompt)**
- `sparc-orchestrator` 에이전트 프롬프트가 5개 phase와 gate를 서술한다.
- gate 판정 자체를 LLM이 한다.
  - "Evaluate gate criteria — every criterion must pass … On success: advance phase counter, notify user, begin next phase" ([sparc-orchestrator.md L67-L75](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/plugins/ruflo-sparc/agents/sparc-orchestrator.md#L67-L75))

**`workflow_run` MCP 엔진: runtime이 step 순서를 정한다**
- step 타입은 `task`(agent 실행), `wait`(최대 60초 타이머), `condition`이다. parallel/loop는 "not yet implemented"로 skip된다 ([workflow-tools.ts L402-L457](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/v3/@claude-flow/cli/src/mcp-tools/workflow-tools.ts#L402-L457)).
- **사람 입력용 step 타입은 없다.**
- `workflow_pause`/`workflow_resume`는 일반 MCP 도구라서 모델이 직접 부를 수 있다 ([L618-L656](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/v3/@claude-flow/cli/src/mcp-tools/workflow-tools.ts#L618-L656)).

### Gate
- **"Stakeholder sign-off recorded"는 self-reported다.**
  - "Spec must include at least 3 acceptance criteria … Stakeholder sign-off recorded." ([sparc-orchestrator.md L19](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/plugins/ruflo-sparc/agents/sparc-orchestrator.md#L19))
  - 결과는 모델이 `memory_store`로 `sparc-gates` namespace에 기록한다.
- **코드 hook은 secret 차단만 한다.** `ruflo-sparc/hooks/guard.ts`는 sparc namespace에 secret이 써지는 것만 막는다 ([guard.ts L23-L28](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/plugins/ruflo-sparc/hooks/guard.ts#L23-L28)).
- **gate를 건너뛰는 명령이 있다.** `sparc phase <name>`은 경고만 남긴다.
  - "Jumping forward skips gate checks." ([ruflo-sparc.md L50-L56](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/plugins/ruflo-sparc/commands/ruflo-sparc.md#L50-L56))

### Interview: 없다
- spec phase는 사용자에게 묻지 않고 "feature description and the codebase"에서 요구사항을 추출한다 ([sparc-spec/SKILL.md L24](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/plugins/ruflo-sparc/skills/sparc-spec/SKILL.md#L24)).
- 마지막에 결과를 보여 주고 `/sparc advance`를 *제안*할 뿐이다 ([L60](https://github.com/ruvnet/ruflo/blob/5f709e36799274be6bb68fff69090bdbc8621220/plugins/ruflo-sparc/skills/sparc-spec/SKILL.md#L60)).

### 관련 이슈
- **#3518** (open) "feat: durable missions with typed waits and explicit side-effect reconciliation"
  - "A workflow can persist a status, but long-running work still needs a durable execution boundary: wait for a typed human/external signal". 사람 대기 원시 기능이 아직 없다는 자인이다.
- **#3815** (closed) "ruflo-console: … Claude can spend, write and reach the network unasked … and a deferred ask can replace the person's pending confirm"
  - console 승인 UI에서 Claude 요청이 사람 confirm을 *대체*할 수 있었던 버그다.

### agent-flow와의 유사도: **Low**
- SPARC는 prompt-only다. workflow 엔진은 사람 대기 기능이 없는 agent-step 러너다.

---

## 4. eyaltoledano/claude-task-master

| 항목 | 값 |
|---|---|
| repo | https://github.com/eyaltoledano/claude-task-master |
| SHA | `c0c98d367c55296bfe69e65680625b6db437af02` (2026-04-23) |
| license / maturity | NOASSERTION (MIT + Commons Clause) · ★28,190 · 마지막 push 2026-04-28. 반년간 활동이 정체돼 있다 |

### Driver: 부분별로 다르다
**PRD → tasks: host LLM이 MCP 도구를 부른다**
- `parse_prd`, `expand_task`, `next_task`, `set_task_status` 등이다.
- 순서는 LLM과 사용자가 정한다.

**`autopilot` TDD: runtime state machine이 정한다**
- `WorkflowOrchestrator`가 `RED → GREEN → COMMIT`을 subtask마다 순환시킨다.
- host LLM은 `autopilot_next`로 다음 행동을 받고, 수행한 뒤 `autopilot_complete_phase`를 부른다.
  - "Get the next action to perform in the TDD workflow." ([next.tool.ts L25-L26](https://github.com/eyaltoledano/claude-task-master/blob/c0c98d367c55296bfe69e65680625b6db437af02/apps/mcp/src/tools/autopilot/next.tool.ts#L25-L26))
- 상태 파일: `~/.taskmaster/{project-id}/sessions/workflow-state.json` ([workflow-state-manager.ts L2-L4](https://github.com/eyaltoledano/claude-task-master/blob/c0c98d367c55296bfe69e65680625b6db437af02/packages/tm-core/src/modules/workflow/managers/workflow-state-manager.ts#L2-L4))

### Gate: transition 규칙은 machine-check, 입력값은 self-reported
- `autopilot_complete_phase`는 모델이 숫자로 보고한 `testResults{total,passed,failed,skipped}`를 받는다 ([complete.tool.ts L11-L21](https://github.com/eyaltoledano/claude-task-master/blob/c0c98d367c55296bfe69e65680625b6db437af02/apps/mcp/src/tools/autopilot/complete.tool.ts#L11-L21)).
- orchestrator는 "GREEN phase must have zero failures" 같은 규칙만 검사한다 ([workflow-orchestrator.ts L215-L230](https://github.com/eyaltoledano/claude-task-master/blob/c0c98d367c55296bfe69e65680625b6db437af02/packages/tm-core/src/modules/workflow/orchestrators/workflow-orchestrator.ts#L215-L230)).
- **테스트를 직접 실행하지 않는다.**
- **self-report skip 경로가 있다.** RED 단계에서 `failed === 0`을 보고하면 "feature already implemented"로 간주해 subtask를 완료 처리하고 다음으로 넘어간다 ([L171-L207](https://github.com/eyaltoledano/claude-task-master/blob/c0c98d367c55296bfe69e65680625b6db437af02/packages/tm-core/src/modules/workflow/orchestrators/workflow-orchestrator.ts#L171-L207)).
  - 모델이 숫자 하나로 구현 전체를 건너뛸 수 있다.
  - agent-flow의 marker gate와 같은 구조적 약점이다.

### Interview: 없다. PRD는 사람이 쓴다
- 문서는 "Place your PRD document in the `.taskmaster/docs/` directory"라고 안내하고 템플릿만 준다 ([docs/tutorial.md L63](https://github.com/eyaltoledano/claude-task-master/blob/c0c98d367c55296bfe69e65680625b6db437af02/docs/tutorial.md#L63)).
- 플러그인 에이전트 프롬프트에 "Request clarification from user before proceeding"이라는 prompt-only 지시가 있다 ([task-orchestrator.md L108](https://github.com/eyaltoledano/claude-task-master/blob/c0c98d367c55296bfe69e65680625b6db437af02/packages/claude-code-plugin/agents/task-orchestrator.md#L108)).

### 관련 이슈
- **#456** "feat: PRD q-and-a" (closed): "a 'wizard' that interviews the user about what they want". interview 기능이 없어서 요청된 것이다.
- **#347** "bug: Tasks marked as done but not implemented" (closed): "Cursor agent … marked all the sub-tasks as done even if they were not implemented". self-reported 상태 전환의 실패 사례다.

### agent-flow와의 유사도: **High (autopilot 한정)**
- runtime state machine이 phase를 소유한다. host LLM이 phase를 수행한 뒤 runtime에 `complete`를 보고하고, gate는 self-reported 값이다.
- 이 루프는 agent-flow의 red/green + `continue` 구조와 거의 같다. 다만 interview나 PRD 단계는 runtime 밖에 있다.

---

## 5. LangGraph `interrupt()` / `Command(resume=...)` (reference primitive)

| 항목 | 값 |
|---|---|
| repo | https://github.com/langchain-ai/langgraph |
| SHA | `bfcfea554ed5c7f7be562cebf8825e911b493ab1` |
| license / maturity | MIT · ★42,915 |

**동작**
- node 안에서 처음 `interrupt(value)`를 호출하면 `GraphInterrupt` 예외가 던져지고 그래프 실행이 멈춘다.
- 상태는 checkpointer가 저장한다.
- 재개하려면 *그래프를 호출하는 클라이언트*가 `Command(resume=...)`로 값을 넘겨야 한다.
  - "the first invocation of this function raises a `GraphInterrupt` exception, halting execution … A client resuming the graph must use the `Command` primitive to specify a value for the interrupt" ([types.py L903-L925](https://github.com/langchain-ai/langgraph/blob/bfcfea554ed5c7f7be562cebf8825e911b493ab1/libs/langgraph/langgraph/types.py#L903-L925))
- resume 값이 없으면 무조건 raise한다 ([L1019-L1046](https://github.com/langchain-ai/langgraph/blob/bfcfea554ed5c7f7be562cebf8825e911b493ab1/libs/langgraph/langgraph/types.py#L1019-L1046)).
- `response_schema`를 주면 resume 값을 Pydantic으로 검증한다. 실패하면 "Nothing is saved, so the interrupt can be answered again."

**모델이 위조할 수 없는 이유**
- resume 값은 LLM 출력이 아니라 `graph.invoke(Command(resume=…), config={"thread_id":…})`의 *호출 인자*다.
- LLM은 그래프 *안*의 node에서 돌기 때문에 그래프를 바깥에서 다시 호출할 수 없다.
- 대기는 "프로세스가 반환되고, 다음 invoke가 올 때까지 아무 일도 없음"으로 구현된다.
- 공식 문서: https://docs.langchain.com/oss/python/langgraph/interrupts. checkpointer와 `thread_id`가 필수다.

**한계:** 클라이언트 자체가 LLM agent라면(agent가 도구로 그래프를 호출하는 경우) 같은 위조 문제가 다시 생긴다.

---

## 6. humanlayer/humanlayer (reference primitive)

| 항목 | 값 |
|---|---|
| repo | https://github.com/humanlayer/humanlayer |
| SHA | HEAD `99abe673498cf8bdcd5f989aebe9406a27185b3b` (2026-06-18). SDK 마지막 버전 `ce816f743c78733cbe771e478157e0313ec8f818` (2025-02-15) |
| license / maturity | NOASSERTION · ★11,678 |
| 상태 | README: "the code here is pretty much all deprecated". 2025-09-29 커밋 `fd19339` "Clean up humanlayer sdk stuff, this is codelayer now" |

**SDK `@require_approval`**
- 감싼 함수를 호출하면 HumanLayer 백엔드에 FunctionCall을 생성한다.
- 3초 간격 `while True` polling으로 `status.approved`가 채워질 때까지 블로킹한다.
- 승인되면 원래 함수를 실행하고, 거절되면 "User denied …" 문자열을 LLM에 반환한다.
  - [approval.py L213-L271](https://github.com/humanlayer/humanlayer/blob/ce816f743c78733cbe771e478157e0313ec8f818/humanlayer/core/approval.py#L213-L271)
  - polling 루프 [L376-L398](https://github.com/humanlayer/humanlayer/blob/ce816f743c78733cbe771e478157e0313ec8f818/humanlayer/core/approval.py#L376-L398)
- 위조 불가 이유:
  - 승인 상태는 외부 SaaS에 있고, Slack/email/web으로 사람이 응답한다.
  - LLM에게는 승인 API가 없다.
  - side effect를 일으키는 함수 자체가 래퍼 *안*에 있다. 모델이 "승인됐다"고 말해도 함수는 실행되지 않는다.

**현행 hld (CodeLayer daemon)**
- Claude Code를 `--permission-prompt-tool mcp__codelayer__request_permission`으로 띄운다 ([hld/session/manager.go L259](https://github.com/humanlayer/humanlayer/blob/99abe673498cf8bdcd5f989aebe9406a27185b3b/hld/session/manager.go#L259)).
- MCP `request_approval` 핸들러는 approval 레코드를 만들고 `decisionChan`에서 사람 결정을 기다리며 블로킹한다 ([hld/mcp/server.go L59-L77](https://github.com/humanlayer/humanlayer/blob/99abe673498cf8bdcd5f989aebe9406a27185b3b/hld/mcp/server.go#L59-L77), [L166-L183](https://github.com/humanlayer/humanlayer/blob/99abe673498cf8bdcd5f989aebe9406a27185b3b/hld/mcp/server.go#L166-L183)).
- tool-call 권한을 host 하네스 자체가 이 MCP 도구에 위임하므로 모델이 우회할 수 없다.
- **적용 범위:** *도구 호출* 승인용이다. 요구사항 interview용이 아니다.

---

## 7. GSD 계열 (gsd-build/get-shit-done, gsd-build/gsd-2) — scout 조사

| 항목 | GSD v1 | GSD-2 (`gsd-pi`) |
|---|---|---|
| repo / SHA | https://github.com/gsd-build/get-shit-done `bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815` | https://github.com/gsd-build/gsd-2 `33c00aaffa56e5d394bccce1c8df59fb842e84c5` |
| license / maturity | MIT · ★64,345 · **archived** → open-gsd/gsd-core | MIT · ★7,776 · **archived** → open-gsd/gsd-pi |
| Driver | 기본: LLM이 `Skill(gsd-plan-phase …)`로 다음 phase를 직접 체인한다. 선택: `gsd-sdk auto` `PhaseRunner`(runtime, 각 단계는 별도 Agent SDK 세션) | runtime. `deriveState`(SQLite + `.gsd/`)와 순서 있는 `DISPATCH_RULES`("first match wins")로 다음 unit을 고른다 |

### GSD v1
- **interview**
  - `discuss-phase`에서 "Ask 4 questions using AskUserQuestion"한다. 대기는 host의 블로킹 도구 또는 턴 종료로 한다 ([default.md L47-L50](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/discuss-phase/modes/default.md#L47-L50)).
  - 사람이 답했는지 검사하는 코드는 없다.
- **skip 경로**
  - `--auto`: "Claude picks recommended option for every question; no AskUserQuestion" ([discuss-phase.md L349](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/discuss-phase.md#L349))
  - `workflow.skip_discuss`: placeholder CONTEXT.md를 생성한다 ([autonomous.md L188-L210](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/autonomous.md#L188-L210))
  - YOLO가 "Recommended"로 표시된다 ([new-project.md L593](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/new-project.md#L593))
- **SDK runner의 self-discuss**
  - "This session is running headless with no human present.… Make all decisions autonomously" ([phase-runner.ts L159-L161](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/phase-runner.ts#L159-L161))
  - approval callback이 없으면 기본값이 승인이다: `return 'skip'; // Auto-approve` ([L1400-L1401](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/phase-runner.ts#L1400-L1401))
- **hook:** "This is a SOFT guard — it advises, not blocks"이고 기본으로 꺼져 있다 ([gsd-workflow-guard.js L7-L11](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/hooks/gsd-workflow-guard.js#L7-L11)).
- **이슈 (interview 자동 통과 계열이 다수)**
  - **#1009** "AskUserQuestion auto-resolves without user input when invoked from nested Skill calls": "Claude interpreted these empty responses as selecting the "(Recommended)" option each time"
  - **#3018** "bug(codex): discuss-phase defaults decisions when request_user_input is unavailable, then writes artifacts": "planning artifacts that look discussion-backed even though the user never made the decisions"
    - **agent-flow가 관측한 Codex 실패와 동일한 패턴이다.**
  - 제목만 확인한 같은 계열: #869, #874, #938, #950, PR #929 "fix: add AskUserQuestion answer verification to prevent auto-answering"

### GSD-2: 조사 대상 중 유일하게 interview 대기를 tool hook으로 강제
1. **질문을 등록하면 gate가 pending이 된다.** 모델이 `ask_user_questions`로 `depth_verification_<MID>` 질문을 내면 hook이 gate를 pending으로 설정한다 ([register-hooks.ts L788-L796](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/bootstrap/register-hooks.ts#L788-L796)).
2. **pending 동안 다른 모든 도구와 bash를 막는다.** "HARD BLOCK: Discussion gate … has not been confirmed by the user" ([write-gate.ts L379-L399](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/bootstrap/write-gate.ts#L379-L399))
3. **CONTEXT.md 쓰기를 막는다.** depth-verified 표시가 있을 때까지 쓰기 금지 ([L464-L497](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/bootstrap/write-gate.ts#L464-L497))
4. **unlock 조건:** 답이 정확히 첫 번째 옵션이거나, 다음 *사용자* 메시지가 승인 패턴과 일치해야 한다 ([register-hooks.ts L1031-L1036](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/bootstrap/register-hooks.ts#L1031-L1036), [L563-L567](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/bootstrap/register-hooks.ts#L563-L567), [user-input-boundary.ts L115-L132](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/user-input-boundary.ts#L115-L132))
5. **턴이 승인 질문으로 끝나면 auto-mode를 pause한다** ([auto-post-unit.ts L1663-L1675](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/auto-post-unit.ts#L1663-L1675)).
6. **skip 경로:** non-deep auto 모드에서는 dispatcher가 `markDepthVerified`를 직접 호출한다(`isAutoActive() && prefs?.planning_depth !== "deep"`) ([auto-dispatch.ts L141-L143](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/auto-dispatch.ts#L141-L143)).
   - headless 모드 프롬프트: "do NOT ask the user questions" ([discuss-headless.md L3](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/prompts/discuss-headless.md#L3))
- **이슈 (gate를 고쳐 나간 흔적)**
  - **#4973** "auto-mode discuss-milestone deadlocks on depth-verification gate": "Auto-mode has no user." 이 이슈가 auto bypass가 생긴 계기다.
  - **#3749** "Depth verification write-gate unlocks without checking user answer": "Selecting "Not quite — let me clarify" … or typing garbage all unlock the gate"
  - **#5278/PR #5279**: "the agent skips waiting and announces "ready" anyway"
  - **#5091** "Built-in AskUserQuestion short-circuits depth-verification gate in interactive sessions"

### agent-flow와의 유사도
- GSD v1 slash 경로: **Low**. LLM이 체인하고 gate는 prompt다.
- GSD v1 `gsd-sdk auto`: **Medium**. runner가 순서를 소유하지만 별도 세션에서 돌고, 승인 기본값이 yes다.
- GSD-2: **Medium**. runtime이 순서를 소유하고 artifact와 텍스트 마커를 확인한다. 다만 host 세션이 아닌 자체 agent 프로그램이고, 사람 gate는 사용자 입력 이벤트로 unlock된다.

---

## 8. 기타 runtime/hybrid 러너 — scout 조사

### Pimzino/spec-workflow-mcp
`d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0` · GPL-3.0 · ★4,302

- **Driver:** prompt. 가이드 도구가 "Complete phases in sequence (no skipping)"을 텍스트로 반환한다 ([spec-workflow-guide.ts L262-L271](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/spec-workflow-guide.ts#L262-L271)).
- **Interview:** 없다. Requirements를 생성한 뒤 바로 승인을 요청한다.
- **승인: 외부 UI(대시보드/VS Code)**
  - MCP `approvals` 도구는 `request/status/delete`만 있고 approve가 없다. 요청은 즉시 `pending`과 "Poll status"를 반환하므로 블로킹이 아니다 ([approvals.ts L31-L47](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/approvals.ts#L31-L47), [L326-L361](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/approvals.ts#L326-L361)).
- **위조 가능성**
  - 승인 레코드는 서명 없는 JSON이다: `return JSON.parse(content)` ([approval-storage.ts L308-L315](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/dashboard/approval-storage.ts#L308-L315)).
  - 대시보드 `POST …/approvals/:id/:action`에 인증이 없다 ([multi-server.ts L677-L705](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/dashboard/multi-server.ts#L677-L705)).
  - 따라서 모델이 파일이나 curl로 자가 승인할 수 있다 [INFERENCE, 관측 사례는 없음].
- **이슈**
  - **#100** "[Bug]: Workflow doesn't pause between approvals": agent 발화 "Since the approval system appears to be in headless mode without a dashboard, I'll proceed with creating the tasks document and then implementing the solution."
  - **#232**: "a prompt-only "do not complete" rule is guidance, not a deterministic server-side gate."
- **유사도: Medium.** 서버가 승인 상태를 소유하고 사람이 대역 외로 승인하는 점은 agent-flow보다 낫다. 순서는 prompt가 정하고 파일은 위조 가능하다.

### Arch1eSUN/Arcgentic
`8539955052859f315436b961465341c7bc2b3c20` · MIT · ★286

- **Driver:** bash `transition.sh`가 상태 전이표를 검사하고 gate 스크립트를 실행한다.
  - 상태표는 프로젝트의 `.agentic-rounds/state.yaml` 안에 있어 agent가 편집할 수 있다 ([transition.sh L53-L80](https://github.com/Arch1eSUN/Arcgentic/blob/8539955052859f315436b961465341c7bc2b3c20/scripts/state/transition.sh#L53-L80), [init.sh L96-L112](https://github.com/Arch1eSUN/Arcgentic/blob/8539955052859f315436b961465341c7bc2b3c20/scripts/state/init.sh#L96-L112)).
- **gate 3종**

  | gate | 검사 내용 | 성격 |
  |---|---|---|
  | handoff | state.yaml의 `sections_present >= sections_required` | self-reported |
  | commit chain | `git cat-file -e` | 실제 검사 |
  | verdict | state.yaml의 `fact_table_pass == total` | self-reported |

- **skip 경로:** `--skip-gates` ([L63-L64](https://github.com/Arch1eSUN/Arcgentic/blob/8539955052859f315436b961465341c7bc2b3c20/scripts/state/transition.sh#L63-L64))
- **Interview:** 코드에는 없다. "If any required input is missing, ASK the user"가 전부다 ([plan-round SKILL L54](https://github.com/Arch1eSUN/Arcgentic/blob/8539955052859f315436b961465341c7bc2b3c20/skills/plan-round/SKILL.md#L54)).
- **사람 승인:** 없다. session mode의 `requires_user_confirmation=True`는 아무것도 강제하지 않는 플래그다.
- **유사도: High.** 스크립트 + YAML phase 표, host LLM이 Markdown 작성 후 CLI 호출, self-reported 마커 gate, host가 직접 넘길 수 있는 bypass가 모두 agent-flow와 같다.

### catlog22/maestro-flow
`f888ceca62dd8bf8c4b5b4c5d70a2083342f9d9a` · LICENSE 파일 없음(README는 MIT 표기) · ★564

- **Driver:** runtime CLI가 Session chain, 순서, CAS fence를 소유한다.
  - "The Session chain is the only authority for step order, gates…" ([orchestrator-run-loop L32](https://github.com/catlog22/maestro-flow/blob/f888ceca62dd8bf8c4b5b4c5d70a2083342f9d9a/workflows/orchestrator-run-loop.md#L32))
  - chain 자체는 LLM(Ralph)이 의도에서 만든다.
- **자가 승인:** `run decide`는 "The evaluation stays in the prompt layer… This verb only records that verdict… It never re-evaluates"이고, `--actor`는 임의 값이다 ([decide.ts L6-L9](https://github.com/catlog22/maestro-flow/blob/f888ceca62dd8bf8c4b5b4c5d70a2083342f9d9a/src/run/decide.ts#L6-L9)).
- **runtime의 대기 지시:** `authority=user_required`에서 "End the turn only for user_required authority"를 출력해 모델에게 *조언*한다 ([continuation.ts L481](https://github.com/catlog22/maestro-flow/blob/f888ceca62dd8bf8c4b5b4c5d70a2083342f9d9a/src/run/continuation.ts#L481)).
- **Interview:** "One decision per turn via AskUserQuestion". `-y`면 "code exploration replaces human answers"이지만, broad intent의 boundary 질문은 "never skipped (even with `-y`)"이다 ([interview-mechanics L8-L11](https://github.com/catlog22/maestro-flow/blob/f888ceca62dd8bf8c4b5b4c5d70a2083342f9d9a/workflows/interview-mechanics.md#L8-L11)).
- **사람 confirm:** `S_CONFIRM`은 `-y`가 없거나 risk가 high이거나 confidence < 60일 때만 실행된다. risk와 confidence는 모델이 자가 평가한다 ([maestro-ralph.md L102-L108](https://github.com/catlog22/maestro-flow/blob/f888ceca62dd8bf8c4b5b4c5d70a2083342f9d9a/.claude/commands/maestro-ralph.md#L102-L108)).
- **유사도: Medium–High.** runtime chain + `run next`/`run complete --advance`는 `agent-flow continue`와 같은 모양이다. `run decide`로 자가 승인할 수 있다.

### gmickel/flow-next
`09e291ee1e941ec7961697b85fbc2b55cf1adcc2` · MIT · ★709

- **Driver:** prompt conductor.
- **Interview:** refine에서 "Every question goes through `AskUserQuestion`… Never print questions as narration" ([refine SKILL L83-L85](https://github.com/gmickel/flow-next/blob/09e291ee1e941ec7961697b85fbc2b55cf1adcc2/plugins/flow-next/skills/flow-next-refine/SKILL.md#L83-L85)).
  - 단 refine은 "Skip unless the open decision can be named"이다 ([route-matrix L23](https://github.com/gmickel/flow-next/blob/09e291ee1e941ec7961697b85fbc2b55cf1adcc2/plugins/flow-next/skills/flow-next-flow/references/route-matrix.md#L23)). 열린 결정이 있는지 판단은 모델이 한다.
  - `--auto`는 "never stops to ask"이다.
- **유사도: Low.**

### OrchestratorInc/agent-orchestrator
`0df86c53e414a8d91a89eeaa5ee25cdb15b2a70a` · Apache-2.0 · ★12,935

- 병렬 세션 supervisor다. phase 목록도 clarification 단계도 없다.
- 사람 승인은 하네스 자체의 permission prompt뿐이다: "Automation must never inject input into a blocked session" ([architecture.md L56](https://github.com/OrchestratorInc/agent-orchestrator/blob/0df86c53e414a8d91a89eeaa5ee25cdb15b2a70a/docs/architecture.md#L56)).
- **유사도: Low.**

### OpenHands (software-agent-sdk)와 SWE-agent
- **OpenHands:** `9c4fd8c6664dfce98524d04d965b4f009f390fbe` · MIT
  - 요구사항 clarification phase는 없다.
  - 관련 원시 기능은 **confirmation mode**다. `confirmation_policy.should_confirm(risk)`가 참이면 상태를 `WAITING_FOR_CONFIRMATION`으로 두고 실행을 멈춘다 ([agent.py L1164-L1170](https://github.com/OpenHands/software-agent-sdk/blob/9c4fd8c6664dfce98524d04d965b4f009f390fbe/openhands-sdk/openhands/sdk/agent/agent.py#L1164-L1170), [state.py L48-L58](https://github.com/OpenHands/software-agent-sdk/blob/9c4fd8c6664dfce98524d04d965b4f009f390fbe/openhands-sdk/openhands/sdk/conversation/state.py#L48-L58)).
  - 거절은 클라이언트 API `reject_pending_actions()`로 한다 ([local_conversation.py L2649-L2655](https://github.com/OpenHands/software-agent-sdk/blob/9c4fd8c6664dfce98524d04d965b4f009f390fbe/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L2649-L2655)).
  - tool-call 승인용이고 interview용은 아니다.
- **SWE-agent:** 비대화형 이슈 해결기라서 clarification 단계가 없다. 범위에서 제외했다(★20,509).

---

## 9. Claude Code / Codex hooks: host 세션 workflow가 쓸 수 있는 강제 지점

출처
- Claude Code: https://code.claude.com/docs/en/hooks (2026-10-09 조회)
- Codex: https://developers.openai.com/codex/hooks (2026-10-09 조회)

### 이벤트별 정리

| 이벤트 | 언제 | 할 수 있는 일 | interview 강제에서 갖는 의미 |
|---|---|---|---|
| `UserPromptSubmit` | 프롬프트가 제출되어 Claude가 처리하기 전 | `prompt` 원문 수신, `decision:"block"`, `additionalContext` 주입 | runner가 "pause 이후 사용자 턴이 있었는지"를 기록할 수 있는 지점. **주의:** scheduled task, `/loop`, background subagent 보고, 다른 세션 메시지에서도 발화한다(문서: "`UserPromptSubmit` hooks don't fire only on prompts you type"). Codex는 `turn_id`도 준다 |
| `Stop` | 메인 에이전트 응답이 끝났을 때(사용자 interrupt 시에는 발화하지 않음) | `decision:"block"`으로 계속 진행 강제. 연속 8회 상한(`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`), `stop_hook_active`, `last_assistant_message` | 기다리게 하는 장치가 아니라 *계속 굴리는* 장치다. 반대로 질문으로 끝나지 않은 턴을 block해 "질문하고 멈춰라"를 강제하는 데 쓸 수 있다 [INFERENCE]. oh-my-openagent #5548처럼 잘못 쓰면 interview를 망친다 |
| `PreToolUse` | 도구 실행 전. `AskUserQuestion`, `ExitPlanMode`, Bash 등 매칭 가능 | `permissionDecision: allow/deny/ask/defer` | `Bash(agent-flow continue --approve*)`를 deny하는 등 자가 승인 명령 차단에 쓸 수 있다. `ask`는 사람에게 확인 프롬프트를 띄운다 |
| `AskUserQuestion` + `defer` | headless `-p`에서 | 도구 미실행, `stop_reason:"tool_deferred"`로 프로세스 종료 → 호출 측이 UI로 질문 → `claude -p --resume` | runner가 *외부 프로세스*로 질문을 중계하는 공식 경로. LangGraph interrupt와 같은 구조다 |

- `AskUserQuestion` + `defer` 경로의 원문: "The hook returns `permissionDecision: "defer"`. The tool doesn't execute. The process exits with `stop_reason: "tool_deferred"`" (hooks 문서 PreToolUse/AskUserQuestion 절)
- Codex도 `UserPromptSubmit`(필드 `turn_id`, `prompt`; block 가능)과 `Stop`, `PreToolUse`, `PermissionRequest`를 지원한다(Codex hooks 문서 "UserPromptSubmit" 절).

### 한계 (중요)
- hook 프로세스는 모델과 같은 OS 사용자로 돈다. hook이 쓰는 ledger 파일은 모델이 Bash로 쓸 수 있는 위치에 있으면 위조될 수 있다.
- hook만으로는 *완화*다. 위조를 막으려면 다음 중 하나가 함께 필요하다 [INFERENCE]:
  - (a) ledger 경로에 permission deny 규칙이나 sandbox 쓰기 금지를 건다.
  - (b) runner가 하네스 transcript(`transcript_path`)의 사용자 메시지와 대조한다.
  - (c) 승인을 host 밖 채널(별도 TTY, UI)로 받는다.

---

## 10. 모델이 위조할 수 없는 대기 메커니즘

**공통 원리:** "사람이 응답했다"는 사실을 *모델의 출력 채널이 아닌 곳*에서 관측한다. 모델이 쓸 수 있는 채널을 정리하면 다음과 같다.
- 모델이 쓸 수 있다: 텍스트, 도구 인자, 파일, Bash.
- 모델이 쓸 수 없다: 하네스가 생성하는 사용자 메시지와 도구 결과, 외부 프로세스의 함수 인자, 외부 서비스의 상태.

| 등급 | 메커니즘 | 예시 (근거) | 모델이 위조할 수 있나 |
|---|---|---|---|
| A | **프로세스 경계 interrupt/resume**: 실행이 *반환*되고, 재개 값은 외부 호출 인자로만 들어온다 | LangGraph `interrupt()`/`Command(resume=)` (§5). Claude Code `PreToolUse defer` + `--resume` (§9). Archon DB `paused` + 노드 세션 종료 (§1) | 불가. 단 resume을 호출하는 주체가 같은 LLM이면 다시 위조 가능해진다(Archon "AI approvers") |
| A | **외부 승인 서비스 + 래핑된 side effect**: 승인 전에는 효과를 내는 코드가 실행되지 않는다 | HumanLayer `@require_approval` polling, hld `request_approval` 블로킹 (§6). OpenHands `WAITING_FOR_CONFIRMATION` (§8) | 불가(승인 API가 모델에게 없음). 적용 범위는 도구 호출 |
| B | **하네스 블로킹 질문 도구**: 도구 결과를 하네스 UI가 만든다 | Claude Code `AskUserQuestion`, OpenCode `question`, gsd-2 `ask_user_questions` | 결과는 위조 불가. 다만 **도구를 아예 안 부르는 것**은 막지 못한다. 중첩 Skill에서 빈 응답이 "Recommended"로 처리된 사례가 있다(GSD #1009, #950) |
| B+ | **블로킹 도구 + pending 동안 tool HARD BLOCK + 사용자 메시지로만 unlock** | gsd-2 write-gate (§7) | interactive 모드에서는 사실상 불가. runtime의 auto bypass가 구멍이다 |
| C | **사용자 턴 이벤트 기록**: `UserPromptSubmit` hook으로 pause 이후 사용자 턴을 기록하고 runner가 검사 | Claude Code / Codex hooks (§9) | ledger를 보호하지 않으면 가능. 사용자가 아닌 주체도 이벤트를 발화시킨다 |
| D | **사람이 채우는 파일 / 대시보드가 쓰는 파일** | GSD v1 `--power` QUESTIONS.json, spec-workflow-mcp approvals JSON (§8) | 가능. 서명·인증이 없다 |
| E | **prompt 지시 + self-reported 마커** | agent-flow `## Completion Gate`, Prometheus `status: awaiting-approval`, SPARC "Stakeholder sign-off recorded", Arcgentic state.yaml, task-master `testResults` | 가능. 조사한 실패 사례 대부분이 여기서 나왔다 |

### agent-flow에 주는 함의 [INFERENCE — 설계 제안이며 구현·검증하지 않음]
1. **`--approve <hash>`를 모델이 실행할 수 없는 채널로 옮긴다.** 위 표의 A/B+ 등급이 되려면 다음 중 하나가 필요하다.
   - runner가 직접 TTY에서 읽기
   - 별도 터미널에서 사람이 실행하는 `agent-flow approve`
   - PreToolUse에서 `Bash(agent-flow continue --approve*)`를 deny하거나 `ask`로 전환
   - 또한 Archon #2565의 교훈대로 "승인 = 아무 메시지"로 처리하지 말고 명시적 결정 값을 받아야 한다.
2. **domain-grill을 runtime pause로 만든다.**
   - domain-grill 뒤에 `pause_after`를 두는 것만으로는 부족하다(같은 `--approve` 결함이 있다).
   - Archon의 `interactive` loop처럼 runner가 "질문 라운드 → 반드시 사용자 응답 → 다음 라운드"를 소유해야 한다.
   - 마커 대신 질문/답 원문을 runner 소유 ledger로 기록하고, 하네스 transcript의 사용자 메시지와 대조한다.
3. **질문은 산문이 아니라 관찰 가능한 이벤트로 낸다.** `AskUserQuestion`/`request_user_input` 같은 도구를 쓰게 한다. oh-my-openagent #5548과 GSD #3018(Codex에서 `request_user_input`이 없어 기본값으로 결정)이 근거다.
   - Codex처럼 질문 도구가 없을 수 있는 하네스에서는 "도구 없음 = 진행 금지(fail-closed)"로 처리한다.
4. **자동화 모드에서 interview를 끄는 경로는 명시적 사용자 플래그로만 허용한다.** 생성되는 artifact에 "discussion skipped"를 표시한다(GSD v1 `skip_discuss` placeholder 방식).
   - 모델의 자가 분류(Prometheus UNCLEAR, maestro confidence)를 skip 근거로 삼지 않는다.

---

## 11. agent-flow와 가장 가까운 프로젝트

agent-flow의 구조: runner가 순서를 소유하고, host LLM 세션이 phase를 수행하며, gate는 artifact와 마커를 본다.

| 순위 | 프로젝트 | 유사도 | 같은 점 | 다른 점 / 교훈 |
|---|---|---|---|---|
| 1 | **claude-task-master `autopilot`** | High | runtime state machine(RED/GREEN/COMMIT)이 순서를 소유. host LLM이 `autopilot_next`로 지시를 받고 수행한 뒤 `autopilot_complete_phase`로 보고. gate 입력(`testResults`)이 self-reported | interview/PRD는 runtime 밖이다. RED `failed=0` 보고 한 번으로 subtask를 건너뛰는 self-report skip이 agent-flow 마커 gate와 같은 약점이다 |
| 2 | **Arch1eSUN/Arcgentic** | High | bash `transition.sh` + YAML 상태표가 순서를 소유. host LLM이 Markdown을 쓰고 CLI를 호출. gate가 모델이 쓴 YAML 수치를 읽음. `--skip-gates`가 host 자가 bypass | 사람 gate가 전혀 없다. commit gate만 `git cat-file`로 실제를 검사한다 |
| 3 | **catlog22/maestro-flow** | Medium–High | runtime CLI가 Session chain과 순서, CAS fence를 소유. `run next`/`complete --advance` ≈ `agent-flow continue`. `run decide`가 모델 판정을 기록만 하는 자가 승인 | chain을 LLM이 의도에서 만든다(정적 YAML 아님). `user_required`에서 턴 종료는 *조언*이다 |

**참고 모델 (구조는 덜 비슷하지만 fix 설계에 가장 유용)**
- **Archon:** 실제 DB pause와 다채널 resume이 있고, agent가 gate를 해석하도록 바꾼 이력(#2565)이 있다. 반면 "No actor is recorded"와 AI approver는 반면교사다.
- **GSD-2:** 질문 대기 중 tool HARD BLOCK, 사용자 메시지로만 unlock, 턴이 질문으로 끝나면 auto pause. 그리고 auto 모드 bypass가 다시 구멍이 되는 과정이 이슈 #4973에서 #3749까지 기록돼 있다.

---

## 부록: 이 조사에서 확인하지 못한 것 (UNVERIFIED)
- Archon 노드 agent 세션에 `archon` CLI 실행 권한이 있는지(Bash 도구로 자기 run을 approve할 수 있는지)는 확인하지 않았다. pause 시점에는 노드 세션이 이미 끝나 있다는 사실만 확인했다.
- gsd-2의 `/gsd verdict pass`를 모델이 스스로 호출할 수 있는지는 scout가 추적하지 못했다.
- spec-workflow-mcp에서 모델이 JSON이나 HTTP로 자가 승인한 *실제 관측 사례*는 찾지 못했다. 코드상 가능성만 확인했다([INFERENCE]).
- maestro-flow `run recover`를 모델이 실행할 수 있는지는 [INFERENCE]다.
