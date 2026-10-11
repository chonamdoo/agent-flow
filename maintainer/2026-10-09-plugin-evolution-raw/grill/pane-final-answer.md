## 결론

- **원본 grilling:** 질문하고 기다리는 동작은 전부 프롬프트 문장입니다. 대신 원본은 세 가지 환경 조건 위에서 돕니다. ① 사람이 직접 `/grill-with-docs`를 호출하고, ② 사람이 대화에 앉아 있고, ③ 다음 단계도 사람만 열 수 있습니다. 이 중 코드로 강제되는 건 ①과 ③을 만드는 `disable-model-invocation: true` 하나입니다(모델은 user-invoked skill을 스스로 부를 수 없습니다).
- **agent-flow:** skill 텍스트는 그대로 가져왔지만 위 세 조건을 모두 없앴습니다. 그 자리에 모델이 직접 쓰는 마커 4개와, 모델 눈에 보이는 승인 토큰이 들어왔습니다.
- **같은 형태의 OSS:** 가장 가까운 건 AWS AI-DLC입니다. 조사한 프로젝트 중 사람이 실제로 답했는지를 코드로 검사하는 곳은 둘뿐이고(AI-DLC, GSD-2), AI-DLC가 그중 하나입니다. AI-DLC는 우리와 같은 실패를 겪고 2026-10-08에 고쳤습니다([PR #2186](https://github.com/awslabs/aidlc-workflows/pull/2186)).

## 1. 원본 grilling 동작 vs agent-flow

| 항목 | 원본 (mattpocock/skills @b0618bc) | agent-flow domain-grill |
|---|---|---|
| 호출 | 사람이 `/grill-with-docs` 입력. `disable-model-invocation: true`, Codex용 `allow_implicit_invocation: false` | runner 프롬프트가 모델에게 실행하라고 지시 |
| 대기 | 질문을 평문으로 출력하고 턴을 끝냄. AskUserQuestion은 [의도적으로 배제](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/.out-of-scope/native-question-tool.md) | 문구는 같지만 phase 완료 = artifact 작성 + `continue` |
| 종료 | "The skill is finished when **you say** the understanding is shared" ([L61](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L61)) | 모델이 `shared_understanding: reached`를 씀. 프롬프트가 완성된 마커 블록을 미리 보여줌 |
| 다음 단계 | 사람이 `/to-spec`을 입력 | host가 `continue` 실행. pause 토큰도 `next_command`에 들어 있음(`runner.py:2723-2726`) |
| 사람이 없을 때 | async 모드 없음. 사람이 채우는 `to-questionnaire` 파일로 돌림 | 마커만 있으면 통과 |
| skip | 사람 말로만("wrap up", "don't grill me", triage에서 maintainer 지시) | 공식 경로는 없고, 사실상 모델이 판단 |

원본 문서가 우리 실패 패턴을 그대로 적어 두었습니다([L64](https://github.com/mattpocock/skills/blob/b0618bc436ad893b3c5e84e55fba86586d34a404/docs/productivity/grilling.md#L64)):
> "It happens most when another skill runs `grilling` to resolve a ticket, and the agent takes that task as permission to keep moving … there is no async mode … a grilling session that nobody answers has produced the agent's opinion rather than yours."

- **원본도 다른 워크플로에 끼워 넣으면 같은 문제가 납니다:** #363, #540, #625(Codex가 HITL grilling 티켓을 혼자 해결), #1093(open, 상위 워크플로와 grilling 종료 조항이 충돌).
- **사람이 대화에 있어도 게이트는 뚫립니다:** #134, #975, #1068, #1098. 원본 문서가 권하는 해결도 "AGENTS.md에 한 줄 추가, plan mode 사용"입니다.
- **우리 pin(4588b32) 이후 바뀐 것:** `grilling`에 한 줄 추가뿐입니다. "Word each question so "yes" accepts your recommended answer."(`95249b0`)

## 2. 우리와 같은 형태의 OSS (적대적 리뷰 후 재순위)

| 순위 | 프로젝트 | 우리와 같은 점 | 인터뷰 강제 방식 |
|---|---|---|---|
| 1 | [awslabs/aidlc-workflows](https://github.com/awslabs/aidlc-workflows) (AI-DLC) | 엔진이 지시 → host 세션이 수행 → `report`. Stop hook이 루프를 강제. Claude Code와 Codex 지원 | **코드.** `UserPromptSubmit` hook이 사람 턴(HUMAN_TURN)을 append-only 원장에 기록. `verifyApprovalDecision`은 게이트가 열린 뒤 사람 응답이 없으면 승인 거부. 답변 경로에는 아직 구멍(#2209 open). PR/merge 단계는 없음 |
| 2 | [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) | Codex host, deep-interview → ralplan → … 체인, hook으로 검증 | 질문 하나씩은 막지만 완료는 모델이 선언. [#2558](https://github.com/Yeachan-Heo/oh-my-codex/issues/2558) "Just modifies code. No deep-interview no ralplan" — 우리와 같은 실패 |
| 3 | [coleam00/Archon](https://github.com/coleam00/Archon) | YAML DAG runner, worktree, review/PR/CI 단계 | 인터뷰 단계 없음("No one is watching this run"). 실제 DB pause/resume은 있지만 CLI 승인에 "No actor is recorded", AI 승인자를 공식 지원. host 세션이 아니라 자체 세션을 띄움 |
| 4 | gentle-ai v3.7.0 | runner + 하위 에이전트 | harness transcript에서 사람 답을 찾아 승인 권한을 판단(PR #4531). **v4.0.0에서 이 방식을 폐기** — 운영 비용이 컸다는 증거 |
| 5 | GSD-2 / gsd-pi | runtime이 단계를 dispatch | 질문이 대기 중이면 [모든 도구를 HARD BLOCK](https://github.com/gsd-build/gsd-2/blob/33c00aaffa56e5d394bccce1c8df59fb842e84c5/src/resources/extensions/gsd/bootstrap/write-gate.ts#L379-L399)하고 사용자 메시지로만 풀림. auto 모드에서는 우회됨 |

- **참고 사례로 남긴 것:** spec-kit `specify workflow`(TTY 게이트는 있지만 clarify 단계 없음), maestro-flow(사실상 1인 유지보수), task-master autopilot(사실상 휴면).
- **순위에서 뺀 것:** Arcgentic, GSD SDK PhaseRunner(archived).

인터뷰 단계가 있는 프로젝트 약 26개를 세면 다음과 같습니다.

| 구분 | 개수 | 프로젝트 |
|---|---|---|
| 코드로 강제 (현재) | 2 | AI-DLC, GSD-2 |
| 코드로 강제했다가 폐기 | 1 | gentle-ai |
| 일부만 강제 | 4 | oh-my-codex, agent-os v3, Kilo, Cline(VS Code) |
| 프롬프트만 | 20 | 나머지 |

## 3. 가설 판정: "자동화하면 인터뷰를 건너뛴다"

부분적으로만 맞습니다.

- **지지하는 근거:**
  - 약 11개 프로젝트가 사람 답을 대신하는 auto 모드를 공식 제공합니다(BMAD `#yolo`, GSD `--auto`, cc-sdd `-y` 등).
  - 엔진형 도구는 인터뷰 단계를 아예 뺍니다(spec-kit 엔진, Archon).
  - 다른 워크플로 안에서 돌 때의 실패 기록이 있습니다(oh-my-codex #2558, mattpocock #540/#625, AI-DLC의 "10 runner-submitted prompts, zero humans" 측정).
- **반박하는 근거:**
  - 인용된 실패 중 상당수가 사람이 있는 대화형 세션에서 났습니다(mattpocock #134/#1068/#1098, superpowers #2372, spec-kit #4582).
  - 핵심 원인은 자동화 자체보다 "모델이 완료를 스스로 선언"하는 구조와 "모델에게 보이는 승인 토큰"입니다.
- **harness 요인(직접 확인함):** 우리 실패는 Codex에서 났는데, Codex Default 모드 템플릿에 "In Default mode, strongly prefer making reasonable assumptions and executing the user's request rather than stopping to ask questions."라고 적혀 있습니다([default.md L11](https://github.com/openai/codex/blob/22ebb0fb178fe369190123c256ba200585765725/codex-rs/collaboration-mode-templates/templates/default.md#L11)). `request_user_input` 도구도 Plan 모드에서만 열립니다.

팩트체크에서 무너지거나 약해진 주장:
- mattpocock #1175는 자동 체인을 원칙적으로 거부한 게 아닙니다. "a new skill, and I'm not taking those"였습니다.
- superpowers #2372에는 "HARD-GATE"라는 표현이 없습니다. #1441은 사용자가 켠 auto 모드에서 났고 재현되지 않았습니다.
- Claude Code #40593/#40716은 모델이 스스로 승인한 게 아니라 background agent 알림이 촉발한 경우입니다.
- GSD SDK의 자동 승인은 discuss 승인이 아니라 `onBlockerDecision`에 적용됩니다.
- AI-DLC에서 원장 쓰기를 막는 PreToolUse 코드는 찾지 못했습니다. 우회용 env var 차단만 확인됐습니다.
- spec-workflow-mcp 승인은 위조가 가능합니다(서명 없는 JSON, 인증 없는 localhost API).
- "사람이 답했는지 검사하는 프로젝트가 없다"는 주장은 틀렸습니다(AI-DLC, GSD-2, gentle-ai).

## 4. agent-flow에 주는 시사점 (공격을 버틴 것만)

| 대책 | 판정 | 이유 |
|---|---|---|
| PreToolUse로 `--approve` 차단 | 단독으로는 불가 | Claude Code와 Codex 문서 모두 hook을 보안 경계로 보지 말라고 합니다. [Cline #13729](https://github.com/cline/cline/issues/13729)에서는 모델이 Python으로 직접 파일을 써서 plan mode 가드를 우회했습니다. 근본 해법은 토큰을 `next_command`에 찍지 않는 것입니다 |
| `UserPromptSubmit`으로 사람 턴 기록 | 부분적으로만 유효 | scheduled task, `/loop`, background subagent, Codex 대기열 입력에서도 발화하고, 누가 보냈는지 증명하지 못합니다. 출처 필터와 fail-closed가 필요하고, 그래도 증명하는 건 "순서"이지 "동의"는 아닙니다 |
| 인터뷰 강제 vs 원본의 skip 경로 | 양립 가능 | "질문 라운드 뒤, 그리고 최종 확인 요청 뒤에 사람 턴이 최소 1개"를 요구하고, 사람이 직접 친 "skip"/"wrap up"을 원문 그대로 기록하면 됩니다. 질문 개수 하한을 두면 원본과 충돌합니다(원본은 질문 수 제한을 의도적으로 두지 않습니다) |

리뷰어 두 명의 공격을 버틴 최소 설계안입니다(구현하지 않음):
1. `next_command`에서 `--approve <token>`을 뺍니다(`runner.py:2723-2726`).
2. domain-grill에 runner가 관리하는 하위 상태를 둡니다: `questions_open` → `awaiting_confirmation`. 열려 있는 동안 `continue`는 거부하고 "턴을 끝내라"고 출력합니다.
3. 사람 턴 원장을 둡니다. `continue`는 게이트가 열린 뒤 사람 턴이 있을 때만 받고, artifact에는 그 턴을 원문 그대로 인용하게 합니다.
4. hook이 설치되지 않았거나 신뢰되지 않으면 진행을 막고(fail-closed), 사람이 `start --no-grill` 플래그로만 우회하게 합니다.

남는 위험: 모델이 같은 OS 사용자로 돌기 때문에 샌드박스 없이는 원장 위조를 막을 수 없습니다. 교착 상태도 예상해야 합니다(AI-DLC #1540/#708, gentle-ai는 결국 폐기).

조사 보고서 6개는 세션 로컬 파일(`local://research-*.md`, `local://adv-*.md`)로만 남겼고 repo에는 넣지 않았습니다. 원하시면 `docs/`로 옮기겠습니다. 다음 작업으로는 1번(토큰 제거)이 가장 작고 효과가 큽니다.