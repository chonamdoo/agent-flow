# 적대적 리뷰: domain-grill / 사람 확인 제안

## 결론

- 관찰된 실패는 코드와 맞아요. 질문 없이 통과한 것과 PRD를 스스로 승인한 것 둘 다요. 원인은 두 가지예요. 하나는 게이트가 에이전트가 직접 쓴 marker만 본다는 점이에요. 다른 하나는 `next_command`를 실행하기만 하면 승인까지 끝나는 출력 모양이에요.
- 컨트롤러와 패인 둘 다 놓친 사실이 있어요. `UserPromptSubmit` 기반 사람 입력 훅은 **예전에 있었다가 폐기됐어요.** 지금은 테스트가 다시 등록되는 걸 막고 있어요. 그래서 사람 턴 원장은 폐기 이유를 확인하기 전에 시작하면 안 돼요.
- 토큰은 `meta.json`과 artifact만 있으면 다시 계산할 수 있어요. 그래서 토큰을 "숨겨도" 보안은 생기지 않아요. 효과는 "`next_command` 실행 = 승인"이라는 자동 경로를 끊는 것뿐이에요.
- 권고: 비용이 낮은 것부터 해요. (1) 승인 명령을 `next_command`에서 분리해요. (2) domain-grill에 `pause_after`를 붙이고 B안을 고쳐서 넣어요. 원장, 하위 상태, fail-closed, `--no-grill`은 지금 하지 않아요.

## 사실 검증 표

| claim | 판정 | 근거 |
|---|---|---|
| `next_command`에 `--approve <token>`이 붙음 | 확인 | `src/agent_flow/runner.py:2723-2726`, `src/agent_flow/artifact.py:290-294`, `runner.py:976` |
| 토큰은 사람 인증이 아니고 artifact 내용에 묶는 장치 | 확인 | `artifact.py:562`, `docs/USAGE.md:450-453` |
| 토큰을 에이전트가 직접 다시 계산할 수 있음 (새 사실) | 확인 | `artifact.py:541-549`: run_id, phase_id, phase_entered_at, artifact sha256을 묶은 sha256 |
| 채팅 승인 흐름이 토큰에 의존 | 확인 | `src/agent_flow/cli.py:329-333`, `:1031-1032`, `skills/agent-flow/SKILL.md:67-68`, `tests/test_approval_status.py:12-49` (토큰이 next_command에 있는 걸 테스트가 계약으로 고정) |
| domain-grill에는 pause가 없고 prd에만 있음 | 확인 | `src/agent_flow/workflows/full-feature.yaml:14-40`, `:55` |
| 프롬프트가 값까지 채운 marker 블록을 보여 줌 | 확인 | `full-feature.yaml:33-37` (`not_needed`까지 채워져 있음) |
| 게이트는 marker 4줄만 확인 | 확인 (코드 기준) | `full-feature.yaml:18-22` |
| skill read gate 없음 | 확인 | `src/agent_flow/core/skill_resolver.py:76-90`, `:1088-1090`, `src/agent_flow/core/local_skills.py:247-253` |
| `disable-model-invocation`인데 runner가 실행을 지시함 | 확인. 다만 이 설정은 Read 경로를 막지 못함 [판단] | `skills/grill-with-docs/SKILL.md:4`, `full-feature.yaml:25` |
| 사람 입력 훅이 등록돼 있지 않음 | 확인. 단 **폐기 이력과 금지 테스트가 빠져 있었음** | `lib/installer-shared.mjs:3504-3598`, `lib/managed-hooks.mjs:15-21` (`prepare-spec-user-prompt.py`, `confirm-spec-user-prompt.py`, `guard-spec-approval.sh` 폐기), `tests/test_custom_skill_install.py:2349-2377`, `tests/test_cli.py:2024`, `:2037` |
| OMP `input` 이벤트 | 부분 확인. 실측 주석은 있지만 구독은 테스트가 금지 | `lib/omp-hooks-extension.mjs:331-333`, `tests/test_cli.py:2037` |
| Codex `UserPromptSubmit` 지원 | [미확인] | 조사 문서 주장뿐 (`research-RuntimeOrchestrators.md:486`) |
| 사람 참여를 확인하는 런타임 경로 | 없음 확인 | `runner.py:1382-1386` (토큰 일치만), `src/agent_flow/core/design_ledger.py:516-518` (문장 일치만), `full-feature.yaml:799-800` (merge 승인도 에이전트가 기록) |
| SPEC 확인 방식 | 확인. `next_command`와 분리된 `spec_confirm_command` 줄을 에이전트가 실행 | `cli.py:3801-3811`, `SKILL.md:43-49`, `tests/test_cli.py:7709-7748` |
| `optional` 필드에 건너뛰기 로직 없음 | 확인 | `runner.py:248`, `:2817` (값을 전달만 함) |
| `SKILL.md:68`은 "design or slice review"만 언급 (새 사실) | 확인. full-feature의 PRD pause는 문구상 빠져 있음 | `skills/agent-flow/SKILL.md:68` |
| Stop 훅은 안내만 하고 진행을 강요하지 않음 | 확인 | `scripts/hooks/show-phase-status.sh:36-38` (`systemMessage`만 출력) |
| web_qa, chatbot, kroute 실제 run 기록 | [미확인] | 세션 원본이 제공되지 않음 |

