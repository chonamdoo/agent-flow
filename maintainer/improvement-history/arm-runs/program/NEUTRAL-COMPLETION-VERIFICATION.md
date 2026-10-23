# 중립 완료 선언 변경 검증 (2026-10-23)

기반: `feat/epic-409-arm-ps` `5534c537cdec6fe1a9a9c46404b9e3187d29dda9`. 변경은 `TASK-STATUS` 프로토콜, C′ rules_version 5, 관련 fixture와 설명이다. 현재 실험에는 적용하지 않고 PR만 연다. hook·runner 판단·승인·case·개별 과제 평가기는 변경하지 않았다.

## 영향과 검증 선택

harness가 전달하는 선언 문구와 공통 parser의 접두어만 바꾼다. 상태 이름·성공 조건·통계·무결성은 같다. 설치 산출물 변경 0개. phase skill prompt, resolver/routing, workflow YAML, profile 및 installer 변경이 없어 phase-budget·skill-routing과 설치 fixture 검증은 미적용이다. 실제 모델 실험도 실행하지 않았다.

`maintainer/verification-policy.md`와 회귀 원장 전체를 대조했다. 바꾼 함수 본문은 없고 모듈 상수만 변경했다. `git log -L`로 classify, _outside_fences, driver_prompt, bare_prompt, c_prime.build 계보를 확인했고 README 파일 계보도 확인했다. 이 범위와 관련된 원장 미반영 merged PR을 아래에 보충한다.

| 계보 | 지켜야 할 조건 | focused 검증 |
|---|---|---|
| #432 | 마지막 fenced-code 밖 선언, undeclared/unmeasured 구별, 줄바꿈·CRLF, 정상 과제의 blocked 실패 | completion 7 node(새 protocol 경계 포함) |
| #444·#448 | C′ arm-common 선언, prompt 변경을 rules_version에 기록, C−C′의 과거 결과 해석 유지 | c_prime의 arm-common 선언 node, 문서·base/head diff |
| #447 | 모든 arm의 같은 선언, 성공에 완료 선언 필수, 모든 배정 run 분모, handback와 subprocess 마지막 메시지 구분 | arm_compare 11 node(아래 명령 범위), integrity 1 node |
| #449 | 자가 승인 실패, harness가 human approver가 아님, 산출물 fingerprint 흐름 보존 | human_only_approvals / driver_is_not_the_human_approver node |
| #451 | 공통 headless controller, 권한 우회 없음, 외부 case 입력 보존 | every_arm_gets_the_same_headless_controller / external_case_seeds node |

미반영 merged PR 가운데 #430의 비용 계측, #437의 리뷰 통계, #439의 운영 명세, #440의 skill 은퇴 목록, #443·#446의 격리, #445의 case, #450의 사전등록은 해당 코드·계약·기록을 변경하지 않는다. 그 경계는 diff로 확인했으며 이 PR에서 새 동작이나 검증 완료를 주장하지 않는다. 관련 원장 보충은 위 표에 한정한다.

## 실행 결과

- focused pytest: **24 passed** (20개 9.62초, 승인·controller·외부 case 경계 4개 0.13초).
- parser base/head 대조: pin한 base의 구형 문구와 head의 신형 문구를 대응시킨 입력 10개에서 분류 10/10 일치. None·미선언·inline·complete·blocked·대소문자·연속 선언·CRLF·backtick/tilde fence 포함.
- git diff --check 통과. 설치 kit asset 변경 0개.
- 전체 suite는 저장소 정책대로 CI에 맡긴다. 실제 agent/controller run과 `arm_compare.py drive`는 실행하지 않았다. pytest는 결정적 fixture·mock 경로만 사용했다.

정확한 focused 범위는 `tests/test_completion_declaration.py`의 last_status, undeclared/unmeasured, honest_block, next_line, code_fence, blocked_normal, neutral_protocol 각 node; `tests/test_arm_compare.py`의 bare_arm, workflow_arm, score_hidden, success_needs, summary_denominator, subagent_handback, subagent_tokens, default_workflow, followon_start, arm_ap, arm_ps, human_only_approvals, driver_not_human, same_headless_controller, external_case_seeds 각 node; `tests/test_c_prime.py::test_pauses_become_visible_self_approval_and_the_declaration_is_arm_common`; `tests/test_integrity.py::test_a_test_claim_needs_a_test_command_in_the_transcript`다.

## 의도한 프로토콜 경계와 남은 제약

구형 `EVAL-STATUS`는 새 parser에서 undeclared다. 기존 결과를 이 parser로 재채점하지 않고 당시 pin한 harness를 사용한다. 과거 기록을 수정하거나 기존 시행과 합치지 않는다. prefix만 치환한 비교는 의미 보존 검사이며 과거 run의 재채점이 아니다.

기존 UI case 계약에 구형 완료 선언이 남아 있다. 사용자 지시상 기존 case를 수정할 수 없어 그대로 두었다. 따라서 현재 실험에 이 PR을 바로 적용하면 안 된다. 다음 판에서 그 case를 쓰려면 별도 승인 범위의 새 계약 판과 지문이 필요하다. 새 holdout에는 구형 선언을 넣지 않았다. PR은 merge하지 않는다.
