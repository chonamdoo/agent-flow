# Issue #222: architecture 후속 구현과 검증

## 등록과 승인 경계

- 원문: [2026-09-14 추가 댓글](https://github.com/chonamdoo/agent-flow/issues/222#issuecomment-5661368665), 작성·수정 시각 `2026-09-14T08:46:17Z`.
- 비교 기준: 원격 `main`의 `d2a9e70abe1760b012c4e78a4d9a04254f5e9ec3` (T6 PR #224 머지).
- 공식 새 run: `full-feature/20260914-115653`; worktree `feat-issue222-architecture-followup`; branch `feat/issue222-architecture-followup`.
- 승인된 일: A1–A8 전부 등록, 현재 코드상 해결/잔여 판별, 설계·SPEC 작성, 승인 범위의 잔여 구현과 검증.
- 사용자 승인: Q1–Q7에 이어 R1–R4 권고안을 제시받고 `진행해`라고 답했다. 상세 schema/API의 구현 승인이며, 구현 완료·회귀 통과·manual SPEC 검증 승인은 아니다.
- 종료된 T6 run은 재사용하지 않는다. 원래 #222 본문의 승인 범위 거버넌스 7항목은 별도 작업군이다. 자동 scope gate나 전역 guard를 추가하지 않는다.
- copier·lease 담당 작업, 보류 stash, 다른/기존 run metadata를 수정하지 않는다. leader 수정·빌드·재설치·branch switch 금지.

원문 JSON은 새 run의 `context/sources/issue222-source.json`에 보존했다. 댓글의 옛 줄 번호와 설치본 0.2.11 실측은 과거 증거이며 이번 구현의 검증 결과와 별개다. 과거 실패를 확인하려고 같은 재현을 반복하지 않는다.

## 항목별 SPEC와 acceptance

아래 줄 번호를 포함한 기존 코드 판별은 착수 기준 `d2a9e70`의 조사 기록이다.
현재 구현은 각 항목의 상태와 승인된 R1–R4를 따른다. 행동 검증 결과는 아래 실행 기록으로 구분한다. 공식 gate·독립 review·manual SPEC 승인을 대신하지 않는다.

### A1 — local role override가 받는 설정과 실제 검사 일치

- 목표: 프로젝트 `architecture.roles`를 받으면서 검사하지 않는 상태를 없앤다.
- 현재 근거: `architecture_lint.py:114-116`은 profile을 읽기 전에 non-Clean에서 반환한다. `architecture_policy.py:483-489`는 Clean만 role lint 대상으로 삼는다. CLI는 `architecture_lint.py:1475-1481`에서 non-Clean을 `n/a`로 표시한다. 따라서 현재 CLI를 과거의 무조건 `passed` 출력과 동일하다고 쓰지 않는다.
- 이미 있는 거부: `core/profiles.py:454-470`은 non-Clean의 `architecture` override를 거부하며 selection/runner/skills/status가 사용한다. 직접 lint/CLI에는 이 검사가 없어 entry point가 갈린다. local 사용자 role 실행은 기존 정책을 바꾸는 대안이다.
- 승인 결정: 기존 non-Clean architecture override 거부를 직접 lint/CLI에 일치시킨다. local 사용자 role 실행이나 내장 Clean 전용 검사 활성화는 하지 않는다.
- Acceptance/D: 실제 `.local.yaml` role 입력, 유효 profile, `match_role` 및 lint/CLI 출력과 exit code를 연결한다. 선택된 정책이 정상·위반 입력을 구별해야 한다. role 미선언 local과 기존 Clean 동작을 보존한다.
- 상태: 구현·행동 검증 완료. 직접 lint와 CLI가 기존 non-Clean override 거부를 적용한다. 실제 CLI에서 기존 exit 0이 exit 1로 바뀌었고, local role 실행은 추가하지 않았다.

### A2 — 모노레포 role 검사 범위와 미매칭 표시

- 목표: `apps/*/src` 등 실제 대상에서 검사하지 않은 결과와 검사 후 통과를 구별한다.
- 검토 범위: 배포 role 경로, `match_role`, activation roots, 검사 파일 집합, lint finding과 CLI 출력. 경로 지원과 미매칭 비영 종료는 서로 다른 변경이다.
- 현재 판별: `architecture_lint.py:509-574`는 이미 `**`와 placeholder를 지원한다. Next.js 배포 role은 root-anchored이고 CLI의 `checked`는 실제 매칭 수가 아니다(`:1504-1518`). `apps/.../productList`는 접두뿐 아니라 배포 `api|presentation` 모양도 다르다. matcher에 wildcard를 새로 구현하는 과제가 아니다.
- 범위 주의: leading `**`의 managed roots, pair 검사, build 경로 해석은 matcher와 같지 않다. 다른 앱의 pair를 인정하거나 wildcard만 붙여 전체 지원이라고 선언하지 않는다.
- 승인 결정: 실제 매칭 없음은 이유/수와 n/a로 표시하며 기존 exit 정책을 유지한다. 기존 명시 앱 prefix 설정을 문서화하고 배포 wildcard 확대·전역 구조 guard·미매칭 비영 종료는 추가하지 않는다.
- Acceptance/D: 대표 모노레포 경로와 role 표의 매칭/미매칭, 검사 대상 없음, 매칭 후 정상, 매칭 후 위반을 구별한다. CLI와 직접 lint 결과의 의미가 일치해야 한다. 과거 672파일/finding 0을 새 실행 결과로 복제하지 않는다.
- 상태: 구현·행동 검증 완료. 실제 CLI에서 empty `0/0/0`, 미매칭 `1/0/0`, 정상 `1/1/0`, 위반 `1/1/1`의 candidates/matched/findings를 구별했다. exit은 각각 `0/0/0/1`이다.

### A3 — workflow skills와 marker의 같은 정의 사용

- 목표: runner, `skills resolve`, artifact marker 판정이 동일한 workflow 정의를 사용한다. 유효한 기존 run pin을 그대로 보존한다.
- T6 해결분: `core/workflow_pin.py:28-138`, `runner.py:436-453`, `artifact.py:768-787`은 bound run의 source·skills·markers를 같은 검증된 정의에서 읽는다. malformed full pin은 fallback하지 않고 legacy는 기존 digest와 정확히 맞는 원본만 복구한다.
- 잔여: `cli.py:3538-3606`의 `skills resolve/prompt/markers`는 현재 kit YAML을 읽는다. `_skill_context`는 active run의 task/time만 가져온다. unbound artifact는 여전히 project-first 진단 fallback을 갖는다(`artifact.py:788-835`). 이를 bound status의 문제로 오인하지 않는다.
- 허용 범위: T6로 충족된 부분은 변경하지 않고 근거로 종결한다. 잔여 reader 분기가 있으면 기존 pin 정책 안에서만 정리한다. 현재 YAML로 기존 run을 재승인/재고정하지 않는다.
- Acceptance/D: 같은 run의 `skills resolve` required 목록과 marker 원본이 같은 pinned source/digest에 귀속됨을 확인한다. 프로젝트/kit 사본 불일치, 유효 full pin, 검증 가능한 legacy 원본, 검증 불가 pin의 행동을 구별한다. 기존 회귀 테스트를 먼저 재사용한다.
- 상태: 구현·행동 검증 완료. 세 CLI subprocess가 source 교체 뒤에도 full pin의 guide/marker를 사용했고 meta bytes는 같았다. current-kit에 없는 workflow의 `--fresh` 조회는 project 사본을 대신 읽지 않고 exit 2였다. exact legacy 복구·invalid pin·sibling 격리는 기존 회귀 테스트로 확인했다.

### A4 — durable review_angles override

- 목표: 프로젝트 리뷰 각도를 배포 profile 직접 편집 없이 유지한다.
- 현재 근거: `core/profiles.py:79-87,514-529`는 `review_angles`를 여전히 거부한다. `:779-790`의 기존 merge는 list 전체 교체다. profile 교체 뒤 multi-profile·시스템 baseline 각도 합성이 별도로 남는다(`core/profile_resolution.py:185-195`, `adapters/hosted.py:944-972`).
- 승인 결정: `.local.yaml`의 `review_angles` whole-list 교체를 허용한다. `skills` 거부는 유지하고 list 요소 병합·각도 자동 추가는 하지 않는다.
- 보존할 의미: `review_angles: []`는 profile 각도만 비우고 시스템 baseline은 없애지 않는다. 교체 대상은 capability-expanded profile 목록이다. built-in prompt 우선순위/경로 제한은 유지한다. 지원 shape는 R4로 승인됐다.
- Acceptance/D: 현재 거부 또는 지원 출력과 변경 후 유효 목록을 기록한다. override 없음은 배포값 유지, 명시 list는 전체 교체, update 뒤에도 프로젝트 override 유지, `skills`는 여전히 거부됨을 확인한다.
- 상태: 구현·행동 검증 완료. whole-list 교체·빈 목록·multi-profile 합성과 실제 reviewer job의 선택을 확인했다. mandatory `generalist`/`types`, 기존 prompt 우선순위, `skills` override 거부는 유지한다.

### A5 — local 부속 문서의 경로별 전달

- 목표: 단일 계약 root를 항상 적용하면서 모노레포의 무관한 부속 문서 전달을 줄인다. 적용 대상 규범을 누락하지 않는다.
- 현재 근거: `core/architecture_policy.py:278-300`은 모든 `requires_docs` 문서를 읽어 계약에 포함한다. `:323-333`은 digest drift와 untracked를 차단한다.
- `core/skill_metadata.py:170-199`의 현행 참조는 문자열 목록이다. root·모든 참조를 pin하는 것과 전달하는 본문을 구별해야 한다. 현재 변경 경로 수집은 working/staged/untracked 중심이며 reviewer의 committed branch diff와 같지 않다(`core/local_skills.py:31-79`, `adapters/hosted.py:861-884`).
- 승인 결정: R1의 문자열 또는 `{path, pathGlobs?}` 혼합 목록을 채택했다. root·조건 없는 참조는 항상 전달한다. unknown scope는 전체 전달, 증명된 empty scope는 root·무조건 참조만 전달한다. 전체 pin과 전달 subset은 별개다.
- 불변식: root 상시 적용, 기존 문자열 `requires_docs` 의미 보존, 규범 digest와 author/각 독립 reviewer 전달 보장 유지. 계약을 drop-box로 옮겨 강제력을 없애는 방식은 해결로 인정하지 않는다.
- Acceptance: 앱 A만 변경, 앱 A+B 변경, 공통/root 변경, rename·삭제·변경 범위 미상, 조건 없는 참조, 선택되지 않은 문서의 drift를 각각 정의하고 검증한다. 전달하지 않은 문서를 리뷰했다고 기록하지 않는다.
- 상태: 구현·행동 검증 완료. author와 각 reviewer의 실제 구성 prompt에서 committed+dirty scope, 증가 후 재진입·축소 시 유지, author/reviewer 이력 분리를 확인했다. baseline diff에서 상쇄된 staged rename도 양쪽 경로를 보존한다. 미선택 문서의 drift는 계속 차단된다.

### A6 — 잔여 Clean 전용 마커와 계약별 의무

- 목표: non-Clean 계약에 무관한 UseCase/cache 답변을 요구하지 않되 실제 계약 의무와 검증을 보존한다.
- 현재 근거: `workflows/default.yaml:26-29`, `full-feature.yaml:141-144`에는 Clean 이름과 UseCase 마커가 남아 있다. T6는 selected-contract 문구와 허용된 비적용 해석을 추가했다(`default.yaml:92`, `full-feature.yaml:175`); 아무것도 바뀌지 않았다고 쓰지 않는다.
- 승인 결정: R2의 `required_markers_by_architecture`를 workflow phase에 추가했다. 중립 공통 마커와 선택 mode의 마커를 합친다. `requires_docs` marker DSL, 의무 삭제, 전역 n/a 허용은 추가하지 않는다.
- Acceptance/D: local에서 허용된 n/a와 강제된 옛 범주 답변을 담은 artifact를 실제 local 계약 증거와 대조한다. 현재 `cache-required: yes|no`, `usecase-composition`, 필수 applied 값은 n/a를 허용하지 않으므로 “모두 n/a인 유효 artifact”를 만들지 않는다. Clean의 dependency/usecase/repository/mapping/cache 의무·예외를 보존한다.
- pin 주의: YAML bytes 보존만으로 충분하지 않다. `core/local_skills.py:356-365`의 Python 조건부 검사도 옛 마커를 사용하므로 기존 pin의 정상·위반 판정까지 보존해야 한다.
- 상태: 구현·행동 검증 완료. main과 6개 workflow의 62개 phase를 대조해 Clean 의무가 승인된 이름 변경 외에 삭제되지 않았음을 확인했다. 실제 artifact 판정에서 local/Clean 조건과 old pin의 이름·enum·legacy guard를 구별했다.

### A7 — pending에서 허용된 변경과 구조 결정 대기 명시

- 목표: 기존 패턴의 국소 수정 허용과 미결 구조 변경 차단을 유지하면서 작은 workflow의 계약 상태를 드러낸다.
- 현재 근거: `architecture_policy.py:492-501`은 `architecture_decision: required`일 때 pending을 차단한다. `skills/code-generation-discipline/SKILL.md`는 design 없는 workflow도 새 모듈 소유권·의존 방향·저장 경계·wiring 결정 전에 architecture 선택을 요구한다. 계약이 전혀 없다고 단정하지 않는다.
- runtime도 이미 작은 workflow를 검사한다. `runner.py:2317-2373`은 role/profile routing/명시 concern에서 구조 결정을 발견하면 required로 승격한다. 일반 status에는 계약 mode/부재 안내가 없으며 미매핑 경로는 의미적 구조 검증의 증거가 아니다. prompt에는 pending 안내가 이미 있다.
- 승인 결정: human-readable status에 pending/선택 계약 부재를 명시한다. 새 최소 구조 계약을 강제하거나 JSON·exit·next_command를 바꾸지 않는다.
- Acceptance: 작은 workflow의 기존 패턴 국소 수정은 허용, 구조 결정 필요 작업은 선택 대기, required design은 차단 유지. author/reviewer의 근거 의무와 사용자에게 보이는 상태를 함께 확인한다.
- 상태: 구현·행동 검증 완료. pending/선택 부재의 human-readable 안내와 JSON·exit·next_command 불변을 확인했다. 기존 구조 결정 차단과 국소 수정 허용은 유지한다.

### A8 — architecture 선택 사용 문서

- 목표: `docs/USAGE.md`, `docs/GETTING-STARTED.md`에서 선택·설치·실행 절차를 정확히 찾을 수 있게 한다.
- 착수 판별: 선택 명령·3모드·local 참조 설명은 두 문서에 없었다. T6의 기존 workflow pin migration은 재사용하고 중복하지 않는다.
- 근거: `cli.py:432-448`의 `architecture select --mode/--skill`; `architecture_policy.py:35-79,313-333,377-415`의 선택 파일, 기본 Clean, tracking, digest, 설치 제외 규범.
- Acceptance: `clean/local/pending`, `.agent-flow.project.yaml`과 계약/참조 tracking, 고정 local root, R1 참조 문법·scope·전체 pin/drift, 선택별 설치 규범을 설명한다. GETTING-STARTED는 최소 절차와 USAGE 링크만 둔다.
- 상태: 문서 반영·명령/schema 대조 완료. [USAGE의 선택 안내](../USAGE.md#architecture-selection), [skills pin/fresh 조회](../USAGE.md#bound-run-or-fresh-inspection), [review_angles override](../USAGE.md#durable-review-angles), [GETTING-STARTED](../GETTING-STARTED.md#choose-the-architecture)에 반영했다. [T6 migration](../USAGE.md#workflow-definition-migration)은 기존 절차를 링크한다.

## B — 변경하지 않을 계약

1. 배포 자산 `.agent-flow/`는 gitignore 유지; 선택 파일과 계약은 repo 추적 파일이다.
2. install/update가 배포 profile·workflow 사본을 갱신하는 정책 유지.
3. `skills` override 거부 유지; A4의 runtime 리뷰 각도와 구별.
4. 계약·부속 문서 digest pin과 drift 차단 유지.
5. `--accept-workflow-drift` 제거 및 현재 pin 보존/복구 정책 유지.
6. ESLint flat config 옵션 merge 동작은 upstream 변경 대상 아님.

## C — oyg-cbe-fe 외부 후속 목록 (이 저장소에서 실행하지 않음)

- [ ] C1: `apps/*/eslint.config.mjs` zones와 `LEGACY_ZONE_VIOLATORS` 유지·축소.
- [ ] C2: `.agent-flow/profiles/nextjs.local.yaml`의 required lint 유지. A1 해결과 별개.
- [ ] C3: 진행 중 run이 없을 때 0.2.12 규칙서/설정 이행: 계약 root + `requires_docs` 세 문서, local 선택, 내구성 없는 직접 편집 설정 정리. 실제 제거 항목과 이행 절차는 소비 프로젝트에서 확인. 이 저장소에서는 수행하지 않았다.
- [ ] C4: 소비 프로젝트가 A5를 채택하기 전 경계만 계약으로 두고 앱별 세 문서를 drop-box로 둘지 결정. upstream 구현이 소비 프로젝트의 채택·이행을 대신하지 않으며, 여기서는 결정하거나 수행하지 않았다.

## 승인된 1차 설계 방향

사용자는 Q1–Q7 권고안에 “승인하고 https://github.com/chonamdoo/agent-flow/pulls?q=is%3Apr+state%3Aclosed 버그 회귀 되면 안돼”라고 답했다. 아래는 당시 1차 선택 기록이다. 여기서 후속 승인 대상으로 남겼던 상세 schema/API는 다음 R1–R4 승인으로 확정됐다. 닫힌 PR의 현행 수정 계약을 보존하며, 이후 PR이 의도적으로 대체한 옛 정책이나 미머지 PR을 복원하지 않는다.

| 질문 | 권고 | 다른 선택과 영향 |
|---|---|---|
| Q1 / A1 | 기존 local override 거부를 직접 lint/CLI에도 일치시킨다. role 없는 local/pending의 n/a는 유지한다. | local 사용자 role 실행은 기존 non-Clean 거부를 바꾸며, 내장 Clean ID 검사와 분리 설계가 필요하다. 직접 lint의 새 거부 적용은 권고안에서도 명시 승인 대상이다. |
| Q2 / A2 | 실제 매칭 수에 따라 무검사/미매칭을 n/a로 보고하고 기존 exit 정책을 유지한다. 기존 명시 앱 prefix role 설정법을 지원 범위로 문서화한다. | 미매칭 비영 종료 또는 배포 wildcard 확대는 별도 gate 실패/검사 범위 변경이다. 전자는 매핑 밖의 합법적 변경도 막을 수 있고 후자는 same-app pair 등 전체 소비자 정합성이 필요하다. |
| Q3 / A3 | bound active run의 skills CLI도 기존 pin을 읽게 하고, fresh/unbound inspection을 구별한다. | 지금의 current-kit-only CLI를 유지하면 해당 CLI의 run 일치 요구는 잔여로 남는다. fresh workflow의 project-first 전환은 이번 최소안에 포함하지 않는다. |
| Q4 / A4 | review_angles만 기존 list 교체 의미로 허용한다. baseline 각도·skills 거부·prompt 우선순위는 보존한다. | 현 상태 유지는 프로젝트 각도 내구성 요구를 해결하지 못한다. 시스템 baseline 제거는 별도 정책이며 제안하지 않는다. |
| Q5 / A5 | opt-in 경로 선택을 설계하되 root/기존 문자열 참조는 항상 필수, 모든 선언 문서는 계속 pin하고 전달 subset만 줄인다. | 선택되지 않은 문서를 pin에서 빼면 drift 보장이 약해진다. 전체 전달 유지는 schema 변경이 없지만 A5 기능은 잔여다. 정확한 schema와 scope 입력은 후속 설계에서 승인한다. |
| Q6 / A6 | 새 정의의 공통 마커는 중립화하고 Clean 전용 의무는 Clean 적용 시 유지한다. local은 실제 선택 계약을 검토한다. | 단순 이름 변경은 불필요한 범주 답변을 남긴다. 의무 삭제·무조건 n/a는 허용하지 않는다. conditional marker 표현과 기존 pin validator 호환성은 후속 설계 대상이다. |
| Q7 / A7 | human-readable status에 pending/선택 계약 부재를 표시하고 기존 차단·허용·JSON/exit/next_command는 유지한다. | 새 최소 계약 강제는 규범·pin·review·거부 조건을 추가하는 별도 변경이며 권고하지 않는다. |

## R1–R4 상세 승인과 구현 근거

사용자 `진행해` 응답으로 다음 권고안이 승인됐다. 상세 정본은 공식 run
`full-feature/20260914-115653`의 `context/architecture-api-proposal.md`다.

| 승인 | 확정 계약 | 구현 근거 |
|---|---|---|
| R1 / A5 | `requires_docs` 문자열/object 혼합, 필수 `path`와 선택 `pathGlobs`; 기존 skill fnmatch 의미 재사용; unknown/empty 구별; committed+dirty와 rename/delete; 전체 pin·선택 전달·phase 내 요구 유지와 증가 시 재진입 | `core/skill_metadata.py`, `core/skill_resolver.py`, `core/skill_scope.py`, `runner.py`, `adapters/hosted.py`, `lib/skill-selection.mjs` |
| R2 / A6 | `required_markers` + 선택 mode의 `required_markers_by_architecture`; fresh 공통 이름 중립화, Clean 의무 유지, old pin의 이름·enum·legacy guard 보존 | `core/phase_workflow.py`, `adapters/base.py`, workflow YAML, `core/local_skills.py`, `artifact.py` |
| R3 / A3 | 세 skills 명령의 checkout-bound pin과 명시 `--fresh` 구별; 다른 workflow/없는 phase는 exit 2; run context 전달, 조회 시 기록 불변 | `cli.py`, `core/workflow_pin.py` |
| R4 / A4 | `review_angles`만 local whole-list 교체; 기존 reviewer shape 검증; 빈 목록 허용, mandatory baseline과 skills 거부 유지 | `core/profiles.py`, `adapters/hosted.py` |

위 Python 경로는 `src/agent_flow/` 기준이다. 구현 파일은 통과 증거가 아니다.
A1은 기존 거부의 entrypoint 일치, A2는 실제 coverage 표시, A7은 human text,
A8은 확정 기능 문서화로 제한한다. wildcard role engine이나 exit 정책 확대는 없다.

공식 run 내부의 추가 근거 위치:

- `context/architecture-api-proposal.md`: R1–R4 승인 계약.
- `context/closed-pr-regression-matrix.md`: A1–A8별 기존 회귀 계약과 우선 검증.
- `context/closed-pr-governance.md`: 동결된 정책 경계.
- `artifacts/prd.md`, `design-spec.md`, `artifacts/slice-plan.md`, `artifacts/ddd-design.md`: 승인 범위와 설계·구현 계획.
- `artifacts/red.log`: red 단계 실행 기록의 위치. GREEN/회귀 통과 증거로 사용하지 않는다.

### 이번 실행의 행동 검증

- 수정 경로와 기존 증거 경계의 집중 회귀: **842 passed** (`70.22s`). 전체 pytest 실행 결과가 아니다.
- 변경된 artifact helper를 쓰는 Node CLI·status/profile 경계: **5 passed, 275 deselected** (`37.74s`).
- 실제 CLI smoke와 62개 phase 비교 원문: 공식 run의 `context/architecture-smoke.json`. 정상/위반 exit, full-pin 조회와 metadata 불변, fresh lookup, Clean marker 차이를 보존했다.
- 초기 Python 3.9 collection 실패와 광범위 파일 묶음의 timeout은 통과로 계산하지 않았다. bound worktree의 Python 3.12.13 환경에서 수정 경로를 분리해 검증했다.
- 수정 중 발견한 회귀: reviewer prompt 조회의 metadata 쓰기를 dispatch 준비로 분리했고, baseline에서 상쇄된 staged rename의 경로 누락을 고쳤다. 테스트의 불완전한 phase 대역은 실제 `Phase`로 바꿨다.
- marker의 YAML 저장 위치·본문 문구만 고정하던 두 assertion은 삭제했다. runtime 조건별 artifact 판정과 main 대비 전체 Clean 의무 비교를 증거로 사용한다.
- pytest가 이전 임시 경로를 정리할 때 permission 경고를 냈다. 그 경로의 권한·격리 정책은 변경하지 않았다.
- 공식 profile gate·독립 review·manual SPEC 승인과 소비 프로젝트 C1–C4는 위 행동 검증과 별개다. 현재 phase/next command는 `agent-flow status`가 권위다.

### 독립 review와 첫 fix-loop

- 공식 runner의 Claude/Codex CLI subprocess가 각 5개 각도를 검토했다. 1차 결과는 **request-changes 4개, approve 6개**다. 원본은 run 루트의 `multi-review-*-{claude,codex}.md`, 집계는 `artifacts/multi-review.md`다. controller 판정으로 대체하지 않았다.
- 재현한 결함: scope 조회가 배포한 reviewer patch를 덮어씀, 좁은 code phase의 문서 subset이 다음 unknown phase에 남음, `color.status=always`가 문서 경로에 ANSI를 섞음. `context/review-fix-reproduction.json`에 실제 관측을 보존했다.
- 수정 후 같은 경로의 실제 smoke에서 patch bytes 보존, 다음 phase의 전체 문서 전달, 정확한 무색상 경로를 확인했다. 선택 파일 부재는 compatibility `clean`, 명시 `pending`만 구조 결정 대기로 안내한다. 두 status 호출 모두 exit 0이며 runtime state를 만들지 않았다. 원문은 `context/review-fix-confirmation.json`이다.
- frozen 모델과 marker Protocol의 읽기 전용 계약을 맞췄고, 설치 prompt가 조건부 의무를 mode별로 표시하도록 고쳤다. 실제 Node 설치 경로를 포함한 CLI 검증은 **6 passed, 275 deselected, 3 subtests passed** (`38.82s`)다.
- scope/marker 변경과 호출부 이행을 포함한 집중 회귀는 **529 passed** (`388.74s`)다. 실제 next-phase 전달·snapshot 보존 회귀를 포함한다. 수정 후 코드 diff 42개 파일의 comment-checker도 exit 0이었다.
- 1차 review의 request-changes는 보존한다. 수정 후 재리뷰, 공식 gate와 manual SPEC 완료는 별도 조건이다.

## 검증과 완료 기준

- 수정 경로의 실제 행동 테스트와 active profile gate만 로컬 실행. 전체 pytest는 기존 CI 전용.
- 닫힌 PR 목록/본문/merge 여부를 새 run의 `context/sources/closed-pr-catalog.json`에 보존했다. 관련 PR → 현재 보존 계약 → 회귀 테스트 → A항목 위협을 연결하며, `tests/frozen_contracts.txt`의 기존 CI 검증과 봉인된 review/격리/복구 경계를 유지한다. 과거 PR의 검증 수치는 이번 통과 결과가 아니다.
- 새 run의 현재 phase가 허용하는 검증 시점/owner를 따른다. 지금의 소스 조사와 기존 테스트 이름은 새 통과 결과가 아니다.
- D 자료를 A1/A2/A3/A4/A6 acceptance에 포함했다. 소비 프로젝트의 과거 실측을 복제하지 않는다.
- 독립 구현 review는 공식 CLI reviewer subprocess로 수행한다. 조사 scout는 review 승인으로 계산하지 않는다.
- 여덟 항목 각각 해결 근거 또는 승인된 잔여 구현·검증 결과가 있어야 종결한다. 미승인 설계는 완료 처리하거나 자동 축소하지 않는다.
