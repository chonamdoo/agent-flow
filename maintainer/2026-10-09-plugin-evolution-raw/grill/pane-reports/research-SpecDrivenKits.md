# Spec-driven 키트의 인터뷰(clarify)와 승인 게이트 조사

조사일: 2026-10-09. 대상: github/spec-kit, Fission-AI/OpenSpec, gotalab/cc-sdd, Pimzino/spec-workflow-mcp, Pimzino/claude-code-spec-workflow, awslabs/aidlc-workflows, AWS Kiro(비공개 소스, 공식 문서만 참고).
방법: 각 저장소를 `git clone --depth 1`로 받아 소스와 프롬프트를 직접 읽었다. 이슈는 `gh issue list --search`로 찾았다. 아래 링크는 모두 읽은 커밋 SHA로 고정했다.
표기: **[CODE]** = 런타임 코드가 강제하는 동작. **[PROMPT]** = 모델에게 지시만 하는 텍스트. **[INFERENCE]** = 코드를 읽고 추론했지만 실행해서 확인하지 않은 내용.

---

## 요약 표

| 프로젝트 | 다음 단계를 정하는 주체 | 인터뷰(clarify) 단계 | 사용자 대기를 강제하는 방식 | 모델이 만들어낼 수 없는 승인 기록 | agent-flow와 유사도 |
|---|---|---|---|---|---|
| spec-kit (slash 명령) | LLM과 사용자(명령을 직접 호출) | `/speckit.clarify`, 선택 사항, 최대 5문항 | 턴 종료 [PROMPT] | 없음 | Medium |
| spec-kit (`specify workflow`) | Python 엔진 | 기본 workflow에는 **clarify 단계가 없음**. 명령은 headless(`claude -p`)로 실행 | `gate` step이 TTY `input()`으로 대기, TTY가 아니면 PAUSED [CODE] | **있음**: gate 선택은 엔진 프로세스의 stdin에서 받음 | **High** |
| OpenSpec | LLM(“actions, not phases”) | `/opsx:explore`, propose 1단계 | 턴 종료 [PROMPT] | 없음. 상태는 파일 존재 여부뿐 | Low–Medium |
| cc-sdd | LLM과 사용자 | requirements 단계의 질문(AskUserQuestion 허용), `kiro-discovery` | 턴 종료나 AskUserQuestion [PROMPT] | 없음. `spec.json` 승인 플래그를 **LLM이 직접 씀**. `-y`로 자동 승인 | Medium |
| spec-workflow-mcp | LLM이 guide 도구를 따름 | **없음**. 대신 대시보드 리뷰 코멘트 | MCP `approvals` 도구 폴링. 승인 상태는 대시보드/VS Code UI가 기록 [CODE] | **부분적**: 별도 UI 프로세스가 기록. 단 로컬 JSON 파일과 무인증 localhost API라서 우회는 가능 [INFERENCE] | Medium |
| claude-code-spec-workflow | LLM | 없음(승인 루프만) | 턴 종료 [PROMPT] | 없음. 대시보드가 `✅ APPROVED` 텍스트 마커로 승인을 판정 | Medium(마커 방식이 agent-flow와 같음) |
| aidlc-workflows (AI-DLC v2) | TS 엔진(`aidlc-orchestrate next/report`)과 Stop hook | requirements-analysis의 질문 파일(`[Answer]:`), 요약 확인 | `UserPromptSubmit` hook이 `HUMAN_TURN`을 기록. 엔진은 게이트를 연 뒤 사람 턴이 없으면 approve/answer를 거부 [CODE] | **있음**: append-only 감사 원장의 HUMAN_TURN. PreToolUse hook이 원장 위조와 우회 env 설정을 차단 | **Very High** |
| Kiro (비공개 소스) | IDE/CLI 제품 | Spec 모드, Quick Spec은 질문을 먼저 받음 | UI 체크포인트("Proceed to Design")가 있다고 문서에 나옴 | UNVERIFIED(소스 비공개) | Medium |

**핵심 결론**
- 사용자 대기를 텍스트 프롬프트에만 맡긴 키트(spec-kit slash, OpenSpec, cc-sdd, claude-code-spec-workflow)에서는 모두 모델이 질문을 건너뛰거나 스스로 진행했다는 이슈가 나온다.
- 모델이 만들어낼 수 없는 신호를 쓰는 곳은 세 군데뿐이다.
  - (a) AI-DLC: 사람 프롬프트 hook이 기록하는 `HUMAN_TURN`
  - (b) spec-kit workflow 엔진: TTY stdin
  - (c) spec-workflow-mcp: 별도 대시보드 UI
- AI-DLC는 agent-flow가 겪은 실패와 **같은 모양**의 실패(“게이트를 열고 30초 뒤 사람 메시지 없이 Approve 기록”)를 PR #2186에서 직접 보고하고 코드로 막았다.

---

## 1. github/spec-kit

- repo: https://github.com/github/spec-kit
- 읽은 커밋: `93989802b1401c99cc7de6e4ed76572f9cb662e8` (마지막 커밋 2026-10-08)
- 라이선스: MIT. stars 140,483(관측 시점).

