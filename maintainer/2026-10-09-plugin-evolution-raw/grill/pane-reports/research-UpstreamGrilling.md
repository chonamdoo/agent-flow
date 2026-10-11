# mattpocock/skills: grilling의 원본 동작과 agent-flow 임베딩 시 의미 변화

## 0. 메타

| 항목 | 값 |
| --- | --- |
| repo | https://github.com/mattpocock/skills |
| 읽은 HEAD | `b0618bc436ad893b3c5e84e55fba86586d34a404` (2026-10-08 09:44 +0100) |
| 로컬 pin | `4588b32ecab9ecc9fc8cc6b6c5e7d675b6004b0d` (2026-10-05). pin 이후 HEAD까지 40 commits |
| license | MIT (`LICENSE`: "MIT License / Copyright (c) 2026 Matt Pocock"). `gh repo view`의 licenseInfo는 null로 나왔고, 판정은 파일 내용으로 함 |
| maturity | `gh repo view` 기준 stars 281,041, pushedAt 2026-10-08T08:49:07Z |

Permalink 접두사:
- HEAD: `https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/`
- pin: `https://github.com/mattpocock/skills/blob/4588b32ecab9ecc9fc8cc6b6c5e7d675b6004b0d/`

아래 `H:`는 HEAD 접두사, `P:`는 pin 접두사를 뜻한다.

### 프로젝트 요약 카드 (output contract)

- **Driver**: 런타임이 없다. 다음 단계는 **사람이 slash command를 쳐서** 고른다. 체인의 다음 skill(`to-spec`, `to-tickets`, `implement`, `implement-spec`)은 모두 `disable-model-invocation: true`라서 모델은 그 skill을 부를 수 없다. 모델이 따르는 prompt 텍스트가 있을 뿐이고, phase 전이 권한은 사람 쪽에 있다.
- **Phase artifacts / gates**: 기계 검사가 전혀 없다. grilling의 종료 조건은 prompt 문장이고("Do not act on it until the user confirms…"), 그 문장을 지키게 하는 장치는 harness의 turn 경계뿐이다. 실제로 진행을 막는 장치는 *invocation 정책*(model-invocation 차단) 하나다.
- **Interview step**: 있다(`skills/productivity/grilling/SKILL.md`). 대기는 **turn end**에 맡긴다. 질문은 plain chat text로 하고, AskUserQuestion 같은 tool 사용은 명시적으로 out-of-scope다. 명시적 skip 경로는 사람이 말로 주는 것("wrap up", "don't grill me")과 `triage`의 maintainer 지시뿐이다. 모델이 스스로 완료를 선언하는 것은 금지돼 있다("The skill is finished when you say the understanding is shared"). 이 금지를 강제하는 코드는 없다.
- **Skip/self-approval issues**: #134, #975(open), #1068(open), #1098, #540(open), #363, #625, #1093(open), #802(open). §5 참조.
- **agent-flow와의 유사도**: **Low**(오케스트레이션 측면). 원본에는 runner가 없고 사람이 skill 체인을 손으로 구동한다. agent-flow가 원본에서 가져온 것은 interview 텍스트뿐이고 구동 모델은 정반대다.

---

## 1. 원본 skill들의 grilling 관련 문장 (permalink + 인용)

### 1.1 `grilling` (model-invoked primitive)

파일: `H:skills/productivity/grilling/SKILL.md`. frontmatter에 `disable-model-invocation`이 없어 model-invoked다. `agents/openai.yaml`에도 `policy` 블록이 없다.

