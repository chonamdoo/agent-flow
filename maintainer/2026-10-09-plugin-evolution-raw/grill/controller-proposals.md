# Controller proposals under review (Korean, verbatim from the controller's answers)

## A. 권고 B: domain-grill은 유지하고 "미리 정리한 문서가 있으면 확인만"으로 바꾼다

현재 동작(컨트롤러가 확인했다고 주장):
- `domain-grill`은 full-feature 필수 첫 단계. 건너뛰는 route 없음. phase 정의의 `optional` 필드는 있지만 쓰는 workflow도, runner의 건너뛰기 로직도 없음.
- 완료 조건은 marker 4개(`domain-grill: complete`, `shared_understanding: reached`, `context_docs_checked: true`, `context_docs_updated: true|not_needed`, full-feature.yaml:18-22). grilling 스킬은 질문 개수가 아니라 합의 확인으로 끝남.
- 인터뷰 결과는 GLOSSARY.md와 docs/adr/에 쌓이고, grilling에 "찾아볼 수 있는 사실은 묻지 마라" 규칙이 있음. 하지만 미리 정리한 인터뷰 문서를 이 단계 입력으로 인정하는 규칙은 없음.
- prd는 domain-grill.md와 product-brief.md를 바탕으로 쓰고(:64-65) 끝나면 pause_after로 멈춤.
- 인터뷰 없는 workflow: development, bugfix, diagnosing-bugs, review. default는 design 단계 안에 인터뷰가 있음.

선택지: A 현재 유지 / B 단계는 두고 동작만 바꿈 / C 단계 건너뛰기(플래그·route) / D 인터뷰 전용 workflow 분리.

권고 B:
- 동작: 태스크가 기존 정리 문서를 가리키거나 GLOSSARY·ADR이 이미 답하고 있으면, 그 내용을 결정 트리에 대조하고 빈 결정만 묻는다. 비어 있는 게 없으면 요약을 보여 주고 사용자 확인만 받는다.
- 유지: domain-grill.md artifact, 완료 marker, 사용자의 합의 확인, prd 이후 승인 pause.
- 기록: artifact에 근거 문서와 새로 물은 질문을 남긴다.
- 영향 범위: full-feature.yaml domain-grill 프롬프트, default.yaml design 1절 인터뷰 부분, 필요하면 grill-with-docs 스킬 본문. 프롬프트 변경이라 evals/phase_budget.py 비교, 판단 기준 변경이라 실제 모델 평가(evals/phase_eval.py) 필요.
- 결론: 인터뷰의 결과는 매번 필요하지만 인터뷰 행위는 매번 필요하지 않다.

## B. 컨트롤러가 GRILL 패인 조사 결과를 대조한 리뷰

직접 확인했다는 사실:
- next_command에 `--approve <token>`을 붙임(runner.py:2722-2726). 에이전트가 status 출력에서 토큰을 볼 수 있음.
- 진입 스킬에 "artifact를 쓰면 next_command를 실행하라"(skills/agent-flow/SKILL.md:67)와 "pause면 사용자 승인을 기다려라"(:68)가 함께 있음. 토큰은 표시된 artifact에 승인을 묶는 장치이지 사람이 승인했다는 증거가 아님.
- domain-grill에는 pause가 없고, 프롬프트가 값까지 채운 완료 marker 블록을 보여 줌(full-feature.yaml:30-37).
- 원본 grill-with-docs는 `disable-model-invocation: true`(skills/grill-with-docs/SKILL.md:4)인데 runner 프롬프트가 그 파일을 그대로 실행하라고 지시.
- 원본 문서 인용(mattpocock grilling.md "finished when you say the understanding is shared", "another skill runs grilling ... no async mode") 직접 확인.
- 사람 입력을 관측하는 훅(UserPromptSubmit, OMP input)은 현재 등록돼 있지 않음.

보완이 필요하다고 지적한 부분:
1. 토큰 제거만 따로 하면 안 된다. 채팅 "진행해" → 에이전트가 토큰으로 승인하는 흐름이 끊긴다. 사용자에게 터미널 명령 입력을 요구하면 SKILL.md:49("Never require an exact phrase or ask the user to enter a terminal command")와 충돌. 토큰이 어디든 에이전트가 읽을 수 있으면 의도적 자가 승인은 남음. 사람 턴 원장과 묶어야 함.
2. 세 호스트 모두 새 훅 필요. Claude UserPromptSubmit, Codex 지원은 패인 주장(미확인), OMP는 extension `input` 이벤트 관측 주석만 있음(lib/omp-hooks-extension.mjs:331).
3. fail-closed는 미결 결정(훅 없는 에이전트를 표시 후 허용 vs 거부)과 직결.
4. `start --no-grill`은 에이전트도 실행 가능. 우회 플래그는 사람 턴에 묶거나 두지 말 것.
5. 하위 상태(questions_open → awaiting_confirmation)는 교착 주의. continue 거부해도 status/abort 복구 경로 유지(5067004, 786794c 계약).
6. 범위가 domain-grill보다 넓다. pause_after 모든 단계와 SPEC 확인(SKILL.md:46)이 같은 구조.
7. B안과 양립. "사람 턴 최소 1개" 조건이면 빈 결정이 없을 때도 최종 확인 한 번. 질문 개수 하한 없음은 원본과 같음.

진행 순서 권고:
1. 훅 없는 에이전트 정책 결정(표시 후 허용 또는 거부).
2. 세 호스트에서 사람 입력 이벤트 실측.
3. 사람 턴 원장과 토큰 비노출을 한 번에. 모든 pause와 SPEC 확인에 적용.
4. domain-grill "확인 대기" 상태 + B안.
5. 우회 플래그는 두지 않거나 사람 턴에 묶어서만.

당장 비용이 거의 없는 개선 [추론]: 완료 marker 예시에서 값을 빼고 키만 보여 주기, domain-grill에 pause_after 붙이기. 강제력이 아니라 잘못 진행될 확률만 줄임.

패인 보고서 6개는 세션 local://에만 있음. 보존하려면 maintainer/ 아래(docs/는 배포 대상).
