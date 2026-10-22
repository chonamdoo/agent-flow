# full-feature phase 분류와 축소 순서 (#429)

기준: `experiment/epic-409`의 `src/agent_flow/workflows/full-feature.yaml`. 2026-10-22에 작성했다.
이 문서는 설계 기록이다. 여기서 정한 것은 순서와 각 변경이 함께 바꿔야 할 것이다. 실제 삭제는
#417의 ablation이 측정으로 정한다.

## 외부 비판에 대한 답 (요약)

1. **"시간이 줄면 토큰을 더 써도 된다"는 결정 규칙으로는 맞다. 하지만 agent-flow가 그 전제를 만족하는지는 아직 재지 않았다.**
   - 토큰을 더 써서 벽시계를 줄인 측정은 병렬화되는 일에서만 나왔다.
   - 시간은 run 하나의 길이가 아니라 **성공 1건까지의 시간**(재시도·사람 재작업 포함)으로 잰다.
   - 유일한 실측 run(13/24 phase에서 멈춤)은 근거가 되지 못한다.
2. **비용 허용 규칙은 `ΔC ≤ 0.5 × ΔA_h × w` 하나다.**
   - ΔC: 성공 1건당 추가 실청구 비용. ΔA_h: 절감한 사람 능동 시간(시간). w: 시급.
   - ΔA_h는 평가 run에서 잴 수 없다. 사람이 참여하는 A 대 C 파일럿에서만 잰다(#410).
   - 상대 비율 표("30% 줄면 6배")는 공식과 맞지 않고 구간 경계에서 끊겨서 폐기했다.
3. **24는 정의된 phase 수이고 비용 단위가 아니다.** 아래 「셈」을 본다.
   - 무엇이 비용을 지배하는지는 #411 계측 뒤에 정한다.
   - 정적 예산(bytes/4)으로는 리뷰어가 약 70%다. 실측 controller는 13 phase에서 캐시 읽기만 약 2,700만 토큰이었다.
4. **"모델이 좋아지면 필요 없다"는 절차 phase에는 맞고, 판정 장치에는 맞지 않는다.**
   - 최신 모델도 거짓 완료와 평가기 공략을 보인다(#409 배경).
   - 그래서 판정(관측·장부·상한·승인)은 남긴다. 서술형 절차 phase는 측정으로 하나씩 뺀다.

## 셈

- **정의된 phase 24개.** domain-grill부터 handoff까지다.
- **정상 경로 21개.** 조건부 수정 phase 3개(`fix-loop`, `pr-comment-fix`, `pr-ci-fix`)를 뺀다.
- **LLM artifact 단계 20개.** 정상 경로에서 runner 소유 `gates`(`runner.py`의 `RUNNER_OWNED_PHASES`)를 뺀다.
- 반복 경로가 있으므로 24는 최대 실행 길이도 아니다. fix-loop·refactor·slice-plan은 collector별로 최대 3라운드(`FIX_LOOP_MAX_ROUNDS`)를 더 돈다.

## 분류

| 분류 | phase | 처리 | 상태 |
|---|---|---|---|
| runner 이관 | `worktree`, `run-start`, `pr-watch`, `merge`의 기계적 부분 | 사람이 리뷰하는 PR로 처리한다. autoresearch 루프의 편집 대상이 아니다(금지 영역 "runner 판정 코드"). | merge 선행 조건 #426은 PR #434(draft, merge 금지) |
| 병합·ablation | `product-brief`, `plan-review`, `architecture-review`, `comment-authoring` | #417에서 하나씩 뺀다. 아래 명세의 상태기계 변경을 한 PR에 함께 넣는다. | 명세 작성(이 문서) |
| 크기별 생략 | `domain-grill`, `ddd-design` | 작업 크기·위험도 라우팅. #410의 크기 층 결과를 보고 정한다. | 대기(#410) |
| 유지 | `prd` 장부, `red`/`green` 관측, `gates`, `fix-loop` 상한, `merge-approval` | 결정론 판정이다. 모델 세대와 무관하게 남긴다. | — |

[추론] 병합·ablation 후보 4개가 모두 빠지면 LLM 단계는 20개에서 16개가 된다. runner 이관 4개까지 더하면 12개다. 일부가 ablation에서 살아남는다고 보면 약 13~15개다. 이 숫자는 측정값이 아니라 설계 추정이다.

## runner 이관 범위와 순서

- **대상**:
  - `worktree`·`run-start`의 기록 작성. checkout·binding은 이미 runner가 한다.
  - `pr-watch`의 관측. `agent-flow pr-watch` 결과를 runner가 직접 route key로 읽는다.
  - `merge`의 기계적 부분. 승인된 head의 merge 실행과 결과 기록이다.
- **이관하지 않는 것**:
  - 충돌 해결. 판단이 필요하고, 해결 commit은 재승인 대상이다.
  - PR 본문·handoff 서술.
- **merge 순서**: 다음이 먼저 고정돼야 한다.
  1. #426: merge 승인을 HEAD commit에 결속한다(PR #434).
  2. 충돌 해결 commit은 publish → CI → 재승인 → merge 순서를 밟는다. PR #434가 merge 진입·완료에서 HEAD가 다르면 `merge-approval`로 되돌린다.
  3. 그다음 merge 실행을 runner로 옮긴다.
- **서버 측 보장은 별개다.** branch protection과 required review는 #413이 맡는다. runner 이관이 그것을 대신한다고 쓰지 않는다.
- 각 이관은 별도 PR이다. runner·승인 경로를 바꾸므로 에픽 규칙상 구현 PR까지만 하고 사람이 리뷰한다.

## ablation 후보별 상태기계 변경 명세

공통 규칙:

- **한 PR에 후보 하나.** 삭제 commit 하나로 둔다. subtract keep은 잠정이고 최종 168 unit 게이트에서 비열등을 확인해야 확정된다.
- **default workflow에 같은 phase가 있으면 같이 다룬다**(comment-authoring은 default에도 있다).
- **함께 바꿔야 하는 것.** 하나라도 빠지면 검증 공백이나 의도하지 않은 상한 공유가 생긴다.
  - 입력 참조(다른 phase prompt가 읽는 artifact)
  - route와 collector
  - collector별 라운드 상한
  - 되돌림 시 무효화 범위(`_plan_transition`은 목적지부터 현재 phase까지의 artifact를 지운다)
  - resume과 전이 원장: 진행 중인 run의 `phase_index`가 가리키는 phase가 사라지면 cursor drift다. run pin(`workflow_pin`)이 옛 YAML을 고정하므로 진행 중인 run은 옛 정의로 끝난다. 새 정의는 새 run부터 적용된다.
  - parity 검사(`scripts/check-agent-flow-parity.mjs`)와 이 phase id를 직접 쓰는 코드·테스트
- **측정**:
  - phase_eval 단위로 먼저 거른다. n≥8, 이항 지표는 Fisher, 토큰·시간은 log-ratio bootstrap을 쓴다.
  - 확정은 OYG(#428)에서 n=10으로 한다.
  - 1차 지표와 비열등 margin은 실행 전에 고정한다.

### 1. `product-brief`

| 항목 | 현재 | 변경 |
|---|---|---|
| 입력 참조 | `prd`가 `domain-grill.md`와 `product-brief.md`를 읽는다 | `prd` prompt에서 `product-brief.md` 참조를 지운다. "target user / problem / cut list"는 prd의 scope·non-goals 절이 흡수한다. |
| route | 없음(다음 phase로) | 없음. domain-grill → prd |
| collector·상한 | 해당 없음 | 해당 없음 |
| 무효화 | 되돌아오는 route 없음 | 없음 |
| 코드 결합 | `lib/skill-selection.mjs`·`bin/agent-flow-kit.mjs`·parity script가 `product-brief` skill을 안다 | skill 설치 목록은 그대로 둔다(다른 workflow가 쓸 수 있다). parity script의 skill 기대만 확인한다. |
| 잃을 수 있는 것 | cut list, build/defer/cut 결정 | prd의 non-goals 품질. oracle의 `plan` 축(slice-plan에만 있는 결정)으로 본다. |

### 2. `plan-review`

| 항목 | 현재 | 변경 |
|---|---|---|
| 입력 참조 | `slice-plan.md`만 읽는다 | 없음 |
| route | `approve → ddd-design`, `request-changes → slice-plan` | slice-plan → ddd-design 직진 |
| collector·상한 | `slice-plan`이 collector다(rejection target). 별도 3라운드 예산이 있다. | slice-plan은 collector가 아니게 된다. 예산 소멸을 명시한다. |
| 무효화 | request-changes 때 slice-plan..plan-review | 없음 |
| 코드 결합 | `plan-reviewer` skill required. 이 id를 직접 쓰는 코드는 없다(테스트 1개). | 테스트 갱신 |
| 잃을 수 있는 것 | 빠진 단계·순서·과대 slice·모호한 검증 | 이 점검은 `green`의 plan 축 oracle과 multi-review의 계획 대조가 일부 흡수한다. 흡수 여부는 fixloop-wrongplan 사례(잘못된 계획)로 잰다. |

### 3. `architecture-review`

| 항목 | 현재 | 변경 |
|---|---|---|
| 입력 참조 | `domain-grill.md`, `ddd-design.md`와 코드를 대조한다 | 이름 drift·소유권 점검을 multi-review의 architecture-design angle이 맡는다. 그 angle prompt에 `domain-grill.md`·`ddd-design.md` 대조를 명시한다. |
| route | `approve → gates`, `request-changes → refactor`. `blocked` route 없음 | multi-review `approve → gates`. architecture 거절은 multi-review의 `request-changes → fix-loop`로 합쳐진다. |
| collector·상한 | `refactor`가 collector(3라운드). fix-loop와 별개 예산 | refactor는 collector가 아니게 된다. architecture 거절이 fix-loop 예산(3)을 공유한다. 예산 축소인지 명시하고, 필요하면 fix-loop 상한을 다시 정한다. |
| 무효화 | request-changes 때 refactor..architecture-review | fix-loop 경로의 무효화(fix-loop → comment-authoring → multi-review)로 대체 |
| 코드 결합 | `runner.py`의 DDD 전용 분기 2곳(`missing_ddd_design_terms` 차단, blocked artifact 자동 재검증), `adapters/base.py`의 DDD 블록 대상, `REVIEW_PHASES`, `CODE_PHASES`, `phase_budget_compare.CONTROLLER_PHASES`, parity script route 검사 3개 | DDD 용어 검사는 multi-review 진입이나 ddd-design 완료로 옮긴다. 옮기지 않으면 DDD 강제가 사라진다. 나머지는 id 제거 |
| 잃을 수 있는 것 | 계약 수준 구조 리뷰가 두 번째로 독립 실행되는 것(리뷰어 2개 이상 × 2회) | 비용은 리뷰어 subprocess의 상당 부분이다. 품질 손실은 mode-rule 결함 탐지율(phase_eval review)로 잰다. #425 파일럿에서 단일 결함은 모든 angle이 잡았으므로, 손실이 있다면 다결함 사례에서 보인다. |

### 4. `comment-authoring`

| 항목 | 현재 | 변경 |
|---|---|---|
| 입력 참조 | 변경 diff | 주석 규칙(`comment-authoring-discipline`)을 refactor·fix-loop·pr-*-fix의 required skill로 옮긴다. #416 이후 작성 phase는 본문을 inline으로 받는다. |
| route | refactor·fix-loop `default → comment-authoring`, comment-authoring `default → multi-review`(default workflow는 `final-review`) | refactor·fix-loop `default → multi-review`(default는 `final-review`) |
| collector·상한 | 해당 없음(collector 아님) | 해당 없음 |
| 무효화 | fix-loop 되돌림 경로의 일부 | 경로가 한 칸 줄어든다 |
| 코드 결합 | parity script의 route 기대 4곳, `comment-checker` hook 마커, #415의 범위 대조(`SCOPE_CONFIRMATION_PHASES = {"comment-authoring"}`, PR #436) | parity 기대 갱신. 범위 대조는 multi-review 진입 직전 phase(refactor·fix-loop 완료)로 옮긴다. 옮기지 않으면 #415의 사용자 확인이 사라진다. |
| 잃을 수 있는 것 | WHAT 주석 제거 | evals의 comment_norm은 baseline 100%였다(evals/README). 이 후보가 가장 먼저 시도할 만하다. |

## 권장 시도 순서

1. **comment-authoring.** 가장 싸고, 측정 근거(comment_norm baseline 100%)가 이미 있다. #415 범위 대조를 옮기는 것만 주의한다.
2. **product-brief.** route가 없어 상태기계 변경이 가장 작다.
3. **plan-review.** collector 예산 소멸을 명시해야 한다.
4. **architecture-review.** 결합이 가장 많다. DDD 용어 검사 이전과 fix-loop 예산 공유를 먼저 설계한다. #419(리뷰어 fan-out) 측정과 같이 본다.

## 연결

- #411: 비용 계측(run_usage.py, PR #430 merge됨)
- #426: merge 이관 선행(PR #434)
- #410: 비용 허용 식과 사람 파일럿(이 문서 「요약」 2)
- #417: ablation 실행
- #419: 리뷰어 수
- #420: subtract 루프
- #425: 리뷰 평가 해상도(PR #437)
- #428: OYG 벤치마크