## 제안별 적대적 평가

- **B (미리 정리한 문서면 확인만): 수정해서 유지.**
  - B만 넣으면 오히려 약해져요. "문서가 이미 답했다"는 판단을 에이전트가 하기 때문이에요. chatbot run의 "기존 대화에서 검토"와 같은 모양을 공식 경로로 만드는 셈이에요.
  - 핵심 규칙도 이미 있어요. "찾을 수 있는 건 묻지 마라"(`skills/grilling/SKILL.md:26`)와 "사용자 확인 전 진행 금지"(`:28`)예요.
  - 그래서 pause와 함께일 때만 유지해요. 새로 넣는 건 "사람이 미리 돌린 인터뷰 결과를 입력으로 인정"과 "결정마다 출처 기록" 두 가지로 줄여요.
- **토큰 제거: "분리"로 바꿈.**
  - 그냥 지우면 두 경로가 함께 끊겨요. 채팅 승인 흐름과, 사람이 `next_command`를 따르는 generic emit 경로(`src/agent_flow/adapters/generic.py:5-7`)예요.
  - 토큰은 다시 계산할 수 있어서 "숨기기"를 근본 해법이라 부를 수 없어요. 패인이 `pane-final-answer.md:73`에서 그렇게 부른 건 과장이에요.
- **사람 턴 원장: 보류 (지금은 제외).**
  - 같은 계열의 장치가 폐기됐고, 테스트가 재등록을 막고 있어요.
  - 등록은 실행의 증거가 아니에요(`docs/USAGE.md:545`). 그래서 "훅이 안 돌았다"와 "사람이 없었다"를 구분할 수 없어요. 거부하면 교착이 생겨요. 사용자에게 터미널 입력을 시킬 수도 없어요(`SKILL.md:49`). 허용하면 효과가 없어요.
  - `MANAGED_HOOK_SCRIPTS`에 넣는 순간 기존 설치는 run 시작을 거부당해요(`src/agent_flow/core/hook_integrity.py:46-98`).
  - generic 경로에서는 아예 쓸 수 없어요.
- **하위 상태 (`questions_open` → `awaiting_confirmation`): 제외.**
  - runner는 질문을 볼 수 없어요. 그래서 상태 전이를 에이전트가 신고하게 되고, 강제력은 0이에요.
  - phase id별 특례가 하나 더 늘고, 교착 위험이 있어요. `pause_after`로 대신할 수 있어요.
- **fail-closed와 `start --no-grill`: 제외.**
  - 아직 결정되지 않은 정책 #1(`maintainer/2026-10-09-plugin-evolution-discussion.md:21`)을 미리 정해 버려요.
  - 에이전트도 그 플래그를 실행할 수 있어요. 가벼운 우회 경로가 하나 생기는 셈이라 제약 5와 부딪혀요.
- **marker 예시 값 비우기: 유지.** 작은 변경이고 강제력은 없어요.
- **domain-grill에 `pause_after`: 유지.** 단 제안 1 이후에만 의미가 있어요.
- **권장 순서: 수정.** 컨트롤러 순서는 싼 수정을 비싼 원장 작업 뒤에 묶어 둬서, 싼 것도 같이 늦어져요.

