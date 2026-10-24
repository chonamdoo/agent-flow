# oy-global-fullstack (v1): 고정 풀스택 평가 과제 (#428)

목표만 받은 에이전트가 웹 프론트엔드와 백엔드를 만든다. 같은 run에서 다섯 가지를 잰다: 성공, 거짓 완료, 예상 밖 UI
결함, 성공 1건당 비용, 성공까지 걸린 시간. 이 과제는 #410의 arm 비교와 #420의 확정 단계에 쓴다.

## 공개분과 비공개분

| 위치 | 내용 |
|---|---|
| 이 폴더(공개) | 입력 두 층(`goal.md`, `prd.md`), 평가 인터페이스(`contract/interface.md`, `contract/openapi.yaml`), 데이터(`fixtures/commerce.json`, 카탈로그는 `task.json`이 `beauty-store` 사본을 sha256으로 고정), 지표와 기록 형식(이 문서) |
| 비공개 평가기 저장소 | 브라우저 수용 검사(두 viewport), API 계약·견고성 테스트, 구조 검사, 기준 구현, mutant, canary, holdout 과제 |

beauty-store와 달리 `acceptance.json`·`reference/`를 여기 두지 않는다. 저장소가 public이고, 에이전트가 공개 검사에
맞춰 만들면 G가 의미를 잃는다.

## 입력 층

| 층 | 에이전트가 받는 파일(`task.json` `layers`) | 숨기는 것 |
|---|---|---|
| goal-only | `goal.md` + 인터페이스 계약 | 동작 요구(Spec Item), viewport 요구 |
| PRD | `prd.md` + 인터페이스 계약 | 검사 내용 |

goal-only run의 G는 PRD의 Spec Item으로 채점한다. 목표만 줬을 때 사람이 기대하는 동작을 얼마나 만드는지가 그
층의 질문이다.

## 실행 격리

- 평가 컨테이너(#423)에서 돌리고 네트워크는 끊는다. 에이전트 작업 폴더에는 위 층의 파일과 fixture만 복사한다.
  kit 저장소의 `evals/`는 mount하지 않는다. C′ smoke에서 hidden test가 kit 저장소 안에 있어서 읽을 수 있었다(#427).
- 비공개 평가기는 에이전트가 끝난 뒤에만 컨테이너에 들어온다. 평가기에는 canary 문자열을 심는다. 에이전트의 출력이나
  명령에 canary가 보이면 그 run을 `contaminated`로 표시하고 지표에서 뺀다.
- holdout(다른 도메인 과제 2개 이상)은 사이클 끝에 한 번만 쓴다. 결과는 루프 제안자에게 보여 주지 않는다.

## 지표

| 기호 | 정의 |
|---|---|
| S 성공 | G = 100% ∧ V = 0 ∧ API 계약·견고성 100%. 분모는 block·무선언을 포함한 전체 run |
| G 목표 충족 | Spec Item 검사가 `--repeat 3`에서 모두 통과한 비율 |
| V 예상 밖 UI 결함 | 두 viewport에서 수집: CDP `Runtime.exceptionThrown`, 계약상 맞는 4xx를 뺀 실패 응답, 가로 넘침, 필수 요소 누락·가림 |
| F 거짓 완료 | `EVAL-STATUS: complete`를 선언한 run 중 S가 아닌 run. 선언률·성공률과 함께 Clopper-Pearson 상한으로 보고 |
| C 성공 1건당 비용 | 전체 run의 실청구 비용 ÷ S run 수. 토큰은 input·output·cache read·cache write를 나눠 적는다 |
| W 성공 1건까지 시간 | 재시도 포함 벽시계. LLM 생성 / 도구·테스트 / 리뷰어 대기 / 사람 대기로 분해. 로컬 gate와 CI gate 끝점을 둘 다 적는다 |
| E (진단) | `--repeat 3` 중 한 번이라도 실패한 check 비율. flake 진단에만 쓴다 |
| I, P (기록만) | 사람 개입 수, LLM phase 수·프롬프트 바이트. 목표로 두지 않는다 |

완료 선언은 #424 규칙(`evals/completion.py`)으로 읽는다.

## 기록 형식 `results.tsv`

한 줄이 run 하나다. 탭으로 나누고 첫 줄은 머리글이다.

| 열 | 값 |
|---|---|
| `commit` | kit commit(arm A는 `-`) |
| `model` | 모델 id와 CLI 버전, `claude-…@2.1.308` 형식 |
| `arm` | `A`·`B`·`C`·`C′`(#410) |
| `layer` | `goal-only`·`prd` |
| `S` | `1`·`0` |
| `G` | 0~1, 소수 셋째 자리 |
| `V` | 결함 수 |
| `F` | `1`(완료 선언했고 S=0)·`0`·`-`(선언 없음) |
| `C` | USD, 소수 넷째 자리. 실청구를 모르면 `-`를 쓰고 `status`에 이유를 적는다 |
| `W` | 초 |
| `E` | 0~1 |
| `I` | 정수 |
| `P` | `<phase 수>/<프롬프트 바이트>` |
| `status` | `ok`·`blocked`·`undeclared`·`contaminated`·`harness-error:<이유>` |

## 진행 상태 (2026-10-22)

- 이 폴더: 과제 문서, 공개 계약, fixture, 기록 형식.
- 남은 것(비공개 저장소가 있어야 한다):
  - 평가기와 기준 구현. 평가기는 기준 구현에 100%를 줘야 한다.
  - 평가기 타당성: mutant 검출률 ≥ 95%, 기준 구현 오탐 0, 고정 빌드 10회 채점 변동 0.
  - canary 유출 검사.
  - 맨 모델(A) 파일럿 5회로 헤드룸을 판정한다. PRD 층에서 A 성공률이 90% 이상이면 과제를 어렵게 하고 goal-only를
    주 비교로 쓴다.
