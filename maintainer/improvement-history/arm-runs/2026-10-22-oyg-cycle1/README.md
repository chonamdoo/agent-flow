# #409 사이클 1: OYG 파일럿, A 5회 + C 2회 (2026-10-22)

`PREREGISTRATION.md` 사이클 1의 run이다. OYG는 개발 과제이므로 이 결과는 확정이 아니라 탐색이다.

## 설정

- 과제: OYG(`--case oyg`, `layer: prd`). 채점은 비공개 평가기 `oyg/score.py`가 한다.
  - `evaluator_sha256` e5f2b64d…, 7회 모두 같다〔measured〕.
- 조건
  - A: 과제 문장과 완료 선언 규칙만 준다. kit 없음.
  - C: `full-feature` workflow를 `--architecture-mode clean`으로 설치한다. phase 승인은 harness가 runner의 `approval_command`로 한다(`approval_rules: harness-approves-phases`).
- 실행: 모든 run이 같은 `claude -p` controller(`drive`)다.
  - 설정: `--permission-mode auto`, 프로젝트 hook 로드, 허용 목록 env, 제한 2시간, 상한 $30.
  - kit commit은 7회 모두 930b0b5다.
- 순서: C-1을 먼저 돌렸다. 나머지 6회는 `order.txt`의 순서(seed 20261022로 섞음)로 한 번에 하나씩 돌렸다.
- 채점 코드: #452의 `arm_compare.py score`/`summarize`다. 성공 규칙은 사전등록 그대로다.
  - A·C 7회는 dc766fb로, harness 오류 5회는 ec58206으로 채점했다.
  - 그 사이 두 commit(7df515a, ec58206)은 어느 project와 worktree를 채점할지만 바꾼다. 옮기지 않은 7회에서는 같은 checkout을 고른다는 것을 확인했다〔measured〕.
  - 성공 = S=1 ∧ 완료 선언 ∧ 규칙 위반 없음 ∧ `run_status == ok`.
  - C-1은 #452 이전 harness(ee5bce8)로도 채점했다. 평가기 점수(S·G·API·V·E)와 실패 검사 목록이 같았다〔measured〕.

## 결과

`summary-table.md`가 원표, `results.jsonl`이 run별 기록이다. 비용은 CLI가 보고한 세션 누적 `total_cost_usd`다. transcript 토큰 × Opus 5.5 단가로 낸 추정이 7회 모두 이 값과 일치했다〔measured〕.

| run | S | G | API | 선언 | 시간 | 비용 |
|---|---|---|---|---|---|---|
| A-1 | 0 | 0.954 | 1.0 | complete | 534초 | $1.98 |
| A-2 | **1** | 1.000 | 1.0 | complete | 976초 | $2.32 |
| A-3 | 0 | 0.908 | 1.0 | complete | 523초 | $1.94 |
| A-4 | 0 | 0.877 | 1.0 | complete | 554초 | $1.87 |
| A-5 | 0 | 0.985 | 1.0 | complete | 674초 | $2.28 |
| C-1 | 0 | 0.938 | 1.0 | blocked | 4,049초 | $17.52 |
| C-2 | 0 | 0.908 | 1.0 | blocked | 3,959초 | $21.51 |

- 성공은 A 1/5, C 0/2다. Fisher 단측 p(C가 A보다 나은가)는 1.000이다. n=2로는 아무것도 판정할 수 없다.
- E(평가기 오류율)는 7회 모두 0이다. 브라우저 검사 3회 반복의 결과 수가 매번 기대값과 같았다(65·12)〔measured〕.
- 리뷰어 subprocess 비용은 여전히 빠져 있다. C의 실제 총비용은 위 값보다 크다.
- 사이클 1 지출: 위 7회 $49.4. 여기에 harness 오류로 뺀 4회 $6.5와, env 누출로 중단한 1회(비용 미측정)가 더해진다.

### 실패한 검사

| 검사 | 실패한 run |
|---|---|
| currency-jpy, currency-override, ship-location (desktop·mobile) | A-3, A-4, C-2 |
| hero-swipe@mobile | A-1, A-5 |
| listing-page-size-empty (desktop·mobile) | A-1 |
| rail-ranks (desktop·mobile) | A-4 |
| hero-autoplay, hero-wrap (desktop·mobile) | C-1 |

