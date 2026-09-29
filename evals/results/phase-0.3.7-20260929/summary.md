# phase skill 전달 평가 — 0.3.7, 2026-09-29

- 변경 전 kit: `6bf94bb`(0.3.7).
- 변경 후 kit: `feat/phase-skill-accuracy` 작업 트리를 최종 상태로 복사한 것.
- 실행 방법: `evals/README.md`의 "phase skill 전달 평가".

## 파일

| 파일 | 내용 |
|---|---|
| `budget-before.json.gz`, `budget-after.json.gz` | `phase_budget.py` 원자료. 37개 profile × mode 조합 |
| `eval-results.jsonl.gz` | `phase_eval.py` 원자료. author 24행, review 48행. 리뷰어 출력 전문 포함 |
| `eval-repeat-app-team.jsonl.gz` | app/team author 반복 시행. 변경 전 3회, 변경 후 6회 |

## 정적 측정

README의 "0.3.7 기준 정적 측정" 표와 같다.

- 합계는 20,640k에서 17,872k로 13.4% 줄었다(bytes/4).
- 빠진 필수 skill은 red/green의 `code-review` 하나다.

## 모델 평가

작업자는 Claude, 리뷰어는 claude·codex의 모든 관점이다. 조합마다 1회 시행했다.

### author (full-feature `green`, 12 case)

| 지표 | 변경 전 | 변경 후 |
|---|---:|---:|
| behavior (red 테스트) | 12/12 | 12/12 |
| plan (slice-plan에만 있는 요구) | 8/12 | 12/12 |
| norm (mode 규칙) | 11/12 | 12/12 |
| 세 축 모두 통과 | 7/12 | 12/12 |
| 필수 SKILL.md 실제 읽음 | 94/94 | 82/82 |
| envelope 바이트 | 216,053 | 194,809 (−9.8%) |
| 입력 토큰(캐시 포함) | 8,028,093 | 8,377,382 (+4.3%) |
| 캐시 안 된 입력 토큰 | 631,780 | 611,673 (−3.2%) |
| 출력 토큰 | 128,379 | 136,024 (+6.0%) |
| 비용(USD) | 9.10 | 9.17 |

- plan 개선은 green 프롬프트가 slice-plan·ddd-design·prd·design-spec을 가리키게 된 데 따른 것으로 보인다[INFERENCE]. 변경 후 세션은 계획 산출물을 평균 3.5개 열었고 변경 전 세션은 2.3개 열었다.
- 입력 토큰이 늘어난 이유는 계획 산출물을 더 많이 읽었기 때문으로 보인다[INFERENCE]. envelope 자체는 줄었다.
- 반복 시행(app/team):
  - 변경 전 3회: 3회 모두 필수 skill을 전부 읽었고, plan은 3회 모두 실패했다.
  - 중간 버전(계획 문장이 skill 읽기 지시 앞): 4회 중 2회는 필수 skill을 하나도 읽지 않았다. 그래서 계획 산출물 문장을 skill 읽기 지시 뒤로 옮겼다.
  - 최종 프롬프트 6회: 6회 모두 필수 skill을 전부 읽었고 세 축을 모두 통과했다.

### review (multi-review 리뷰어 전체, 24 case)

| 지표 | 변경 전 | 변경 후 |
|---|---:|---:|
| 유효 시행 | 21/24 | 22/24 |
| 판정 정확 (채점 대상) | 18/19 | 20/20 |
| 결함 diff에 request-changes | 11/11 | 11/11 |
| 심은 결함 탐지 | 22/22 | 22/22 |
| 정상 diff approve | 7/8 | 9/9 |

토큰은 양쪽 모두 유효한 21개 case만 비교한다.

| 지표 | 변경 전 | 변경 후 |
|---|---:|---:|
| 리뷰어 프롬프트 바이트 | 6,862,416 | 4,617,720 (−32.7%) |
| 입력 토큰(캐시 포함) | 43,686,914 | 44,656,102 (+2.2%) |
| 캐시 안 된 입력 토큰 | 8,881,853 | 8,391,179 (−5.5%) |
| 출력 토큰 | 511,062 | 524,150 (+2.6%) |

- 변경 전의 오탐 1건: backend/clean. codex generalist가 use case port 부재와 SPEC 테스트 이름 증거를 must-fix로 들었다.
- 무효 시행: claude 리뷰어가 판정 줄을 형식에 맞게 쓰지 않은 경우다. 변경 전 app/clean/clean, rn/clean/defect, web/team/clean, 변경 후 app/clean/clean, rn/clean/defect.
- 채점하지 않은 판정(`expect: null`): flutter·python의 team case는 pending mode라 정상 diff의 정답이 정해지지 않는다.
  - app/team은 두 kit 모두 approve였다.
  - backend/team은 변경 전 request-changes, 변경 후 approve였다.

### 채점과 사례 보정 기록

- 사후 채점 보정: 결함 패턴 두 개를 넓혔다. 리뷰어가 결함을 분명히 지적했는데 패턴이 놓친 경우다. `eval-results.jsonl.gz`는 보정한 패턴으로 다시 채점한 값이다.
  - backend/team `bug`: 한국어 표현을 추가했다. 이 보정으로 변경 전 1건이 탐지로 바뀌었다.
  - rn·web/local `bug`: `` `error` `` 표기와 한국어 표현을 추가했다. 최종 시행에서 바뀐 값은 없다.
- 평가 중 실제 결함이 발견되어 고친 정상 fixture가 있다. 모든 review 결과는 고친 뒤에 다시 돌린 값이다.
  - web: composition root, 상태 알림 live region.
  - rn: effect cleanup, `feature-api` 진입점, 접근성.
  - app: team 구조를 설계와 맞췄고, 음수 금액 형식을 고쳤다.
  - backend: row·response 타입.
- 하네스 보정도 모두 최종 시행 전에 반영했다.
  - 리뷰 run 폴더에 계획 산출물과 테스트 실행 기록(`test-evidence.md`, 전체 출력)을 둔다.
  - 비차단 구간과 하위 제목을 파싱한다.
  - 리뷰어 출력 전문을 저장한다.

## 해석

- 각 case를 1회만 시행했으므로 결과는 방향만 보여 준다. 반복 시행한 것은 app/team author뿐이다.
- 이번 결과에서 변경 후 kit의 정확도는 변경 전보다 낮아진 항목이 없다.
- 토큰:
  - 줄어든 것: 프롬프트 바이트, 캐시 안 된 입력 토큰.
  - 늘어난 것: 캐시 포함 입력 토큰. author에서 4.3%, review에서 2.2% 늘었다.
- 측정하지 않은 것: 대화 맥락이 이어지는 실제 run 전체, pending team case의 판정 정확도, host 전역 skill을 격리한 조건.
