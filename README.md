# agent-flow

AI 코딩 에이전트를 검증 가능한 개발 절차 위에서 동작시키는 CLI 워크플로 도구입니다.

에이전트의 자기 보고를 신뢰하지 않는 것이 이 도구의 전제입니다. "테스트를 작성했습니다"는
증거가 아니므로, 훅이 기록한 명령 실행 로그를 런너가 직접 읽어 판정합니다.

아직 개인 도구입니다. 조직이 함께 합의하고 신뢰하는 단계로 가는 방법은 풀지
못했습니다 — [지금 남은 문제](#지금-남은-문제)에 적어 두었습니다.

설치와 사용법은 [docs/USAGE.md](docs/USAGE.md)에 있습니다.

---

## 왜 만들었나

AI 코딩 도구를 실무에 붙이면서 반복해서 부딪힌 문제가 셋이었습니다.

**① 산출물은 나오는데 검증이 안 된다.** "테스트를 작성했습니다"라고 보고하지만 실제로
실행됐는지는 알 수 없습니다. 리뷰를 했다고 하지만 무엇을 기준으로 봤는지 남지 않습니다.

**② 대화가 길어지면 요구사항이 증발한다.** 초반에 지시한 값(간격, 색상, 임계치)이 컨텍스트
압축 과정에서 사라집니다. 구현 단계에 도달했을 때는 이미 원래 요구와 다른 것이 만들어져
있습니다.

**③ 에이전트가 범위를 조용히 늘린다.** "이것도 필요할 것 같아서" 하며 요구하지 않은
리팩터링, 모듈 분리, 성능 최적화가 섞여 들어옵니다.

세 문제 모두 프롬프트를 개선하는 방식으로는 해결되지 않았습니다. 개인이 잘 쓰는 요령이
아니라 절차 자체를 고정해야 하는 문제였습니다.

---

## 그래서 무엇을 어떻게 검증하는가

### 판정 권한을 에이전트에서 떼어낸다

워크플로 YAML은 단계 **정의**일 뿐이고, 다음 단계를 결정하는 것은 런너입니다. 에이전트는
런너가 출력한 `next_command`를 그대로 따라야 하며 스스로 단계를 건너뛸 수 없습니다.

### 산출물 파일이 있다고 완료가 아니다

`required_markers`를 선언한 단계는 산출물 문서 끝의 `## Completion Gate` 블록에 그 마커가
전부 있어야 다음 단계로 넘어갑니다. 마커를 선언하지 않은 단계는 산출물 파일 존재만 봅니다.

```
## Completion Gate
usecase-interface: required|optional|n/a
usecase-composition: none|domain-service|application-service|orchestrator|justified
cache-required: yes|no
cache-invalidation-policy: <policy or n/a>
solid-dip-dependency-direction: <summary>
```

설계 단계에서만 20개 넘는 마커를 요구합니다. 계층 경계, 의존성 방향, UseCase 포트,
Repository 어댑터, 캐시 정책, 매핑 경계, 합성 루트, SOLID 각 항목까지 명시적으로 답하지
않으면 통과하지 못합니다.

마커 값도 본문과 대조합니다. `spec-items:`에 개수만 적으면 막히고, 실제 항목 ID 목록과
일치해야 합니다. `design-values: none`을 적었는데 본문에 값이 기록되어 있으면 그 모순을
런너가 잡습니다.

### 주장이 아니라 관찰로 판정한다

TDD(테스트 주도 개발)의 red 단계에서 테스트를 실제로 돌렸는지는 에이전트의 보고가 아니라 훅이 기록한 명령 실행
로그로 판정합니다.

```
The run itself is observed by the record-command-run.py PostToolUse hook,
so "I wrote a test" without a test command in this phase is rejected
regardless of what you record.

A test that passes on the first run is not a red phase.
```

마커에 무엇을 적든 런너가 로그를 직접 읽습니다.

### 요구사항에 검증 방식을 하나씩 붙인다

`design`이나 `prd` 단계를 가진 워크플로(`default`, `full-feature`)에서 사용자의 모든 지시를
순번이 매겨진 항목으로 기록하고, 각 항목마다 검증 방식을 하나씩 붙입니다.

```
SPEC-<n>: <requirement>
verify: test:<name> | symbol:<symbol>=<value> | manual
```

사용자가 제공한 구체적 값(간격, 크기, 색상, 지속시간, 임계치)은 `Design Values`로 따로
기록합니다. 이미지나 디자인 링크에서 읽은 값이면 "내가 읽어낸 값이며 원본이 아니다"를
전제로 기록하고, 표로 되읽어 사용자에게 확인받습니다.

이 원장은 대화가 압축되어도 살아남는 유일한 전달자이므로, 여기서 누락된 지시나 값은
구현에 도달하지 못합니다. `final-review`와 `multi-review` 단계에서 런너가 이 원장을 다시
검증하며, **리뷰어가 approve해도 미충족 항목이 있으면 단계가 완료되지 않습니다.** 원장이
없는 작은 워크플로(`review`, `bugfix`, `development`)에는 이 보장이 없습니다.

### 같은 세션이 만든 코드를 같은 세션이 리뷰하지 못하게 한다

구현 리뷰와 아키텍처 리뷰 두 단계 모두 독립된 서브에이전트 2개 이상이 나눠서 리뷰합니다.

- 각 리뷰어 섹션에 `reviewer-source: sub-agent` 필수
- 한 명이라도 `request-changes`면 전체 판정은 `request-changes`
- 리뷰어 프로세스가 실패하면 컨트롤러 세션이 대신 리뷰할 수 없음 (차단)
- 변경 범위가 여러 영역에 걸치면 리뷰어를 추가 투입

### 범위 확장을 막는다

작업 도중 SPEC이 추가·변경·삭제되면 변경분만 사용자에게 보고하고 확인을 받은 뒤
진행합니다. 최초 SPEC 목록은 별도 승인을 요구하지 않아 흐름을 끊지 않으면서도, 에이전트가
요구사항을 늘리는 경로는 막습니다. 주석 정리 단계에도 같은 제약이 걸립니다.

```
comment-scope: final-pass-only
refactor-scope: none
performance-optimization: none
module-split: none
```

### 자동화 도구가 메인 브랜치를 훼손할 수 없게 한다

- 모든 작업을 격리된 git worktree 안에서만 합니다. 브랜치만 만들어서는 안 됩니다
- `guard-protected-branch.sh` 훅으로 보호 브랜치 커밋·푸시 차단
- `worktree-tripwire.py`로 리더 체크아웃 이탈 감지
- 머지 직전에 사용자가 직접 승인하는 단계를 따로 둡니다

### 승인 경로 자체를 공격 표면으로 본다

승인 훅이 타는 유일한 실행 경로이므로 런처를 방어합니다.

- `LD_PRELOAD`, `LD_AUDIT`, `DYLD_INSERT_LIBRARIES` 등 로더 주입 환경변수 전부 해제
- Python 실행 시 `sys.path`에서 현재 디렉터리 제거 — 프로젝트에 놓인 `argparse.py` 하나가
  승인 프로세스 안에서 실행되는 경로를 차단
- 런처 파일 다이제스트를 run-start 훅이 대조

### 이 README의 숫자도 검사 대상이다

아래에 적힌 워크플로 단계 수, 프로파일 이름, 스킬 수는 `npm run parity:check`가 정본
파일과 대조합니다. 문서가 낡으면 검사가 깨집니다. 자기 보고를 신뢰하지 않는다는 말을
문서에도 적용한 것입니다.

---

## 워크플로

작업 크기로 고릅니다. 정본은 `src/agent_flow/workflows/<name>.yaml` 한 벌뿐입니다.
표의 `PRD`는 제품 요구사항 문서, `DDD`는 도메인 주도 설계를 뜻합니다.

| 워크플로 | 쓰는 때 | phase |
|---|---|---|
| `review` | 코드 변경 없는 리뷰 | 3 |
| `bugfix` | 재현되는 버그 하나 | 5 |
| `development` | 관심사 하나 | 6 |
| `default` | PR과 머지까지 | 15 |
| `full-feature` | PRD와 DDD부터 | 24 |

작은 변경에 `default`를 쓰면 단계 대기가 작업 자체보다 커집니다.

### `full-feature` 흐름

```
domain-grill      도메인 인터뷰 (한 번에 한 질문씩, 공유 이해 도달까지)
product-brief     만들 가치가 있는지 검증
prd               요구사항 문서 + SPEC 원장 + Design Values 기록  [일시정지]
slice-plan        독립 배포 가능한 단위로 분할
plan-review       계획 리뷰          → approve: 다음 / request-changes: slice-plan
ddd-design        DDD 도메인 모델링 → Clean Architecture 경계 설계
worktree          격리된 작업 공간 생성
run-start         실행 설정 기록
red               실패하는 테스트 작성 및 실행 (훅이 실행 관찰)
green             최소 구현으로 통과
refactor          동작 유지하며 구조 정리
comment-authoring 주석 최종 정리 (범위 확장 금지)
multi-review      서브에이전트 2+ 병렬 구현 리뷰
architecture-review  설계 대비 구현 검증 (서브에이전트 2+)
gates             프로파일 정의 검증 실행 (빌드·테스트·린트)
fix-loop          리뷰/검증 실패 수정 → 다시 리뷰로
commit            검증된 변경 커밋
push-pr           브랜치 푸시 및 PR 생성
pr-watch          PR 체크·코멘트 감시
pr-comment-fix    리뷰 코멘트 대응 → pr-watch
pr-ci-fix         CI 실패 대응 → pr-watch
merge-approval    사용자 명시 승인
merge             머지
handoff           인수인계 문서 작성
```

리뷰와 검증은 단방향이 아니라 루프입니다. 실패하면 수정 단계로 돌아가고, 수정 후에는 주석
정리와 리뷰를 다시 거친 뒤에야 검증이 재실행됩니다.

---

## 스킬과 프로파일

스킬 48종을 두고, 변경된 파일과 활성 프로파일을 기준으로 필요한 것만 로드합니다. 전부
읽으면 컨텍스트 비용이 감당되지 않기 때문입니다.

아키텍처(`clean-architecture-core`, `ddd-architecture`, `domain-modeling`), 플랫폼별
(Android, iOS, React, React Native, Python), 개발 규율(`tdd`,
`code-generation-discipline`, `comment-authoring-discipline`), 리뷰(`code-review`,
`architecture-reviewer`, `plan-reviewer`), 요구사항 정제(`grilling`, `to-prd`,
`product-brief`), 운영(`agent-flow`, `push-watch`) 계열로 나뉩니다.

프로파일 9종 — `android` `generic` `ios` `nextjs` `node` `python` `react-native` `spring` `typescript`

프로파일은 플랫폼별 검증 명령과 리뷰 관점을 선언합니다. 자세한 형식은
[docs/USAGE.md](docs/USAGE.md)에 있습니다.

---

## 만들면서 버린 접근

**프롬프트 최적화.** 컨텍스트를 정리하고 지시를 다듬는 방식으로 시작했습니다. 결과 품질은
올라갔지만 재현되지 않았습니다. 같은 프롬프트로 다른 날 다른 결과가 나오는 상태에서는
조직에 전파할 수 없었습니다.

**자기 보고 기반 체크리스트.** "이 단계에서 X를 확인했는가"를 마커로 적게 했더니, 에이전트가
확인하지 않고 마커만 채웠습니다. 그래서 훅으로 실제 실행을 관찰하고 런너가 로그를 직접
읽는 방식으로 바꿨습니다.

**단일 리뷰어.** 리뷰 단계를 한 번 두는 것만으로는 부족했습니다. 같은 세션이 만든 코드를
같은 세션이 리뷰하면 통과합니다. 독립 서브에이전트를 별도 프로세스로 띄우고, 실패 시
컨트롤러 세션이 대신할 수 없게 막았습니다.

**최초 SPEC 승인 요구.** 초기에는 SPEC 목록 전체를 사용자에게 승인받게 했는데, 흐름이
끊기고 매번 같은 확인을 반복했습니다. 변경분만 확인받는 방식으로 바꿔 마찰을 줄였습니다.

**셸 명령별 "쓰기 대상" 표.** 호스트 쓰기 경계에서 명령이 무엇을 쓰려는지 맞히려 했습니다.
셸 문법은 무한하고 목록은 유한해서 예외만 늘고, 무엇이 막히는지 설명할 수 없게 됐습니다.
지금은 사전 차단을 두 규칙(보호 경로 리터럴, 되돌릴 수 없는 명령)으로 줄이고 나머지는
사후 탐지에 맡깁니다.

---

## 지금 남은 문제

개인 도구에 머물러 있습니다. 제가 만든 기준으로 제 프로젝트에 적용한 상태이고, 조직이
함께 합의하고 신뢰하는 단계로 가는 방법은 아직 모릅니다.

풀리지 않은 것은 넷입니다.

- **검증 기준을 조직이 합의하는 절차** — 마커 목록을 누가 정하고 어떻게 바꾸는가.
  지금은 제가 정했기 때문에 제게만 맞습니다.
- **팀원마다 다른 숙련도** — 절차를 이해하고 쓰는 사람과, 막히는 지점에서 우회로를 찾는
  사람에게 같은 마커 목록이 같은 뜻으로 읽히는지 확인하지 못했습니다.
- **절차가 만드는 마찰이 얻는 신뢰만큼 값을 하는가** — 단계와 마커가 많아 혼자 쓸 땐
  감당되지만, 팀에 넣으면 "왜 이렇게 오래 걸리냐"는 반발이 먼저 올 것입니다. 어디까지가
  지불할 만한 마찰인지 기준이 없습니다.
- **리뷰 서브에이전트 비용** — 리뷰어를 병렬로 띄우면 토큰 비용이 배로 늘어납니다. 팀
  규모로 곱했을 때 얼마가 되는지, 그 비용이 리뷰로 걸러낸 결함에 값하는지 계산해 본 적이
  없습니다.

앞의 셋은 기술 문제가 아니라 사람과 조직의 문제입니다. 자동화를 늘리는 일이 곧 신뢰를
얻는 일은 아니라는 것이, 이 도구를 쓰면서 가장 분명해진 것입니다.
