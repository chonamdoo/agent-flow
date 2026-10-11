## 결론

[판단] B안은 **사전 인터뷰 문서를 재사용하는 정책**으로는 타당하지만, 자가 승인 문제를 고치지는 못합니다.  
[판단] 토큰 제거, 사람 턴 개수 검사, `pause_after` 추가를 각각 단독 해법으로 채택하면 안 됩니다.  
[판단] 먼저 호스트별 입력 출처를 검증하고, 승인 대상에 묶인 증거를 runner가 확인하는 공통 계약을 정해야 합니다.  
[판단] `start --no-grill`은 폐기하고, 별도 질문 하위 상태는 우선 추가하지 않는 편이 낫습니다.  
이번 검토에서는 파일 수정·설치·Agent Flow lifecycle 실행을 하지 않았습니다.

## 사실 검증 표

경로는 `/Users/namdoo/Developer/Aiproject/agent-workflow` 기준입니다.

| claim | 판정 | 근거 file:line |
|---|---|---|
| 승인 토큰이 `next_command`에 들어간다 | **confirmed** | runner 출력과 독립 status 출력 모두 해당. [runner.py:2723](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/runner.py:2723), [artifact.py:291](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/artifact.py:291) |
| `next_command`에서만 제거하면 토큰이 비노출된다 | **wrong** | 별도 `[approval]` 출력에도 노출. 토큰은 run·phase·진입 시각·artifact 해시로 계산 가능. [runner.py:976](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/runner.py:976), [artifact.py:541](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/artifact.py:541) |
| domain-grill 뒤 pause가 없다 | **confirmed** | 첫 phase에는 없고 PRD에는 있음. default는 slice-plan 뒤 pause. [full-feature.yaml:14](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/workflows/full-feature.yaml:14), [full-feature.yaml:55](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/workflows/full-feature.yaml:55), [default.yaml:163](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/workflows/default.yaml:163) |
| 프롬프트에 완료 marker 값이 미리 채워져 있다 | **confirmed** | 네 값 모두 제시됨. [full-feature.yaml:33](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/workflows/full-feature.yaml:33) |
| `grill-with-docs`에 `disable-model-invocation: true`가 있다 | **confirmed** | runner는 해당 파일을 그대로 실행하라고 지시. [SKILL.md:4](/Users/namdoo/Developer/Aiproject/agent-workflow/skills/grill-with-docs/SKILL.md:4), [full-feature.yaml:25](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/workflows/full-feature.yaml:25) |
| 그 frontmatter가 다음 단계 진행까지 사람에게만 허용한다 | **wrong** | 자동 skill 호출 제한이다. runner 승인자 인증으로 확대할 근거가 없다. [Claude 공식 설명](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill) |
| domain-grill에 skill-read gate가 없다 | **confirmed** | `CODE_PHASES`에 없고 gate도 이 집합에만 적용. [skill_resolver.py:76](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/core/skill_resolver.py:76), [local_skills.py:247](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/core/local_skills.py:247) |
| 유일한 검사는 marker 네 줄뿐이다 | **wrong** | 인터뷰 증거 검사는 없지만 freshness·artifact·architecture·hook 검사도 존재. [runner.py:699](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/runner.py:699), [runner.py:480](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/runner.py:480) |
| 현재 pause 채팅 승인 흐름은 토큰을 사용한다 | **confirmed** | 스킬은 사람 승인을 기다리라고 하고 CLI는 `--approve`를 처리. 토큰 없는 `continue`는 승인 기록을 만들지 않음. [SKILL.md:67](/Users/namdoo/Developer/Aiproject/agent-workflow/skills/agent-flow/SKILL.md:67), [cli.py:1031](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/cli.py:1031) |
| 사람 입력 훅이 등록되어 있다 | **wrong** | 관리 등록에는 Pre/PostToolUse·Stop만 있고 OMP에는 `input` 핸들러 없음. `input` 언급은 실측 주석. [installer-shared.mjs:3504](/Users/namdoo/Developer/Aiproject/agent-workflow/lib/installer-shared.mjs:3504), [installer-shared.mjs:3553](/Users/namdoo/Developer/Aiproject/agent-workflow/lib/installer-shared.mjs:3553), [omp-hooks-extension.mjs:132](/Users/namdoo/Developer/Aiproject/agent-workflow/lib/omp-hooks-extension.mjs:132), [omp-hooks-extension.mjs:331](/Users/namdoo/Developer/Aiproject/agent-workflow/lib/omp-hooks-extension.mjs:331) |
| 현재 승인 경로가 실제 사용자 참여를 검사한다 | **wrong** | 조사한 phase 승인·SPEC 변경 확인·manual 승인 경로에는 사용자 이벤트 검사가 없음. phase 승인 함수도 호출자 신원 확인이 아니라고 명시. [artifact.py:562](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/artifact.py:562), [design_ledger.py:676](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/core/design_ledger.py:676), [cli.py:1478](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/cli.py:1478) |
| SPEC 확인도 pause 토큰에 의존한다 | **wrong** | 초기 목록은 별도 승인 없이 baseline. 변경은 `spec confirm`; manual 승인 문장은 CLI가 직접 생성. fingerprint와 publication HEAD 검사는 있으나 사람 확인은 없음. [SKILL.md:43](/Users/namdoo/Developer/Aiproject/agent-workflow/skills/agent-flow/SKILL.md:43), [cli.py:1462](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/cli.py:1462), [design_ledger.py:455](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/core/design_ledger.py:455) |
| 질문·답 없는 artifact로 domain-grill 통과가 가능하다 | **confirmed — 소스·테스트 정의** | marker만 작성하고 `product-brief` 전이를 기대하는 테스트가 있음. 이번 검토에서 실행하지는 않음. [test_cli.py:5117](/Users/namdoo/Developer/Aiproject/agent-workflow/tests/test_cli.py:5117), [test_cli.py:5144](/Users/namdoo/Developer/Aiproject/agent-workflow/tests/test_cli.py:5144) |
| 보고된 실제 Codex run이 PRD를 자가 승인했다 | **unverified [미확인]** | `pane-first-answer.md:22`의 보고만 확인. 원본 run·세션 로그는 대조하지 못함 |
| generic은 훅 등록도 필요 없는 경로다 | **wrong** | generic은 프롬프트 emit 경로지만 runner lifecycle은 관리 훅 등록 검사를 공통 실행. **등록 검사와 현재 호스트의 실행 증명은 다름.** [generic.py:5](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/adapters/generic.py:5), [runner.py:480](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/runner.py:480) |

