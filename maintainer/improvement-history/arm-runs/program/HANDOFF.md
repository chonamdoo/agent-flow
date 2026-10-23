# 인수인계: 어디까지 했나 (마지막 갱신 2026-10-23 05:15 UTC)

이 세션의 토큰이나 컨테이너가 끝나도 다른 계정이 이어서 할 수 있게, 끝난 것, 돌던 것, 다음 할 일을 순서대로 적는다.
- 환경과 명령: `REPRODUCE.md`
- 결과와 PR 지도: `README.md`
- 계획: `program-v2.md` → `program-v2.1.md` → `program-v2.2.md`
- 판정 규칙: `../PREREGISTRATION.md` v1 절(#459)

**중요.** 이 세션의 scratchpad(run 디렉터리, 기준 구현 checkout)는 git에 없다. 컨테이너와 함께 사라진다(오늘 두 번 재시작됨). 남는 것은 PR과 비공개 저장소뿐이다.

## 0. 이어받는 계정이 가장 먼저 읽을 것 (2026-10-23 10:00 UTC, 사용량 소진 직전 기록)

**지금 돌던 것**
- 사이클 2a(mobile-tabs v1)가 09:48 UTC에 시작했다. 순서는 AP-1, PS-1, AP-2, PS-2, AP-3, PS-3, B-1, D-1이다. 컨테이너 스크래치패드의 `cyc2a/run.sh`가 돌리며, 이 스크립트와 로그 사본은 비공개 저장소 `results/2026-10-23/scripts/`에 있다.
- 컨테이너는 사라질 수 있다. 새 세션에서는 다음 순서로 다시 만든다.
  1. 공개 저장소 #463 브랜치(`feat/epic-409-arm-ps`)를 worktree로 받는다(harness).
  2. 비공개 저장소 `chonamdoo/agent-flow-evals-private`의 `feat/mobile-tabs-v0` 브랜치를 받는다(평가기와 과제).
  3. `results/2026-10-23/scripts/cyc2a-run.sh`의 경로(S, V, worktree)를 새 환경에 맞게 고친다.
  4. 같은 순서로 다시 돌린다. 스크립트는 drive.json이 있는 run을 건너뛴다.
- 남은 run이 없으면 결과를 요약한다. 비교 기준:
  - A: A/A 6 run(n=6), 비공개 `results/2026-10-23/runs.json`에 있다.
  - 잡음 바닥: 비용 14%, 시간 10%.
  - 천장: mobile-tabs에서는 비열등과 비용만 판정한다.
- B-1·D-1에서 볼 것:
  - pending 아키텍처 때문에 막혔는가. 막혔으면 kit에 대한 발견으로 기록하고 `--architecture-mode clean`으로 다시 돌린다.
  - 리뷰어 비용 하한이 run 비용의 20% 이상인가. 그렇다면 runner 계측 PR을 낸다(merge하지 않는다).
  - B·D를 더 돌릴 회수는 남은 예산으로 정한다.

**Codex 클라우드나 Claude 클라우드에서 이어받는 법** (어느 agent든 같다)
1. 저장소 두 개를 받는다.
   - 공개: `chonamdoo/agent-flow`. 문서는 `docs/epic-409-program-record`(#456), harness는 `feat/epic-409-arm-ps`(#463, #462 위에 있다).
   - 비공개: `chonamdoo/agent-flow-evals-private`의 `feat/mobile-tabs-v0`. 평가기, 과제, `results/`가 있다.
   - holdout 내용(과제 PRD, 검사, 기준 구현)은 비공개 저장소에만 둔다. 공개 저장소와 실험 agent가 볼 수 있는 곳에 쓰지 않는다.
2. 읽는 순서: 이 HANDOFF §0 → `program-v2.md` → `program-v2.1.md`·`v2.2.md`·`v2.3.md` → `PREREGISTRATION.md`의 v1 개정 → 비공개 각 과제 README.
3. 실험 run은 harness `evals/arm_compare.py`의 `prepare → drive → score`로 돌린다. 그 뒤 비공개 `mobile-common/archcheck.py`로 구조 위반을 잰다. 스크립트 예시는 비공개 `results/2026-10-23/scripts/`에 있다.
   - `drive`는 Claude Code CLI(`claude -p`)를 headless로 띄운다. 비교 실험의 피실험 agent가 Claude이기 때문이다. Codex 환경에서 이어받아도 run은 Claude CLI와 인증이 있어야 돌릴 수 있다. 없으면 분석·문서·평가기·harness 작업만 하고 run은 Claude 환경에 맡긴다.
   - Android 검사에는 `ANDROID_HOME`(Android SDK)과 JDK가 필요하다(Robolectric, 에뮬레이터는 쓰지 않는다).
   - `--dangerously-skip-permissions`는 절대 쓰지 않는다.
4. 사이클 2a가 끝나면 이 파일 끝에 "사이클 2a 결과(자동 기록)"가 붙고, 비공개 `results/2026-10-23/cycle2a-runs.json`이 생긴다. 컨테이너가 그보다 먼저 사라졌다면 비공개 `scripts/cyc2a-run.sh`로 다시 돌린다. 이미 끝난 run은 건너뛰지만, 컨테이너가 사라졌다면 끝난 run도 다시 돌려야 한다.
5. 운영 규칙(§5)은 그대로다. main과 태그는 건드리지 않는다. 금지 구역은 구현 PR만 낸다. 최근 PR은 merge하지 않는다. 예산은 사이클당 약 $150이다.

**사용자 결정 (10:30 UTC)**: Codex에는 더 맡기지 않는다. 주간 사용량이 초기화되면 Claude가 원래 계획을 전부 이어서 한다.
- Codex가 낸 PR은 merge하지 않고 입력으로만 쓴다. Claude가 다시 시작하면 아래 순서로 처리한다.
  - 비공개 #3(holdout 후보, 숨은 검사 38개, mutant 20개, lint 검토 문서): Android SDK 환경에서 실행 검증한다. 기준 구현을 독립적으로 짜서 확인하고, mutant 검증까지 마친 뒤 동결한다. 쓸 만하지 않으면 새로 만든다.
  - 공개 #464(`TASK-STATUS`로 이름 변경): 다음 판에 쓴다. 사이클 2와 비교가 끊기므로 사이클 2가 끝날 때까지 쓰지 않는다.
  - #465(CODEX-HANDOFF.md): 참고만 한다.
- 다시 시작할 때 순서:
  1. 사이클 2a 결과를 확인하고 판정한다. HANDOFF 끝의 자동 기록을 본다. 없으면 다시 돌린다.
  2. 두 번째 holdout을 마무리한다(비공개 #3 검증).
  3. A 3회로 그 holdout의 난이도를 확인한다.
  4. 사이클 2b를 돌린다.
  5. Q2 오토리서치를 한다.
  6. 버그·리팩터 과제를 만들고 Q4·Q5 시작 조건을 판단한다.
- 아래 "Codex 전용 작업 목록"은 기록으로만 남긴다. 더 쓰지 않는다.

**Codex 전용 작업 목록** (실험 run 없이 할 수 있는 일. Codex는 이것만 하고 이것만 기록한다)
1. **두 번째 holdout 제작**(가장 중요): A가 1~2/3만 성공할 난이도의 모바일 과제다.
   - 만들 것: PRD, 계약, 숨은 검사, 독립 기준 구현(S=1), mutant 20개(18개 이상 잡혀야 한다), 동결 fingerprint. 형식은 비공개 `mobile-tabs/`를 따른다.
   - 위치: 비공개 저장소의 새 브랜치 `feat/holdout-2`.
   - A로 난이도를 확인하는 run은 Claude 환경에 남긴다.
2. **사이클 2a 판정**: HANDOFF 끝의 "사이클 2a 결과(자동 기록)"가 올라온 뒤에 한다.
   - 기준은 §0이다. A는 n=6, 잡음 바닥은 비용 14%·시간 10%, 천장 때문에 비열등과 비용만 본다.
   - B·D에서 pending 때문에 run이 멈췄는지, 리뷰어 비중이 20% 이상인지 확인한다.
3. **`EVAL-STATUS` 중립 이름 변경 PR**: 다음 판에 쓴다. 지금 merge하지 않는다.
4. **archcheck 규칙을 lint로 바꿀 수 있는지 검토**: pstack `correct`에서 배울 것. 문서만 쓴다.

- 기록 위치: 새 브랜치(`codex/...`)와 그 PR. 이 브랜치(`docs/epic-409-program-record`)와 비공개 `feat/mobile-tabs-v0`에 직접 push하지 않는다. 사이클 2a 자동 기록이 이 두 브랜치에 push한다.
- 하지 않는 것: 실험 run(`arm_compare.py drive`). 피실험 agent가 Claude라 Codex에서는 돌릴 수 없고, 모델을 바꾸면 비교가 끊긴다.

**오늘(10-23) 확정한 것**
- 후속 변경 지표 ②는 판정에서 뺐다. 연쇄 F1→F2→F3을 정상과 망가뜨린 기반에서 각 2회 돌렸고 12/12 S=1이었다.
- A/A 보정: 6/6 성공. 잡음 바닥은 비용 $0.65(14%), 시간 117초(10%)다.
- mobile-tabs v1 평가기를 a225eeee로 동결했다.
- pstack 스킬 전체 검토 결과로 arm PS를 추가했다(#463). 근거는 `program-v2.3.md`와 `research/pstack-skills-review.md`다.
- 사이클 2 전에 고친 harness·과제 결함 셋:
  - AP의 CLAUDE.md가 seed의 .gitignore에 걸려 커밋되지 않았다. `--force`로 고쳤다(#462 6aa926a).
  - 모바일 case의 설치 옵션 local에 계약 파일이 없었다. kit 기본값(pending)으로 바꿨다(비공개 056ecac).
  - 연쇄 후속 채점이 앞 단계의 MC224 면제를 넘기지 않았다. `--after`로 고쳤다(비공개 6a1c160).
- 사용자 결정: 모든 판단은 "베스트 추천안"대로 한다(§4).

**다음 순서 (권장)**
1. 사이클 2a를 마무리하고 요약한다(위).
2. **더 어려운 두 번째 holdout을 만든다.** A가 1~2/3만 성공하는 난이도가 목표다. 지금 두 모바일 과제는 A가 천장에 닿아 품질 차이를 볼 수 없다. 이 일을 계획의 앞으로 당긴다.
3. 사이클 2b(mobile-checkout과 새 holdout): 살아남은 arm만 돌린다.
4. Q2 오토리서치(OYG)를 한다. 그다음 버그·리팩터 과제를 만들고 Q4·Q5 시작 조건을 판단한다.
5. 다음 판에서 할 것: 완료 선언 이름 `EVAL-STATUS`를 평가 단서가 없는 중립 이름으로 바꾼다. 사이클 1과의 비교를 끊는 시점에 한다.

**PR 상태**: #456~#463 모두 CI 녹색이고 열린 스레드가 없다. 규칙대로 하나도 merge하지 않았다.

**운영상 주의**
- **세션이 오래 놀면 컨테이너가 회수된다**: 10-23 11:00~12:51 사이에 turn도, harness가 추적하는 백그라운드 작업도 없자 회수됐고, 돌던 사이클 2a의 PS-1이 끊겼다. PS-1은 `harness-error-PS-1-restart`로 빼고 12:52에 다시 시작했다. 긴 run을 돌릴 때는 harness가 추적하는 백그라운드 대기(최대 2시간)를 계속 다시 걸어 둔다.
- 컨테이너 디스크 할당은 작다. 끝난 run의 build와 .gradle은 지운다. 스크립트에는 3 GB 미만이면 기다리는 대기가 들어 있다.
- 공용 /tmp 삭제와 돌고 있는 프로세스 kill은 권한에서 거부된다.
- 백그라운드 대기는 최대 2시간이다.

## 1. 끝난 것

### harness (public, 모두 merge 안 함)

| PR | 내용 | 상태 |
|---|---|---|
| #452 | 채점·계측 결함 수정 | CI 녹색 |
| #453 | OYG 사이클 1 결과 | 녹색 |
| #454 | 외부 과제 `--case-dir` | 녹색 |
| #455 | fix-loop 상한 재승인 | 녹색 |
| `feat/epic-409-integration` | #454 + #455 통합 브랜치(PR 없음) | |
| #457 | 조건 D(`default`), phase 기록, 오케스트레이션 비용 비율 | 녹색, Codex 지적 2건 반영 |
| #458 | 무결성 검사(테스트 변조·stub·근거 없는 주장·완료 뒤 미commit) | 녹색, 2건 반영 |
| #459 | 사전등록 v1(문서) | 녹색, 1건 반영 |
| #460 | 후속 변경 harness(`followon`, 블라인드) | 녹색, 1건 반영 |
| #463 | arm PS(pstack 원칙을 CLAUDE.md로, `ps-v1`), 출처 `research/pstack-skills-review.md` | 새로 올림 |
| #462 수정 6aa926a | AP가 CLAUDE.md를 `git add --force`한다. mobile seed의 .gitignore가 CLAUDE.md를 무시해 prepare가 실패했다. 사이클 2 전에 mobile-tabs smoke로 발견했고 #463에도 merge했다 | 새로 올림 |
| #461 | 리뷰어 비용 하한, 모델 가용성 `preflight` | 녹색, 2건 반영 |
| #462 | 조건 A+P(고정 8원칙 CLAUDE.md) | 녹색, 지적 없음 |

쌓인 순서: integration → #457 → #458 → #460 → #461 → #462. #459는 integration 위에 따로 있다.

### 비공개 평가기 (`chonamdoo/agent-flow-evals-private`, 브랜치 `feat/mobile-tabs-v0`. 앞선 과제를 모두 포함)

| 과제 | 결과 |
|---|---|
| mobile-checkout | 맨 모델 3/3, mutant 18/20 |
| **mobile-tabs**(무신사 실물 탭 구조, UI) | 기준 구현 29/29, mutant 19/20, 맨 모델 2/3(동결 기준). 실패 하나(MC332)는 검사가 PRD보다 엄격해서 났다. PRD에 충실한 판정으로는 3/3(탐색) |
| `mobile-common/archcheck.py` | 비공개 아키텍처 규칙 AR1~8. 기준 구현 2개·맨 모델 run 4개에서 0건, 결함 주입 10건을 모두 잡음 |
| mobile-checkout 후속 F1·F2·F3 | 독립 기준 구현으로 검증. **연쇄 v1 민감도도 실패**: 기반 2개 × 2회 × F1→F2→F3에서 12/12 S=1이었다. 연쇄 비용은 정상 $4.75, 망가뜨림 $5.07(n=2, 회차 간 차이가 더 크다). 새 위반은 0이다. **② 지표는 판정에서 빼고 탐색 지표로만 기록한다.** 구조 품질은 archcheck 위반으로 판정한다 |

### 검토와 결정

- pstack 스킬 전체(57개와 플레이북 23개): arm PS를 추가했다. 나머지는 하위 agent, 사람, PR이 필요하거나 방법론 참고용이다. `program-v2.3.md`

- 플로우 선택(Q4, pstack): 나중에. `program-v2.1.md` §3
- 역할별 모델(Q5, setup-pstack): 나중에. 리뷰어 계측부터. `program-v2.2.md`
- 사용자 결정: v2 승인, 심판 없이, $150/사이클, runner 변경 없음, UI는 클라우드(Robolectric), 추천안대로 진행.

## 2. 돌던 것

§0을 본다(사이클 2a). 앞선 연쇄 후속 실험과 A/A 보정은 끝났고 기록했다.

## 3. 다음 할 일 (순서대로)

1. ~~후속 변경 F v1~~ 완료. ②는 탐색 지표로 내렸다. 다시 살리려면 사용자 결정이 필요하다(§4).
2. ~~mobile-tabs v1~~ 완료: 평가기 a225eeee로 동결. 기준 구현과 A-3 모두 S=1이고, M02와 M17을 모두 잡는다.
3. ~~A/A 보정~~ 완료 (mobile-tabs v1, A 두 벌 × 3)
   - 6/6 성공, 새 위반 0, 무결성 0. 두 벌 사이에 차이가 없다.
   - 잡음 바닥(3회 대 3회, 평균 차이 2σ): 비용 $0.65(14%), 시간 117초(10%). 이 폭 안은 차이로 보지 않는다.
   - **천장**: A가 이미 다 통과하므로 mobile-tabs에서는 비열등과 비용만 판정할 수 있다. 품질 차이는 mobile-checkout과 두 번째 holdout에서 본다.
   - A/A의 6 run을 사이클 2a의 A(n=6)로 쓴다.
4. **사이클 2**($150): mobile-tabs + mobile-checkout
   - A·AP·**PS**·B·D 각 3회, C는 참조 1~2회(`program-v2.3.md`). 예산을 넘으면 2a(tabs)와 2b(checkout)로 나눈다
   - 무결성, 아키텍처 규칙, UI, phase 기록, 리뷰어 하한
   - 이 사이클에 쓰는 harness는 **#463** 브랜치(#462 위에 arm PS를 더한 것)다.
5. 오토리서치(Q2): OYG에서 축소판 탐색
6. 두 번째 holdout으로 확정. 버그·리팩터 과제를 만든 뒤 Q4·Q5 시작 조건을 판단한다.

## 4. 사용자 결정 (2026-10-23 09:05 UTC, "베스트 추천안대로")

- 사이클 2a 구성: AP·PS 각 3회, B·D는 먼저 1회씩 돌려 비용을 잰다. A는 A/A 보정 6 run을 그대로 쓴다. B·D를 더 돌릴 회수는 남은 예산으로 정한다.
- ② 후속 지표는 지금 살리지 않는다. 탐색 지표로만 기록한다. 사이클 2에서 archcheck에 arm 간 구조 차이가 보이면 그때 더 약한 고정 후속 모델로 다시 검토한다.
- 리뷰어 토큰 정확 측정(runner 변경)은 지금 하지 않는다. B·D 파일럿에서 리뷰어 하한이 run 비용의 20% 이상이면 구현 PR을 낸다(merge하지 않는다).
- 이전부터 보류 중인 것: #442 이미지 빌드, draft #431, #433~#436, #438, #441.

## 5. 운영 규칙

- 기준 브랜치는 `experiment/epic-409`다. main과 태그는 건드리지 않는다.
- 금지 구역(hook, runner 판단, 승인 경로, 평가기와 case)은 구현 PR만 내고 merge하지 않는다. 최근 PR은 모두 merge하지 않는다(사용자 지시).
- `--dangerously-skip-permissions`는 쓰지 않는다. holdout 내용은 공개 저장소에 쓰지 않는다.
- 평가기를 동결한 뒤 결과를 보고 검사를 고치면, 그 뒤 수치는 탐색으로만 적는다.
- 컨테이너 재시작에 대비해 긴 run은 `setsid nohup`으로 띄운다. 이미 돈 run은 건너뛰게 짠다. 끊긴 run은 harness 오류로 따로 두고(분모 밖) 새로 돌린다.

## 사이클 2a 결과 (자동 기록 2026-10-23T17:47:36Z, 해석 전 원자료)

| run | S | 선언 | 무결성 | 시간(초) | 비용 | 새 위반 | 리뷰어 작업 |
|---|---|---|---|---|---|---|---|
| AP-1 | 1 | declared-complete | 0 | 1770 | $6.68 | 0 | None |
| AP-2 | 0 | declared-complete | 0 | 1249 | $4.80 | 0 | None |
| AP-3 | 1 | declared-complete | 0 | 1175 | $4.82 | 0 | None |
| B-1 | 1 | declared-blocked | 0 | 6907 | $27.60 | 0 | 160 |
| D-1 | 1 | undeclared | 0 | 2723 | $13.39 | 0 | 30 |
| PS-1 | 1 | declared-complete | 0 | 1631 | $5.22 | 0 | None |
| PS-2 | 1 | declared-complete | 0 | 1469 | $5.01 | 0 | None |
| PS-3 | 1 | declared-complete | 0 | 1564 | $5.57 | 0 | None |
| harness-error-PS-1-restart | None | None | None | None | $0.00 | None | None |

AP: 비용 중앙값 $4.82 (범위 $4.80~$6.68), 성공 2/3

PS: 비용 중앙값 $5.22 (범위 $5.01~$5.57), 성공 3/3

기준 A(A/A n=6): 비용 중앙값 $4.69, 범위 $4.01~$5.21, 6/6 성공. 잡음 바닥은 비용 14%, 시간 10%.

해석과 판정은 아직 하지 않았다. 이어받는 쪽이 §0 기준으로 판정한다. run별 상세는 비공개 `results/2026-10-23/cycle2a-runs.json`에 있다.
