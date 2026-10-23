# 인수인계: 어디까지 했나 (마지막 갱신 2026-10-23 05:15 UTC)

이 세션의 토큰이나 컨테이너가 끝나도 다른 계정이 이어서 할 수 있게, 끝난 것, 돌던 것, 다음 할 일을 순서대로 적는다.
- 환경과 명령: `REPRODUCE.md`
- 결과와 PR 지도: `README.md`
- 계획: `program-v2.md` → `program-v2.1.md` → `program-v2.2.md`
- 판정 규칙: `../PREREGISTRATION.md` v1 절(#459)

**중요.** 이 세션의 scratchpad(run 디렉터리, 기준 구현 checkout)는 git에 없다. 컨테이너와 함께 사라진다(오늘 두 번 재시작됨). 남는 것은 PR과 비공개 저장소뿐이다.

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

없다. 연쇄 run 12개를 채점하고 기록했다. 그 과정에서 생긴 일:
- 디스크가 다 차 ref-1 run이 무효가 됐다. harness 오류로 분모에서 빼고 다시 돌렸다.
- 연쇄 채점에서 앞 단계 제외가 이어지지 않던 결함을 판정 전에 고쳤다(`--after`).
- 큰 run을 돌리기 전에는 `df /`를 확인한다. 끝난 run의 build와 .gradle은 지운다.

- **사이클 2a 대기열**: 스크래치패드 `cyc2a/run.sh`(로그 `cyc2a/run.log`)가 A/A가 끝나면 시작한다. 순서는 AP-1, PS-1, AP-2, PS-2, AP-3, PS-3, B-1, D-1이고 harness는 `wt-ps`(#463)다.
- 준비 단계 시험에서 B·D 설치 실패를 발견해 고쳤다. 모바일 case.json이 계약 없이 `--architecture-mode local`을 지정하고 있었다. 이제 kit 기본값(pending)을 쓴다(비공개 056ecac). pending 때문에 run이 멈추면 kit에 대한 발견으로 기록하고 clean으로 다시 돌린다.

## 3. 다음 할 일 (순서대로)

1. ~~후속 변경 F v1~~ 완료. ②는 탐색 지표로 내렸다. 다시 살리려면 사용자 결정이 필요하다(§4).
2. ~~mobile-tabs v1~~ 완료: 평가기 a225eeee로 동결. 기준 구현과 A-3 모두 S=1이고, M02와 M17을 모두 잡는다.
3. **A/A 보정**: 같은 조건(A) 2벌 × 3회로 잡음 바닥을 잰다.
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
