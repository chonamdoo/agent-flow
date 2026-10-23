# #409 실험 프로그램 v1 초안: 조건 탐색 + 품질 평가 확장

## 0. 맥락 (지금까지 측정된 것)

- agent-flow는 Claude Code/Codex용 멀티 phase 워크플로 kit이다. runner가 phase(기획 → TDD → 리뷰 → fix-loop → gates)를 강제한다. workflow는 YAML이고, 프로젝트의 `.agent-flow/workflows/<name>.yaml`이 kit 기본을 덮어쓴다.
- 조건
  - A: 맨 모델
  - B: `development` workflow(가벼움)
  - C: `full-feature` workflow(기획, multi-review 리뷰어 10명, fix-loop 상한 3)
- 결과〔measured〕
  - OYG(웹 풀스택): A 1/5, C 0/2. C는 2회 모두 fix-loop 상한에서 멈췄다. run당 비용은 A $2, C $20.
  - 모바일 과제 v0·v1(기능 하나): A 3/3씩. 너무 쉬웠다.
  - mobile-checkout(대형, 검사 28개): A 파일럿 진행 중.
- 사용자가 관찰한 맨 모델의 실패 양상: "됐다고 했는데 안 했다", 아키텍처를 지키지 않음, 함수 하나가 비대해짐 등, 기능 정확도 밖의 실패가 다양하다.
- agent-flow는 이를 막으려고 원칙을 도입했다: SRP, 부작용 회피, DRY, 매개변수 최소화(클래스로 묶기), Fail-Fast, Guard Clauses, SLAP, Explicit Receivers.
- 지금 평가기는 기능 정확도(H, S), 기존 테스트 회귀(R), 빌드(B), 거짓 완료 선언만 본다. 위 품질 축은 재지 않는다.

## 1. 질문

- Q1. agent-flow(전체·축소)가 맨 모델보다 나은가? 기능과 품질 모두.
- Q2. 품질을 지키면서 phase와 토큰을 줄인 축소판이 있는가?
- Q3. 기획이 부족한 입력(goal만)에서 인터뷰(grilling)가 결과를 바꾸는가?
- Q4. 품질 축(아래 §3)이 재현 가능하게 측정되는가?

## 2. 조건 공간

- 고정 조건: A, B(`development`), C(`full-feature`).
- 맞춤 축소판 S1..Sk: harness가 실험 프로젝트에 넣는 project-local workflow YAML이다. kit 소스는 바꾸지 않는다. 탐색할 차원:
  - 기획 phase(domain-grill/product-brief/prd/slice-plan/plan-review)를 둘지 뺄지
  - TDD red/green 강제
  - multi-review 리뷰어 수(2/5/10)와 관점
  - architecture-review
  - fix-loop 상한(2/3/5)
  - comment-authoring
  - gates 구성
- 입력 층: PRD 층과 goal 층.
  - goal 층에서는 harness가 "사용자 시뮬레이터"를 붙인다. 이 LLM은 숨긴 PRD에 적힌 사실만 답하고, 없으면 "정하지 않았다"고 답한다.
  - 질문 수, 질문이 PRD 사실을 끌어낸 비율, 시뮬레이터 비용을 기록한다.
- 탐색(오토리서치)
  - 개발 과제(OYG, mobile-cart v0·v1, devflow)에서 변형을 하나씩 바꿔 돌린다.
  - 가중 합산 지수는 쓰지 않는다. 비용 대 품질 벡터의 Pareto 지배로 채택을 정한다.
  - 상위 1~2개만 holdout(mobile-checkout)에서 사이클 끝에 한 번 확정한다.
- 예산: 사이클당 약 $150. 추정 run당 비용은 A $3, B $7, C $20, 시뮬레이터 +$1~3이다.

## 3. 품질 평가 확장

모든 조건의 산출물에 같은 평가기를 쓴다. 평가기는 run이 끝난 뒤 사본에서 돌고, 에이전트는 이를 볼 수 없다.

### 3a. 결정적 지표 (도구로 잰다)

| 축 | 지표 | 도구(후보) |
|---|---|---|
| 기능 정확도 | 비공개 검사 통과율 H, S | 기존 평가기 |
| 회귀 방지 | 기존 테스트 R, 기존 동작 mutant 재사용 | 기존 + mutant |
| 거짓 완료 | 완료 선언 ∧ S=0 비율, 주장 대 증거 대조 | `completion`, `claim_audit` |
| 작업 범위 | 바뀐 파일·줄 수, 과제와 무관한 모듈 수정, 기준 구현 대비 diff 크기 비 | git diff |
| 분량·비대 | 바뀐 함수의 길이(최대, p90), 순환 복잡도, 중첩 깊이 | lizard(Kotlin/JS), detekt |
| 매개변수 | 함수당 매개변수 수(최대, 4 초과 개수) | lizard, detekt `LongParameterList` |
| 중복 | 바뀐 코드의 복제율 | jscpd / PMD CPD |
| 아키텍처 | 모듈 의존 방향 위반, 계층 규칙 위반 | agent-flow `architecture-lint`(모든 조건에 평가기로 적용), import 규칙 |
| 보안 | 정적 규칙 위반(입력 검증, 비밀값, 위험 API), 웹은 XSS·주입 검사 | semgrep, detekt |
| 일관성 | 저장소 규칙 대비 lint 위반 증가량, 이름 규칙 | ktlint/detekt baseline 차이, eslint |

### 3b. 판정 지표 (LLM 심판)

- 원칙 8개 각각에 대해 루브릭(0~3점)과 근거 줄 인용을 요구한다.
- 심판은 Claude와 Codex 둘이다. 조건 이름을 지운 diff를 준다(blind).
- 같은 과제의 두 산출물을 위치를 바꿔 두 번 비교한다(pairwise).
- 보정
  - 품질 mutant: 기준 구현에 "함수 비대화", "중복 복붙", "가드 없는 깊은 중첩" 같은 결함을 일부러 넣고, 심판이 잡는지 잰다.
  - 심판 간 일치도(Cohen's κ)와 위치 편향을 보고한다.
- 판정 지표는 결정적 지표와 따로 보고한다. 합산하지 않는다.

## 4. 판정 규칙 (초안)

- Q1: 기능은 S로 본다(Fisher, 사전등록 v0). 품질은 축별로 조건 간 차이와 95% CI를 보고한다. 다중 비교 보정을 한다.
- Q2: 축소판 Sx vs C를 비교한다.
  - 기능 H는 비열등(허용 폭 −0.05).
  - 품질 축마다 비열등(허용 폭은 축별로 사전등록).
  - 비용은 우월.
- Q3: goal 층에서 grilling 있는 조건과 없는 조건의 H와 품질을 비교한다.
- Q4: 같은 산출물을 두 번 채점해 결정적 지표가 같은지 본다. 심판은 κ와 보정 결과로 본다.

## 5. 알려진 위험

- C의 fix-loop 상한 정지(runner 변경은 금지 구역). 축소판이 상한을 바꾸면 "축소 효과"와 "상한 효과"가 섞인다.
- 오토리서치가 개발 과제에 과적합할 수 있다. 그래서 holdout은 사이클 끝에 한 번만 쓴다.
- 품질 지표를 에이전트가 알면 게임할 수 있다. 그래서 평가기는 비공개로 둔다.
- n이 작다. 품질 축이 많아서 우연한 차이가 나올 수 있다.