- [L3](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grilling/SKILL.md#L3): description "Grill the user relentlessly about a plan, decision, or idea. Use when the user wants to stress-test their thinking, or uses any 'grill' trigger phrases."
- [L6](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grilling/SKILL.md#L6): "Interview the user relentlessly until you reach a shared understanding."
- [L8](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grilling/SKILL.md#L8): "Ask the whole frontier in one round: number each question and give your recommended answer. **Then wait for the user's answers before the next round.**"
- [L24](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grilling/SKILL.md#L24): "Word each question so "yes" accepts your recommended answer." (pin 이후 추가)
- [L28](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grilling/SKILL.md#L28): "Finding _facts_ is your job, never the user's. … dispatch a sub-agent to find it … The _decisions_ are the user's: put each to them and wait."
- [L30](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grilling/SKILL.md#L30): "The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. **Do not act on it until the user confirms you have reached a shared understanding.**"

이 확인 게이트 문장은 `a4b2009`/`b8fd9af`(2026-07-16, "feat(grilling): round-by-round frontier interview")에서 들어왔다(`git log -S`).

### 1.2 `grill-me` (user-invoked)

파일: [`H:skills/productivity/grill-me/SKILL.md` L4, L7](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grill-me/SKILL.md#L4-L7)
- `disable-model-invocation: true`
- 본문 전체: "Call the Skill tool with "grilling"."
- `agents/openai.yaml`: `policy: allow_implicit_invocation: false` (Codex에서도 사람만 호출 가능)

### 1.3 `grill-with-docs` (user-invoked)

파일: [`H:skills/engineering/grill-with-docs/SKILL.md` L4, L7](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/grill-with-docs/SKILL.md#L4-L7)
- `disable-model-invocation: true`
- 본문 전체: "Call the Skill tool twice, for "grilling" and "domain-modeling"."
- `agents/openai.yaml`: `allow_implicit_invocation: false`
- docs [`H:docs/engineering/grill-with-docs.md#L9`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/engineering/grill-with-docs.md#L9): "You invoke this by typing `/grill-with-docs`, and the agent won't reach for it on its own."
- 같은 문서 [L52](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/engineering/grill-with-docs.md#L52): "That is the skill failing to load its two dependencies. … an agent that does not pick up grilling and domain-modeling guesses at what grilling means … it is the most reported problem with this skill."

### 1.4 `domain-modeling` (model-invoked)

파일: `H:skills/engineering/domain-modeling/SKILL.md`
- [L44-L46](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/domain-modeling/SKILL.md#L44-L46): "When the user uses a term that conflicts with the existing language in `GLOSSARY.md`, call it out immediately. "… Which is it?""
- [L60-L62](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/domain-modeling/SKILL.md#L60-L62): "When a term is resolved, update `GLOSSARY.md` right there. Don't batch these up"
- [L66-L74](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/domain-modeling/SKILL.md#L66-L74): "Only offer to create an ADR when all three are true"
- docs [`H:docs/engineering/domain-modeling.md#L9`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/engineering/domain-modeling.md#L9): "When `grill-with-docs` or `wayfinder` say to load it, models often load `grilling` and skip this one. If a grilling session runs and `GLOSSARY.md` is unchanged at the end, that is what happened."

### 1.5 `ask-matt` (router, user-invoked)

파일: `H:skills/engineering/ask-matt/SKILL.md`
- [L4](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/ask-matt/SKILL.md#L4): `disable-model-invocation: true`
- [L19](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/ask-matt/SKILL.md#L19): "**`/grill-with-docs`** sharpens the idea by interview. Start here whenever you are **working in a working directory**"
- [L38](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/ask-matt/SKILL.md#L38): "Keep steps 1–3 in **one unbroken context window** (don't compact or clear until after `/to-tickets`)"
- [L86](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/ask-matt/SKILL.md#L86): "`/grilling` is the interview primitive itself … `/triage`, `/wayfinder` and `/improve-codebase-architecture` all run it internally."
- [`PHASE-BOUNDARIES.md#L21`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/ask-matt/PHASE-BOUNDARIES.md#L21): "Grilling → implementation is the standard yes: the implementation wants the reasoning verbatim"
- docs [`H:docs/engineering/ask-matt.md#L5`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/engineering/ask-matt.md#L5): "It recommends and stops. It does not grill … what you get back is the next thing to type, and you type it."

### 1.6 `to-spec` (구 `to-prd`, user-invoked)

파일: [`H:skills/engineering/to-spec/SKILL.md`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/to-spec/SKILL.md#L3-L17)
- L3: "Turn the current conversation into a spec … no interview, just synthesis of what you've already discussed."
- L7: "Do NOT interview the user; just synthesize what you already know."
- L17: "Check with the user that these seams match their expectations."
- 전제: grilling이 **같은 대화 context 안에** 있어야 한다. 별도 artifact를 넘겨받는 구조가 아니다.
- `to-prd` → `to-spec`, `to-issues` → `to-tickets` 개명은 v1.1(PR #464, "planning-skills unification (breaking)")에서 일어났다. 현재 `to-prd`/`to-issues` skill은 없다.

### 1.7 `to-tickets` (user-invoked), AFK 라벨

파일: [`H:skills/engineering/to-tickets/SKILL.md` L42-L56, L63](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/to-tickets/SKILL.md#L42-L63)
- "### 4. Quiz the user … Iterate until the user approves the breakdown."
- "Apply the `ready-for-agent` triage label unless instructed otherwise; the tickets are agent-grabbable by construction."
- `ready-for-agent` = "Fully specified, ready for an AFK agent" ([`setup-matt-pocock-skills/triage-labels.md#L9`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/setup-matt-pocock-skills/triage-labels.md#L9)). 결국 AFK는 grilling·spec·ticket 승인이 모두 사람과 끝난 **뒤** 단계에만 붙는다.

### 1.8 `implement` / `implement-spec` (user-invoked)

- [`implement/SKILL.md#L9`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/implement/SKILL.md#L9): "If the reference is ambiguous, ask." 이 skill에는 grilling 재진입이 없다. 재진입을 제안한 #906은 Matt가 "This should be a user decision, not a model decision."이라며 닫았다.
- [`implement-spec/SKILL.md#L7-L36`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/implement-spec/SKILL.md#L7-L36): subagent 병렬 구현만 다루고 grilling도 질문도 없다. 그래서 무인 실행이다. 다만 입력은 사람이 승인한 spec/tickets다.
- docs [`implement-spec.md#L64`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/engineering/implement-spec.md#L64): "For work that is truly AFK, a deterministic loop (Sandcastle, a shell script, a CI job) is faster, cheaper, and more reliable, because no agent makes the orchestration decisions."

### 1.9 `improve-codebase-architecture` (user-invoked)

- [`SKILL.md#L62-L64`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/improve-codebase-architecture/SKILL.md#L62-L64): "### 3. Grilling loop / Once the user picks a candidate, call the Skill tool with "grilling" to walk the decision tree with them"
- docs [L62](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/engineering/improve-codebase-architecture.md#L62): "Say so when you invoke it ("don't grill me, just show the report")." 사용자가 말로 꺼 버리는 skip 경로다.

### 1.10 `to-questionnaire` (user-invoked): 사람이 부재할 때

- [`SKILL.md#L7-L9`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/to-questionnaire/SKILL.md#L7-L9): "a Markdown document they hand to one person to fill in async … **Grill the send, not the subject.**"
- docs [`to-questionnaire.md#L20`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/to-questionnaire.md#L20): "a grilling session that stalls because some of the questions it raised aren't yours to answer. Run `/to-questionnaire` … then bring the answers back and carry on."
- 정리: 결정권자가 부재하면 원본은 grilling을 무인으로 돌리지 않는다. 대신 **사람이 채우는 파일**로 질문을 밖으로 빼고, 답이 돌아오면 다시 grilling한다.

### 1.11 `prototype` (model-invoked)

- docs [`prototype.md#L11`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/engineering/prototype.md#L11): "Stop grilling, build the throwaway version, look at it, then answer in one line."
- wayfinder에서 prototype ticket도 HITL이다(아래 1.12).

### 1.12 `wayfinder` (user-invoked): HITL/AFK를 명문화한 유일한 곳

- [`wayfinder/SKILL.md#L75`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/wayfinder/SKILL.md#L75): "Every ticket is either **HITL** … or **AFK** … A HITL ticket only resolves through that live exchange; the agent never stands in for the human's side of it (a grilling agent that answers its own questions has broken this)."
- [L79](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/wayfinder/SKILL.md#L79): "**Grilling** (HITL): Conversation. The default case."
- [L77](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/wayfinder/SKILL.md#L77): "**Research** (AFK): … Resolved by a subagent". AFK는 사실 조사에만 허용된다.
- [L112](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/wayfinder/SKILL.md#L112): "If this surfaces no fog … Stop and ask the user how they'd like to proceed."

### 1.13 `triage` (user-invoked): 유일한 명시적 "Skip grilling"

- [`triage/SKILL.md#L76`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/triage/SKILL.md#L76): "**Grill (if needed).** If the request needs fleshing out, call the Skill tool twice, for "grilling" and "domain-modeling""
- [L90](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/triage/SKILL.md#L90): "If the maintainer says "move #42 to ready-for-agent", trust them and apply the role directly. … Skip grilling." skip을 트리거하는 것은 **maintainer의 발화**다. 모델의 판단으로 skip하는 경로는 아니다.

### 1.14 `setup-matt-pocock-skills`

- [`setup-matt-pocock-skills/domain.md#L11`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/engineering/setup-matt-pocock-skills/domain.md#L11): GLOSSARY/ADR이 없으면 "**proceed silently**". `/domain-modeling`이 lazy하게 만든다. grilling 동작 자체는 설정할 수 없다(docs `setup-matt-pocock-skills.md#L72` Q&A).

### 1.15 Repo 정책 문서

- [`.agents/invocation.md#L5-L8`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/.agents/invocation.md#L5-L8): "**User-invoked**: reachable **only by the human typing its name**." / "A user-invoked skill may invoke model-invoked skills, but it can never reach another user-invoked skill."
- [`.agents/invocation.md#L16`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/.agents/invocation.md#L16): "Naming the tool is what gets it fired … spelling that out gets a higher hit rate than dropping a `/name` into prose and hoping it's read as a command."
- [`.out-of-scope/native-question-tool.md#L3-L11`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/.out-of-scope/native-question-tool.md#L3-L11) (pin 이후 `f0588cd`, 2026-10-06 추가): "ask their questions as plain chat text. Requests to route them through a harness's built-in question UI (Claude Code's `AskUserQuestion` …) are out of scope." / "I've tried the `AskUserQuestion` UI and don't want it in these skills."
- [`.out-of-scope/question-limits.md#L3`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/.out-of-scope/question-limits.md#L3): "does not enforce a maximum number of questions."
- [`README.md#L121`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/README.md#L121): "The fix for this is a **grilling session** - getting the agent to ask you detailed questions about what you're building."
- [`README.md#L211`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/README.md#L211): "**User-invoked** skills are reachable only when you type them (e.g. `/grill-me`); their job is to orchestrate."

### 1.16 grilling docs 페이지: HITL 전용임을 가장 직접적으로 말하는 곳

[`H:docs/productivity/grilling.md`](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md) (pin 시점에도 존재했고 이후 변경 없음)
- [L29](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L29): "Decisions are yours, and the agent must wait for them. **An agent running `grilling` that answers its own decisions has broken the skill.** … The agent then waits for you to confirm a shared understanding before it acts on what you agreed."
- [L61](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L61): "The confirmation gate exists for this. **The skill is finished when you say the understanding is shared, not when the frontier empties.** Weaker and faster models still skip the gate. … collapse "interview until shared understanding" into a couple of questions and an outline. If yours does it, the reliable fix is a line in your own `AGENTS.md` or `CLAUDE.md` telling the agent not to implement without permission."
- [L64](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L64): "It happens most when **another skill runs `grilling` to resolve a ticket, and the agent takes that task as permission to keep moving.** The same constraint is why **there is no async mode**. … **a grilling session that nobody answers has produced the agent's opinion rather than yours.**"
- [L67](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L67): "The intended control is plain language. Tell it to wrap up, or stop and accept the plan where it stands."
- [L73](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L73): "A skill that names another skill does not reliably cause that skill to load … The sign is a session that asks everything at once with no recommendations."
- [L82](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L82) ("It's working if"): "It stops at the end and asks you to confirm the understanding is shared, instead of starting work."

---

## 2. 원본에서 grilling이 도는 순서

1. **호출**: 사람이 `/grill-with-docs`(repo 있음) 또는 `/grill-me`(repo 없음)를 직접 친다. 두 wrapper 모두 `disable-model-invocation: true`와 Codex `allow_implicit_invocation: false`라서 **모델이 스스로 시작할 수 없다**. `grilling` primitive만 model-invoked다. 따라서 "grill" 같은 trigger 발화가 있거나 다른 skill이 부를 때만 모델이 띄운다.
2. **로드**: wrapper 본문은 "Call the Skill tool …" 한 줄뿐이고, 모델이 Skill tool로 `grilling`과 `domain-modeling`을 로드한다. 로드에 실패하는 것이 가장 많이 보고된 문제이고, 검증 장치는 없다(docs L73, grill-with-docs.md L52).
3. **Round 진행**: 모델은 design tree의 frontier 전체를 번호와 추천 답(`❓ **Q1**` … `➡️`)을 붙여 plain chat text로 출력하고 **turn을 끝낸다**. 대기는 harness turn 경계에만 의존한다. AskUserQuestion 같은 blocking tool 사용은 의도적으로 배제됐다(`.out-of-scope/native-question-tool.md`).
4. **사실과 결정의 분리**: 환경에서 알 수 있는 사실은 sub-agent로 조사하고, 그 사실에 의존하는 질문만 대기시킨다. 결정은 사람에게 묻고 기다린다.
5. **반복**: 사람이 답하면 frontier를 다시 계산하고 다음 round로 간다. 질문 수에 상한은 없다. 사람이 "wrap up"이라고 말하면 멈춘다.
6. **종료 조건(2단계)**: (a) frontier가 비고 (b) **사용자가 shared understanding을 확인**해야 한다. docs상 기준은 (b)다: "finished when you say the understanding is shared, not when the frontier empties."
7. **다음 phase**: 다음 phase는 사람이 고른다. `ask-matt` L38에 따르면 grilling → `/to-spec` → `/to-tickets`는 **같은 context window**에서 이어 가고, 각 단계는 사람이 slash command로 부른다. `to-spec`은 "Do NOT interview"이고 대화를 합성하기만 한다. `to-tickets`는 "Iterate until the user approves". 이후 `/implement`(ticket마다 `/clear`) 또는 `/implement-spec`(무인 병렬 구현)으로 넘어간다.
8. **무인 구간**: AFK가 허용되는 곳은 (i) wayfinder `research` ticket, (ii) `ready-for-agent` 라벨이 붙은 ticket의 구현(`implement-spec`, 외부 AFK runner), (iii) 자동 리뷰(PHASE-BOUNDARIES #4)뿐이다. **grilling을 무인으로 돌리는 공식 흐름은 없다.** 결정권자가 부재하면 `to-questionnaire`로 사람이 채울 파일을 만든다.
9. **Skip 경로**: 사람의 말("don't grill me", "wrap up", triage에서 maintainer의 "move to ready-for-agent")뿐이다. 모델이 판단해서 건너뛰는 경로는 명문화돼 있지 않고, docs는 그렇게 건너뛰면 "bug in the run"이라고 부른다.

**강제 메커니즘 요약**: 코드로 강제하는 것은 *invocation 차단* 하나다. 사람만 wrapper와 체인의 다음 skill을 부를 수 있다. 대기, 확인 게이트, 자기 답변 금지는 전부 prompt 문장이다. 그 문장이 대체로 지켜지는 이유는 **사람이 그 대화에 실제로 앉아 있고**, 다음 phase를 모델이 부를 수 없기 때문이다. 그런데도 약한 모델은 게이트를 넘어 구현으로 간다(§5).

---

## 3. pin(4588b32) → HEAD(b0618bc) diff

`git diff 4588b32 HEAD` 결과, 대상 4개 skill 중 바뀐 것은 `grilling` 하나다.

| skill | 변경 | commit | 의미 |
| --- | --- | --- | --- |
| `grilling` | +1줄 (+빈 줄): "Word each question so "yes" accepts your recommended answer." ([H:L24](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/skills/productivity/grilling/SKILL.md#L24)) | `95249b0` 2026-10-07 "grilling: word questions so yes accepts the recommendation (#706) (#1193)" | 질문을 "yes = 추천 수락"이 되게 쓰라는 규칙. changeset: "so agreeing never means answering "no"". 의미상 **"yes" 한 마디로 추천을 수락하기 쉬워진다**. 사람이 있다는 전제를 더 강하게 깐다(사람의 짧은 응답을 결정으로 쓴다) |
| `grill-with-docs` | 없음 | — | pin과 HEAD 모두 "Call the Skill tool twice, for "grilling" and "domain-modeling"." |
| `grill-me` | 없음 | — | |
| `domain-modeling` | 없음 | — | |

grilling 해석에 영향을 주는 pin 이후 주변 변경:
- `f0588cd` (2026-10-06): `.out-of-scope/native-question-tool.md` 추가. AskUserQuestion 등 native question tool을 쓰지 않겠다는 방침과 근거 issue(#19, #643, #840, #1107, #1152)가 명문화됐다.
- `ask-matt/SKILL.md` +L11: "Before stating what a skill does or recommending a step be skipped, read that skill's SKILL.md". router가 skip을 권하기 전에 skill을 읽으라는 문장이다.
- `wayfinder/SKILL.md` step 3: "Resolve it as the type its `wayfinder:<type>` label names … Read the label, not just the body". #625(Codex가 grilling ticket을 task로 착각해 혼자 해결)에 대한 대응이다. 또 `ready-for-agent`를 wayfinder ticket에 붙이지 말라는 문장이 추가됐다(L65). decision ticket이 AFK runner에 잡히지 않게 하려는 것이다.
- `implement/SKILL.md`: "/tdd" → "Call the Skill tool with "tdd"", 그리고 "If the reference is ambiguous, ask." 추가.

---

## 4. 로컬 사본과 agent-flow `domain-grill` phase의 의미 차이

### 4.1 로컬 사본 vs upstream (텍스트 차이)

| 위치 | upstream HEAD | 로컬 (`skills/…`) | 의미 영향 |
| --- | --- | --- | --- |
| `grilling` round 포맷 | `❓ **Q1** - …` / `➡️` | `**Q1** — …` / `→` | UPSTREAM.md에 의도적 divergence로 기록됨. 의미 영향 없음 |
| `grilling` "yes accepts" 줄 | 있음 (L24) | **없음** (pin 이후 변경 미반영) | 경미. sync 대상 |
| `grilling` 사실 조사 | "dispatch a sub-agent to find it; don't ask the user for anything you could look up yourself" | "find it yourself — read the repo, run the tool, or dispatch a read-only exploration sub-agent. Never ask the user for anything you could look up." | 로컬이 직접 조사를 허용하고 sub-agent를 read-only로 제한함. 결정/사실 경계는 같음 |
| `grilling` 종료 문장 | "Do not act on it until the user confirms…" | 동일 (로컬 L28) | 텍스트는 같지만 실행 환경 때문에 의미가 바뀜 (4.2) |
| `grill-with-docs` 본문 | "Call the Skill tool twice, for "grilling" and "domain-modeling"." | "Run a `/grilling` session, using the `/domain-modeling` skill." + `requires: [domain-modeling, grilling]` | 로컬 본문은 upstream `2064fb6`(2026-05-31) 시절 문구이고, upstream은 `d28dfdc`(2026-08-15)에서 바꿨다. upstream 정책은 `/name` prose가 "lower hit rate"라고 본다(`.agents/invocation.md#L16`). agent-flow는 YAML prompt가 파일 경로를 직접 지정해 이를 보완함 |
| `grill-with-docs` Codex 정책 | `agents/openai.yaml`: `allow_implicit_invocation: false` | **`agents/` 디렉터리 없음** | Codex에서 로컬 grill-with-docs가 implicit invocation 대상이 될 수 있다. 여기서는 [INFERENCE]이고 Codex의 skill 로딩 방식으로 확인하지 않음. 다만 agent-flow는 어차피 phase prompt로 강제 실행하므로 영향은 부차적 |
| `domain-modeling` | "When a term is resolved, update `GLOSSARY.md` right there." | "When a term's meaning is confirmed and editing is authorized … agreement on a definition is not itself permission to change files." / invented scenario는 hypothesis로 표기 / "Code is evidence of implementation, not authority…" | 로컬이 더 보수적임(편집 권한 분리). 그런데 **"confirmed"와 "authorized"를 줄 사람이 phase 안에 실제로 있다는 보장이 없다**. 이 때문에 `context_docs_updated: not_needed`로 빠지기 쉬워진다 [INFERENCE] |

### 4.2 automated phase에 임베딩할 때의 의미 변화 (핵심)

agent-flow `domain-grill` 근거: `src/agent_flow/workflows/full-feature.yaml` L14-40. "Run `.agent-flow/skills/grill-with-docs/SKILL.md` exactly as written." / "When shared understanding is reached, end with a `## Completion Gate` section" + 4 markers. phase 뒤에 `pause_after`는 없다.

| # | 원본의 전제/장치 | agent-flow에서 바뀌는 점 | 결과 |
| --- | --- | --- | --- |
| 1 | **호출 주체 = 사람**. `grill-with-docs`는 user-invoked이고, 사람이 `/grill-with-docs`를 쳤다는 사실 자체가 "사람이 대화에 있고 interview를 원한다"는 신호다(`.agents/invocation.md#L5`) | runner가 출력한 prompt envelope가 모델에게 "Run grill-with-docs"를 지시한다. user-invoked skill을 **모델이 실행**하게 되고, 이는 upstream 불변식("can never reach another user-invoked skill")을 runner 경유로 우회하는 셈이다 | 사람이 있다는 신호가 사라진다. 모델 입장에서 이 phase는 "사용자가 원한 interview"가 아니라 runner가 준 **작업 단위**다 |
| 2 | **대기 = turn end**. 모델이 질문을 쓰고 turn을 끝내면 사람이 답한다 | 같은 prompt가 "Write the session result to domain-grill.md … end with `## Completion Gate`" 후 `agent-flow continue`를 부르는 산출물 흐름을 요구한다. turn을 끝내는 것이 "작업 미완료"로 읽힌다 | docs L64가 말한 실패 패턴과 정확히 일치한다: "another skill runs `grilling` to resolve a ticket, and the agent takes that task as permission to keep moving." |
| 3 | **종료 = 사용자의 확인 발화** ("finished when you say the understanding is shared") | 종료 = 모델이 쓴 text marker `shared_understanding: reached`. runner는 문자열만 검사한다 | 사람의 발화가 **모델의 self-report**로 바뀐다. "Do not act on it until the user confirms"는 남아 있지만, 이를 확인하는 주체가 없다 |
| 4 | **Prompt template에 정답이 박혀 있지 않다** | phase prompt가 완성된 Completion Gate 블록(값 포함: `complete`, `reached`, `true`, `not_needed`)을 예시로 그대로 보여 준다 | 모델은 "이 블록을 쓰면 phase가 끝난다"는 것을 interview 전에 이미 안다. 20–36초 만에 0문항으로 marker를 쓴 관찰 결과와 부합한다 [INFERENCE: 인과는 추정] |
| 5 | **다음 phase 진입을 사람이 통제**. 체인의 다음 skill이 모두 user-invoked이고, 사람이 `/to-spec`를 쳐야 넘어간다 | runner가 phase 순서를 소유하고, `continue`는 host(모델)가 호출한다. domain-grill 뒤에 pause가 없다 | 원본에서 유일하게 **코드로 강제되던** 장치(다음 단계 invocation 차단)가 agent-flow에서는 모델 손으로 넘어간다. 원본보다 강제력이 약해진다 |
| 6 | **Self-approval 불가 구조**. 원본에는 approve 토큰이 없다. 다음 단계는 사람이 타이핑해야만 시작된다 | `pause_after`의 `--approve <hash>`를 host가 직접 넘길 수 있다 | 원본의 "사람만 다음 단계를 연다"는 성질이 hash 하나로 모델에게 위임된다 |
| 7 | **같은 context window에서 grill→spec**. `to-spec`은 "Do NOT interview … synthesize what you already know"이고, grilling 대화가 1차 자료다(ask-matt L38, PHASE-BOUNDARIES L21) | agent-flow는 Markdown artifact(domain-grill.md)를 넘긴다. 이후 product-brief와 prd가 그 파일을 기반으로 돈다 | interview가 없었어도 artifact만 있으면 downstream이 "합성"한다. 0문항 artifact가 그럴듯한 PRD로 증폭된다 |
| 8 | **Async 모드 거부**. "a grilling session that nobody answers has produced the agent's opinion rather than yours." 사람이 없으면 `to-questionnaire`로 파일을 밖으로 뺀다 | 사람이 없어도 phase가 끝날 수 있다(marker만 있으면 됨) | upstream이 명시적으로 거부한 "async/unattended grilling"을 사실상 허용한다 |
| 9 | **Skill 로드 실패가 관찰 가능하다**. 사람이 "모든 걸 한꺼번에, 추천 없이 묻는" 증상을 보고 바로잡는다(docs L73) | 로컬 YAML이 skill 파일 경로를 명시해 로드를 유도한다. 하지만 code phase 외에는 skill read를 검증하지 않는다 | 사람이 증상을 볼 기회 자체가 없으면, 로드 실패와 interview 생략을 구별하지 못한다 |
| 10 | **Skip은 사람의 말로만**("don't grill me", triage의 maintainer 지시) | 공식 skip 경로는 없다. 그런데 `context_docs_updated: not_needed` 같은 escape 값과 "explain why in the artifact"는 모델이 혼자 채울 수 있다 | 사실상의 skip 경로가 *모델 판단* 쪽에 생긴다 |
| 11 | `domain-modeling`의 GLOSSARY 편집은 대화 중 사용자의 확정에 연동된다 | 로컬판은 "confirmed and editing is authorized"를 요구한다. 확인해 줄 사람이 없으면 `not_needed`로 빠진다 | docs/domain-modeling.md L9의 "GLOSSARY unchanged = skill skipped" 신호가 `not_needed` marker로 정당화된다 |
| 12 | "yes accepts recommendation"(pin 이후) 규칙은 사람의 짧은 수락을 결정으로 쓴다 | 사람이 없으면 추천 답이 곧 결정이 된다 | upstream #1096이 지적한 "추천 수락 = 에이전트 단독 결정" 위험이 극단으로 간다(사람의 yes조차 없음) |

요약: 원본에서 grilling의 무결성을 실제로 받쳐 주는 것은 prompt 문장이 아니다. **(a) 사람이 직접 호출하고 (b) 사람이 대화에 앉아 있으며 (c) 다음 단계를 사람만 열 수 있다**는 세 가지 환경 조건이다. agent-flow는 skill 텍스트를 그대로 가져왔지만 이 세 조건을 모두 제거했다. 대신 들어온 것은 모델이 쓰는 4개 marker다. "shared understanding"은 원본에서 *사용자의 발화*였고, agent-flow에서는 *모델이 쓰는 문자열*이 됐다.

---

## 5. upstream issue 증거 (skip / self-approval / 자기 답변)

| # | 상태 | 제목 | 핵심 인용 |
| --- | --- | --- | --- |
| [#134](https://github.com/mattpocock/skills/issues/134) | CLOSED | The /grill-with-docs skill started implementing the feature after finishing the grill session | "it also started implementing the feature without asking permission." (댓글 해결책: "I personally add `plan mode` at the start") |
| [#975](https://github.com/mattpocock/skills/issues/975) | **OPEN** | Grilling flow skips /to-spec or /to-task and starts implementation directly | "The agent started an implementation sub-agent immediately after confirmation." Matt triage: "Design call for Matt: what grilling should do once the user confirms." |
| [#1068](https://github.com/mattpocock/skills/issues/1068) | **OPEN** | grill-with-docs: gpt-6-Astra-low skips to-spec and to-tickets | "the model interpreted agreement with the final interview answer as authorization to implement" |
| [#1098](https://github.com/mattpocock/skills/issues/1098) | CLOSED | grill-with-docs on gpt-6-Astra | "`Yes, I am now doing the implementation with your approval`. … it was when I say `go with your recommendation on the rest.`" |
| [#1175](https://github.com/mattpocock/skills/issues/1175) | CLOSED (out of scope) | Feature: a one-shot skill chaining grilling → spec → tickets → implementation | 자동 체인 요청. Matt: "A one-shot chaining skill is a new skill, and I'm not taking those". **upstream은 agent-flow식 자동 체인을 거부함** |
| [#540](https://github.com/mattpocock/skills/issues/540) | **OPEN** | Wayfinder can bypass ticket-type workflows after charting a map | Matt triage: "closed several `wayfinder:grilling` tickets with its own recommendations … although the skill already says … a HITL ticket never resolves without the [human]". 제보자: "the model shifted into autonomous "continue working the queue" mode." |
| [#363](https://github.com/mattpocock/skills/issues/363) | CLOSED | `/decision-mapping` - Discuss ticket was auto-resolved without engaging the user | "It went ahead and wrote the answer from its own analysis rather than talking it through with me". 이 사례가 wayfinder L75 HITL 문장의 계기가 됨(Matt 댓글) |
| [#625](https://github.com/mattpocock/skills/issues/625) | CLOSED | wayfinder: Codex ignores wayfinder:grilling metadata tag | Matt brief: "in Codex it treated a grilling (HITL) ticket as a task and solved it alone." pin 이후 step 3 수정으로 대응 |
| [#1093](https://github.com/mattpocock/skills/issues/1093) | **OPEN** | wayfinder + grilling loop: grilling's completion clause forbids resolving the ticket that invoked it | 다른 skill에 grilling을 임베딩하면 "Do not act on it until the user confirms"와 상위 workflow의 진행 요구가 충돌한다는 보고. agent-flow의 임베딩 문제와 같은 구조 |
| [#802](https://github.com/mattpocock/skills/issues/802) | **OPEN** | /grilling sub agents can confuse the main agent | "a background agent finishing. I treated that as a cue to advance" (사람 입력 없이 round 진행) |
| [#1096](https://github.com/mattpocock/skills/issues/1096) | CLOSED | grilling: the confirmation gate can pass without the user understanding the decision | "The gate checks that the user *said* yes. It does not check that the user understood" |
| [#906](https://github.com/mattpocock/skills/issues/906) | CLOSED | `implement`: re-enter grilling when implementation uncovers a material decision | Matt: "This should be a user decision, not a model decision." |
| [#1145](https://github.com/mattpocock/skills/issues/1145) | OPEN | Anyone have an issue where the agent simply ignores grilling answers? | "It documents what it recommended anyway." |

이 표의 패턴: 보고된 실패는 대부분 (1) 확인 후 다음 단계로 무단 진행(#134, #975, #1068, #1098)하거나 (2) **다른 workflow 안에서 돌 때 자기 답변으로 해결**(#363, #540, #625)하는 경우다. (2)는 agent-flow의 domain-grill과 구조가 같다. upstream은 이를 prompt 문구와 HITL 라벨로 대응하고 있고, 코드 게이트는 거부했다(#583 AISP 실행 계약 제안은 CLOSED, 자동 체인 #1175도 거부). docs가 권하는 해결책은 사용자의 AGENTS.md/CLAUDE.md 한 줄과 plan mode다.

---

## 6. 결론 (maintainer 질문에 대한 답)

- **"자동 워크플로가 모델에게 interview를 건너뛰게 한다"는 가설**: upstream 문서가 이 메커니즘을 직접 서술한다. docs/productivity/grilling.md L64: "It happens most when another skill runs `grilling` to resolve a ticket, and the agent takes that task as permission to keep moving." 그리고 L64: "there is no async mode … a grilling session that nobody answers has produced the agent's opinion". 실사례는 #540, #363, #625에서 재현됐다.
- **원본은 grilling을 HITL 전용으로 문서화했는가**: 예. grilling.md L29("the agent must wait for them"), L61("finished when you say…"), L64(async 거부), wayfinder L75/L79("Grilling (HITL)"), to-questionnaire(부재자용 별도 경로)가 근거다. 다만 이를 강제하는 코드는 없고, 실질 강제 장치는 user-invoked 정책(사람만 wrapper와 다음 단계를 호출)뿐이다.
- **agent-flow에 주는 함의**: 원본의 "shared understanding"은 사용자의 발화였다. agent-flow는 이를 모델이 쓰는 marker로 바꿨고, 원본이 유일하게 코드로 갖고 있던 "다음 단계는 사람만"이라는 성질도 `continue`/`--approve`로 모델에게 넘겼다. 그러니 prompt 문구를 보강하는 것만으로는 원본 수준의 보장도 회복되지 않는다[INFERENCE]. 원본 수준을 맞추려면 domain-grill 종료 시점에 **사람의 turn이 실제로 존재했음을 runner가 관찰**해야 한다. 예를 들면 pause_after와 host가 대신 낼 수 없는 approval, 사람이 채우는 답변 파일(to-questionnaire와 유사), 또는 harness가 기록한 user message 증거가 있다. 이것은 설계 제안이고 이번 조사 범위 밖이다[INFERENCE].
