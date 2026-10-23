# #409 실험 기록 색인 (2026-10-23 기준)

다른 Claude 세션이나 계정이 이 실험을 이어받거나 다시 돌릴 수 있게 남긴 기록이다.

- **이어서 할 때는 `HANDOFF.md`부터 읽는다**: 끝난 것, 돌던 것, 다음 할 일.
- 재현 절차는 `REPRODUCE.md`에 있다.

## 문서

| 파일 | 내용 |
|---|---|
| `../PREREGISTRATION.md` | 사전등록 v0과 v1 초안 메모(이 브랜치 시점). fix-loop 재승인 규칙은 #455에 있다 |
| `program-v1.md` | 실험 프로그램 v1 초안(조건 탐색, 그릴링, 품질 평가 확장) |
| `reviews/BRIEF.md` | 적대적 리뷰 요청문(두 리뷰어 공통) |
| `reviews/claude-adversarial-v1.md` | Claude 서브에이전트의 v1 적대적 리뷰(독립) |
| `reviews/codex-adversarial-v1.md` | Codex CLI 0.163.0의 v1 적대적 리뷰(독립) |
| `research/architecture-quality-measures.md` | 아키텍처·SE 품질 측정 문헌 조사(출처 URL 포함, 원문을 직접 읽지 못한 수치가 있음) |
| `research/karpathy-autoresearch-prs.md` | karpathy/autoresearch PR에서 가져올 루프 기능 |
| `program-v2.md` | 리뷰·조사·PR 검토를 반영한 v2(**승인됨**) |
| `program-v2.1.md` | v2 뒤 변경: mobile-tabs 과제, 조건 D(`default`), phase 기록, 채택·출시 규칙, Q4(플로우 선택) 보류 조건 |
| `program-v2.2.md` | 역할별 모델 분리 검토: 나중에, 리뷰어 계측·모델 가용성 점검부터 |
| `reviews/*-models.md`, `research/setup-pstack-summary.md` | 역할별 모델 분리 적대적 리뷰 2개(Claude, Codex)와 setup-pstack 요약 |
| `reviews/*-router.md`, `research/pstack-summary.md` | "에이전트가 플로우를 고른다" 적대적 리뷰 2개(Claude, Codex)와 pstack 요약 |

## 지금까지의 결과 (measured)

| 과제 | 결과 | 기록 |
|---|---|---|
| devflow(Python, 1차) | A 5/5, B 5/5, C 2/5(#449 규칙으로 재채점), C′ 5/5. 천장 과제 | `../2026-10-22-devflow/`, #452 |
| OYG(웹 풀스택, 개발 과제) | A 1/5, C 0/2. C 2회 모두 multi-review fix-loop 상한 정지. 비용 A $2.08, C $19.5/run | #453 |
| mobile-cart v0(holdout 후보) | 기준 구현 S=1, mutant 15/20, 맨 모델 3/3 → 너무 쉬워 은퇴 | 비공개 저장소 |
| mobile-cart v1 | 기준 구현 S=1, mutant 18/20, 맨 모델 3/3 → 은퇴 | 비공개 저장소 |
| mobile-checkout(대형 holdout) | 기준 구현 S=1(검사 28/28), mutant 18/20, 맨 모델 3/3(평균 $4.29, 17분) → 기능으로는 헤드룸 없음 | 비공개 저장소 |
| mobile-tabs(탭 구조 holdout 후보) | 기준 구현 S=1(29/29), 평가기 `b3051a4b…`. mutant·맨 모델 파일럿 진행 중 | 비공개 저장소 |
| 클라우드 UI 검증 POC | Roborazzi(Robolectric native graphics) 렌더 + 픽셀·semantics 비교. 기준 재실행 오탐 0, 간격·색·텍스트 결함 3/3 잡음 | 비공개 저장소 |

## PR과 브랜치

**chonamdoo/agent-flow** (base `experiment/epic-409`)
- merge됨: #449(arm 무결성), #450(사전등록 v0), #451(OYG 과제와 drive).
- 열림, **merge하지 않음**
  - #452: 채점·계측 결함 5건과 checkout 선택 수정
  - #453: 사이클 1 결과. base는 epic이고 #452 규칙으로 채점했다
  - #454: 외부 과제 `--case-dir`, seed patch. base는 #452
  - #455: fix-loop 상한 재승인. base는 #452
- #454와 #455는 둘 다 #452 위에 있다. 두 기능을 같이 쓰려면 두 브랜치를 합쳐야 한다.
- 보류(사용자 결정 대기): #442(이미지 빌드), draft #431, #433~#436, #438, #441.
- 시도하다 막힌 것: runner에 `continue --extend-fix-loop` 추가. 자동 모드 권한 분류기가 막았고, 사용자 권한 결정을 기다린다.

**chonamdoo/agent-flow-evals-private** (holdout. 내용은 공개 저장소에 적지 않는다)
- PR #1 `feat/oyg-evaluator-v1`: OYG 평가기 v1
- PR #2 `feat/mobile-cart-holdout-v0`: mobile-cart v0
- 브랜치 `feat/mobile-cart-v1`(#2 위), `feat/mobile-checkout-v0`(v1 위): PR은 아직 없음
- 각 과제 폴더의 `README.md`에 타당성 기록(seed 채점, 기준 구현 판정, mutant, 파일럿)이 있다.

## 사용자 결정 대기

1. 프로그램 v2 승인
2. 심판 검증용 사람 라벨 30~50개를 맡을지
3. 사이클 예산($150 유지 또는 증액)
4. runner 상한 연장 경로를 위한 권한
