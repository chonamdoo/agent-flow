# pstack 스킬 전체 검토 (2026-10-23)

- 대상: `cursor/plugins` 저장소 `pstack/`, 커밋 8b3326d, MIT 라이선스(Lauren Tan).
  - 스킬 57개, `poteto-mode` 플레이북 23개, references, `poteto-help`, agent 2개, README를 모두 읽었다.
  - 세 묶음(원칙, 플레이북, 나머지 스킬)으로 나눠 독립적으로 읽고, 인용은 원문과 대조해 확인했다.
- 판정 기준:
  - 텍스트(CLAUDE.md나 프롬프트)만으로 넣을 수 있는가. runner 변경은 금지 구역이다.
  - 사람, PR, 하위 agent, 다른 모델 없이 한 세션으로 돌릴 수 있는가.
  - 성공, 무결성, archcheck, UI, 비용 중 하나를 움직일 수 있는가.

## 실험에 넣은 것: arm PS (#463, `ps-v1`)

AP와 같은 자리(CLAUDE.md)를 pstack 문장으로 채운다. 모든 문장이 원문 그대로이고, 예외는 파일 크기 규칙의 "PR"을 "change"로 바꾼 것 하나다.

| 묶음 | 출처 | 기대 효과 |
|---|---|---|
| 착수 전 | redesign-from-first-principles, autonomous-run(종료 조건을 검사할 수 있는 조건으로 먼저 적기) | 성공, 무결성 |
| 구조 | model-the-domain, boundary-discipline, minimize-reader-load, interrogate의 code-quality lens(1k줄, 엉뚱한 if, 정해진 레이어, 상태 소유자 하나), laziness, subtract | archcheck(god class, 레이어 누수, 중복, 긴 함수) |
| 테스트 | test-behavior-not-implementation(요구사항에서 기대값 도출, 동작을 깨 보기), tdd(테스트를 구현에 맞추지 말 것, assertion 약화 금지) | 무결성(약화된 테스트) |
| 실패할 때 | fix-root-causes, attack-the-premise | 성공, 비용 |
| 완료 | prove-it-works, figure-it-out(INCONCLUSIVE는 통과가 아님), never-block-on-the-human, poteto-mode(주장마다 근거) | 무결성(실행하지 않은 "테스트 통과" 주장) |

**뺀 것과 이유:**
- 테스트 삭제를 허용하는 줄(test-behavior의 "rewrite or delete the rest", migrate-callers). 무결성이 지표라서 뺐다.
- 하위 agent, 다른 모델 계열, 사람, PR이 필요한 스킬: arena, interrogate 패널, architect, swarm, how, why, reflect, show-me-your-work의 교차 모델 감사, build-the-lever, guard-the-context-window.
- 2~3개 프로토타입을 만드는 설계 탐색(exhaust-the-design-space). 비용이 크고 사람의 취향을 전제한다.
- 플레이북 라우터. Q4로 미뤘다.

**비교로 알 수 있는 것:**
- AP 대 PS: 어떤 원칙이 지표를 움직이는가. 일반 원칙 8개 대 구체적 규칙.
- A 대 AP 또는 PS: 원칙을 적어 두는 것 자체가 지표를 움직이는가.
- AP와 PS 둘 다 이기지 못하면, 원칙 텍스트로는 지표가 움직이지 않는다는 뜻이다.

## 넣지 않은 후보 (다음 판에서 검토)

- 기능 플레이북 순서형 arm: 종료 조건, 데이터 모양, 검증 단위 반복, 근거 보고 순서. PS와 겹치는 부분이 크다. PS 결과를 보고 순서 자체가 효과가 있는지 따로 잴지 정한다.
- blast-radius: 안전한 이유 하나를 실행해서 증명한다. 숨은 회귀를 줄일 수 있지만 비용이 늘어난다.
- create-verification-skill의 증거 기준("실제 사용자 경로를 거칠 것, 내부 setter가 아니라"): UI 지표 후보다.
- show-me-your-work의 결정 로그: 진단을 돕지만 성공을 움직이지 않을 것 같다.

## 방법론에서 배울 것 (조건이 아니라 harness·평가 쪽)

| pstack | 우리 상태 | 조치 |
|---|---|---|
| eval: 후보가 eval, test, judge 같은 단서를 보면 안 된다 | 완료 선언 형식이 `EVAL-STATUS:`다. 모든 arm에 똑같이 들어가므로 arm 간 비교는 치우치지 않지만 단서다 | 기록만 한다. 다음 판(사이클 1과 비교를 끊을 때)에 중립 이름으로 바꾼다 |
| eval: 다른 모델 계열의 블라인드 판정자가 한 번에 한 척도로 채점한다 | 결정적 평가기만 쓴다(심판 없음은 사용자 결정) | 바꾸지 않는다 |
| hillclimb: harness의 민감도를 증명한 뒤 동결한다 | mutant, 독립 기준 구현, 후속 민감도 검증을 이미 한다 | 일치한다 |
| benchmark-checklist: 5회 이상 번갈아 돌리고 중앙값과 범위를 본다. 회차 간 차이보다 작은 차이는 차이가 아니다 | A/A 보정이 진행 중이고 n=3이다 | 비용과 시간 비교에 중앙값과 범위를 함께 보고한다 |
| formal-verification: 한 번도 실패하는 걸 본 적 없는 검사는 아무것도 증명하지 않는다 | mutant 검증과 같은 생각이다 | 일치한다 |
| interrogate lead 필터(nit 부풀림, "나라면 다르게") | 리뷰어 구조는 runner 안에 있다(금지 구역) | 나중에 runner PR 후보로 둔다 |

pstack은 숨은 기능 검사, 반복 시행, 통계, 비용 측정, 무결성 검사를 하지 않는다. 이 다섯 가지는 우리 harness가 더 강하다.