API 검사는 7회 모두 통과했다. 실패는 모두 화면 동작이다.

### harness 오류로 뺀 시도 (분모 밖)

C-1을 처음 돌릴 때 harness 결함으로 끊긴 5회다. 고친 뒤 C-1을 다시 돌렸다. 각각 `score --harness-error`로 이유를 남겼다.

- 이 시도들의 기록에는 각 시도 자신의 transcript, 사용량, phase 이력만 있다.
- 시도마다 runner worktree는 남기지 않았다(같은 경로를 다음 시도가 다시 썼다). 그래서 평가기는 채점할 build가 없었다. `ui.error`가 그 실패를 기록하고, S·G·build 지문은 비어 있다.
- 처음 올린 기록(bfdfe0a)에는 C-1 rerun의 리뷰 40회, build 지문, G가 이 행들에 잘못 들어가 있었다(#453 Codex 지적). harness가 옮겨 둔 run을 기록된 옛 경로, 곧 rerun의 project로 채점했기 때문이다. #452 ec58206·7df515a로 고치고 다시 채점했다.

| 기록 | 이유 | 고친 곳 |
|---|---|---|
| aborted-C-1-env-leak | controller가 운영자 세션의 env(`CLAUDE_CODE_SESSION_ID`)를 물려받음 | `CONTROLLER_ENV` 허용 목록 |
| harness-error-C-1-cwd | Bash cwd가 허용 디렉터리 밖으로 재설정됨 | `--add-dir ~/.agent-flow/worktrees` |
| harness-error-C-1-gitprotect | acceptEdits가 보호 경로 `.git` 쓰기를 막음 | `--permission-mode auto` |
| harness-error-C-1-approval | auto mode 분류기가 에이전트의 phase 자기 승인을 막음 | harness 승인자 |
| harness-error-C-1-archpending | 아키텍처 미정으로 설치되어 runner가 `architecture_policy_drift`로 멈춤 | `--architecture-mode clean` |

## 해석

1. **OYG에는 헤드룸이 있다〔measured〕.** 맨 모델(A)이 1/5다. 사전등록의 "3/3이면 과제를 어렵게 한다" 기준에 걸리지 않는다. devflow(A 5/5)와 달리 비교에 쓸 수 있다.
2. **C 2회는 둘 다 같은 곳에서 멈췄다〔measured〕.**
   - multi-review의 fix-loop가 3라운드 상한에 걸렸고, commit phase에 가지 못했다.
   - 에이전트는 자기 테스트가 통과한다고 보고했다(C-1 74/74, C-2 80/80). 그런데 비공개 평가기는 C-2에서 A-3과 같은 6개 화면 검사를 실패로 냈다.
   - 둘 다 `declared-blocked`이므로 사전등록 규칙상 S와 무관하게 실패다.
   - gates phase에 닿지 않았으므로 required gate는 한 번도 돌지 않았다(`gate_ledgers` 0).
3. **비용〔measured〕.** C는 run당 평균 $19.5, A는 $2.07이다(약 9.4배). 시간은 약 6.1배다.
4. **리뷰가 찾은 결함과 평가기가 찾은 결함이 다르다〔inferred〕.** C-2는 리뷰 3라운드를 돌고도 A-3·A-4와 같은 currency·ship-location 결함을 남겼다. 리뷰가 PRD의 화면 요구를 대조하지 못했을 가능성이 있다. transcript로 확인하지 않았다.

## 제안 (이 PR에서 바꾸지 않는다)

- fix-loop 상한에 걸린 run의 처리. 지금은 runner가 사람에게 넘기며 멈추고, harness는 이를 `blocked`로 센다.
  - runner 판정 코드는 금지 구역이므로 구현 PR로만 낸다.
  - 사전등록 v0의 주 비교(A vs C)와 성공 규칙은 바꾸지 않는다.
- 리뷰어 subprocess 비용 계측(지금 빠져 있음).
- 사이클 2에서 이 비용으로 n과 일정을 고정한다(사전등록 v1). C run당 약 $20이면 사이클당 $150으로 A·C 각 6회 정도다〔inferred〕.
