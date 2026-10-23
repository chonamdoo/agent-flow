# 인수인계: 어디까지 했나 (마지막 갱신 2026-10-23 03:00 UTC)

이 세션의 토큰이나 컨테이너가 끝나도 다른 계정이 이어서 할 수 있게, 끝난 것, 돌던 것, 다음 할 일을 순서대로 적는다. 환경과 명령은 `REPRODUCE.md`, 결과와 PR 지도는 `README.md`.

**중요.** 이 세션의 scratchpad(run 디렉터리, 기준 구현 checkout, mutant 작업 폴더)는 git에 없고 컨테이너와 함께 사라진다. 남는 것은 아래 "보존된 산출물"뿐이다. 돌던 작업은 §2의 명령으로 다시 돌린다.

## 1. 끝난 것

| 항목 | 결과 | 보존 위치 |
|---|---|---|
| harness 무결성·계측 수정 | 사전등록 성공 규칙, subagent·토큰 구성, checkout 선택, CI 미실행 gate, 옮긴 run 채점 버그 | PR #452(열림, merge 안 함) |
| OYG 사이클 1 | A 1/5, C 0/2(둘 다 fix-loop 상한 정지 + 평가기 실패) | PR #453 |
| 비공개 과제 지원 | `--case-dir`, seed patch | PR #454 |
| fix-loop 상한 재승인 | harness가 최대 2회 재승인("리뷰 한 번 더"). 상한 초기화 경로는 runner에 없음 | PR #455 |
| mobile-cart v0 | 기준 S=1, mutant 15/20, 맨 모델 3/3 → 은퇴 | 비공개 `feat/mobile-cart-holdout-v0` |
| mobile-cart v1 | 기준 S=1, mutant 18/20, 맨 모델 3/3 → 은퇴 | 비공개 `feat/mobile-cart-v1` |
| mobile-checkout 기준 구현 | S=1(28/28), 평가기 동결 `08b1e1d9…` | 비공개 `feat/mobile-checkout-v0` `reference/` |
| mobile-checkout 타당성 | mutant 18/20(0.9), 맨 모델 파일럿 **3/3 S=1**($3.72~4.73, 15~19분) → 기능 정확도로는 헤드룸 없음. 무결성·아키텍처·후속 변경·UI 비교용으로 쓴다 | 비공개 `feat/mobile-checkout-v0` 9eef97a |
| 클라우드 UI 검증 POC | 에뮬레이터 없이 Robolectric native graphics + Roborazzi로 Compose 화면을 찍고, 픽셀 차(채널 16) + semantics 트리 차로 비교. 기준 재실행 0 오탐, 간격·색·텍스트 결함 3종 모두 잡음 | 비공개 `mobile-checkout/visual-poc/`, `validity-reports/2026-10-23/visual/` |
| 프로그램 v1, 적대적 리뷰 2개, 조사, karpathy PR 검토, 프로그램 v2 | v2 승인 대기 | 이 폴더 |

## 2. 돌던 것

없음. 파일럿과 mutant는 끝났고 기록했다. PR #452~#456 CI 녹색, 열린 스레드 없음(새 세션에서 다시 확인).

## 3. 다음 할 일 (순서대로)

1. **사용자 결정 받기**(§4). v2가 승인되면 그 내용을 `PREREGISTRATION.md` v1에 옮긴다(PR, merge 안 함).
2. v2 §9 구현 순서
   1. 무결성 검사: 테스트 변조, stub, 원자 주장 감사
   2. 결정적 구조 지표와 **비공개 아키텍처 규칙**(mobile-checkout, OYG부터)
   2-1. **디자인 층과 UI 충실도**(v2 §3e, 사용자 요청): 과제별 디자인 명세, 좌표 기반 배치 검사, 토큰 준수, 시각 mutant 보정
   2-2. **UI 검증 평가기화**: 사용자 결정 "클라우드에서 UI가 맞게 구현됐는지 검증만 되면 된다" → 에뮬레이터(moru-artemis) 대신 Roborazzi POC를 쓴다. 남은 일: 화면 함수 서명을 계약에 고정, 디자인 명세(배치 순서·정렬·토큰)를 semantics 좌표 검사로, 픽셀 비교는 명세가 고정한 영역에만 허용치와 함께, 시각 mutant로 보정. 결과는 "UI 맞음" 통과/실패 한 항목
   3. **후속 변경 harness**와 과제별 후속 변경 2개. 기준 구현과 결함 주입본으로 먼저 검증한다
   4. A+P 조건, project-local workflow 설치기
   5. A/A 보정, 잡음 바닥
   6. 오토리서치 workflow track(karpathy PR 기능 10개)
   7. 심판 harness와 보정 세트(사람 라벨이 필요하면 사용자에게)
   8. 두 번째 holdout(헤드룸이 있는 과제. 지금까지 헤드룸은 OYG 같은 큰 웹 과제에서만 관찰됨), Q3 시뮬레이터
3. #454와 #455를 합친 로컬 브랜치로 mobile-checkout에서 B·C 설치를 확인한다(drive 없이 prepare만).

## 4. 사용자 결정 대기

1. 프로그램 v2 승인
2. 심판 검증용 사람 라벨 30~50개를 사용자가 맡을지, 심판 없이 갈지
3. 사이클 예산 $150 유지 또는 증액(후속 변경을 넣으면 4조건 1세트가 약 $53)
4. runner에 `continue --extend-fix-loop`(상한 연장 승인 경로) 구현 PR을 낼 권한. 자동 모드 분류기가 막음
5. 이전부터 보류 중: #442 이미지 빌드, draft #431, #433~#436, #438, #441

## 5. 사용자가 정한 운영 규칙 (지금까지)

- 기준 브랜치는 `experiment/epic-409`다. main과 태그는 건드리지 않는다.
- 금지 구역(hook, runner 판단, 승인 경로, 평가기와 case)은 구현 PR만 내고 merge하지 않는다. evals 전용 PR은 merge할 수 있다. 다만 최근 작업들(#452~#456)은 사용자 지시로 merge하지 않았다.
- `--dangerously-skip-permissions`는 금지한다.
- holdout 내용은 공개 저장소에 쓰지 않는다.
- 자율로 진행하되, 결과를 바꾸는 판단이나 비용이 큰 결정은 보고하고 승인을 받는다.