## 제안별 적대적 평가

| 제안 | 평가 |
|---|---|
| **B** | **modify [판단]**. 불필요한 재질문은 줄인다. 하지만 문서 존재·GLOSSARY·코드가 현재 태스크의 결정과 동의를 대신하지는 않는다. GLOSSARY는 스킬상 SPEC이 아니다. [domain-modeling/SKILL.md:46](/Users/namdoo/Developer/Aiproject/agent-workflow/skills/domain-modeling/SKILL.md:46) |
| **토큰 제거** | **modify [판단]**. 자동으로 실행할 명령에서 승인 행동을 분리하는 것은 유용하다. 전면 제거만 하면 채팅 승인 이후 재개가 끊기며, 다른 출력·계산 경로로 자가 승인은 남는다. 대상 해시 검사는 보존해야 한다. |
| **human-turn ledger** | **modify [판단]**. 이벤트 수나 시각은 동의가 아니다. “아니요”, 무관한 메시지, 이전 질문 답변도 통과할 수 있다. Claude에서는 예약 작업·백그라운드 보고에도 이벤트가 발생한다. [공식 훅 설명](https://code.claude.com/docs/en/hooks#userpromptsubmit) |
| **`questions_open → awaiting_confirmation`** | **drop — 별도 상태 구현 [판단]**. 질문을 runner가 실제로 관측하지 못하면 상태 이름만 늘어난다. 종료 조건이 불명확하면 교착된다. 먼저 기존 승인 요청 상태를 재사용할 수 있는지 검증해야 한다. |
| **fail-closed + `--no-grill`** | **drop — 결합안 [판단]**. 플래그는 에이전트도 실행 가능하며, 인터뷰·합의 요구를 지운다. 신규 승인 검사를 fail-closed로 만드는 것과 generic 전체를 거부하는 것은 다른 결정이다. |
| **marker 예시 변경** | **keep — 보조 개선 [판단]**. 완성 예시 대신 설명형 placeholder를 쓰되, 허용 값은 유지한다. 키만 쓰게 하면 빈 값 때문에 정상 artifact도 막힌다. 실패 방지의 증거로 삼지는 않는다. |
| **domain-grill `pause_after` 추가** | **modify [판단]**. 단계 경계는 생기지만 지금 승인 방식이면 같은 자가 승인이 반복된다. 승인 증거 변경과 함께 적용해야 한다. |
| **컨트롤러 권고 순서** | **modify [판단]**. 호스트 입력 출처 실측·공통 승인 계약을 먼저 정한다. generic 허용/거부를 먼저 고르면 가능한 대체 경로를 조사하기 전에 호환성 정책부터 제한하게 된다. |

추가 누락: full-feature의 `merge-approval`은 pause가 아니라 모델이 쓴 `verdict: approve`로 진행한다. 따라서 “모든 pause와 SPEC”만 고쳐도 사람 승인 전체가 강화되지는 않는다. [full-feature.yaml:791](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/workflows/full-feature.yaml:791)

## 최종 제안

### 1. 호스트 입력·승인 증거의 수용 계약부터 검증

- **문제·변경:** [판단] 등록만으로 실행·입력 출처를 증명할 수 없다. Claude/Codex/OMP와 generic 연결 경로에서 실제 사용자 입력, 예약 입력, subagent 알림, 대기열 입력, 세션 전환을 구분하는 최소 fixture를 만든다. 구분 불가능한 이벤트를 `human`으로 기록하지 않는다.
- **영향 범위:** 호스트 연결부, installer, hook integrity, 세션 binding, native smoke·payload replay 평가. 기존 lifecycle 동작 변경은 이 검증 단계에서 하지 않는다.
- **장점 / 위험:** 호환성과 위양성을 먼저 확인한다. 호스트 버전별 비용이 들고, 입력 이벤트가 있어도 동의 의미까지 증명되지는 않는다.
- **보존:** 기존 generic 지원, 보호 훅, routing 권한.
- **수용 증거:** 세 호스트에서 실제 사람 응답은 관측되고 자동 알림은 제외됨. generic에도 동등한 승인 증거 전달 경로가 입증되어야 한다. 불가능하면 그 제한을 먼저 보고한다.
- **선후관계:** 제안 3의 선행 조건.

### 2. B를 “기존 결정 재사용 + 빠진 결정 확인”으로 제한

- **문제·변경:** [판단] 사전 인터뷰를 활용할 규칙을 명시한다. 현재 태스크의 scope·acceptance criteria·예외·제약을 기존 문서와 대조하고, 출처와 충돌·미결 결정만 기록한다. 빈 질문 목록은 허용한다. 문서 자체를 사용자 동의 증거로 간주하지 않는다.
- **영향 범위:** full-feature domain-grill, default Interview 프롬프트, 관련 skill 전달·실제 모델 평가. wrapper를 자동 호출하라는 문구는 의존 skill을 읽고 적용하라는 지시로 정리한다.
- **장점 / 위험:** 사전 인터뷰를 재사용하고 재질문을 줄인다. 오래된 문서나 구현을 현재 의도로 오인할 위험이 남는다.
- **보존:** phase·artifact·완료 marker·기존 후속 승인. 새 workflow와 skip route 없음.
- **수용 증거:** 충분한 사전 문서는 재인터뷰하지 않음; 상충·누락 문서는 필요한 결정만 질문함; 질문 0개를 동의 완료로 자동 취급하지 않음. `phase_budget.py` 비교와 해당 `phase_eval.py` 사례 필요.
- **선후관계:** 제안 1과 독립 진행 가능. 강제력 수정 완료로 보고하지 않는다.

### 3. 승인 대상을 고정한 증거 검사 — 제안 1 통과 후에만

- **문제·변경:** [판단] 대상 해시는 맞아도 사람이 승인했는지 확인하지 않는다. 승인 요청에 `request_id`, run, phase attempt, 대상 해시를 고정한다. 승인 기록은 해당 요청에 대한 호스트 출처 이벤트와 명시적인 동의 범위를 연결한다. 입력 한 건만으로 승인하지 않는다.
- **영향 범위:** `runner.py`, `artifact.py`, SPEC 승인 함수·CLI, YAML 승인 선언, 상태 출력, 세 호스트 연결부와 설치 회귀. domain-grill의 확인 대기는 기존 pause 구조를 우선 재사용한다. merge-approval도 검사 범위에 포함한다.
- **장점 / 위험:** 증거 없는 `--approve`, `spec confirm`, `spec approve`, 승인 verdict를 막을 수 있다. 출처·동의 판별이 입증되지 않으면 구현을 출시할 수 없다. 같은 OS 사용자 위조를 막는 보안 경계로 주장하지 않는다.
- **보존:** 기존 artifact/fingerprint/HEAD 결속, 초기 SPEC 별도 승인 없음, unchanged SPEC 계속 진행. 승인 토큰은 내부 대상 결속에 남기고, 승인 대기 중 자동 `next_command`에서는 승인 행동을 분리한다. 사용자는 자연어 채팅으로 답한다.
- **수용 증거:** 부정·무관 응답, 과거 입력, 다른 run 입력, 알림은 거부. artifact·SPEC·HEAD 변경 후 이전 승인은 무효. 정상 채팅 승인·재시작·호스트 전환은 성공. status·abort·복구 호출은 새 대기 상태에 갇히지 않음.
- **선후관계:** 제안 1 통과 → 공통 승인 검사 → 출력 변경·domain pause를 함께 적용. YAML pin과 진행 중 run의 계약을 명시적으로 검증한다.

## 제안하지 않는 것과 이유

- **질문 개수 하한·`## Rounds`를 승인 증거로 사용:** 모델이 작성할 수 있다. **no proposal**.
- **domain-grill skill-read 관측을 강제 gate로 승격:** 인터뷰·동의를 증명하지 못한다. 기존 코드에도 관측 강제로 인한 host 교착 이력이 설명되어 있다. [local_skills.py:472](/Users/namdoo/Developer/Aiproject/agent-workflow/src/agent_flow/core/local_skills.py:472)
- **모든 도구 차단, `--approve` 명령별 사전 차단, lifecycle 면제 확대:** 두 pre-block 계약과 복구 경로를 흔든다. **no proposal**.
- **generic 전체 거부, 새 workflow, phase 삭제, `--no-grill`, 시작 시 runtime/YAML digest 검사:** 현재 근거와 소유자 제약으로 정당화되지 않는다. **no proposal**.
- **단순 영구 채팅 원문 원장:** 필요한 승인 범위를 넘어 저장·개인정보·보존 비용을 늘린다. **no proposal**.

## 확인 못 한 것

[미확인] 실제 보고 run의 원본 로그, Codex 사용자 입력 훅의 출처 구분 능력, OMP `input`의 현재 payload 계약, generic의 동등한 승인 전달 경로.  
[미확인] upstream pin 문서와 OSS 비교·수량·폐기 사유는 독립 검증하지 못했으며 최종 제안의 근거로 사용하지 않았습니다.  
테스트 정의는 읽었지만 실행·재현·live eval은 수행하지 않았습니다.