### 놓친 시나리오
- `merge-approval`(`full-feature.yaml:791-800`)에는 pause도 토큰도 없어요. 에이전트가 `verdict: approve`를 직접 써요. 사람 확인이 가장 중요한 자리인데 범위 목록에 없어요. 원장을 언젠가 만든다면 첫 대상은 domain-grill이 아니라 여기예요 [판단].
- "run 시작 전 계획 승인"이 pause 승인을 대신하지 않는다는 규칙이 어디에도 없어요.
- 위 `SKILL.md:68` 문구 누락도 같은 쪽에 힘을 보태요.

## 최종 제안

### P1. pause 승인 명령을 `next_command`에서 분리

- **해결하는 문제:**
  - `SKILL.md:67`("artifact를 쓰면 `next_command` 실행")이 `:68`("멈추면 기다려라")보다 먼저 작동해요.
  - 토큰이 `next_command` 안에 들어 있어서 그냥 실행하면 승인까지 돼요(`runner.py:2726`, `artifact.py:294`, `runner.py:976`).
  - web_qa run의 PRD 자가 승인이 이 모양이에요 [미확인 run].
- **변경:**
  - pause 중 `next_command`는 토큰 없는 `agent-flow continue ...`로 둬요. 실행해도 같은 blocked 상태가 다시 출력될 뿐이고 상태는 바뀌지 않아요(`runner.py:1382-1386`).
  - 승인 명령은 별도 줄 `approval_command:`와 `status_json` 필드로 내보내요. SPEC의 `spec_confirm_command`(`cli.py:3808-3811`)와 같은 모양이에요.
  - `SKILL.md:68`을 바꿔요. "모든 pause에서 artifact를 요약하고 채팅으로 물은 뒤, 그 요약에 대한 사용자 답이 오면 `approval_command`를 실행한다. pause 이전의 승인은 세지 않는다."
  - 이 문구는 `:49`(정확한 문구 요구 금지, 터미널 입력 요구 금지)를 지켜요.
- **영향 범위:**
  - 코드: `runner.py:961-983`, `:2722-2726`, `artifact.py:290-294`, `src/agent_flow/core/workflow_status.py:26-54`
  - 문서: `SKILL.md:67-68`, `docs/USAGE.md:450-453`
  - 테스트: `tests/test_approval_status.py`, `tests/test_runner_smoke.py:191`, `:421`
  - 대상 phase: 모든 `pause_after` phase (default slice-plan, full-feature prd)
  - 호스트: Claude, Codex, OMP, generic 모두 같은 출력. JS는 approve를 파싱하지 않아요 (`lib`, `bin`, `scripts`에 approve 파싱 코드 없음, Grep 확인)
  - 평가: `evals/phase_budget.py`, `evals/phase_eval.py`
- **장점:** 훅이 필요 없어요. 세 호스트가 같은 결과를 내요. generic 경로가 유지돼요. SPEC 확인과 모양이 같아져요.
- **단점·위험:** 증명 장치가 아니에요. 의도적인 자가 승인은 여전히 가능해요(토큰 재계산 가능, 제약 7 범위 안). 그리고 Codex Default 모드는 질문 없이 진행하는 성향이라 효과가 약할 수 있어요 [미확인].
- **보존되는 것:** 토큰과 artifact 내용의 결속, stale 거부, lifecycle 예외 범위, 복구 경로.
- **수용 증거:**
  - 단위 테스트 3개: `next_command`에 `--approve`가 없음 / `approval_command`의 토큰으로 승인 성공 / artifact가 바뀌면 거부.
  - `phase_eval`에 "pause 도달 + 사용자 응답 없음" 사례를 추가해요. Claude와 Codex에서 진행하지 않아야 해요.
- **선후:** 첫 번째.

### P2. domain-grill: `pause_after` + 미리 한 인터뷰를 입력으로 인정 (B 수정판) + marker 예시 정리

- **해결하는 문제:**
  - 질문 0개로 통과돼요. 이 phase에는 pause가 없어요(`full-feature.yaml:14-40`).
  - 값을 채운 예시가 복사를 부추겨요(`:33-37`).
  - 소유자 질문에 답해요: 인터뷰를 run 밖에서 미리 할 수 있게 해요.
