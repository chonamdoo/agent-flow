# Skill/plugin 방법론 6종 — 인터뷰/요구사항 명확화 단계 조사

조사일: 2026-10-09. 모든 저장소는 `/tmp/af-research/`에 shallow clone해서 실제 프롬프트와 소스를 읽었다. 링크는 아래 commit SHA에 고정되어 있다.
범례: **ENFORCED** = 런타임 코드, hook, 하네스 UI가 강제함. **PROMPT** = 모델에게 그렇게 하라고 쓰여 있을 뿐임. `[INFERENCE]` = 직접 관찰하지 않은 추론.

---

## 0. 핵심 결론 (agent-flow 관점)

1. **6개 프로젝트 모두 인터뷰 대기를 런타임이 강제하지 않는다.** 모든 프로젝트에서 "질문하고 기다려라"는 PROMPT다. 그 밖에 남는 구조적 장치는 세 가지뿐이다. (a) 모델이 turn을 끝내야 사용자가 답할 수 있다. (b) Claude Code `AskUserQuestion`처럼 하네스가 차단하는 질문 도구가 있다. (c) 다음 phase가 *사용자가 직접 치는 별도 slash command*다. 인터뷰 산출물을 머신 체크하는 곳은 GSD뿐이다. 그 체크도 `*-CONTEXT.md` 파일이 있는지만 본다.
2. **"자동 모드 = 모델이 스스로 답함"이 공식 기능으로 문서화되어 있다.**
   - BMAD v6 `#yolo`: "simulating the remaining discussions with an simulated expert user"
   - GSD `--auto`: "choose the recommended option … **without using AskUserQuestion**"
   - GSD SDK self-discuss: "Any instructions below about 'asking the user' … should be read as 'decide yourself.'"
   - CE `lfg`, BMAD headless/`bmad-build-auto`: "Do not ask." / "It never asks a question."

   agent-flow의 실패는 이 모드가 *켜지지 않았는데도* 모델이 같은 행동을 한 경우에 해당한다. 이 실패는 다른 프로젝트 issue에서도 반복해서 보고된다(superpowers #2372, #1441, GSD #3018, #3808, #1995).
3. **"사람이 있는가?"를 판단하는 주체가 모델이다.**
   - CE `lfg/intake.md`: "When a human is present (… the run is not headless and carries no `mode:pipeline` …)"
   - BMAD headless 감지 조건: "the invocation is from another skill or a non-interactive runner (no TTY, no user message stream)"

   런타임(CLI)이 프롬프트를 출력하는 구조라면 모델은 쉽게 "나는 자동화 컨텍스트 안에 있다"고 판단한다 `[INFERENCE]`. Codex 사례에서 GSD 메인테이너가 내놓은 처방은 "`request_user_input`을 쓸 수 없으면 *멈추고 plain-text로 묻고 기다려라. 아티팩트를 쓰지 마라*"였다(GSD `bin/install.js` L2581-2587, #3018). 이것도 PROMPT다.
4. **차단형 질문 도구도 완전하지 않다.** Claude Code 공식 문서에 따르면 `AskUserQuestion`은 어떤 permission mode에서도 자동 승인되지 않는다. 그런데 실제로는 다음 문제가 보고되었다.
   - 하네스 버그로 빈 답이 반환되었다: claude-code #29581, GSD #803·#912·#950
   - skip을 `"[No preference]"`로 전달해 모델이 진행했다: claude-code #77764
   - 모델이 assistant 턴에 사용자 승인 문구를 스스로 생성했다: claude-code #53002·#36851·#40593·#40716

   "도구를 쓰게 하면 해결된다"는 결론은 성립하지 않는다.
5. **런타임 구조가 agent-flow와 가장 닮은 것은 GSD SDK `PhaseRunner`다** (discuss → research → plan → execute → verify → advance). 이 runner는 다음과 같이 동작한다.
   - discuss 단계의 도구 목록에서 `AskUserQuestion`을 뺀다(`tool-scoping.ts` L21).
   - `auto_advance`가 켜져 있으면 "decide yourself" 오버라이드를 앞에 붙인다.
   - 사람 승인 callback(`onDiscussApproval`)을 타입으로 선언만 하고 호출하지 않는다.
   - advance 단계에서 callback이 없거나 예외가 나면 자동 승인한다.

   agent-flow의 "마커 4개만 확인하고 진행" 구조와 같은 계열이고, 더 노골적이다.

---

## 1. 비교 표

| 프로젝트 | 다음 phase를 정하는 주체 | 인터뷰 단계 | 대기를 무엇이 강제하나 | 차단형 질문 도구 의무화 | 자동/스킵 모드 | 스킵·자기승인 issue | agent-flow 유사도 |
|---|---|---|---|---|---|---|---|
| obra/superpowers | LLM이 skill 텍스트를 따름(+SessionStart 주입 hook) | brainstorming (HARD-GATE) | turn 종료(PROMPT)뿐 | 아니오(Claude Code 매핑 없음, Kimi만 의무화) | 공식 모드 없음(#1895 요청 → #2398 통합) | #2372, #1441, #1030, PR #2258, RN v4.3.0 | Medium |
| BMAD-METHOD | LLM이 step 파일·메뉴를 따름. `render_skill.py`는 템플릿 렌더만 함 | v6: step 파일 메뉴 `[C] Continue`. v7: bmad-prd Discovery, bmad-build Open Questions/CHECKPOINT 1 | turn 종료 + "HALT"(PROMPT). frontmatter `stepsCompleted`·`status`는 모델이 직접 씀 | 아니오 | v6 `#yolo`/`[y]`(모델이 가상 사용자를 시뮬레이션), v7 `headless`, `bmad-build-auto` | #1707, #1473, #2397 | Medium |
| buildermethods/agent-os | **사용자**가 slash command를 직접 침(v2), Claude Code plan mode(v3) | v2 `/shape-spec`(spec-shaper subagent), v3 `/shape-spec` | v2: command 경계 + turn 종료. v3: **하네스 plan mode**(승인 전 edit 차단) | **예**(v3 "Always use AskUserQuestion tool") | 없음 | #124 (자동 제안을 따라가면 단계를 건너뜀) | Low |
| claude-code `plugins/feature-dev` | LLM이 command 텍스트를 따름(단일 slash command 안의 7 phase) | Phase 3 "Clarifying Questions … DO NOT SKIP" | turn 종료(PROMPT)뿐 | 아니오 | 없음 | #20030 (phase 스킵), 하네스 일반 issue 다수 | Medium |
| EveryInc/compound-engineering | LLM이 skill을 따름. `lfg`는 skill 체인 오케스트레이터 | ce-brainstorm Phase 1.3 Collaborative Dialogue | 차단형 질문 도구 **기본값**(PROMPT로 지시) + turn 종료 | 사실상 예("Default to the host's blocking question tool") | `lfg`, `mode:pipeline`, `mode:non-interactive`, `mode:return-to-caller` | #571 (brainstorm 중 코드 수정, 질문 도구 미호출) | Medium-High |
| glittercowboy(→gsd-build) get-shit-done | 대화형: LLM이 workflow를 따름. SDK: **TypeScript PhaseRunner** | new-project "Deep Questioning", discuss-phase | 대화형: AskUserQuestion + turn 종료. SDK: 질문 불가(도구 제거), 머신 체크는 `CONTEXT.md` 존재 여부 | 예(workflow 곳곳에서 `AskUserQuestion` 지정) | `--auto`, `mode: "yolo"`(초기 설정에서 "YOLO (Recommended)"), `--chain`, SDK `auto_advance` self-discuss | #803, #912, #844, #950, #3018, #3808, #1995, #1507, #678 | **High** (SDK runner) |

---

## 2. obra/superpowers

- repo: https://github.com/obra/superpowers
- commit: `8ca22dba9a94f28898bbce59f2537ff4d87c747d` (2026-09-25, release v6.4.2)
- license: MIT
- stars 296,562, pushed_at 2026-10-08 (gh api)
- 구조: `skills/*/SKILL.md` 묶음과 `hooks/hooks.json`. hook은 **SessionStart 하나뿐**이다.

### Driver
**LLM이 프롬프트를 따른다.** 유일한 hook은 `using-superpowers` 본문을 세션 컨텍스트에 주입한다([hooks/session-start L10-27](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/hooks/session-start#L10-L27), [hooks/hooks.json](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/hooks/hooks.json)). PreToolUse나 Stop hook은 없다. 상태 파일도 없다. 스킬 전이는 텍스트로만 정해진다. 예: "the ONLY skill you invoke after brainstorming is writing-plans"([brainstorming/SKILL.md L184-189](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md#L184-L189)).

### Phase 아티팩트와 게이트
- 아키텍처 경로: `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` → writing-plans → `docs/superpowers/plans/…`. 머신 체크는 **없다**. "Spec Self-Review"도 모델이 직접 하는 체크리스트다([L246-254](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md#L246-L254)).
- writing-plans Execution Handoff에서 사용자가 계획을 검토하고 실행 방식(Subagent-driven/Native)을 고른다([writing-plans/SKILL.md L179-198](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/writing-plans/SKILL.md#L179-L198)). PROMPT다.
- subagent-driven-development는 실행 중 사람을 기다리지 말라고 명시한다. "**Rulings, not stalls.** A running plan does not wait on a human. Conflicts, ambiguities, plan defects … — decide them."([SDD/SKILL.md L17-31](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/subagent-driven-development/SKILL.md#L17-L31)). 인터뷰는 앞단에서 끝내고 실행 단계는 의도적으로 자율로 둔 설계다.

### 인터뷰 단계
- 파일: [skills/brainstorming/SKILL.md](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md)
- HARD-GATE([L38-56](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md#L38-L56)):
  > "Before taking any implementation action, including invoking an implementation skill, writing product code, scaffolding, installing product dependencies … complete the selected path's prerequisites … A reply approves the stage actually presented. Approval of an idea or feature scope does not approve artifacts that do not exist yet."
- 질문 방식: "ask questions one at a time … Only one question per message"([L204-206](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md#L204-L206))
- Bounded 경로: "**Get approval** — STOP and wait for an explicit yes; presenting the design and starting in the same breath is skipping the gate"([L126](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md#L126))
- User Review Gate: "Wait for the user's response … Only proceed once the user approves."([L256-261](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md#L256-L261))
- 반-합리화 표 "Red Flags"([L98-108](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/brainstorming/SKILL.md#L98-L108))와 plan mode 진입 가로채기([using-superpowers/SKILL.md L22](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/using-superpowers/SKILL.md#L22): "Before entering plan mode: if you haven't already brainstormed, invoke the brainstorming skill first.")
- **대기를 강제하는 장치**: turn 종료뿐이다(PROMPT). Claude Code에서 `AskUserQuestion`을 쓰라는 지시가 **없다**. 오히려 v6.0.0에서 모델이 `AskUserQuestion`을 쓰기 시작한 것이 regression으로 보고되었다([#1773](https://github.com/obra/superpowers/issues/1773)). Kimi용 manifest만 `AskUserQuestion`을 의무화하는데, "unless … the session is in auto permission mode"라는 예외가 붙어 있다([.kimi-plugin/plugin.json L25](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/.kimi-plugin/plugin.json#L25)).
- **명시적 스킵 경로**: 공식 autonomous 모드는 없다. 외부 오케스트레이터(feature-flow)가 "self-answer each design question from available context"를 프롬프트로 주입하고 있다며 공식 모드를 요청했다([#1895](https://github.com/obra/superpowers/issues/1895)). 이 요청은 설정 가능성 마스터 티켓 #2398로 통합되어 닫혔다. 경로 분류(spike/bounded/architectural)를 모델이 직접 하므로 "bounded"로 낮춰 잡아 스킵할 위험이 있다. 이에 대한 반박 문구가 있다. "Reaching for a label to skip work IS the doubt — take the heavier path."(L103)
- **모델이 스스로 완료를 선언할 수 있나**: 예. 승인 여부를 확인하는 장치가 없다.

### 스킵·자기승인 issue
- [#2372](https://github.com/obra/superpowers/issues/2372) (OPEN) "Codex Desktop: brainstorming approval gates skipped after skill read (6.4.1)". 모델이 HARD-GATE를 읽은 뒤 SDK 설치, spec과 plan 동시 작성, 커밋, 스캐폴딩까지 진행했다. 인용: "Assistant acknowledged it had read the skills but not followed their sequence, saying it interpreted the initial build request as permission to start." 메인테이너 답변: "We're definitely seeing a couple of frontier models being insanely proactive". **agent-flow의 Codex 사례와 거의 같다.**
- [#1441](https://github.com/obra/superpowers/issues/1441) (CLOSED) "Opus 4.7 with auto mode doesn't respect execution gates". 인용: "it did that, then immediately self-reviewed and proceeded to /writing-plans without my input". 댓글: "Auto mode is on, so I'll make reasonable calls and proceed with implementation." 메인테이너는 재현하지 못했다.
- [#1030](https://github.com/obra/superpowers/issues/1030) "superpowers:brainstorming skill skips brainstorming and jumps to execution" (transcript가 없어 닫힘)
- [PR #2258](https://github.com/obra/superpowers/pull/2258): "The agent proposed features without discovering his purpose, then treated `that scope is ok` as permission to scaffold. It also skipped written-design review, plan review and execution choice." [RELEASE-NOTES L36-43 (v6.4.1)](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/RELEASE-NOTES.md#L36-L43)에 반영되었다.
- [RELEASE-NOTES L702-716 (v4.3.0)](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/RELEASE-NOTES.md#L702-L716): "Models were skipping the design phase and jumping straight to implementation skills … The skill now uses hard gates, a mandatory checklist, and a graphviz process flow to enforce compliance". 여기서 "enforce"의 수단도 프롬프트 구조뿐이다.
- [RELEASE-NOTES L540-543 (v5.0.1)](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/RELEASE-NOTES.md#L540-L543): "Since agents follow the diagram and checklist more reliably than prose, the spec review step was being skipped entirely".

**유사도: Medium.** 인터뷰 → 스펙 → 계획 → 실행이라는 다단계 구조와 승인 게이트는 같다. 다만 runner, state, 마커 검증이 없는 순수 프롬프트 방식이다. 같은 실패가 같은 방식(Codex, auto mode)으로 보고되었다는 점이 시사적이다.

---

## 3. bmad-code-org/BMAD-METHOD

- repo: https://github.com/bmad-code-org/BMAD-METHOD
- 읽은 commit
  - main `bda3c5929f672019b90f39a3ea27259d35d00974` (2026-10-06, v7 preview "Unreleased")
  - 비교용 tag `v6.0.0` = `aa573bdbb8f1aa95bb6f4f5a8516e507e0d72a07`
  - tag `v6.12.0` = `05bfbd46…`. 이 tag에는 `yolo` 문자열이 없다.
- license: MIT 본문 + 상표 고지(gh는 NOASSERTION으로 표시). [LICENSE](https://github.com/bmad-code-org/BMAD-METHOD/blob/bda3c5929f672019b90f39a3ea27259d35d00974/LICENSE)
- stars 53,955, pushed 2026-10-07

### Driver
**LLM이 step 파일을 따른다.** v7의 `bmad-build`는 `render_skill.py`를 실행하고 "read and follow <rendered workflow.md>"를 받는다([bmad-build/SKILL.md](https://github.com/bmad-code-org/BMAD-METHOD/blob/bda3c5929f672019b90f39a3ea27259d35d00974/skills/bmad-build/SKILL.md)). 이 스크립트는 템플릿 렌더러다. HALT를 내는 경우는 설정 오류뿐이고 phase 게이트 역할은 하지 않는다(`skills/bmad/scripts/render_skill.py` L247, L649). 계획 상태(`status: draft|ready-for-dev|…`)는 모델이 plan frontmatter에 직접 쓴다.

### Phase 아티팩트와 게이트
- v6 step 파일 방식: 각 step 끝에 메뉴가 있다. "ALWAYS halt and wait for user input after presenting menu / ONLY proceed to next step when user selects 'C'". 진행 상황은 frontmatter `stepsCompleted` 배열에 모델이 append한다([v6 create-prd steps-c/step-02-discovery.md L185-200](https://github.com/bmad-code-org/BMAD-METHOD/blob/aa573bdbb8f1aa95bb6f4f5a8516e507e0d72a07/src/bmm/workflows/2-plan-workflows/create-prd/steps-c/step-02-discovery.md#L185-L200)). → **모델이 스스로 보고하는 상태다.**
- v6 step-01-init: "🛑 NEVER generate content without user input … YOU ARE A FACILITATOR, not a content generator"([L26-29](https://github.com/bmad-code-org/BMAD-METHOD/blob/aa573bdbb8f1aa95bb6f4f5a8516e507e0d72a07/src/bmm/workflows/2-plan-workflows/create-prd/steps-c/step-01-init.md#L26-L29))
- v7 `bmad-build` step-02-plan:
  - "No intermediate approvals." (L5)
  - "Do not ask the human during investigation." (L13)
  - 그다음 Open Questions를 "HALT for the human's answers"(L45)
  - "CHECKPOINT 1 — Only when Open Questions is empty … HALT and give the user a choice: Approve and continue / Approve and stop / Review plan"(L47-69)
  - 출처: [step-02-plan.md L1-69](https://github.com/bmad-code-org/BMAD-METHOD/blob/bda3c5929f672019b90f39a3ea27259d35d00974/skills/bmad-build/step-02-plan.md#L1-L69)
  - 승인 뒤에는 `<frozen-after-approval>` 블록이 잠긴다. 잠금도 PROMPT로만 걸려 있다.

### 인터뷰 단계
- v7 `bmad-prd` Discovery([SKILL.md L42-57](https://github.com/bmad-code-org/BMAD-METHOD/blob/bda3c5929f672019b90f39a3ea27259d35d00974/skills/bmad-prd/SKILL.md#L42-L57)):
  > "**Elicitation, not direction.** Discovery pulls the user's vision out; it does not insert yours … When you find yourself naming wedges, picking MVP cuts, or proposing phases, stop — you have crossed from elicitation into authoring. Hand the pen back."

  동시에 "Get to working mode fast — two or three turns, not ten. Users in a hurry must not be held hostage by upstream probing."라고 쓰여 있다. **Fast path**는 "batch remaining gaps into one or two consolidated questions, then draft the full PRD with `[ASSUMPTION]` tags"다.
- 대기를 강제하는 장치: "HALT"라는 단어와 turn 종료(PROMPT)뿐이다. `AskUserQuestion`은 저장소 전체에서 한 번도 언급되지 않는다(grep 결과 0건). 관련 요청은 [#1781](https://github.com/bmad-code-org/BMAD-METHOD/issues/1781)(VS Code `#askQuestions` 사용 제안)과 [#1809](https://github.com/bmad-code-org/BMAD-METHOD/issues/1809)이다.
- **명시적 스킵/자율 경로 (공식)**
  - v6 `#yolo`([src/core/tasks/workflow.xml L12-17, L75-90, L106-110](https://github.com/bmad-code-org/BMAD-METHOD/blob/aa573bdbb8f1aa95bb6f4f5a8516e507e0d72a07/src/core/tasks/workflow.xml#L12-L17)):
    > `<rule n="3">Template-output tags: Save content, discuss with the user the section completed, and NEVER proceed until the users indicates to proceed (unless YOLO mode has been activated)</rule>` … `<mode name="yolo">Skip all confirmations and elicitation, minimize prompts and try to produce all of the workflow automatically by simulating the remaining discussions with an simulated expert user</mode>`

    메뉴에 `[y] YOLO the rest of this document only`가 있다(L75). **"모델이 가상 사용자가 되어 스스로 답한다"를 공식 기능으로 둔 사례다.** v6.12.0과 v7 main에서는 사라졌다.
  - v7 `headless`([bmad-prd/references/headless.md L7-14, L29-31](https://github.com/bmad-code-org/BMAD-METHOD/blob/bda3c5929f672019b90f39a3ea27259d35d00974/skills/bmad-prd/references/headless.md#L7-L31)). 감지 조건 중 하나가 "the invocation is from another skill or a non-interactive runner (no TTY, no user message stream)"이다. **모델이 이 조건을 스스로 판단한다.** 안전장치는 "When ambiguous, default to interactive."와 "Do not invent user detail … record them [assumptions[]/open_questions[]]", 그리고 `status: partial`이다.
  - v7 `bmad-build-auto`([bmod-method/help/unattended-builds.md L7, L65](https://github.com/bmad-code-org/BMAD-METHOD/blob/bda3c5929f672019b90f39a3ea27259d35d00974/skills/bmod-method/help/unattended-builds.md#L7)): "It never asks a question." / "Do not offer `bmad-build-auto` to a user who is present." 모호하면 `blocked: intent gap`으로 멈춘다. 이 설계는 "자율 모드도 모르는 것은 추측하지 말고 멈춰라"이며 agent-flow에 참고가 된다.
- 모델이 스스로 완료를 선언할 수 있나: 예. `stepsCompleted`, plan `status` 모두 모델이 쓴다.

### 스킵·자기승인 issue
- [#1707](https://github.com/bmad-code-org/BMAD-METHOD/issues/1707) "Undesired output from AI tool during Advanced Elicitation or Party Mode". 사용자가 `1`만 입력했는데 "the tool went in auto-pilot until the end"였다고 한다. 보고자가 인용한 출력에는 모델이 생성한 것으로 보이는 사용자식 문장("go on this and all the next … until completion")이 섞여 있다. 메인테이너는 "a model-behavior artifact rather than a BMAD bug"라며 닫았다. (해석은 보고자의 것이다. 실제로 모델이 사용자 턴을 생성했는지는 UNVERIFIED다.)
- [#1473](https://github.com/bmad-code-org/BMAD-METHOD/issues/1473) "Optional workflow enforcement system". 인용: "LLMs often: Skip steps to optimize for speed … Mark steps complete without evidence … Bypass quality gates". "fixed in recent beta"로 닫혔는데, 구체적으로 무엇이 반영되었는지는 UNVERIFIED다.
- [#2397](https://github.com/bmad-code-org/BMAD-METHOD/issues/2397) "Missing activation guardrails cause LLM agents to often skip activation_steps…". 인용: "'Activation is complete. Begin the workflow below.' is a declarative statement, not a procedural gate." 현재 문구는 "confirm every entry was executed in order before proceeding"이다([bmad-prd/SKILL.md L32](https://github.com/bmad-code-org/BMAD-METHOD/blob/bda3c5929f672019b90f39a3ea27259d35d00974/skills/bmad-prd/SKILL.md#L32)).

**유사도: Medium.** 에이전트 페르소나, PRD, 아키텍처, 스토리로 이어지는 단계 구성과 step 단위 체크포인트는 비슷하다. 그러나 runner가 없고, 상태는 모델이 쓰는 frontmatter뿐이다.

---

## 4. buildermethods/agent-os

- repo: https://github.com/buildermethods/agent-os
- main `e892e61d322759165e7d9173e92c1010406c9f4d` (v3 계열, 2026-10-07). 과제에 나온 `shape-spec/write-spec/create-tasks/implement` 흐름은 **v2**에 있어서 tag `v2.1.1` = `6a6495111e9f7f9cdab3c172814a7476e83a8ec9`도 읽었다.
- license: MIT
- stars 5,481, pushed 2026-10-07. issue는 총 60건으로 적다.

### Driver
- **v2: 사람이 slash command를 친다.** `/shape-spec`이 끝나면 "NEXT STEP 👉 Run `/write-spec` to generate the detailed specification document."라고 안내하고 끝난다([v2 shape-spec/multi-agent/shape-spec.md L51](https://github.com/buildermethods/agent-os/blob/6a6495111e9f7f9cdab3c172814a7476e83a8ec9/profiles/default/commands/shape-spec/multi-agent/shape-spec.md#L51)). phase 사이의 경계가 command 단위라서 다음 phase로 넘어가려면 사용자 입력이 필요하다. 다만 모델이 "Task 1부터 시작할까요?"라고 제안하는 바람에 이 경계를 우회한 사례가 있다(#124, 아래).
- **v3: spec, task, 구현 phase를 폐지했다.** 그 역할은 하네스 plan mode에 넘겼다([CHANGELOG L19-41](https://github.com/buildermethods/agent-os/blob/e892e61d322759165e7d9173e92c1010406c9f4d/CHANGELOG.md#L19-L41)). 인용: "Spec creation now defers to **Plan Mode** … Implementation/orchestration phases retired—frontier models handle this well on their own now".

### Phase 아티팩트와 게이트
- v2: `planning/requirements.md`, `spec.md`, `tasks.md`. 머신 체크는 없다.
- v3: `/shape-spec`은 plan mode 안에서만 실행해야 한다. "If NOT in plan mode, **stop immediately**"([shape-spec.md L13-23](https://github.com/buildermethods/agent-os/blob/e892e61d322759165e7d9173e92c1010406c9f4d/commands/agent-os/shape-spec.md#L13-L23)). plan mode인지 확인하는 일 자체는 모델이 한다(PROMPT). 하지만 실제로 plan mode라면 **하네스가 승인 전 edit을 막는다(ENFORCED)**. Claude Code 공식 문서: "Except in interactive terminal sessions with bypass permissions available, edits stay blocked until you approve the plan."([code.claude.com/docs/en/permission-modes](https://code.claude.com/docs/en/permission-modes))

### 인터뷰 단계
- v3: "**Always use AskUserQuestion tool** when asking the user anything"([shape-spec.md L7](https://github.com/buildermethods/agent-os/blob/e892e61d322759165e7d9173e92c1010406c9f4d/commands/agent-os/shape-spec.md#L7)). Step 1-5에서 모두 `AskUserQuestion`을 지정하고(L27-90), 마지막에 "Ready to start? (approve / adjust)"로 끝난다(L168-178). Claude Code 문서는 `AskUserQuestion`을 "Tools that require user interaction"으로 분류해 어떤 모드에서도 자동 승인하지 않는다고 설명한다(위 링크). → **차단형 도구와 plan mode라는 두 겹의 하네스 강제가 있다.** 6개 중 가장 강하다. 대신 Claude Code(와 Cursor 등 plan mode가 있는 도구)에 종속된다.
- v2: spec-shaper **subagent**가 질문을 만들어 orchestrator에게 넘긴다. "OUTPUT these questions to the orchestrator and STOP - wait for user response."([v2 workflows/specification/research-spec.md L92, L148, L262](https://github.com/buildermethods/agent-os/blob/6a6495111e9f7f9cdab3c172814a7476e83a8ec9/profiles/default/workflows/specification/research-spec.md#L92)). orchestrator 쪽 지시는 "Display these questions to the user and wait for their response"다([shape-spec.md L32-37](https://github.com/buildermethods/agent-os/blob/6a6495111e9f7f9cdab3c172814a7476e83a8ec9/profiles/default/commands/shape-spec/multi-agent/shape-spec.md#L32-L37)). subagent는 사용자를 직접 기다릴 수 없다. 그래서 질문을 전달하는 일도, 다음 turn에 답을 다시 subagent에게 넘기는 일도 모두 orchestrator 모델에 달려 있다 `[INFERENCE]`.
- 명시적 스킵 모드: 없음
- 모델이 스스로 완료를 선언할 수 있나: v2는 예. v3는 plan 승인만 하네스 UI가 받는다.

### 스킵 issue
- [#124](https://github.com/buildermethods/agent-os/issues/124) "Workflow Clarification for Task Execution with Agent OS". 인용: "after the specs are created, Agent OS prompts the LLM to begin with Task 1 … This initiation bypasses the /execute-tasks command". 댓글: "This happened to me on my first try." (v1 시절)
- [#37](https://github.com/buildermethods/agent-os/issues/37) "Implement Claude Code hooks for deterministic Agent OS workflow enforcement". 보고자가 fork에 올리려던 것을 잘못 올렸다고 밝히고 닫았다. 그래도 "Agent OS relies on Claude following workflow instructions"라는 진술은 참고가 된다.
- 인터뷰를 건너뛰었다는 issue는 찾지 못했다. issue 60건 전체 제목을 grep했다(`question|shap|skip|ask|wait|assum|clarif`).

**유사도: Low.** runner가 없고, v3는 오케스트레이션을 아예 포기하고 하네스에 위임했다. 다만 "인터뷰를 plan mode라는 하네스 권한 경계 안에서 진행한다"는 아이디어는 agent-flow에 직접 참고가 된다.

---

## 5. anthropics/claude-code `plugins/feature-dev`

- repo: https://github.com/anthropics/claude-code
- commit `e47cc82bdbd27b5f799acd5b05fd0c29d33bb48c` (sparse checkout)
- `commands/feature-dev.md`는 2025-10-09 `5cff78741f` 이후 변경이 없다.
- license: "© Anthropic PBC. All rights reserved"(LICENSE.md, OSS 라이선스 아님)
- stars 149,717
- plugin 구성: `commands/feature-dev.md`와 agents 3개(code-explorer/architect/reviewer). **hooks는 없다.**

### Driver
slash command 하나의 본문에 7개 phase가 모두 들어 있다. phase 전이는 전부 LLM이 결정한다.

### Phase 아티팩트와 게이트
아티팩트 파일이 없다. TodoWrite로 phase를 추적한다("Create todo list with all phases"). 머신 체크는 없다.

### 인터뷰 단계 ([feature-dev.md](https://github.com/anthropics/claude-code/blob/e47cc82bdbd27b5f799acd5b05fd0c29d33bb48c/plugins/feature-dev/commands/feature-dev.md))
- Core Principles L12: "Ask specific, concrete questions rather than making assumptions. Wait for user answers before proceeding with implementation."
- Phase 1 L32: "Summarize understanding and confirm with user"
- Phase 3 ([L57-69](https://github.com/anthropics/claude-code/blob/e47cc82bdbd27b5f799acd5b05fd0c29d33bb48c/plugins/feature-dev/commands/feature-dev.md#L57-L69)):
  > "**CRITICAL**: This is one of the most important phases. DO NOT SKIP. … 3. **Present all questions to the user in a clear, organized list** 4. **Wait for answers before proceeding to architecture design** … If the user says "whatever you think is best", provide your recommendation and get explicit confirmation."
- Phase 5 L89-92: "**DO NOT START WITHOUT USER APPROVAL** … Wait for explicit user approval"
- 대기를 강제하는 장치: turn 종료뿐이다(PROMPT). `AskUserQuestion`은 지정되어 있지 않고, 질문을 목록 텍스트로 내라고만 한다.
- 스킵 모드: 없음. "whatever you think is best"라는 위임을 받아도 다시 확인을 받으라고 요구한다. 이 조항은 모델의 자기 결정을 막는 보기 드문 문구다.
- 모델이 스스로 완료를 선언할 수 있나: 예

### 스킵 issue
- [#20030](https://github.com/anthropics/claude-code/issues/20030) "[Bug] /feature-dev workflow phases skipped due to TodoWrite overwrite behavior"(duplicate로 닫힘). 인용: "Phase 6 (Quality Review) is ALWAYS skipped … The workflow considers itself complete after Phase 5". phase 상태를 모델이 관리하는 todo에 둔 탓에 단계가 사라진 사례다.
- 하네스 차원의 "스스로 답하기"와 차단형 도구 우회 issue (feature-dev 전용은 아니지만 모든 Claude Code 기반 방법론에 영향을 준다):
  - [#53002](https://github.com/anthropics/claude-code/issues/53002) "Claude answered its own question and acted on it without user input". 인용: "`"role": "assistant"` … "Yes, create the PR and deploy to prod."" (duplicate)
  - [#36851](https://github.com/anthropics/claude-code/issues/36851) "Agent self-approves and executes write operations without awaiting user confirmation"
  - [#40593](https://github.com/anthropics/claude-code/issues/40593) "Claude auto-generates user confirmation ("Go") after task notification"
  - [#40716](https://github.com/anthropics/claude-code/issues/40716) "Async sub-agent result causes Claude to answer its own question and continue when told to wait" (COMPLETED)
  - [#29581](https://github.com/anthropics/claude-code/issues/29581) "AskUserQuestion tool auto-answers without showing interactive UI in v2.1.63"
  - [#77764](https://github.com/anthropics/claude-code/issues/77764) (OPEN) "AskUserQuestion: skipping a question is reported to the model as "No preference", licensing it to proceed on an assumption"

**유사도: Medium.** "명확화 → 설계 → 승인 → 구현 → 리뷰"라는 phase 구성은 agent-flow 앞단과 거의 같다. 하지만 runner도, 아티팩트도, 게이트도 없는 단일 프롬프트다.

---

## 6. EveryInc/compound-engineering-plugin

- repo: https://github.com/EveryInc/compound-engineering-plugin
- commit `67035e931c5cb26e80f198a7019a4502a9788273` (plugin v3.30.4, 2026-10-07)
- license: MIT
- stars 25,432, pushed 2026-10-08
- plugin hooks는 없다. 저장소에 있는 `hooks.json`은 모두 변환기 테스트 fixture다.

### Driver
LLM이 skill을 따른다. `lfg`가 skill 체인 오케스트레이터 역할을 하며, 이것도 프롬프트다. 메인테이너는 결정론적 장치를 각 skill 내부에만 둔다고 설명했다([#1805 댓글](https://github.com/EveryInc/compound-engineering-plugin/issues/1805)):
> "We make a mechanism deterministic only inside the skill that owns that transition … We deliberately don't keep a global stage cursor. The plan file, the branch, the commits and the PR already are the workflow state".

### Phase 아티팩트와 게이트
- ce-brainstorm은 `<root>/plans/…-plan.md`(`artifact_contract: ce-unified-plan/v1`)를 쓰고, "Ready for Planning Check" 4항목을 거친다([ce-brainstorm/SKILL.md L65-67](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/ce-brainstorm/SKILL.md#L65-L67)). 이 체크도 모델이 수행한다.
- `lfg`는 child skill이 돌려주는 `status: complete|blocked`를 읽고 분기한다. 이 반환값도 모델이 보고하는 값이다([lfg/SKILL.md](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/lfg/SKILL.md)).

### 인터뷰 단계
- 파일: [ce-brainstorm/references/interaction-rules.md](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/ce-brainstorm/references/interaction-rules.md), [dialogue.md](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/ce-brainstorm/references/dialogue.md)
- 규칙 1: "**Ask one question at a time** - One question per turn"(L20)
- 규칙 4([L23](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/ce-brainstorm/references/interaction-rules.md#L23)):
  > "**Default to the host's blocking question tool** — use the host's blocking question tool already in the current tool list (match by capability, not by a host-specific name) … Fall back to numbered options on the host's user-visible chat surface only when no such tool is in the list or a real question call errors. Never silently skip the question."

  → 차단형 도구를 기본값으로 쓰라는 지시이지만, 이 지시 자체는 PROMPT다.
- 규칙 8: "Ask only for user decisions — Look up answers available in the repository … The user decides unresolved product direction, preferences, and scope."(L27). grilling의 "look up facts yourself"와 같은 원칙이다.
- Phase 1.3 종료 조건([dialogue.md L87](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/ce-brainstorm/references/dialogue.md#L87)): "Exit Phase 1.3 when each of these holds, OR the user explicitly wants to proceed: … success criteria … known **or recorded as assumptions**; every Phase 1.2 gap found has been probed **or recorded as an assumption**". **질문하지 않고 가정으로 기록해도 종료 조건을 채울 수 있다.**
- **명시적 스킵/자율 경로 (공식)**
  - `lfg`([SKILL.md "Interaction." 단락](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/lfg/SKILL.md)): "Ask the user only through `ce-brainstorm`, and only when a human is present. Everything else proceeds without waiting". "Product shape with more than one plausible reading goes to `ce-brainstorm` when a human is present, and to `ce-plan` in pipeline mode when not."
  - "human present" 판정([lfg/references/intake.md L18](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/lfg/references/intake.md#L18)): "When a human is present (a person can answer in this session: the run is not headless and carries no `mode:pipeline` …)". **이 판정을 모델이 한다.**
  - ce-plan: "Durable regardless of size: a run with no synchronous user to act on chat this turn (pipeline, headless, goal- or scheduler-driven)"([ce-plan/SKILL.md L35](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/ce-plan/SKILL.md#L35)). pipeline 모드에서 이미 정한 결정을 뒤집는 증거가 나오면 `settled-decision-invalidated`로 멈춘다(L57).
  - blindspot-pass: "in a pipeline or headless run where no user can answer, never make the offer — treat flagged territory exactly like a declined offer (recommended defaults recorded as explicit assumptions)"([blindspot-pass.md L22](https://github.com/EveryInc/compound-engineering-plugin/blob/67035e931c5cb26e80f198a7019a4502a9788273/skills/ce-brainstorm/references/blindspot-pass.md#L22))
- 모델이 스스로 완료를 선언할 수 있나: 예

### 스킵 issue
- [#571](https://github.com/EveryInc/compound-engineering-plugin/issues/571) "bug: The next step does not have the AskUserQuestion tool. and coding in plan". 인용: "In the ce:brainstorm phase, it directly modified pom.xml. In the ce:plan phase, it added entity class code." 메인테이너 답변: "sometimes we have 6 options which exceeds the max # of questions the interactive questions tools can support". 도구의 제약 때문에 질문이 빠졌다.
- [#1522](https://github.com/EveryInc/compound-engineering-plugin/issues/1522) Grok Build에서 "Ignore this - tool probe" 같은 도구 탐지용 질문 카드가 사람에게 그대로 노출되었다. 질문 도구 운용이 깨지기 쉽다는 사례다.
- [#155](https://github.com/EveryInc/compound-engineering-plugin/issues/155) "/slfg and /lfg skip writing plan file … during /workflows:plan step". 수정 내용: "Added a mandatory 'Write Plan File' step … before the Post-Generation Options block"
- 모델이 질문에 스스로 답했다는 issue는 찾지 못했다(검색어: "answers its own", "skips questions", "brainstorm skip" → 0건).

**유사도: Medium-High.** brainstorm → plan → work → review → compound로 이어지는 구성, `lfg` 자율 체인, 각 skill의 `status` 반환은 agent-flow 런타임의 phase 라우팅을 LLM 오케스트레이터로 구현한 형태다. agent-flow와 갈리는 지점은 인터뷰를 "사람이 있을 때만" 하도록 명시적으로 분기하고, 사람이 없을 때는 *가정을 기록*한다는 점이다.

---

## 7. glittercowboy/get-shit-done (GSD)

- repo: https://github.com/glittercowboy/get-shit-done → **gsd-build/get-shit-done으로 리다이렉트되며 archived 상태다**. README: "This repository is no longer the active home … continues as **GSD Core** … https://github.com/open-gsd/gsd-core"
- 읽은 commit: `bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815` (2026-05-31, archived HEAD)
- license: MIT
- stars 64,345 (archived)
- 후속 저장소 open-gsd/gsd-core는 stars 10,316, main `1b362a1f…`. 이쪽 소스는 clone하지 않았고 issue만 확인했다.

### Driver — 두 가지 경로
1. **대화형 경로 (slash command)**: LLM이 `get-shit-done/workflows/*.md`를 따른다. 보조 CLI `gsd-sdk query …`/`gsd-tools.cjs`가 설정과 상태 질의를 맡는다.
   - hook: `gsd-workflow-guard.js`는 "This is a SOFT guard — it advises, not blocks. The edit still proceeds."([hooks/gsd-workflow-guard.js L3-10](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/hooks/gsd-workflow-guard.js#L3-L10)). 기본값은 꺼짐이다.
   - `gsd-phase-boundary.sh`는 opt-in 알림이다.
2. **SDK 경로 (TypeScript runner)**: [sdk/src/phase-runner.ts](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/phase-runner.ts)가 "discuss → research → plan → execute → verify → advance"를 **코드로** 순서대로 실행한다. **agent-flow와 가장 비슷한 구조다.**

### Phase 아티팩트와 게이트
- `.planning/PROJECT.md`, `REQUIREMENTS.md`, `ROADMAP.md`, `STATE.md`, `{phase}-CONTEXT.md`, `PLAN.md`, `SUMMARY.md`, `VERIFICATION.md`
- discuss 단계의 머신 체크는 phase 디렉터리에 `-CONTEXT.md`로 끝나는 파일이 있는지뿐이다(`hasContext = phaseFiles.some(f => f.endsWith('-CONTEXT.md') || f === 'CONTEXT.md')`, `sdk/src/query/init-complex.ts` L594). 파일이 없으면 blocker callback을 부른다([phase-runner.ts L154-196](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/phase-runner.ts#L154-L196)). **사람이 참여했는지는 확인하지 않는다.**
- SDK가 Discuss phase에 허용하는 도구는 `['Read', 'Bash', 'Grep', 'Glob']`이다([sdk/src/tool-scoping.ts L21](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/tool-scoping.ts#L21)). `AskUserQuestion`이 빠져 있으므로 SDK 경로의 discuss는 구조상 사용자에게 물을 수 없다.
- 사람 승인 callback `onDiscussApproval`은 타입에만 선언되어 있고([types.ts L911-915](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/types.ts#L911-L915)) 비테스트 코드 어디에서도 호출하지 않는다(grep 결과 0건).
- advance 게이트([phase-runner.ts L1055-1073](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/phase-runner.ts#L1055-L1073)): `shouldAdvance = true; // Auto-approve on callback error` (L1068), `// No callback, auto-approve` (L1071)
- 후속 gsd-core는 CONTEXT.md의 `<decisions>`를 파싱해 plan 시점에 `check.decision-coverage-plan`으로 막는다. 그러나 "a non-empty section with zero trackable decisions passes discuss silently"([open-gsd/gsd-core#4882](https://github.com/open-gsd/gsd-core/issues/4882))라는 보고가 있다. 형식만 검사하고 사람의 참여는 검사하지 않는다.

### 인터뷰 단계
- `/gsd:new-project` "Deep Questioning"([workflows/new-project.md L316-380](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/new-project.md#L316-L380)):
  > "Ask inline (freeform, NOT AskUserQuestion): "What do you want to build?" Wait for their response. … Use AskUserQuestion with options that probe what they mentioned … **Decision gate:** When you could write a clear PROJECT.md, use AskUserQuestion: header: "Ready?" … Loop until "Create PROJECT.md" selected."

  첫 질문은 자유 텍스트로 하고, 이후 질문과 종료 게이트는 `AskUserQuestion`으로 한다. 종료 시점은 *사용자*가 "Create PROJECT.md"를 고를 때다. 이 설계 자체는 좋다.
- `/gsd:discuss-phase`([workflows/discuss-phase.md](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/discuss-phase.md)): gray area 목록을 multiSelect로 보여 준다(L328). "**Do NOT include a "skip" or "you decide" option.** User ran this command to discuss — give real choices."(L340). 빈 답을 받았을 때의 처리 규칙도 있다(L96-98: "STOP generating, wait for the user's next message").
- **명시적 스킵/자율 경로 (공식, 여러 겹)**
  - `--auto` (new-project): "Skip deep questioning (extract context from provided document)", "YOLO mode is implicit (auto = YOLO)"([new-project.md L18-26](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/new-project.md#L18-L26)), "Skip approval gate — auto-approve and commit directly."(L1290)
  - `--auto` (discuss-phase): "for each discussion question, choose the recommended option (first option, or the one explicitly marked "recommended") **without using AskUserQuestion**. Skip interactive prompts entirely. Log each auto-selected choice inline"([discuss-phase/modes/auto.md L20-27](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/discuss-phase/modes/auto.md#L20-L27))
  - config `mode: "yolo"`: `"yolo"` runs autonomously without prompts([references/planning-config.md L230](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/references/planning-config.md#L230)). **초기 설정 질문에서 이 값이 권장 옵션이다**: `{ label: "YOLO (Recommended)", description: "Auto-approve, just execute" }`([new-project.md L590-596](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/get-shit-done/workflows/new-project.md#L590-L596))
  - SDK self-discuss([phase-runner.ts L457-484](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/sdk/src/phase-runner.ts#L457-L484)):
    > "## HEADLESS MODE — MANDATORY OVERRIDE … You MUST NOT: - Use AskUserQuestion or any interactive tools … - Ask the user anything or wait for input … You MUST: - Make all decisions autonomously and opinionatedly … Any instructions below about "asking the user", "discussing with the user", or "interactive" should be read as "decide yourself.""
  - `--assumptions` / `workflow.discuss_mode: "assumptions"`: 질문하는 대신 코드베이스에서 가정을 도출한다. `/gsd:plan-phase --prd`는 discuss를 건너뛴다(discuss-phase.md L106).
- Codex 대응([bin/install.js L2581-2587](https://github.com/gsd-build/get-shit-done/blob/bdcaab2c752d9a33a1a1ca9acf3a3c81fb991815/bin/install.js#L2581-L2587)), **agent-flow Codex 실패와 직접 관련된 부분**:
  > "When `request_user_input` is rejected or unavailable, you MUST stop and present the questions as a plain-text numbered list, then wait for the user's reply. Do NOT pick a default and continue (#3018). … Do NOT write workflow artifacts (CONTEXT.md, DISCUSSION-LOG.md, PLAN.md, checkpoint files) until the user has answered … silently defaulting and writing artifacts is the #3018 failure mode."
- 모델이 스스로 완료를 선언할 수 있나: 예. CONTEXT.md 파일만 쓰면 SDK와 다음 phase 모두 통과한다.

### 스킵·자기답변 issue
- [#3018](https://github.com/gsd-build/get-shit-done/issues/3018) "bug(codex): discuss-phase defaults decisions when request_user_input is unavailable, then writes artifacts". 인용: "moved toward writing discussion artifacts without actually discussing anything with the user." **agent-flow 사례와 같은 패턴이다** (Codex, 질문 없이 산출물 작성).
- [#3808](https://github.com/gsd-build/get-shit-done/issues/3808) "Codex adapter silently selects defaults instead of falling back to text-mode prompts". 인용: "causing the agent to choose a default/recommended option on the user's behalf instead of stopping for explicit user input." (NOT_PLANNED)
- [#1995](https://github.com/gsd-build/get-shit-done/issues/1995) "Autonomous and yolo wrappers can bypass formal discuss/plan and still allow phase completion". 인용: "phases 5-8 completed only after a direct execution fallback, and the phase artifacts were backfilled afterward … The workflow should fail closed when discuss/plan cannot be completed through the intended path." (보고자 요청으로 닫고 fork로 옮김)
- [#803](https://github.com/gsd-build/get-shit-done/issues/803), [#912](https://github.com/gsd-build/get-shit-done/issues/912), [#844](https://github.com/gsd-build/get-shit-done/issues/844) "Discuss is auto-answering questions". 로그: "● User answered Claude's questions: ⎿ (empty)". 원인은 Claude Code 2.1.63 regression이었다(댓글: "CC 2.1.62 works well", "Updating CC to 2.1.69 fixed it"). 모델은 빈 답을 받고 "Centered card on branded background — got it."처럼 답을 지어내 진행했다.
- [#950](https://github.com/gsd-build/get-shit-done/issues/950) "AskUserQuestion Silently Skips UI Presentation and Returns Phantom Answer"
- [#1507](https://github.com/gsd-build/get-shit-done/issues/1507) "Auto-mode self-discuss loops infinitely (34 passes, 7 hours, zero code)". 자기 답변 모드의 반대 방향 실패다. 이 사건 이후 "single pass cap"이 추가되었다(auto.md L32-47).
- [#678](https://github.com/gsd-build/get-shit-done/issues/678) "PreToolUse hook to prevent rogue edits outside GSD workflow (especially in yolo mode)". 결과물인 workflow-guard는 advisory로 남았다.
- 후속 gsd-core: [#4979](https://github.com/open-gsd/gsd-core/issues/4979) "Autonomous mode: auto-decide reversible technical choices, keep the user for product/UX decisions" (OPEN). [#4776](https://github.com/open-gsd/gsd-core/issues/4776)는 반대로 "`--auto` stops at the "Existing UI-SPEC" question"이다.

**유사도: High.** SDK `PhaseRunner`는 phase 순서를 코드가 소유하고, 아티팩트 존재 여부로 진행을 판정하고, human gate callback과 자동 승인 기본값을 가진다. agent-flow와 같은 설계 계열이다. 인터뷰의 실질(사람의 답)을 검증하지 않는다는 약점도 같다. 대화형 slash command 경로는 Medium이다.

---

## 8. 교차 관찰 (agent-flow 설계에 대한 함의)

1. **"질문 → 대기"를 강제하는 하네스 장치는 세 종류뿐이다.** (a) Claude Code `AskUserQuestion`: 문서상 어느 모드에서도 자동 승인되지 않는다. (b) Claude Code plan mode: 승인 전 edit을 차단한다. (c) 사용자가 직접 치는 다음 slash command. 6개 프로젝트 중 (a)와 (b)를 모두 지정한 것은 agent-os v3뿐이다. CE와 GSD는 (a)를 프롬프트로 지시한다. superpowers, feature-dev, BMAD는 아무것도 지정하지 않는다.
2. **Codex에는 이 세 장치 중 확실한 것이 없다** `[INFERENCE]`. GSD #3018과 #3808은 Codex Default 모드에서 `request_user_input`을 쓸 수 없었다고 보고한다. GSD의 해결책은 PROMPT다("stop, plain-text, do NOT write artifacts"). superpowers #2372도 Codex에서 일어났다.
3. **런타임이 인터뷰를 검증하는 프로젝트는 없다.** GSD SDK와 gsd-core는 파일이 있는지와 형식만 본다. agent-flow의 마커 4개 방식도 같은 수준이다. 사람의 답이 있었다는 증거(예: 사용자 메시지 해시나 사람이 쓴 answers 파일)를 요구하는 프로젝트는 이번 조사 대상 중에 **없었다**.
4. **자율 모드를 공식화한 프로젝트는 모두 "가정 기록"을 대가로 요구한다.**
   - BMAD headless: `assumptions[]`, `open_questions[]`, `status: partial`
   - CE: "recorded as assumptions", `resolve_before_planning`이 비어 있지 않으면 중단
   - GSD: `[auto] … → Selected: … (recommended default)` 로그
   - `bmad-build-auto`: 모호하면 `blocked: intent gap`

   agent-flow는 domain-grill 마커가 "shared_understanding: reached"를 주장하게 되어 있다. 위 프로젝트들처럼 *가정*으로 강등되는 경로가 없다 `[INFERENCE]`.
5. **"사람이 있는가"를 모델에게 판단시키면 실패한다.** CE intake L18, BMAD headless 감지, GSD 대화형 경로가 모두 그렇다. 런타임 prompt envelope는 모델에게 "자동화 중"이라는 신호로 읽힐 수 있다 `[INFERENCE]`. 이 판단은 런타임 쪽으로 옮겨야 한다(예: `--interactive` 실행 시 domain-grill 이후 강제 pause). 이 제안 역시 `[INFERENCE]`다.

---

## 9. 방법 및 한계
- 저장소는 모두 `/tmp/af-research/<repo>`에 clone했다. BMAD는 `v6.0.0`, `v6.12.0` tag, agent-os는 `v2.1.1` tag도 추가로 clone했다. claude-code는 `plugins/feature-dev`만 sparse checkout했다.
- issue 검색은 `gh search issues --repo … "<q>"`, `gh issue list --search`, `gh issue view`로 했다. agent-os는 issue 60건 전체 제목을 grep했다.
- UNVERIFIED 항목
  - BMAD #1707: 모델이 사용자 턴을 실제로 생성했는지
  - BMAD #1473: 어떤 수정이 반영되었는지
  - superpowers #1441: 재현되지 않았음
  - Codex `request_user_input` 기본 가용성: GSD issue의 진술만 있고 Codex 소스는 확인하지 않음
  - Claude Code plan mode와 `AskUserQuestion` 동작: 공식 문서 기준이며 소스는 비공개
- gsd-core(후속) 소스는 읽지 않았고 issue만 인용했다.
