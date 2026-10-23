# 인수인계: 어디까지 했나 (마지막 갱신 2026-10-23 01:46 UTC)

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
| 프로그램 v1, 적대적 리뷰 2개, 조사, karpathy PR 검토, 프로그램 v2 | v2 승인 대기 | 이 폴더 |

## 2. 돌던 것 (컨테이너가 사라지면 다시 돌린다)

| 작업 | 마지막 상태 | 다시 돌리는 법 |
|---|---|---|
| mobile-checkout 맨 모델 파일럿 A×3 | **A-1 S=1(28/28), 1,043초, $4.41**. A-2는 01:30 UTC 시작, A-3은 미시작 | `REPRODUCE.md` §3: `prepare --arm A --case-dir <private>/mobile-checkout --trial N`, `drive --timeout 7200 --budget 30`, `score --evaluator <private>/mobile-checkout/score.py`. #454 브랜치에서 |
| mobile-checkout mutant 20개(동결 평가기 1회) | M01~M07 채점 끝, M08 진행 중 | 기준 구현 checkout을 만든다: harness `prepare --arm A --case-dir <private>/mobile-checkout --trial 0`의 project에 `git am <private>/mobile-checkout/reference/reference.patch`. 그다음 `python3 -I <private>/mobile-checkout/validity.py mutants --reference <그 checkout> --output report.json`. 평가기 지문이 `08b1e1d9…`인지 먼저 확인한다 |
| PR 감시 | #452~#456 CI 녹색, 열린 스레드 없음 | 새 세션에서 각 PR의 CI와 리뷰를 확인 |

파일럿 해석: A-2·A-3도 S=1이면 mobile-checkout도 3/3이다. 사전등록 기준으로 **확정 holdout으로 쓸 수 없다.** 다만 v2의 Q2(축소판 비교)와 후속 변경 지표에는 쓸 수 있다. C가 성공하는 과제가 필요하기 때문이다.

## 3. 다음 할 일 (순서대로)

1. **mobile-checkout 결과 정리**: 파일럿 3회와 mutant 결과를 과제 `README.md`와 `validity-reports/2026-10-23/`에 기록하고 비공개 브랜치에 commit한다.
2. **사용자 결정 받기**(§4). v2가 승인되면 그 내용을 `PREREGISTRATION.md` v1에 옮긴다(PR, merge 안 함).
3. v2 §9 구현 순서
   1. 무결성 검사: 테스트 변조, stub, 원자 주장 감사
   2. 결정적 구조 지표와 **비공개 아키텍처 규칙**(mobile-checkout, OYG부터)
   2-1. **디자인 층과 UI 충실도**(v2 §3e, 사용자 요청): 과제별 디자인 명세, 좌표 기반 배치 검사, 토큰 준수, 시각 mutant 보정
   2-2. **에뮬레이터 시각 비교(moru-artemis)**: v2 §3e. 실행 위치(KVM 있는 곳)를 사용자와 정해야 한다. 이 클라우드 컨테이너에는 `/dev/kvm`가 없다. 소스는 사용자가 zip으로 준 moru-artemis-main(이 저장소에 넣지 않음, 출처 github.com/taehwandev/moru-artemis)
   3. **후속 변경 harness**와 과제별 후속 변경 2개. 기준 구현과 결함 주입본으로 먼저 검증한다
   4. A+P 조건, project-local workflow 설치기
   5. A/A 보정, 잡음 바닥
   6. 오토리서치 workflow track(karpathy PR 기능 10개)
   7. 심판 harness와 보정 세트(사람 라벨이 필요하면 사용자에게)
   8. 두 번째 holdout(헤드룸이 있는 과제. 지금까지 헤드룸은 OYG 같은 큰 웹 과제에서만 관찰됨), Q3 시뮬레이터
4. #454와 #455를 합친 로컬 브랜치로 mobile-checkout에서 B·C 설치를 확인한다(drive 없이 prepare만).

## 4. 사용자 결정 대기

1. 프로그램 v2 승인
2. 심판 검증용 사람 라벨 30~50개를 사용자가 맡을지, 심판 없이 갈지
3. 사이클 예산 $150 유지 또는 증액(후속 변경을 넣으면 4조건 1세트가 약 $53)
4. 에뮬레이터 시각 비교를 어디서 돌릴지(사용자 PC 에뮬레이터 / 실기기 / KVM 있는 환경)
5. runner에 `continue --extend-fix-loop`(상한 연장 승인 경로) 구현 PR을 낼 권한. 자동 모드 분류기가 막음
6. 이전부터 보류 중: #442 이미지 빌드, draft #431, #433~#436, #438, #441

## 5. 사용자가 정한 운영 규칙 (지금까지)

- 기준 브랜치는 `experiment/epic-409`다. main과 태그는 건드리지 않는다.
- 금지 구역(hook, runner 판단, 승인 경로, 평가기와 case)은 구현 PR만 내고 merge하지 않는다. evals 전용 PR은 merge할 수 있다. 다만 최근 작업들(#452~#456)은 사용자 지시로 merge하지 않았다.
- `--dangerously-skip-permissions`는 금지한다.
- holdout 내용은 공개 저장소에 쓰지 않는다.
- 자율로 진행하되, 결과를 바꾸는 판단이나 비용이 큰 결정은 보고하고 승인을 받는다.