### Driver
spec-kit에는 동작 방식이 두 가지 있다.
1. **Slash 명령 모드**(기본): 사용자가 `/speckit.specify`, `/speckit.clarify`, `/speckit.plan` 등을 직접 호출한다. 명령 사이의 전환은 사용자나 frontmatter `handoffs`(UI 제안)가 맡는다. 순서를 강제하는 코드는 없다.
2. **`specify workflow run` 엔진 모드**: YAML step을 Python 엔진이 순서대로 실행한다. command step은 통합 CLI를 **비대화형**으로 띄운다.
   - [`base.py#L1102`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/integrations/base.py#L1089-L1108): `args = [self._resolve_executable(), "-p", prompt]`
   - [`command/__init__.py#L216`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/workflows/step/command/__init__.py#L202-L220): "then executes the CLI non-interactively."
   - 일부 통합은 권한까지 자동 승인한다. Copilot `--yolo`([copilot/__init__.py#L328-L337](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/integrations/copilot/__init__.py#L328-L337)), agy `--dangerously-skip-permissions`([agy/__init__.py#L72](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/integrations/agy/__init__.py#L59-L85)).

### Phase artifacts and gates
- 산출물: `spec.md`, `plan.md`, `research.md`, `tasks.md`, `checklists/*.md`.
- **엔진 모드의 `gate` step [CODE]**: [`gate/__init__.py#L172-L182`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/workflows/step/gate/__init__.py#L172-L182)
  ```python
  if choice is None:
      # Non-interactive: pause for later resume (the file is not read here)
      if not sys.stdin.isatty():
          return StepResult(status=StepStatus.PAUSED, output=output)
      choice = self._prompt(self._compose_prompt(message, show_file), options)
  ```
  - 선택은 엔진 프로세스의 TTY `input()`에서 받는다([L247-L249](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/workflows/step/gate/__init__.py#L247-L290)).
  - EOF가 오면 reject로 처리한다([L262-L280](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/workflows/step/gate/__init__.py#L262-L280)): "Fall back to a rejection, which is the safe verdict for an unanswered review."
  - 비대화형 재개는 workflow가 `verdict_input`을 선언한 경우에만 `--input`으로 가능하다([L28-L31](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/src/specify_cli/workflows/step/gate/__init__.py#L20-L31)).
  - [INFERENCE] 그래서 LLM이 bash 도구(TTY 아님)로 `specify workflow run/resume`을 호출해도 게이트를 통과하지 못하고 PAUSED에 머문다. 단 `verdict_input`이 선언된 workflow라면 모델이 `--input`으로 verdict를 넣을 수 있다.
- **내장 `speckit` workflow에는 clarify가 없다**: [`workflows/speckit/workflow.yml#L39-L74`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/workflows/speckit/workflow.yml#L39-L74). 순서는 `specify → gate(review-spec) → plan → gate(review-plan) → tasks → implement`이다. 사람이 개입하는 지점은 산출물 리뷰 게이트뿐이고, 요구사항 인터뷰는 이 흐름에서 빠져 있다. command step이 `-p` headless로 돌기 때문에 넣더라도 질문에 답할 사람이 없다 [INFERENCE].
- slash 모드의 `implement` 체크리스트 게이트는 [PROMPT]다. [`implement.md#L81-L84`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/templates/commands/implement.md#L81-L88): "**STOP** and ask: "Some checklists have unchecked items. Do you want to proceed…" - Wait for user response before continuing"

### Interview/clarification step
- **`/speckit.specify`** [PROMPT]: 먼저 추측하고 질문은 최소화한다.
  - [`specify.md#L123-L128`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/templates/commands/specify.md#L120-L135): "Make informed guesses based on context and industry standards … **LIMIT: Maximum 3 [NEEDS CLARIFICATION] markers total**"
  - 남은 마커가 있으면 Q1–Q3를 한 번에 제시하고 "**Your choice**: _[Wait for user response]_" 한다([L199-L232](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/templates/commands/specify.md#L199-L232)).
- **`/speckit.clarify`** [PROMPT]: 최대 5문항을 한 번에 하나씩 묻고, 추천 답을 제시한다(grilling과 비슷하다).
  - [`clarify.md#L140-L180`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/templates/commands/clarify.md#L129-L180): "Present EXACTLY ONE question at a time." / "Maximum of 5 total questions across the whole session." / "If no valid questions exist at start, immediately report no critical ambiguities."
  - 답은 spec의 `## Clarifications / ### Session YYYY-MM-DD`에 `- Q: … → A: …`로 기록한다([L182-L196](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/templates/commands/clarify.md#L182-L196)).
- **선택 사항이다**: [`clarify.md#L60`](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/templates/commands/clarify.md#L60): "expected to run … BEFORE invoking plan. If the user explicitly states they are skipping clarification … you may proceed, but must warn". 문서도 "optional quality gate; formerly `/quizme`"라고 쓴다([agentic-sdd.md#L34](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/docs/reference/agentic-sdd.md#L34)).
- **대기 강제**: 턴 종료만 있다 [PROMPT]. `plan`은 `## Clarifications` 섹션이 있는지 검사하지 않는다(plan.md에서 clarify 관련 언급은 `NEEDS CLARIFICATION` research뿐이다. [plan.md#L67-L70](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/templates/commands/plan.md#L64-L72)).
- **모델이 스스로 완료를 선언할 수 있는가**: 예. "No critical ambiguities detected"라고 보고하면 끝난다.
- AskUserQuestion 같은 블로킹 도구는 core에 없다. 커뮤니티 preset으로만 있다([presets.md#L17](https://github.com/github/spec-kit/blob/93989802b1401c99cc7de6e4ed76572f9cb662e8/docs/community/presets.md#L17)).

### Issues
- [#4582 "Speckit skills are not interactive"](https://github.com/github/spec-kit/issues/4582) (closed, works-as-designed): "Most of the skills don't ask questions from the user… it's continuously implementing the tasks without asking me for review." 메인테이너 mnriem의 답: "This gives Copilot an explicit instruction; **it is not a mechanically enforced approval gate.**" 보고자는 "I did instructed the skill to stop next task until I approve. By the time I realized the prompt already implemented 25 tasks."라고 반박했다.
- [#1147 "`/prompts:speckit.clarify` with codex CLI offers recommended actions with no clarifying question"](https://github.com/github/spec-kit/issues/1147): Codex에서 질문 문장 없이 추천 옵션 표만 출력했다. 댓글: "Same. Even on more powerful models."
- [#2789 "hands-off Spec Kit orchestration workflow"](https://github.com/github/spec-kit/issues/2789): 자동 오케스트레이션을 원하는 쪽에서 "when to ask the user for a decision"이 미해결 과제라고 적었다.
- 커뮤니티가 grilling을 확장으로 따로 붙이는 중이다: [#4046 "SpecKit Grill Me"](https://github.com/github/spec-kit/issues/4046) ("Exhaustive clarification with no fixed question limit; Dependency-aware design tree").

### Similarity
- 엔진 모드: **High**. YAML step 목록, 엔진이 순서를 소유, gate와 resume 구조가 agent-flow와 거의 같다. 차이점이 둘 있다. gate가 **엔진 프로세스의 TTY**에서 받아서 LLM이 통과시킬 수 없다는 점, 그리고 command가 별도 headless 프로세스로 돈다는 점이다.
- slash 모드: Medium.

---

## 2. Fission-AI/OpenSpec

- repo: https://github.com/Fission-AI/OpenSpec
- 읽은 커밋: `9111a7654d7800391459431fff4eaf66e33a3d2e` (2026-10-06)
- 라이선스: MIT. stars 71,378.

### Driver
LLM이 정한다. 설계 철학 자체가 단계 게이트를 거부한다.
- [`docs/concepts.md#L10`](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/docs/concepts.md#L10): "fluid not rigid — no phase gates, work on what makes sense"
- [`docs/concepts.md#L458`](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/docs/concepts.md#L458): "**Dependencies are enablers, not gates.**"
- CLI(`openspec status/instructions --json`)는 artifact DAG와 다음 instruction을 알려주는 조회 도구다.

### Phase artifacts and gates
- 산출물: `proposal.md`, `specs/**/spec.md`(delta), `design.md`, `tasks.md`.
- **완료 판정은 파일 존재 여부뿐이다 [CODE]**: [`artifact-graph/state.ts#L6-L37`](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/src/core/artifact-graph/state.ts#L6-L37): "Detects which artifacts are completed by checking file existence in the change directory."
- 승인 개념이 코드에 없다(템플릿 외에 `approv` 검색 결과 없음).
- `openspec validate`는 spec delta 형식만 검증한다.
- `archive --yes`는 미완료 task 정지를 건너뛴다([#2006](https://github.com/Fission-AI/OpenSpec/issues/2006)).

### Interview/clarification step
- **`/opsx:explore`** [PROMPT]: grilling과 비슷하게 사실을 먼저 조사하고, 한 번에 한 질문씩 하며, 추천 답을 제시한다.
  - [`explore.ts#L14-L23`](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/src/core/templates/workflows/explore.ts#L14-L23): "Ask one focused question at a time … Silence is not acceptance. Accepting an answer or a batch of recommendations is not permission to write." / "Stop asking when the user has enough clarity."
  - 쓰기 전에 확인을 받으라는 규칙([L388](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/src/core/templates/workflows/explore.ts#L388)): "ask a direct yes/no question, and wait for explicit confirmation in a separate user message."
- **`/opsx:propose`** [PROMPT]: "material" 모호성만 묻고, 나머지는 가정을 기록하고 진행한다.
  - [`propose.ts#L61-L70`](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/src/core/templates/workflows/propose.ts#L61-L70): "If the request contains ambiguity that would materially affect scope … ask the user before creating the change. For minor details, make a reasonable assumption"
  - 계획 경계([L39](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/src/core/templates/workflows/propose.ts#L39)): "After the planning artifacts are complete, stop… Wait for a new user request"
- **`/opsx:ff`**는 진행을 우선한다: [`ff-change.ts#L140`](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/src/core/templates/workflows/ff-change.ts#L140): "If context is critically unclear, ask the user - but prefer making reasonable decisions to keep momentum". 같은 파일 [L53](https://github.com/Fission-AI/OpenSpec/blob/9111a7654d7800391459431fff4eaf66e33a3d2e/src/core/templates/workflows/ff-change.ts#L53)의 "Do NOT proceed without understanding"와 충돌한다는 지적이 [#1837](https://github.com/Fission-AI/OpenSpec/issues/1837)에 있다.
- **대기 강제**: 턴 종료뿐이다. 모델이 완료를 스스로 선언할 수 있다.
- 블로킹 질문 UI를 쓰자는 요청은 아직 open이다([#1924](https://github.com/Fission-AI/OpenSpec/issues/1924)).

### Issues
- [#869 "Claude Opus 4.6 will skip the opsx:ff and went ahead to implement"](https://github.com/Fission-AI/OpenSpec/issues/869): "after I answer the questions, it will automatic proceed to implement mode without asking for spec creation."
- [#1715 "Explore skill: agent modified workspace files without an explicit capture request"](https://github.com/Fission-AI/OpenSpec/issues/1715): "The user had only answered the agent's clarifying questions… the agent treated those answers as a go-ahead and started writing files." 답변을 승인으로 취급한 사례다.
- [#1828](https://github.com/Fission-AI/OpenSpec/issues/1828): capture 분기가 같은 skill의 first-write 확인 규칙을 우회했다.
- [#1084 "Incomplete artifacts marked as "done" after workflow interruption"](https://github.com/Fission-AI/OpenSpec/issues/1084): "`detectCompleted()` … only checks **file existence**". 파일 존재를 완료로 보는 방식이 agent-flow의 마커 방식과 같은 약점을 가진다.
- [#1519 "measure planning readiness, not just planning-artifact existence"](https://github.com/Fission-AI/OpenSpec/issues/1519), [#1142 "Quality gates as first-class schema concept with CLI enforcement"](https://github.com/Fission-AI/OpenSpec/issues/1142): "quality gates … are enforced purely through skill files — hardcoded text instructions that an LLM agent is asked to interpret and follow."
- [#2032 "Agents implement and approve changes without checking what the changed thing is for"](https://github.com/Fission-AI/OpenSpec/issues/2032).

### Similarity
**Low–Medium**. artifact DAG와 CLI instruction 공급 구조는 비슷하다. 하지만 런타임이 순서나 게이트를 소유하지 않는 것이 이 프로젝트의 의도된 설계다.

---

## 3. gotalab/cc-sdd

- repo: https://github.com/gotalab/cc-sdd
- 읽은 커밋: `e2a0c671aef37a482404905b409ab5c27092b25b` (2026-09-23)
- 라이선스: MIT. stars 3,708.
- 파일 경로 접두어: `tools/cc-sdd/templates/agents/claude-code-skills/skills/`

### Driver
LLM과 사용자가 정한다. npm 패키지 `cc-sdd`는 템플릿 설치기다. `src`에서 `approvals`를 읽는 코드는 없다(grep 결과 0건). 다음 단계는 사용자가 다음 명령을 호출해서 진행한다.

### Phase artifacts and gates
- 산출물: `brief.md`, `requirements.md`(EARS), `research.md`, `design.md`, `tasks.md`, `spec.json`.
- `spec.json` 템플릿에 `approvals.{requirements,design,tasks}.{generated,approved}`와 `ready_for_implementation`이 있다([init.json](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/shared/settings/templates/specs/init.json)).
- **이 플래그는 모두 LLM이 직접 쓴다 [PROMPT]**:
  - design 단계가 이전 단계 승인을 스스로 기록한다. [`kiro-spec-design/SKILL.md#L133-L137`](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-design/SKILL.md#L126-L137): "Set `approvals.design.generated: true, approved: false` - Set `approvals.requirements.approved: true`". 사용자가 design 명령을 호출한 것 자체를 requirements 승인으로 간주하는 방식이다. 문서에 "Approve by running: /kiro:spec-design user-auth-oauth"라고 적혀 있다([command-reference.md#L363-L366](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/docs/guides/command-reference.md#L363-L366)).
  - tasks 단계는 requirements와 design 승인을 true로 쓰고, 대화형일 때만 묻는다. [`kiro-spec-tasks/SKILL.md#L107-L129`](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-tasks/SKILL.md#L107-L129): "Ask the user: "Tasks generated. Approve and proceed to implementation?" - If the user approves: Set `approvals.tasks.approved: true`"
  - `-y` 자동 승인: [`kiro-spec-design/SKILL.md#L36-L38`](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-design/SKILL.md#L36-L38): "If auto-approve flag is true: Auto-approve requirements in spec.json". 문서는 "⚠️ **Use carefully** - skips human review of requirements"라고 경고한다([command-reference.md#L548-L551](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/docs/guides/command-reference.md#L548-L551)).
  - 승인이 안 됐을 때 프롬프트가 `-y` 우회를 직접 제안한다([design L169-L172](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-design/SKILL.md#L169-L172)): "Suggested Action: "Run `/kiro-spec-design {feature} -y` to auto-approve requirements and proceed"". 문제를 정지가 아닌 우회 지시로 안내한다는 점에서 OpenSpec #2006과 같은 패턴이다.
  - `kiro-impl`은 "Verify tasks are approved in spec.json (stop if not)"을 확인한다([L44-L45](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-impl/SKILL.md#L44-L45)). 이 확인도 LLM이 LLM이 쓴 플래그를 읽는 것이다.
- **자동 모드**:
  - `kiro-spec-quick --auto` ([L11-L20](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-quick/SKILL.md#L11-L20)): "Execute ALL 4 phases in a continuous loop without stopping".
  - 대화형 quick 모드에서도 design과 tasks는 `-y`로 호출한다([L123](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-quick/SKILL.md#L121-L141)).
  - `kiro-spec-batch`는 subagent에게 `-y`를 넘긴다([L63-L72](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-batch/SKILL.md#L60-L72)). 그러면서도 "do not force its approvals to true"라고 쓴다.
- 리뷰 게이트(requirements, design, task-plan review)는 LLM의 자기 리뷰다(필요하면 subagent 사용).

### Interview/clarification step
- **requirements** [PROMPT]: `allowed-tools`에 `AskUserQuestion`이 있다([L4](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-requirements/SKILL.md#L4)).
  - [L98](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-requirements/SKILL.md#L96-L100): "Ask as many questions as needed; do not generate requirements that contain your own assumptions."
  - 범위 구분이 명확하다([L81-L94](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-spec-requirements/SKILL.md#L81-L94)): requirements 단계에서는 WHAT만 묻고 HOW는 묻지 않는다.
- **`kiro-discovery`** [PROMPT]: 질문을 순차로 한다. 단 "If the user already provided a clear description, skip to Step 5."([L87](https://github.com/gotalab/cc-sdd/blob/e2a0c671aef37a482404905b409ab5c27092b25b/tools/cc-sdd/templates/agents/claude-code-skills/skills/kiro-discovery/SKILL.md#L77-L88))로 모델 판단에 맡긴 생략 경로가 있다.
- **대기 강제**: AskUserQuestion(Claude Code에서는 블로킹 도구)이나 턴 종료다. 런타임 검사는 없다.
- **모델이 스스로 완료를 선언할 수 있는가**: 예. `spec.json` 플래그를 모델이 쓰기 때문이다.

### Issues
- 질문 생략이나 자기 승인을 직접 보고한 이슈는 찾지 못했다(검색어: approval, approve, -y, ask, question, skip, auto, spec.json).
- 관련 이슈:
  - [#190 "Widening scope after task generation re-triggers no validation"](https://github.com/gotalab/cc-sdd/issues/190): 승인 후 범위가 바뀌어도 재검증이 없다.
  - [#6 "Auto-flow mode with y/n confirmation"](https://github.com/gotalab/cc-sdd/issues/6): 자동 흐름 요청이 `spec-quick`으로 이어진 것으로 보인다 [INFERENCE].
  - [#5 `/spec-po-refiner`](https://github.com/gotalab/cc-sdd/issues/5): 사용자가 "Ask one focused question at a time" 방식의 PO 인터뷰를 제안했다.

### Similarity
**Medium**. Kiro식 단계와 승인 플래그 구조는 비슷하다. 하지만 승인 플래그가 agent-flow의 Completion Gate 마커처럼 LLM이 자기 신고하는 값이고, 오히려 `-y`로 자동 승인을 공식 지원한다.

---

## 4. Pimzino/spec-workflow-mcp

- repo: https://github.com/Pimzino/spec-workflow-mcp
- 읽은 커밋: `d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0` (2026-07-03)
- 라이선스: GPL-3.0. stars 4,302.

### Driver
LLM이 `spec-workflow-guide` 도구가 돌려주는 텍스트를 따른다 [PROMPT].
- [`spec-workflow-guide.ts#L258-L271`](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/spec-workflow-guide.ts#L258-L271): "Complete phases in sequence (no skipping)", "CRITICAL: Verbal approval is NEVER accepted - dashboard or VS Code extension only", "NEVER proceed on user saying "approved" - check system status only"
- MCP 서버는 파일 쓰기를 막지 않는다. 모델이 승인 요청 없이 `design.md`를 바로 써도 서버는 알 수 없다 [INFERENCE: 서버에 파일 쓰기 hook이 없음].

### Phase artifacts and gates
- 산출물: `.spec-workflow/specs/{name}/requirements.md`, `design.md`, `tasks.md`, `Implementation Logs/`.
- **승인 상태는 별도 UI가 기록한다 [CODE]**:
  - `approvals` 도구의 `request`가 `.spec-workflow/approvals/<name>/<id>.json`을 `pending`으로 만든다([approval-storage.ts#L291-L295](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/dashboard/approval-storage.ts#L259-L295), [path-utils.ts#L262-L263](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/core/path-utils.ts#L262-L263)).
  - 상태 전환은 대시보드 HTTP `POST /api/projects/:projectId/approvals/:id/:action`(approve, reject, needs-revision)이나 VS Code 확장에서만 일어난다([multi-server.ts#L677-L707](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/dashboard/multi-server.ts#L676-L707)).
  - `status` 액션은 pending이면 "BLOCKED: Status is pending. Verbal approval is NOT accepted"를 돌려준다([approvals.ts#L414-L467](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/approvals.ts#L414-L467)).
  - pending 상태의 delete는 거부한다([L541-L558](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/approvals.ts#L541-L558)).
  - 다만 "승인 후 delete 성공 전에는 다음 단계로 가지 말라"는 규칙 자체는 프롬프트다([guide L138-L142](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/spec-workflow-guide.ts#L118-L142)).
- **우회 가능성 [INFERENCE, 실행하지 않음]**:
  - 승인 레코드는 프로젝트 안의 평문 JSON이다. 모델이 파일 도구로 `"status": "approved"`로 고치면 `status`가 approved를 돌려줄 것이다(서명이나 무결성 검사 코드를 보지 못했다).
  - 대시보드 approve 엔드포인트에는 인증 코드가 없다. localhost 바인딩과 CORS만 있다([multi-server.ts#L73-L80](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/dashboard/multi-server.ts#L73-L80)). 모델의 셸에서 `curl -X POST`로 호출할 수 있을 것이다.
  - 그래도 "승인했다"고 텍스트로 주장하는 것보다는 훨씬 강하다. 모델이 의도적으로 우회 행동을 해야 하기 때문이다.

### Interview/clarification step
- **없다**. Requirements Process([guide L132-L137](https://github.com/Pimzino/spec-workflow-mcp/blob/d38e82eaa8a6a2f5480285fa1feb8cf8378c86e0/src/tools/spec-workflow-guide.ts#L132-L137))는 steering을 읽고, 템플릿을 읽고, "Research market/user expectations"를 한 다음 바로 requirements를 생성한다. 사용자에게 묻는 단계가 없다.
- 그 대신 사람이 생성된 문서를 대시보드에서 annotation이나 needs-revision 코멘트로 고친다. 인터뷰를 사후 리뷰로 바꾼 구조다.
- **모델이 스스로 완료를 선언할 수 있는가**: 승인 상태 자체는 위조가 어렵다(위의 우회 경로는 예외). 하지만 승인을 요청하지 않고 진행하는 것은 막지 못한다.

### Issues
- [#100 "Workflow doesn't pause between approvals"](https://github.com/Pimzino/spec-workflow-mcp/issues/100): "if I start with Requirements, it requests an approval, then keeps going with design… if I'm reviewing the tasks it starts to implement them." 모델 출력: "● Since the approval system appears to be in headless mode without a dashboard, **I'll proceed** with creating the tasks document and then implementing the solution." (Claude Code, Opus). 대시보드가 없다는 사실을 진행해도 된다는 근거로 합리화한 사례다.
- [#58 "approval process … wasted call attempts"](https://github.com/Pimzino/spec-workflow-mcp/issues/58): 사용자가 "I think verbal approval is sufficient."라고 요청했다. 강제 장치가 사용자 비용(폴링)을 만든다는 반대 방향의 불만이다.
- [#182 "Auto approve and infinite work"](https://github.com/Pimzino/spec-workflow-mcp/issues/182): 메인테이너가 MCP는 "client-driven"이라 서버가 흐름을 통제할 수 없다며 거절했다.
- [#98](https://github.com/Pimzino/spec-workflow-mcp/issues/98): 승인하면 에이전트를 자동으로 다시 깨워 달라는 요청. TS MCP SDK의 60초 타임아웃 때문에 보류됐다.
- [#199 "Implementation logging is systematically skipped"](https://github.com/Pimzino/spec-workflow-mcp/issues/199): 프롬프트에 적힌 필수 단계를 체계적으로 건너뛴 사례. "Across dozens of specs, **zero implementation logs** are being created."

### Similarity
**Medium**. 단계 순서는 LLM이 정하지만, 승인을 별도 UI 프로세스에 둔 점이 agent-flow의 `--approve <hash>` 자기 통과 문제를 직접 해결하는 참고 사례다.

---

## 5. Pimzino/claude-code-spec-workflow (전신, 유지보수 축소)

- repo: https://github.com/Pimzino/claude-code-spec-workflow
- 읽은 커밋: `f3de74d8055120658ac199bfca865a3e1de9fd99` (2025-09-07)
- 라이선스: MIT. stars 3,861.
- README: "Development focus has shifted to the **MCP … version** … will receive limited updates."

### Driver / Gates
- LLM이 정한다. `/spec-create` 프롬프트 하나가 Requirements → Design → Tasks를 순서대로 진행한다.
- [`spec-create.md#L16`](https://github.com/Pimzino/claude-code-spec-workflow/blob/f3de74d8055120658ac199bfca865a3e1de9fd99/src/markdown/commands/spec-create.md#L16): "**User Approval Required**: Each phase must be explicitly approved before proceeding"
- [L126-L129](https://github.com/Pimzino/claude-code-spec-workflow/blob/f3de74d8055120658ac199bfca865a3e1de9fd99/src/markdown/commands/spec-create.md#L124-L129): "Ask: "Do the requirements look good?…" **CRITICAL**: Wait for explicit approval… Accept only clear affirmative responses"
- 검증은 Claude subagent(`spec-requirements-validator`)의 자기 리뷰다.
- **대시보드는 텍스트 마커로 승인을 판정한다 [CODE]**: [`dashboard/parser.ts#L208`](https://github.com/Pimzino/claude-code-spec-workflow/blob/f3de74d8055120658ac199bfca865a3e1de9fd99/src/dashboard/parser.ts#L205-L216): `approved: content.includes('✅ APPROVED') || content.includes('**Approved:** ✓')`. 마크다운 마커를 쓴 쪽이 누구든 승인으로 본다. agent-flow의 `## Completion Gate` 마커와 같은 구조다.

### Interview
- 없다. Requirements Process는 곧바로 "Generate requirements.md Document"를 한다([L80-L90](https://github.com/Pimzino/claude-code-spec-workflow/blob/f3de74d8055120658ac199bfca865a3e1de9fd99/src/markdown/commands/spec-create.md#L78-L90)). 사람은 사후 승인 루프에서만 개입한다.

### Issues
- 관련 이슈는 찾지 못했다(검색어: approval, skip, asks).

### Similarity
**Medium**. 승인 판정을 텍스트 마커로 하는 점이 agent-flow와 같은 약점이다.

---

## 6. awslabs/aidlc-workflows (AWS AI-DLC v2)

- repo: https://github.com/awslabs/aidlc-workflows
- 읽은 커밋: `47feb763c2285d47335e67c6b2bb3d49cd561f6d` (2026-10-08). README 배지는 version 2.11.0.
- 라이선스: MIT-0. stars 5,088.
- 대상 harness: Claude Code, Kiro CLI/IDE, Codex CLI, Cursor, opencode, Copilot.

### Driver
**런타임 엔진**이 정한다. agent-flow와 같은 envelope/continue 구조다.
- [`conductor.md#L3-L7`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/aidlc-common/conductor.md#L3-L7): "get a directive from the engine, do that one move, report the outcome, repeat… The engine decides which stage is next; you own the quality of execution inside the move it named."
- [`conductor.md#L34-L38`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/aidlc-common/conductor.md#L34-L38): "Open, reject, revise, approve, complete, or skip a stage only through `aidlc-orchestrate.ts report`; never … hand-edit stage checkboxes."
- **Stop hook이 루프를 강제한다 [CODE]**: [`hooks/aidlc-continue-workflow.ts#L10-L20`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/hooks/aidlc-continue-workflow.ts#L1-L30): "So the loop cannot rest on the conductor's good behaviour: when the conductor tries to end its turn, this hook runs the engine … and, if a directive is still PENDING, blocks the stop… Enforced by the harness, not by the LLM remembering."

### Phase artifacts and gates
- stage frontmatter가 `produces`, `consumes`, `summary_confirmation`, `reviewer`, `sensors`를 선언한다.
  - 예: [`requirements-analysis.md#L1-L54`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/aidlc-common/stages/inception/requirements-analysis.md#L1-L54). `summary_confirmation: required`이고, `produces`에 `requirements-analysis-questions`(질문 파일 자체)가 들어 있다.
- **요약 확인 전제조건 [CODE]**: [`aidlc-state.ts#L4182-L4210`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/tools/aidlc-state.ts#L4182-L4210). invariant: "Generated outputs descend from a current human-backed summary confirmation."
- **reviewer 전제조건 [CODE]**: REVIEW_COMPLETED 행이 없으면 게이트를 열 수 없다(같은 파일 L4212 이하 주석).
- **승인 게이트의 사람 존재 검사 [CODE]**:
  - [`hooks/aidlc-record-human-turn.ts#L1-L8`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/hooks/aidlc-record-human-turn.ts#L1-L8): "UserPromptSubmit hook: record a HUMAN_TURN event (human-presence gate). On every real human prompt, append a HUMAN_TURN event to the active intent's audit shard (the state machine's own append-only ledger). The approval / interview gate (handleApprove / handleAnswer) refuses unless a HUMAN_TURN was recorded since the last gate resolution (and, at a stage gate, after the gate was shown)."
  - [`aidlc-state.ts#L6404-L6490`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/tools/aidlc-state.ts#L6404-L6490) `verifyApprovalDecision`:
    - `--user-input`에 어시스턴트가 작성했다는 표지가 있으면 거부한다: "Approval is the human's decision: end the turn and let them answer".
    - 게이트를 연 뒤 사람 응답이 없으면 거부한다: "Cannot approve … because no new human reply has been received for this approval question: the person's last reply came before the gate was shown… Show the gate and end your turn".
  - **원장 위조와 우회 env 차단 [CODE]**: PreToolUse hook [`runtime-integrity.ts#L44-L62`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/hooks/runtime-integrity.ts#L44-L62) 주석: "The variables that carry AI-DLC's authority or turn a guard off … Only the person sets them, outside the agent." 대상은 `AIDLC_UNATTENDED`, `AIDLC_INTERNAL_HUMAN_TURN_TOKEN`, 우회 플래그들이다. [L1003-L1012](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/hooks/runtime-integrity.ts#L1003-L1012): "`>> $P` would forge the HUMAN_TURN evidence … So this pass re-reads each write word".
  - 끄는 스위치는 사람만 켤 수 있게 설계했다. [`aidlc-lib.ts#L29504-L29513`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/tools/aidlc-lib.ts#L29504-L29513): `AIDLC_SKIP_HUMAN_PRESENCE_GUARD=1`이면 검사를 끄고, `AIDLC_UNATTENDED=1`이면 HUMAN_TURN 발급을 보류한다. [hook L35-L54](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/hooks/aidlc-record-human-turn.ts#L35-L54)는 무인 실행 루프가 HUMAN_TURN을 위조하는 경로를 측정한 결과를 적었다: "10 runner-submitted prompts, zero humans, and humanActedSinceGate() answered true."
  - **한계**: hook은 "records presence and order; it does not authenticate who launched the dispatcher"(hook L7-L8). Construction autonomous 모드에서는 사람 게이트를 건너뛴다(`isAutonomousConstructionGate`).

### Interview/clarification step
- 위치: requirements-analysis Step 6–9([`requirements-analysis.md#L160-L223`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/aidlc-common/stages/inception/requirements-analysis.md#L160-L223)).
  - "PROACTIVE: Always generate clarifying questions unless requirements are exceptionally clear and complete across all six dimensions."
  - 질문 파일 `requirements-analysis-questions.md`에 A–E와 `X. Other`, 빈 `[Answer]:` 태그를 둔다.
  - 진행 방식을 사용자가 고른다: guided(대화), self-guided(파일 직접 편집), chat. 셋 다 결과는 같은 파일로 모인다([conductor.md#L42-L48](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/aidlc-common/conductor.md#L40-L55)).
  - "Confirm ALL `[Answer]:` tags are filled in… Do NOT proceed with partial answers." 모호한 답에는 follow-up 질문을 한다.
  - 그 다음 Consolidated Summary Confirmation을 한다. `engine log decision --checkpoint summary-confirmation`으로 질문을 기록하고, 턴을 끝내고, 사람 응답 뒤에 `engine log answer`로 영수증을 남긴다. "Do NOT create `requirements.md` until the confirmation entry contains the user's explicit `Looks correct` answer and the receipt command succeeds."
- 대기 강제 방식:
  - [PROMPT] HARD STOP RULE([`stage-protocol.md#L221-L223`](https://github.com/awslabs/aidlc-workflows/blob/47feb763c2285d47335e67c6b2bb3d49cd561f6d/core/aidlc-common/protocols/stage-protocol.md#L221-L223)): "you MUST end your turn immediately … Do NOT call any tool until the user has typed their choice in a new message."
  - [CODE] 그 뒤의 `report --result approved`와 `log answer`는 HUMAN_TURN이 없으면 엔진이 거부한다. 요약 확인 영수증이 없으면 산출물 단계도 거부한다.
- **생략 경로**:
  - 질문을 몇 개 만들지는 모델이 판단한다("exceptionally clear"). 하지만 질문 파일이 `produces`이고 요약 확인이 `required`이므로, 최소한 사람이 확인하는 턴 하나는 코드로 강제된다.
  - 요약 확인은 scope/ceremony 설정(`summary_confirmation on|off`)으로 끌 수 있다. 끄는 설정 변경도 사람 턴에 묶여 있다(docs/guide/13-customization.md).
- **모델이 스스로 완료를 선언할 수 있는가**: 승인과 답변 영수증은 원칙적으로 불가능하다. hook이 동작하지 않는 환경은 아래 이슈를 보라.

### Issues
- **[PR #2186 "fix(gates): an approval needs the person's reply to the gate it approves"](https://github.com/awslabs/aidlc-workflows/pull/2186)** (merged 2026-10-08): "the person answered the stage's own questions, the agent then opened the gate and, **30 seconds later and with no message from the person in between, recorded "Approve" on that earlier reply.** The stage completed and the next one started; the gate the person was owed never appeared." agent-flow의 Codex 사례(20–36초 만에 마커를 쓰고 `--approve`로 자기 통과)와 같은 실패다.
- [#2209 "An interview answer is credited to a reply sent before its question was logged, so the agent self-answers on the person's earlier message"](https://github.com/awslabs/aidlc-workflows/issues/2209) (open): "the conductor used a reply the person sent at 15:57 to record its own answer, "Nothing to add", to a learnings question it logged 9 minutes later."
- [#1548 "record-human-turn hook never fires in Claude desktop Code tab or VS Code extension"](https://github.com/awslabs/aidlc-workflows/issues/1548): hook이 없으면 "every question answer and approval gate is refused". 막히는 쪽으로 실패한다.
- [#2203](https://github.com/awslabs/aidlc-workflows/issues/2203): Kiro IDE에서 사용자가 hook을 끄면 "plan-approval and approval-gate guards stayed off for 31 h". 가드 hook은 꺼지면 검사가 사라지는 쪽으로 실패한다.
- [#708 "Gate Human-Presence Guard Blocks Legitimate Tool Calls"](https://github.com/awslabs/aidlc-workflows/issues/708): 게이트가 열려 있는 동안 모든 PreToolUse를 막아서 알림 도구까지 막혔다. 반대 방향의 비용이다.
- [#941 "require a completion receipt before advancing past a stage"](https://github.com/awslabs/aidlc-workflows/issues/941): 단계 진행에 영수증을 요구하자는 제안.
- [#943 "A review verdict has no dispatch credential … unlike GATE_APPROVED with HUMAN_TURN"](https://github.com/awslabs/aidlc-workflows/issues/943).

### Similarity
**Very High**. 엔진이 directive를 주고 LLM이 수행한 뒤 report하는 구조, stage 목록, 질문 파일 산출물, 게이트까지 agent-flow와 같다. agent-flow에 없는 세 가지가 있다.
1. UserPromptSubmit hook이 기록하는 HUMAN_TURN 원장
2. 게이트를 연 뒤 사람 응답이 있어야 approve를 허용하는 순서 검사
3. 원장 위조와 우회 env를 막는 PreToolUse hook

---

## 7. AWS Kiro spec 모드 (비공개 소스, 공식 문서만)

- 출처: kiro.dev 공식 문서(2026-10-09에 `.md` 버전을 받음). 커밋 고정은 불가능하다.
- 라이선스: 상용, **OSS 아님**.

### 문서에 나온 동작
- 단계 사이에 승인 게이트가 있다:
  - [Feature Specs](https://kiro.dev/docs/specs/feature-specs/) 다이어그램: "requirements.md → {Happy?} → no: Edit/Request changes … with edit and request-changes loops at each approval gate."
  - [Requirements-First](https://kiro.dev/docs/specs/feature-specs/requirements-first/): "Once you confirm the requirements, Kiro generates a `design.md`".
- 질문:
  - [Specs](https://kiro.dev/docs/specs/) CLI: "Describe what you want to build or fix. The agent asks clarifying questions if needed."
  - [Quick Spec](https://kiro.dev/docs/specs/quick-spec/): "Kiro asks a set of clarifying questions up front … auto-generates `requirements.md`, `design.md`, and `tasks.md` in sequence, with no approval gates between phases." / "The clarifying questions are the key interaction point. Instead of reviewing and approving each artifact, you front-load your input".
- UI 체크포인트:
  - [Analyze Requirements](https://kiro.dev/docs/specs/analyze-requirements/): "In the Continue dropdown in the editor, alongside **Proceed to Design**".
  - CLI [Spec review](https://kiro.dev/docs/cli/terminal-ui/#spec-review): "When a spec run reaches a phase checkpoint … answering the checkpoint question sends them all to the agent as one revision request."

### Enforcement
**UNVERIFIED**. 소스가 비공개다. IDE의 "Proceed to Design" 버튼이나 CLI 체크포인트가 모델 밖의 UI 상태로 기록되는지, 모델이 이 단계를 건너뛸 수 있는지는 문서로 판단할 수 없다. 체크포인트가 별도 UI 요소로 묘사된 것으로 보아 UI가 주도한다고 추정한다 [INFERENCE].

### Similarity
**Medium**. 단계와 게이트를 제품 UI로 만들었다. "질문을 먼저 받고 이후 게이트 없이 생성"하는 Quick Spec 모드는 agent-flow가 domain-grill 이후 멈추지 않는 구조와 비교해 볼 만하다.

---

## 8. agent-flow에 대한 시사점 (근거 기반 정리)

1. **텍스트 마커와 플래그를 자기 신고하는 방식은 모든 키트에서 실패 이슈가 나왔다.** spec-kit #4582(메인테이너: "not a mechanically enforced approval gate"), OpenSpec #869/#1715/#1084, spec-workflow-mcp #100/#199, cc-sdd(`spec.json`을 LLM이 쓰고 `-y` 제공), claude-code-spec-workflow(`✅ APPROVED` 마커)가 그렇다. agent-flow의 domain-grill Completion Gate 마커도 같은 부류다.
2. **"승인 hash를 host가 직접 넘길 수 있는" 구조를 고친 선례는 세 가지다.**
   - (a) **AI-DLC**: UserPromptSubmit hook이 HUMAN_TURN을 append-only 원장에 기록한다. 엔진은 "게이트 표시 이후의 사람 턴"이 있을 때만 approve와 answer를 받는다. PreToolUse hook이 원장과 우회 env 조작을 막는다. agent-flow의 Claude Code/Codex hook 환경에 가장 직접 옮겨올 수 있는 방식이다.
   - (b) **spec-kit 엔진**: 게이트 입력을 LLM이 아닌 엔진 프로세스의 TTY stdin에서 받고, 비대화형이면 PAUSED로 둔다. 단 이 방식은 엔진이 LLM을 headless 자식 프로세스로 띄울 때 맞는 구조다. agent-flow처럼 host 세션 안에서 돌면 적용하기 어렵다 [INFERENCE].
   - (c) **spec-workflow-mcp**: 별도 대시보드 UI가 승인 상태를 쓰고, 도구는 상태를 읽기만 한다. 로컬 JSON과 무인증 API라서 우회 여지가 남는다.
3. **인터뷰 자체를 코드로 강제하는 곳은 AI-DLC뿐이다.** 질문 파일을 `produces`로 요구하고, Consolidated Summary Confirmation 영수증이 사람 턴에 묶여 있어야 산출물 단계가 진행된다. spec-kit과 OpenSpec은 인터뷰를 선택 사항으로 두거나 "합리적 기본값으로 추측"하라고 명시한다. spec-workflow-mcp와 claude-code-spec-workflow는 인터뷰 대신 사후 리뷰를 한다.
4. **자동화가 질문을 건너뛰게 만든다는 의심은 1차 자료로 뒷받침된다.**
   - spec-kit 내장 workflow는 clarify를 빼고 headless로 돈다.
   - cc-sdd `--auto`와 batch는 `-y`로 승인을 자동화한다.
   - Kiro Quick Spec은 "no approval gates"다.
   - OpenSpec `ff`는 "prefer making reasonable decisions to keep momentum"이다.
   - AI-DLC는 무인 실행 루프가 HUMAN_TURN을 위조하는 경로를 측정한 뒤(10 prompts, 0 humans) `AIDLC_UNATTENDED`로 막았다.
