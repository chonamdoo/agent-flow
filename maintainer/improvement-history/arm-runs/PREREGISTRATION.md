# #409 확정 비교 사전 등록 (v0, 2026-10-22)

이 문서는 확정 run을 하나라도 돌리기 전에 고정한다. 확정 run을 시작한 뒤 바꾸는 항목은 날짜와 이유를 적고 버전을 올린다. 그 뒤의 결과는 "탐색"으로 보고한다.

근거:
- 1차 기록: [2026-10-22-devflow](2026-10-22-devflow/README.md)
- 같은 날의 적대적 리뷰(Claude, Codex)
- [captivus/agent-review](https://github.com/captivus/agent-review) v1.0.0: 산출물 확인, 독립성은 실제여야 한다는 원칙, findings register

## 질문

주 질문은 하나다. 같은 모델과 같은 과제에서, agent-flow `full-feature`(C)가 맨 모델(A)보다 성공률이 높은가.

부 질문은 두 가지다.
- 성공 1건당 총비용과 시간이 얼마나 다른가.
- 그 차이 중 "검증 계약"만으로 얻는 몫이 얼마인가(AL 조건).

## 조건

| 조건 | 내용 | 역할 |
|---|---|---|
| A | 과제, test 명령, 완료 선언 규칙 | 기준 |
| C | kit 설치(hook 포함), `agent-flow run --workflow full-feature` | 주 비교 |
| AL | A + 산출물 계약. 완료 시 검사 명령과 결과 지문을 내고, harness가 검사를 다시 돌린다(agent-review Level 1) | 부 비교. 구현은 아래 "남은 준비" |

B(`development`)와 C′(`c_prime.py`)는 이번 확정에서 빼고 탐색으로만 돌린다. 예산을 주 비교의 n에 쓴다.

C−C′ 차이는 runner 강제력만의 효과가 아니다. C′는 리뷰와 수정 단계도 함께 빠진 조건이다. 그래서 ablation으로 해석하지 않는다.

## 실행 조건 (모든 조건 공통)

- **controller**: 모든 조건이 같은 방식으로 뜬다.
  - 각 run의 프로젝트 디렉터리에서 headless 세션을 시작한다. 프로젝트 hook이 로드되어야 하므로 서브에이전트로는 띄우지 않는다.
  - `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`을 주고, 사용자 CLAUDE.md는 넣지 않는다.
  - 권한은 프로젝트 settings의 allow와 deny 규칙으로 준다. 권한 우회 실행은 runbook대로 쓰지 않는다.
  - 모델은 Claude Opus 5.5다.
- **hook 점검**: 확정 run 전에 C 1회로 hook 이벤트와 test 증거가 실제로 기록되는지 확인한다. 기록 경로를 남긴다.
- **승인**: 에이전트는 사람 승인자가 아니다.
  - `spec approve`/`spec confirm` 실행이나 `verify: manual` 하향이 있으면 규칙 위반이다(transcript 또는 디스크 기록, #449).
  - 위반은 실패로 센다.
- **격리**: run마다 `isolation`을 `instructed` 또는 `enforced`로 기록한다.
  - `enforced`라고 적으려면 controller를 감금한 상태에서, 금지 경로(평가기, hidden test) 읽기가 실패하는 것을 그 run 전에 한 번 확인해야 한다. 감금은 Landlock(#443)이나 컨테이너로 한다.
  - 확인하지 못하면 `instructed`로 적고 오염 검사(transcript와 canary)에 기댄다.
- **동시성과 순서**:
  - 한 번에 한 run씩 돌린다. 동시에 돌리면 시간 비교가 교란된다.
  - 조건 순서는 블록마다 무작위로 정한다. seed를 기록한다.
- **종료**: 벽시계 2시간, 또는 controller 비용 상한 $30 중 먼저 오는 쪽에서 끊는다. 끊긴 run은 실패이고 사유를 기록한다.

## 과제

| 과제 | 용도 | 입력 층 | 공개 여부 |
|---|---|---|---|
| devflow | 끝남(천장, A 5/5) | - | 공개 |
| OYG `oy-global-fullstack` (#428) | 개발: 하네스 점검, 파일럿, 비용 추정 | PRD | 입력 공개, 평가기 비공개 |
| mobile-cart (sduiTest 기반 Android) | holdout 1 | PRD | 전부 비공개 |
| holdout 2 (backend/CLI, 미정) | holdout 2 | PRD | 전부 비공개 |

- goal-only 층은 주 비교에 쓰지 않는다. 숨긴 요구를 PRD로 채점하면 구현 능력이 아니라 명세 추측을 잰다.
- holdout은 후보와 절차를 동결한 뒤 한 번만 쓴다.
  - 결과를 보고 후보를 바꾸면 그 과제는 개발용으로 바뀐다.
  - 다음 확정에는 새 holdout을 쓴다.
- 과제별로 따로 보고하고 합치지 않는다.

## 평가기 동결 조건 (과제마다)

1. 기준 구현이 S=1이어야 한다(repeat 3).
2. 검사를 본 적 없는 별도 에이전트가 PRD만 보고 만든 독립 정상 구현이 S=1이어야 한다. 실패하면 차이마다 PRD 원문으로 판정해 기록한다.
3. 동결한 뒤, 다른 모델이 쓴 새 mutant 세트로 한 번만 검출률을 잰다.
   - mutant마다 기대 실패 검사를 적는다.
   - 시작 실패 같은 인프라 오류는 검출로 세지 않는다.
   - 이 수치가 보고 수치다.
4. 동결 시점의 `evaluator_sha256`을 이 문서에 적는다. 다른 지문으로 채점한 결과는 섞지 않는다.

## 지표와 판정

- **주 지표**: 과제별 성공률. 성공은 S=1 ∧ 완료 선언 ∧ 규칙 위반 없음 ∧ 오염 없음이다.
- **부 지표**:
  - 거짓 완료율 F
  - 성공 1건당 총비용 C: controller와 리뷰어 subprocess 비용을 모두 넣는다. 리뷰어 비용을 잴 수 없으면 C는 "하한"으로 표시한다.
  - 성공까지 시간 W
- **판정**: 과제마다 C − A 성공률 차이와 95% 신뢰구간(Newcombe, Wilson 기반)을 보고한다.
  - 단측 Fisher p < 0.05이고 두 holdout에서 같은 방향이면 "C가 낫다"로 판정한다.
  - 신뢰구간이 0을 포함하면 "판정 불가"다. p가 크다는 사실로 "차이 없음"을 선언하지 않는다.
  - 비열등 판정은 하지 않는다. margin을 정당화할 근거가 아직 없다.
- **제외**: 오염, 인프라 오류, 끊긴 run도 배정된 시행으로 남긴다. 조건별 제외 수와 사유를 표에 함께 보인다.

## 표본 수

모의실험(단측 Fisher α = 0.05, 각 4,000회)에서 조건당 n별 검정력은 아래와 같다.

| 참 성공률 A → C | n=10 | n=15 | n=20 | n=25 | n=30 |
|---|---|---|---|---|---|
| 0.5 → 0.9 | 0.48 | 0.68 | **0.84** | 0.91 | 0.96 |
| 0.4 → 0.8 | 0.43 | 0.59 | 0.76 | 0.87 | 0.91 |
| 0.6 → 0.9 | 0.28 | 0.46 | 0.61 | 0.74 | 0.81 |

holdout마다 A와 C를 각 20회 돌린다. 이 n은 0.5 → 0.9 같은 큰 차이만 볼 수 있다. 더 작은 차이는 "판정 불가"로 끝날 수 있음을 미리 적어 둔다.
AL은 holdout마다 10회로 탐색한다.

## 예산과 사이클

사이클당 Claude 지출 상한은 약 $150이다. 1차 devflow에서 controller 비용은 A $0.25/run, C $5.16/run이었고, 리뷰어 비용은 빠져 있다. OYG와 mobile-cart는 이보다 클 것이다〔추정〕.

| 사이클 | 내용 | 목적 |
|---|---|---|
| 1 (지금) | 무결성 수정(#449, 평가기 지문·장부), hook 점검, OYG에서 A 5회 + C 2회 | 헤드룸과 run당 총비용 실측 |
| 2 | 사이클 1 비용으로 n과 일정을 고정(이 문서 v1). mobile-cart 평가기 동결 | 동결 |
| 3~ | holdout 확정 run. 사이클마다 예산 안에서 A와 C를 같은 수로 무작위 순서로 돌린다 | 확정 |

사이클 1의 OYG 결과는 확정 결과가 아니다. 파일럿은 같은 과제 표본을 쓰므로 탐색으로만 보고한다.

## 리뷰 기록

- 이 설계와 결과에 대한 리뷰는 findings register(`reviews/findings.md`, agent-review Level 3 양식)에 남긴다. id, 심각도, 확인 여부, 수정 확인을 적는다.
- 리뷰어는 이전 리뷰와 초안을 보지 않은 새 세션이다.
  - 산출물은 독자가 받는 그대로 준다.
  - 격리가 `instructed`인지 `enforced`인지 적는다.
- 결과 문서의 주장에는 measured, verified, inferred, assumed 표시를 단다.

## 과제별 required gate의 실행 위치 (v1 초안 메모, 2026-10-22)

runner의 `gates` phase는 `execution: ci`인 gate를 돌리지 않고 `deferred_ci_checks`에 적기만 한다. harness에는 remote가 없으므로 그 gate는 끝내 실행되지 않는다. 결과의 `deferred_ci_checks`와 요약 표의 마지막 열이 이를 보인다.

| 과제 | profile | required gate | `execution` |
|---|---|---|---|
| devflow | python | `architecture-lint` | local |
| devflow | python | `test` (`pytest -q`) | **ci**: runner가 실행하지 않음 |
| OYG | node | `architecture-lint` | local |
| OYG | node | `test` (`npm test`) | local |
| mobile-cart | android | `architecture-lint` | local |
| mobile-cart | android | `build` (`./gradlew assembleDebug`) | local |
| mobile-cart | android | `test` (`./gradlew test`) | local |

C 조건의 "gate가 통과했다"를 test까지 돌았다는 뜻으로 읽지 않는다. 과제마다 이 표를 보고 해석한다.

## fix-loop 상한 정지의 재승인 (v1 초안, 2026-10-23)

**규칙.** C는 fix-loop 상한 정지 시 harness가 최대 2회 재승인한다. 이는 "사람이 상한에서 계속 진행을 눌러 주는 사용 방식"을 측정한다.

- 근거: 사이클 1의 OYG C-1·C-2가 둘 다 multi-review → fix-loop 3라운드 상한에서 blocked로 끝났다〔measured〕. 무인 실행이라 상한에서 계속 진행을 눌러 줄 사람이 없다.
- 재승인이 하는 일은 runner가 상한에서 제시하는 경로뿐이다.
  - runner는 `[block] fix-loop exceeded 3 rounds routing <phase> -> <target>` 뒤에 "Update the artifact or rerun the watcher, then `agent-flow continue`"를 찍는다(`runner.py` `_next_index`, 사이클 1 C-2 출력)〔verified〕.
  - harness는 같은 세션을 재개시켜, 에이전트가 그 phase의 리뷰어를 지금 코드로 다시 돌려 artifact를 갱신하고 `next_command`를 실행하게 한다. 이 단계에서는 코드를 바꾸지 않는다.
- 상한 카운터는 되돌리지 않는다. 되돌리는 runner 명령이 없다(`FIX_LOOP_MAX_ROUNDS`는 상수이고 `fix_loop_rounds`는 늘기만 한다)〔verified〕. 그래서 재승인은 "수정 한 라운드 더"가 아니라 "리뷰 한 번 더"다. 다시 리뷰해도 변경 요청이 남으면 runner는 곧바로 다시 멈춘다.
- 시간 2시간, 예산 $30 상한은 그대로다.
- **적용 범위.** 다음 사이클부터 적용한다. 이미 돈 C-1·C-2(v0 규칙)와 섞어 비교하지 않고, 새 규칙으로 C를 다시 돌린다.

**기록.**
- `drive.json`의 `fix_loop_reapprovals`는 재승인마다 상한 정지 위치(phase, target, rounds), 그때 artifact의 판정 수와 남은 지적, 재승인 뒤 통과했는지(`passed`)를 담는다.
- result의 `fix_loop_reapprovals`는 횟수, `fix_loop_reapproval_log`는 그 목록이다.
- `fix_loop_cap_stop`은 run이 상한 정지 상태로 끝났는지다. runner 상태에서 읽으므로 v0 규칙의 run에도 있다.
- 요약 표에는 "처음부터 성공 / 재승인 뒤 성공" 열과 "상한에서 멈춘 run" 열을 둔다.

**결과 문서.**
- 실패한 C run은 "상한 정지로 실패"와 "코드가 틀려 실패"를 나눠 적는다. 둘이 겹치면 둘 다 적는다.
- blocked run도 평가기 점수(S·G)를 함께 적는다.
- 사이클 1의 분류〔measured〕:

  | run | 상한 정지 | 평가기 | 분류 |
  |---|---|---|---|
  | C-1 | 예 | S 0, G 0.938 | 상한 정지 + 평가기 실패 |
  | C-2 | 예 | S 0, G 0.908 | 상한 정지 + 평가기 실패 |

  - 두 run 모두 상한이 없었더라도 평가기 기준으로는 실패였다.

## v1 개정 (2026-10-23, 다음 사이클부터 적용)

사용자가 프로그램 v2를 승인했고(`program/program-v2.md`), 그 뒤 변경분이 v2.1이다(`program/program-v2.1.md`). 이 절은 두 문서 가운데 판정에 쓰는 규칙만 옮긴 것이다. **위 v0과 다르면 이 절이 이긴다.** 이미 돈 run(사이클 1, 파일럿)은 v0 규칙으로 남기고, 새 규칙의 run과 섞어 비교하지 않는다.

### 질문과 주 지표

| 질문 | 비교 | 주 지표 |
|---|---|---|
| Q1 | A 대 B·C·D 각각 | ① 기능 성공률 ② 후속 변경 성공률 ③ 새 비공개 아키텍처 규칙 위반 수 ④ UI 배치 충실도(UI 과제만) |
| Q2 | 축소판 S 대 그 사이클의 기준 조건 | ①②③(④) 모두 비열등, 비용은 우월 |
| Q4 | 플로우 선택(R1) | **지금 하지 않는다.** v2.1 §3의 시작 조건 셋이 모두 충족되면 별도 사전등록을 쓴다 |

- Q3(인터뷰)은 별도 연구로 미룬다.
- 나머지 지표(중복, 복잡도, 보안, phase 기록, 비용 분해)는 탐색으로만 보고한다.

### 조건

A, A+P, B, C(fix-loop 상한 3, 재승인 최대 2회), D(`default`), S1..Sk. 조건 정의는 v2.1 §5다. AL은 v1에서 뺀다. A+P가 "문장만의 효과"를 대신 잰다.

### 성공 규칙

성공 = S=1 ∧ 완료 선언 ∧ 규칙 위반 없음 ∧ 오염 없음 ∧ **무결성 표시 0**.

- 무결성 표시는 `result.integrity`(#458)다. 테스트 삭제·비활성화·약화, stub, 근거 없는 테스트 통과 주장, 완료 선언 뒤 commit하지 않은 변경을 센다.
- 표시는 결과 문서에서 한 건씩 판정한다. 그 과제의 `r_exempt`에 있는 파일이거나 PRD가 그 변경을 요구하면 해제하고 근거를 적는다. 해제하지 못한 표시가 남은 run은 실패다.
- harness 오류(재시작, 컨테이너 소실, harness 결함)로 끊긴 run은 분모 밖에 두고 사유를 적는다. 같은 설정으로 새로 돌린다.

### 판정

- 과제마다 따로 판정하고, 과제 사이를 합치지 않는다.
- Q1: 주 지표마다 차이와 95% 신뢰구간을 보고한다.
  - 비율은 Newcombe(Wilson 기반)로, 개수는 bootstrap으로 낸다.
  - 신뢰구간이 0을 포함하면 "판정 불가"다.
- Q2 채택:
  - 주 지표 모두에서 신뢰구간 하한이 허용 폭(비율 −0.15, 위반 수 +1) 안이어야 한다.
  - 무결성 표시는 기준 조건보다 많지 않아야 한다.
  - 총비용 평균은 낮아야 한다.
  - 하나라도 어긋나면 채택하지 않는다. 판단할 수 없으면 "결론 없음"이다.
  - 잡음 바닥(A/A로 잰 같은 조건 안 분산)보다 작은 차이는 차이로 보지 않는다. 다만 잡음 바닥만으로 비열등을 증명하지는 않는다.
- 출시 규칙: 모든 주 지표가 비열등인 후보 가운데 총비용이 가장 낮은 것을 기본값으로 한다. 그런 후보가 없으면 현행을 유지한다.

### 표본 수 (정직한 한계)

예산 $150/사이클과 run당 비용(A·A+P 약 $4, B 약 $7, D 미측정, C 약 $20)으로는 조건당 n이 과제마다 3~5다. 이 n으로는 성공률 차이가 0.6 이상일 때만 판정이 날 수 있다(위 v0 검정력 표). 그래서 Q1은 효과 크기와 신뢰구간 보고가 중심이고, "판정 불가"가 흔할 것을 미리 적어 둔다. C는 매 사이클 돌리지 않고 고정 참조 몇 회만 둔다.

### 과제 (v1 시점)

| 과제 | 유형 | 용도 | 상태 |
|---|---|---|---|
| OYG | 기능(웹) | 개발·탐색 | A 1/5, C 0/2(v0) |
| mobile-checkout | 기능(앱) | holdout. 맨 모델이 기능 3/3이라 ①로는 판정을 낼 수 없다. ②③에 쓴다 | 평가기 `08b1e1d9…` |
| mobile-tabs | 기능 + UI(앱) | holdout 후보. ①~④ | 평가기 `b3051a4b…`. mutant·파일럿 진행 중 |
| 버그 수정·리팩터 과제 | 버그, 리팩터 | Q4 전제 조건 | 미작성 |

평가기 동결 조건은 v0과 같다. 다만 독립 기준 구현이 실패하면 차이마다 PRD 원문으로 판정한다. 검사가 틀렸으면 동결 전에 검사를 고치고, 그 내역을 과제 README에 적는다.

### 기록 (모든 run)

- `phase_trace`(#457): 선언된 phase, 실제로 들어간 phase, 건너뛴 phase, phase별 시간·비용, 범주별 비용, `orchestration_share`.
- `integrity`(#458)
- 조건 순서 seed, harness·kit·평가기 지문, CLI 버전

## 남은 준비

- [x] ~~AL 조건~~ v1에서 뺌(A+P로 대체)
- [ ] A+P 조건(CLAUDE.md에 8원칙)
- [ ] 후속 변경 harness와 과제별 후속 변경 2개
- [ ] 비공개 아키텍처 규칙과 결정적 구조 지표
- [ ] A/A 보정과 잡음 바닥
- [ ] 공통 headless controller 실행기와 hook 점검 기록
- [ ] controller 감금(enforced isolation)과 금지 경로 읽기 시험
- [ ] 리뷰어 subprocess 비용 계측
- [x] mobile-cart v0·v1, mobile-checkout(검사, 기준 구현, mutant, 파일럿). mobile-tabs는 진행 중
- [ ] holdout 2 과제 설계