- **변경 (`full-feature.yaml` 한 곳):**
  - a. domain-grill에 `pause_after: true`를 붙여요.
  - b. 프롬프트를 바꿔요. 태스크나 저장소가 미리 해 둔 인터뷰 기록을 가리키면 그 결정은 확정된 것으로 봐요. 사람이 직접 돌린 `/grill-with-docs` 결과, `GLOSSARY.md`, `docs/adr/`가 여기에 해당해요. 비어 있는 결정만 한 라운드씩 묻고 턴을 끝내요. 결정마다 출처(이번 run의 사용자 / 문서 경로)를 `## Decision Sources`에 기록하고, 이 제목을 `required_markers`에 추가해요.
  - c. 예시 값을 `<true|not_needed>`처럼 자리표시로 바꿔요.
  - default.yaml의 design 단계는 b만 적용해요. slice-plan pause가 design.md 검토를 이미 덮어요(`src/agent_flow/workflows/default.yaml:177`).
- **영향 범위:**
  - YAML 2곳. 새 run에만 적용돼요 (기존 run은 pin된 정의를 씀).
  - full-feature의 pause 위치를 고정한 테스트가 있는지 [미확인].
  - `phase_budget.py` 비교와 `phase_eval.py` 실제 모델 평가가 필요해요.
- **장점:**
  - 기존 장치만 써요. 세 호스트가 같아요.
  - 인터뷰를 미리 했다면 run에서는 요약 확인 1회로 끝나요.
- **단점·위험:**
  - full-feature의 승인이 2회(domain-grill, prd)로 늘어요. 마찰이 생겨요.
  - P1이 없으면 PRD처럼 자가 승인될 수 있어요.
  - 출처 기록도 에이전트가 써요.
- **보존되는 것:** phase 순서, artifact, 기존 marker, prd pause.
- **수용 증거:** `phase_eval` 사례 세 가지예요.
  - (i) 사전 문서가 있으면: 새 질문 0개, 요약을 보여 주고 pause에서 멈춤.
  - (ii) 문서가 없으면: 첫 라운드 질문을 출력하고 턴 종료.
  - (iii) Codex에서도 같은 결과.
  - 그리고 설치 fixture에서 `grill-with-docs`가 호스트 slash 스킬로 노출되는지 확인해요.
- **선후:** P1 뒤.

## 제안하지 않는 것과 이유

- **사람 턴 원장:**
  - 폐기 선례와 금지 테스트가 있어요. 등록만으로는 실행을 증명할 수 없어서, 거부하면 교착이고 허용하면 효과가 없어요. generic 경로에서는 쓸 수 없어요.
  - 다시 검토할 조건 세 가지예요. ① 폐기 이유 확인(`git log -S confirm-spec-user-prompt`) ② 세 호스트에서 사람 입력 이벤트 실측 ③ "이 세션에서 훅이 실제로 돈 게 증명된 경우에만 거부" 설계.
  - 다시 하게 되면 첫 대상은 `merge-approval`이에요.
- **하위 상태, fail-closed, `--no-grill`:** 위 평가대로 강제력이 없거나, 교착이 생기거나, 우회 경로가 돼요.
- **`CODE_PHASES`에 domain-grill 추가:** 이 게이트도 "에이전트 말을 믿는다"(`skill_resolver.py:1082-1087`)라서 인터뷰를 증명하지 못해요.
- **`disable-model-invocation` 변경:** Read 경로와 상관이 없어요.

## 확인 못 한 것

- [미확인] 사용자 입력 SPEC 훅을 폐기한 이유. Bash가 없어서 git 이력을 볼 수 없었어요.
- [미확인] Codex `UserPromptSubmit`가 실제로 발화하는지, 대기열에 넣은 입력에서는 언제 발화하는지.
- [미확인] web_qa, chatbot, kroute run 원본 기록.
- [미확인] `grill-with-docs`가 호스트 스킬 폴더에 연결되는지.
- [미확인] full-feature pause 위치를 고정하는 테스트가 있는지.
- [미확인] 외부 OSS 인용 (mattpocock, AI-DLC). 웹에서 직접 열어 보지 않았어요.