# Plugin opt-in 설계·SPEC

상태: 사용자 명시 선택 `분리 구현 시작`으로 planning base에서 구현 시작 승인. 구현·검증 완료 증거는 아니다.

## 1. Interview

사용자가 승인한 다섯 목표와 보존 조건을 설계 입력으로 채택했다. 새 entry skill, skill-only enforcement, host별 core 복제, MCP, SessionStart 자동 run, phase 삭제, 새 workflow는 제외한다. 별도 PR 준비 권한은 있으나 publish, release tag, GitHub Release, PR merge 권한은 없다.

- 전담 controller: plugin-optin, Herdr `wB:p2J`.
- planning checkout: `~/.agent-flow/worktrees/agent-workflow-e11171fb0dea/feat-plugin-optin`, branch `feat/plugin-optin`.
- planning baseline: `ad8d9d8e3b1ab2db1888f5ef4a105d800f7600af`.
- 자기 run: `default/20260916-114638`, `design`. 별도 PR 준비가 포함돼 `default`를 명시 선택했다. 네 local-handoff workflow로 축소하지 않는다.
- 현재 run 출력 adapter는 `claude`, 실제 controller는 OMP다. host 선택/새 checkout hook 로딩은 구현 전 확인 대상이다. worktree create는 새 checkout에서 세션 재시작이 필요하다고 안내했다. 이 세션에서 hook 실행 검증을 했다고 주장하지 않는다.
- Main의 최신 roster 확인에 따르면 t6-context/`wB:p29`와 `wB:p2K` 모두 없다. 이전 담당 질의에 대한 신원 확인은 받지 못했다. 현재 같은 repo의 Main/plugin-optin만 확인됐다는 사실은 기존 run 종료·정리·소유 해제를 인증하지 않는다.
- Main이 전달한 최종 PR225 원격 증거: head `118b29fd75d86deb893e2d4c441f48b2a7274ace`, merge commit `ddacc4ce7e7df07df3c69e840d936477b1353966`, MERGED. parity/pytest/frozen-invariants COMPLETED SUCCESS, CodeRabbit SUCCESS. pytest 완료 `2026-09-16T11:52:52Z`. 이전 PR225 pytest 대기 기록은 이 결과로 대체한다.
- PR225 증거 URL: [Tests](https://github.com/chonamdoo/agent-flow/actions/runs/35091388962), pytest job `104778279958`, frozen job `104778279793`; [Parity](https://github.com/chonamdoo/agent-flow/actions/runs/35091388961).
- 실제 planning base `ad8d9d8e3b1ab2db1888f5ef4a105d800f7600af`는 별도 SHA다. 기존 Tests run `35091802280`의 최신 직접 조회에서 headSha 일치, run COMPLETED SUCCESS, pytest job `104779619478` 및 frozen-invariants job `104779619826` 모두 COMPLETED SUCCESS를 확인했다. parity success는 앞서 Main이 전달한 SHA별 증거다. [Tests 결과](https://github.com/chonamdoo/agent-flow/actions/runs/35091802280).
- PR225 head와 planning base의 CI 대기는 각각 해소됐다. 새 workflow dispatch/re-run은 실행하지 않았다. base의 CI green은 담당/잔여 소유권과 native reviewer provenance 확인을 대체하지 않는다.
- PR225 reviewDecision은 비어 있고 최근 GitHub reviews는 옛 SHA의 COMMENTED였다. merge/CodeRabbit success는 최종 SHA의 두 개 이상 native reviewer provenance를 증명하지 않는다. 기준 커밋의 독립 리뷰 근거는 미확인이며, 새 작업의 독립 리뷰/gates도 그대로 필수다.
- Main이 확인한 최신 GitHub Release는 `v0.2.12` (`2026-09-12`)다. npm/PyPI 게시 상태는 미확인이다. package 배포 자산 변경은 작업 범위지만 release version 변경·publish/tag/GitHub Release/merge는 별도 승인 경계다.

사용자가 `분리 구현 시작`을 명시 선택했다. 담당 확인 대기 조건을 대체하고 CI green인 `ad8d9d8e3b1ab2db1888f5ef4a105d800f7600af`를 구현 기준으로 사용한다. 기존 담당/잔여 소유권과 기준 SHA native reviewer provenance는 미확인 사실로 남긴다. 다른 run 비조작, 버전·릴리스 경계, 새 작업의 독립 리뷰/gates 및 최종 실설치·CI 조건은 유지한다. live WIP나 다른 checkout의 staged 파일은 복사하지 않는다.

## 2. Spec

### 목표와 관찰 가능한 동작

설치는 배포물을 준비한다. enable은 host가 배포물을 발견·노출하게 한다. 명시 호출은 사용자 의사를 표현한다. run 참여는 특정 run과 controller session의 명시 결속이다. hook 신뢰는 host별 별도 전제다. 이 다섯 사건을 하나의 `enabled` 값으로 합치지 않는다.

신규 opt-in 설치는 프로젝트 AGENTS.md/CLAUDE.md를 생성하거나 변경하지 않는다. 일반 개발 요청과 plugin enable만으로 run을 시작하지 않는다. `/agent-flow status`는 조회이며 참여·재개 승인이 아니다. 명시 시작/해당 run 재개에서만 entry 운영 절차를 따른다. 다른 worktree의 active run 발견은 승계 승인이 아니다.

### A. 기존 entry skill 통합

`skills/agent-flow/SKILL.md`를 유일한 운영 진입 정본으로 사용한다. 시작·대상 run 식별·status/next_command·workflow 선택·SPEC·worktree·gates·독립 review·완료/승인 절차를 모은다. `code-generation-discipline`의 구현 규범과 YAML의 phase 라우팅은 복제하지 않고 참조한다.

명시 invocation metadata는 host별로 분리한다. Claude의 `disable-model-invocation: true`와 Codex의 `policy.allow_implicit_invocation: false`를 동일 표준으로 설명하지 않는다. plugin namespace와 기존 standalone 호출을 구별한다. OMP는 기존 adapter/extension 경로를 보존하며 native plugin parity를 약속하지 않는다.

기존 root의 workflow 계약을 없애면서 일반 규칙·skill/docs 인덱스 전달까지 잃지 않는다. 신규 opt-in은 root를 아예 건드리지 않고 phase envelope와 설치된 skill index를 통해 필요한 규범을 전달한다. 기존 관리 블록 전환은 명시 migration과 소유권 판정으로만 한다. 상위 공용 AGENTS는 진단·사용자 안내 대상으로만 취급한다.

### B. 설치·전환·철회

설계 선택: 신규 설치의 root 정책과 기존 설치의 보존 정책을 분리한다. 옵션 이름은 기존 CLI 패턴과 맞춰 구현 시 확정하며, 여기서 미구현 옵션을 실행 가능한 명령으로 제시하지 않는다.

| 상태 | 기본 설치/갱신 | 명시 전환 |
|---|---|---|
| root 파일 없음 | 생성하지 않음 | 사용자가 선택한 조건부 안내만 생성 가능 |
| 일반 규칙만 존재 | byte 단위 보존 | 기존 본문 보존 |
| receipt 일치 관리 블록 | 기존 설치 정책 유지 | 운영 지시만 entry로 전환; receipt 갱신 |
| 사용자 수정 블록 | 보존·충돌 안내 | 검토/승인 없는 덮어쓰기 금지 |
| receipt 없음/손상 | 소유권을 추정하지 않음 | 기존 backup/보존 계약에 따라 별도 처리 |
| CLAUDE의 AGENTS import | 일반 규칙 전달 유지 | import를 통째로 삭제하지 않음 |
| 부모 디렉터리 AGENTS | 읽어 안내할 수 있으나 수정하지 않음 | installer 소유권 없음 |

managed block, skill/docs index, receipt, 백업은 한 transaction 경계로 처리한다. plugin disable/uninstall은 run 완료·abort·cleanup·root migration이 아니다. 사용자 hook/profile/일반 규칙과 git-dir state/evidence를 남긴다. host별 legacy/plugin 등록이 동시에 실행되지 않도록 등록 소유권을 판정한다.

### C. 공통 payload와 host 배포

설계 우선안: 공통 kit payload를 native plugin으로 배포하고, explicit project preparation이 기존 프로젝트 runtime/launcher 및 필요한 host hook 등록을 materialize한다. plugin cache를 실행 중 run의 유일한 runtime/state 저장소로 사용하지 않는다. native cache hooks로 모두 옮기는 방식은 채택하지 않는다.

- 단일 source: Python runner, YAML, profiles, skill/hook 본문, templates. host manifest와 event/path 변환만 별도다.
- Claude `.claude-plugin/plugin.json`, Codex portable `plugin.json`/OpenAI extension의 현재 규격은 공식 문서로 확인한다. 하나의 archive가 실제 두 host에서 동작하는지는 배포 검증으로 입증한다.
- skill discovery를 위한 배포 산출물은 동일 source에서 생성한다. 수작업으로 유지하는 두 번째 entry body를 만들지 않는다.
- project root는 신뢰 가능한 cwd/worktree 등록으로 결정한다. plugin cache cwd, 환경 변수 한 개, task 문자열로 프로젝트를 추정하지 않는다.
- 설치 성공, Python/Node 의존성, PATH, reviewer CLI 가용성, hook 등록, host 신뢰·실제 실행을 각각 진단한다. npm lifecycle script를 Python 준비 전제로 삼지 않는다.
- 보호 hook은 프로젝트에 남기는 우선안을 검증한다. 이 경우 plugin disable이 해당 보호를 해제하지 않는다는 사실을 명시한다. native plugin hook을 추가해야 한다면 등록·신뢰 검증 및 중복 방지부터 설계에 반영한다.
- 기존 active run에서 runtime/등록 교체가 의미를 바꾸면 upgrade를 보류한다. YAML pin만으로 Python/interpreter/hooks까지 고정됐다고 주장하지 않는다. cache 삭제·plugin disable/uninstall 뒤에도 기존 프로젝트 runtime으로 명시 재개할 수 있어야 한다.
- 개인 plugin은 팀 전체 보안 강제 수단이 아니다. Codex IDE의 native plugin 지원은 실제 공식 지원 범위와 분리한다.

### D. 참여와 보호

Stop 안내만 참여 대상에 한정한다. 단순 kit 존재, plugin enabled, cwd가 worktree라는 사실, 다른 세션 active run은 명시 참여의 증거가 아니다. 기존 host checkout binding은 보호용 위치 정보이므로 자동 binding 자체를 사용자 invocation으로 해석하지 않는다.

참여 상태가 필요하다면 기존 신뢰 가능한 session/run binding 경계에 최소 정보만 저장하고, 조회/암묵 hook이 이를 생성하지 못하게 한다. 정확한 저장 API는 기준 커밋의 binding 흐름을 확인한 후 확정한다. 별도 lifecycle state machine은 만들지 않는다.

비참여 세션은 자동 run/Stop 상태 안내를 받지 않는다. 반면 active 작업·leader·sibling·runtime 보호는 기존 정책대로 적용한다. unbound/disabled이면 모든 guard를 통과시키는 예외를 만들지 않는다. host_write_boundary의 literal protected-path와 irreversible reach 두 pre-block, 좁은 lifecycle 예외, tripwire/evidence 의미를 유지한다. hook 신뢰가 없거나 실행을 입증하지 못하면 정상 참여 준비가 끝났다고 표시하지 않는다.

### E. workflow 선택

| 목적과 종료 계약 | 기존 workflow |
|---|---|
| 수정 없는 로컬 검토 결과 | review |
| 재현 가능한 단일 결함의 로컬 수정·검증 | bugfix |
| 원인 불명/간헐 결함의 진단·원증상 검증 | diagnosing-bugs |
| 한 관심사의 로컬 변경·검증 | development |
| PR 공개·피드백·통합 절차가 필요한 변경 | default |
| 제품 판단·PRD·도메인 불확실성부터 PR 통합 | full-feature |

새 run에서 `--workflow`를 명시한다. PR/merge 요청이 있으면 짧다는 이유로 local-handoff를 고르지 않는다. PR 준비 권한은 merge 승인이 아니다. 기존 active run의 workflow/pin을 변경하지 않고 그 run에 대한 명시 재개 여부와 status 출력을 따른다. phase 수와 라우팅 정본은 YAML이다.

### F. 선행 setup과 후행 기록

정상 CLI는 `cli.py:887-940`에서 hook/profile preflight, `_resolve_entry_worktree`, Runner 시작 순으로 진행한다. `_resolve_entry_worktree`는 attach/create 뒤 `_apply_worktree_setup`을 호출한다(`cli.py:2804-2839`). 따라서 `default.yaml:178-195`와 `full-feature.yaml:209-225`의 생성 지시를 이미 bound된 checkout의 경로·branch·base·등록·profile setup 결과 확인/기록으로 바꾼다.

phase ID/order/artifact 책임은 보존한다. full-feature의 run-start는 profile/provider/task/제약 기록을 유지한다. bound checkout이 없거나 등록이 틀리면 기록을 성공으로 채우거나 임의 생성하지 않고 CLI prerequisite 오류로 보고한다. lock/adoption/setup 실행/보호 코드는 제거하지 않는다. 기존 pin은 수정하지 않는다.

### 영향 범위와 동시 변경 경계

| 영역 | 파일·소비자 | 구현 전 조건 |
|---|---|---|
| 운영 entry | skills/agent-flow/SKILL.md, invocation metadata, bootstrap/AGENTS.md.template, root 관리 블록 | receipt와 사용자 문장 보존 |
| 설치 | bin/agent-flow-install.mjs, bin/agent-flow-kit.mjs, lib/installer-shared.mjs | 두 진입점 같은 root 정책 |
| package | host manifests, package.json/pyproject.toml, asset/digest 목록 | owner 릴리스 범위와 버전 충돌 조율 |
| 참여/안내 | scripts/hooks/show-phase-status.sh, session binding, lib/omp-hooks-extension.mjs | 안내와 guard 정책 분리 |
| 등록/신뢰 | core/hook_integrity.py, host 설정/launcher | cache 경로를 신뢰 증거로 쓰지 않음 |
| setup 지시 | workflows/default.yaml, full-feature.yaml | owner 승인 YAML 변경 이후 좁은 수정 |
| 규범/증거 전달 | adapters/base.py, hosted.py, runner.py, artifact.py, core/local_skills.py | 필요성 확인 후 최소 변경; owner 기준 커밋 필수 |
| 검증 | 기존 installer/parity/hook/worktree/workflow 계약 tests와 실제 host smoke | 구현 시작 후 profile gates 준수 |

## 3. DDD model

Bounded contexts: Kit Distribution은 payload와 설치 소유권을 공급하고, Workflow Execution은 run/phase/SPEC/pin/evidence를 소유한다. Host Integration은 schema/event/trust 차이를 번역하는 ACL이며 실행 정책을 복제하지 않는다.

용어 정제: installation(파일 준비), enablement(host 노출), invocation(사용자 명시 시작/재개 의사), participation(특정 session과 run 결속), hook trust(host 실행 신뢰), local-handoff(로컬 결과 전달), integrated completion(승인된 원격 통합 및 cleanup). 이들은 서로 대체 가능한 상태가 아니다.

Aggregate 경계: 기존 run의 pin/승인/증거 결속, 기존 설치 receipt/managed asset transaction, 기존 worktree registration identity를 유지한다. 새 도메인 event bus/entity/repository 계층은 필요하지 않다. domain events: n/a — 이 변경을 위해 새 발행 경로를 만들지 않는다.

불변식: 배포 cache 제거가 run identity/evidence를 지우지 않음; root 소유권 없는 쓰기 금지; 참여 여부로 보호 완화 금지; route는 runner/YAML 소유; 사용자 승인 범위는 publication/merge와 분리.

## 4. Clean Architecture layer map

## Clean Architecture Boundary Map

Domain policy: workflow 종료 계약, 소유권/보호 불변식. Application: 기존 설치 transaction과 run entry/resume orchestration. Data: 파일 receipt, project runtime, git-dir state, host 설정. Inbound/presentation: native manifest, skill entry, CLI 출력, host event adapter. UI/domain mapping은 적용하지 않는다.

## Dependency Rule

Host adapter → 기존 application entry → core policy 방향을 유지한다. Python core가 Claude/Codex manifest 스키마나 plugin cache를 import하지 않는다. JS installer는 phase routing을 결정하지 않는다.

## Use Case Boundaries

기존 설치/전환, 명시 시작/재개, 상태 조회를 분리한다. 신규 범용 configuration framework나 순수 forwarding usecase class는 추가하지 않는다. 다단계 file ownership 변경은 기존 installer transaction에서 수행한다.

## Repository Boundaries

기존 receipt/registration/runtime store가 각각의 상태 정본이다. plugin cache는 run repository가 아니다. 신규 repository interface는 아직 필요성이 입증되지 않았다.

## Cache Boundary

native plugin cache는 폐기 가능한 배포 사본이다. restart-required state/evidence와 호환 runtime은 프로젝트 측 기존 저장 경계에 둔다. active run과 호환되지 않는 upgrade는 거절/보류한다. 새 memory/disk cache는 만들지 않는다.

## Mapping Boundary

host event/path/schema 변환만 adapter 경계에 둔다. domain 값을 host별 DTO로 중복 저장하지 않는다. root document 변환은 receipt 기반 소유권 처리이지 domain mapper가 아니다.

## Composition Root

기존 Node installer와 Python CLI가 concrete filesystem/runtime/host adapter를 조립한다. plugin manifest는 동일 payload의 진입점을 가리킨다. OMP 기존 extension을 별도 native plugin이라고 이름만 바꾸지 않는다.

## Testability Boundary

소유권/전환/선택/참여 판정은 기존 test harness에서 관찰 가능한 계약으로 검증한다. host 신뢰·namespace·실제 hook 실행·cache 철회 후 재개는 실제 CLI smoke로 구분한다. 설치 파일 존재나 mock 호출 횟수만으로 실행 성공을 주장하지 않는다.

## 5. SOLID check

S: entry 지침, 설치 소유권, runner routing, host 변환의 변경 이유를 유지한다. O: 기존 host seam에 manifests/변환을 추가하며 정책 복제는 하지 않는다. L: Claude/Codex 모두 동일 보호/실패 계약을 충족해야 하며 OMP를 약화하지 않는다. I: plugin은 설치/명시 진입만 소비하고 runner 내부 phase mutation API를 노출하지 않는다. D: core가 host/cache 형식에 의존하지 않는다. 구현 전이므로 구체 신규 함수별 충족 판정은 보류하며 구조 개선을 핑계로 무관한 리팩터링하지 않는다.

## 6. Decision log

- Constraint: root 무수정과 명시 invocation은 승인된 제품 요구다.
- Rejected: root pointer만 바꾸고 opt-in 완료라고 선언; hook 전부 제거; 개인 plugin으로 팀 강제 보장; cache-only runtime; host별 core 복제; MCP/default-agent takeover; SessionStart 자동 run.
- Directive: 동일 core + host별 얇은 배포, 프로젝트 runtime/등록 보존 우선. native hook 이관은 필수가 아니며 실제 필요·신뢰 검증 없이 추가하지 않는다.
- Constraint: Clean 의무, local 규범, pending 제한, DDD 별도 축, SPEC/approval/evidence 의미를 보존한다.
- Directive: setup phase 삭제가 아닌 기록 정정만 수행한다.

## Spec Items

아래 manual 항목은 검증 결과에 대한 실제 사용자 승인을 뜻한다. 최초 목록 기록은 통과/승인이 아니다. 구현 시 자동 검증으로 교체하면 SPEC delta 확인 절차를 따른다.

SPEC-1: 루트 운영 지침을 기존 skills/agent-flow/SKILL.md에 통합하고 중복 entry skill을 만들지 않는다.
verify: manual
SPEC-2: 시작/재개, status/next_command, SPEC 승인, worktree, 검증/review/완료 절차를 빠짐없이 보존한다.
verify: manual
SPEC-3: 신규 opt-in 설치는 공용 root AGENTS.md/CLAUDE.md를 기본 수정하지 않는다.
verify: manual
SPEC-4: 명시 호출 또는 해당 run 명시 재개에만 운영 절차를 적용한다.
verify: manual
SPEC-5: 기존 일반 규칙, 사용자 수정, AGENTS import, receipt 소유권을 보존하며 블록을 무조건 삭제하지 않는다.
verify: manual
SPEC-6: 상위 공용 AGENTS를 installer가 자동 수정하지 않는다.
verify: manual
SPEC-7: runner/YAML/profile/skill/hook 본문 한 벌로 Claude/Codex host별 plugin을 배포한다.
verify: manual
SPEC-8: 설치, enable, 호출, run 참여, hook 신뢰를 구분하고 runtime/reviewer/hook 실행 성공을 각각 확인한다.
verify: manual
SPEC-9: 비호출 세션 자동 run/상태 안내를 없애되 active 작업/sibling/runtime 보호를 유지한다.
verify: manual
SPEC-10: OMP 기존 adapter를 보존하고 미검증 native plugin 호환을 약속하지 않는다.
verify: manual
SPEC-11: 신규 entry는 목적과 종료계약에 맞춰 기존 workflow를 명시 선택한다.
verify: manual
SPEC-12: 네 짧은 workflow의 local-handoff를 PR/merge 범위와 혼동하지 않고 active workflow/pin을 변경하지 않는다.
verify: manual
SPEC-13: CLI 선행 attach/create/setup 뒤 YAML은 bound 상태를 확인·기록하며 phase를 삭제하지 않는다.
verify: manual
SPEC-14: 연구 assessment 보정을 적용하고 historical snapshot을 현재 코드나 최종 승인으로 바꾸지 않는다.
verify: manual
SPEC-15: 사용자의 분리 구현 시작 결정에 따라 CI green 기준 ad8d9d8…에서 자기 작업을 진행하고, 기존 담당/기준 리뷰 미확인은 기록하며 다른 run 비조작과 버전·릴리스 경계를 유지한다.
verify: manual
SPEC-16: 다른 페인의 run 승계/advance/승인/수정/rename을 하지 않는다.
verify: manual
SPEC-17: leader에서 source/문서/설치 자산 수정 및 build/test/lint/install을 하지 않는다.
verify: manual
SPEC-18: 공식 managed worktree만 사용하고 구현 전 합의된 기준을 반영하며 live WIP/staged 파일을 가져오지 않는다.
verify: manual
SPEC-19: profile branching/pr/gates를 따르고 설계 중 formatter/build/lint/tests를 실행하지 않는다.
verify: manual
SPEC-20: 구현 검증은 profile gates와 변경 동작 계약을 따르며 전체 local suite로 CI를 중복하지 않는다.
verify: manual
SPEC-21: 공식 독립 리뷰는 실제 Claude/Codex CLI subprocess 두 개 이상으로 수행하고 OMP는 controller로만 사용한다.
verify: manual
SPEC-22: leader graphify cache/stamp를 갱신하거나 baseline을 초기화하지 않는다.
verify: manual
SPEC-23: skill-only enforcement, host별 core 복제, 불필요한 MCP, default-agent takeover, SessionStart 자동 run을 도입하지 않는다.
verify: manual
SPEC-24: phase 수 축소, 두 review 단순 통합, 새 경량 workflow를 도입하지 않는다.
verify: manual
SPEC-25: Clean/local/pending/DDD 규범을 보존하고 owner 승인 YAML/CI 수리/릴리스 범위를 섞지 않는다.
verify: manual
SPEC-26: 개인 plugin을 팀 전체 보안 강제로 설명하지 않고 active state/pin/evidence를 cache에 의존시키지 않는다.
verify: manual
SPEC-27: 별도 PR를 준비하되 외부 publish/release tag/GitHub Release/PR merge를 수행하지 않는다.
verify: manual
SPEC-28: Clean 선택 설치에서 지원 profile의 Clean 및 presentation 규범이 선택·설치·전달되고 local FSD로 대체되지 않음을 최종 candidate 실설치로 확인한다.
verify: manual
SPEC-29: 외주 FSD의 명시 local 계약 본문·필수 reference를 읽고 Clean usecase/repository/UiModel 요구를 강제하지 않음을 확인한다.
verify: manual
SPEC-30: 다른 경로·이름·필수 참조를 사용하는 유효 vendor FSD를 해석하고 회사/표준 폴더 하드코딩과 필수 규범 누락의 false pass가 없음을 확인한다.
verify: manual
SPEC-31: 한 repo의 FSD frontend와 다른 구조 backend에서 scope별 선택·비누출을 확인하고 unmatched role을 checked로 오인하지 않는다.
verify: manual
SPEC-32: /Users/namdoo/Developer/Aiproject/agent-flow-install-smoke-<candidate 식별자>/ 아래 독립 새 Git 프로젝트들에 최종 배포물로 실설치하며 HOME/host/cache를 격리하고 기존·상위·전역 설정과 증거 폴더를 보존한다.
verify: manual
SPEC-33: 동일 최종 commit/package digest의 deterministic 회귀를 기존 GitHub Actions에서 실행하고 SHA별 run URL/job 결과를 기록한다. live AI는 manual/non-blocking으로 구분하며 실패를 skip/xfail/bypass로 숨기지 않는다.
verify: manual

## Design Values

design-values: none

## 수용 검증 행렬

모두 예정 항목이며 아직 실행하지 않았다.

| 사례 | 통과 관찰 |
|---|---|
| 신규 설치, root 없음/존재 | root 생성·수정 없음; host payload 발견 가능 |
| 설치·enable 뒤 일반 요청 | run 생성/Stop 안내 없음; 기존 보호 유지 |
| 명시 status | 조회만 수행; 참여/승계하지 않음 |
| 명시 start/resume | 대상 run 식별; 선택 workflow/기존 pin 유지 |
| 타 worktree active + 새 task | 타 run 변화 없음; 자기 등록 checkout 사용 |
| receipt 일치/없음/손상/편집 | 일반 규칙/import 보존; 소유권 불명 자동 삭제 없음 |
| Claude/Codex 실제 설치 | namespace와 implicit invocation 차단 관찰 |
| runtime/PATH/reviewer 부족 | 설치 성공과 실행 준비 실패를 구분해 진단 |
| 등록 있으나 trust 없음 | 준비 성공을 주장하지 않음; 실제 hook 실행 확인 후 진행 |
| legacy와 plugin 공존 | 중복 hook 실행 없음; 사용자 hook 보존 |
| unbound/disabled session | 안내 없음; 보호 literal/destructive 정책 비완화 |
| plugin upgrade/cache 삭제 | 기존 runtime/state/evidence/pin 보존 및 명시 재개 |
| plugin disable/uninstall | run 완료/cleanup 오인 없음; 프로젝트 보호 잔존 의미 안내 |
| 짧은 변경 + PR 요청 | local-handoff로 요청 범위 축소하지 않음 |
| 이미 bound된 CLI run | YAML가 두 번째 worktree를 만들지 않고 기록만 수행 |
| OMP 기존 경로 | adapter/extension 유지; native plugin parity 주장 없음 |

프로필 출력: branching trunk/main, PR main/squash. local pre-commit architecture-lint, optional mypy/ruff; pytest는 CI-only다. 설계 중에는 실행하지 않는다. 구현 후에도 전체 pytest를 local에서 반복하지 않는다. 실제 smoke는 해당 phase의 실행 권한과 프로젝트 검증 계약을 확인한 뒤 수행한다.

### 최종 candidate 실설치 — 추가 필수 인수조건

이 검증은 구현 완료 뒤 수행한다. 과거 CI, 현재 설치본, 함수 mock, 소스 import만의 성공은 최종 package 설치 증거가 아니다. 개발 checkout은 현재 managed worktree에 유지한다. 명령 실행 cwd는 자기 bound worktree이며 별도 새 Git 프로젝트를 설치 대상으로 지정한다. 기존 폴더를 덮거나 삭제하지 않는다.

대상 부모는 `/Users/namdoo/Developer/Aiproject/agent-flow-install-smoke-<candidate 식별자>/`다. `clean`, `outsourced-fsd`, `vendor-fsd`, `mixed-company`를 각각 독립 Git 저장소로 만든다. suffix는 실제 candidate SHA/digest와 연결하며 동명이 있으면 새 고유 경로를 사용한다. 아직 폴더를 생성하거나 설치하지 않았다.

| 사례 | 실제 계약 차이 | 필수 관찰 |
|---|---|---|
| clean | 지원 profile의 Clean + 해당 presentation 규범 | 선택/설치/phase 전달; local FSD 대체 없음 |
| outsourced-fsd | 외주 제공 local FSD 계약과 필수 reference | 실제 문서 본문 소비; Clean usecase/repository/UiModel 강제 없음 |
| vendor-fsd | 다른 이름·위치·참조 구조의 유효 FSD 계약 | 회사명/표준 폴더 하드코딩 없음; 누락 reference는 정상 성공 아님 |
| mixed-company | 동일 repo FSD frontend + 다른 구조 backend | scope 선택/비누출; unmatched role checked 오인 없음 |

회사명만 바꾸는 중복 fixture는 만들지 않는다. 기존 유효 tests를 재사용·보완해 pending 비구조 허용/구조 차단, 필수 norm 누락, 다른 프로젝트 설정 오염도 검증한다.

각 사례는 동일 최종 package/installer 진입점으로 신규 설치와 반복 설치/갱신을 수행한다. root 일반 문서·사용자 hook 보존, 명시 기존 관리블록 migration, 호출/미호출, runtime/status/재개를 관찰한다. 지원 host별 실제 skill 발견과 hook 신뢰·실행을 확인한다. disable/uninstall과 active-run evidence 보존은 구현된 지원 계약을 따른다.

대상별 HOME/host 설정/cache를 분리한다. 상위 AGENTS/CLAUDE, 기존 프로젝트, 전역 host 설정을 수정하거나 전역 trust를 자동 부여하지 않는다. host 승인 UI/환경/지원 제한으로 실행하지 못한 조합은 미검증/미지원으로 남긴다. 계정/API key/모델 변동을 deterministic CI의 필수 조건으로 넣지 않는다.

결과 기록에는 candidate SHA/version/package digest, 실제 설치 대상, 실행 명령/cwd/exit/log, 선택된 규범/필수 reference, root·사용자 hook 보존 증거, hook 등록과 실제 신뢰/실행 구분, 지원 profile/host/architecture/설치상태 검증표, 구체적 누락 목록, 해당 SHA의 GitHub run URL/job 결과를 포함한다. 무한한 모든 업체 조합을 검증했다고 표현하지 않는다.

실패 시 원인을 수정하고 변경된 최종 candidate로 같은 사례와 CI를 다시 실행한다. skip/xfail, 임의 baseline 완화, runtime test bypass는 해결로 인정하지 않는다. 증거 폴더는 사용자가 확인할 수 있도록 남긴다. 과거 candidate 결과를 최종 pass에 합산하지 않는다.

### 추가 정적 조사로 확정한 설계 근거

- 두 installer의 root block뿐 아니라 skill/docs index writer와 부모의 kit.json 재기록까지 같은 root 정책을 적용해야 한다(`bin/agent-flow-kit.mjs:372-381`, `bin/agent-flow-install.mjs:1149-1150,1186-1229`). 자식 실패 fallback이 root 쓰기를 다시 실행하거나 준비 실패를 성공으로 보고하면 안 된다.
- receipt의 exact equality/index-masked adoption은 기존 갱신 정책이지 삭제 권한이 아니다. migration은 정확한 full-block receipt와 하나의 유효 marker pair를 사전 확인한다. `--force-managed`는 삭제 동의가 아니다. CLAUDE import는 일반 규칙 전달을 유지하도록 별도 보존한다.
- 현재 hook integrity는 기대 script/placement를 set으로 검사한다. 엄격한 중복 실행 검증이 이미 있다고 주장할 수 없다. installer 정규화는 보존하고, plugin/project의 동일 event 중복은 실제 배포 수용조건으로 확인한다. host-write의 의도적 Pre/Post 등록은 중복 오류가 아니다.
- 기존 binding reader가 session→active run/checkout 일치와 파일 무결성을 확인한다(`core/host_write_boundary.py:937-1000`). Stop에 읽기 전용으로 재사용할 수 있으나 현재 recorder의 status 처리와 명시 참여 조건을 대조해야 한다. OMP shutdown은 현재 session 정보 없이 안내 hook을 호출하므로 session identity/cwd 전달이 필요하다(`lib/omp-hooks-extension.mjs:237-242,306-347`).
- root 무수정의 대상은 AGENTS/CLAUDE 운영 지시다. 다른 root 파일까지 무수정이라 주장하지 않는다. `.gitignore` 등 기존 설치 효과는 별도로 명시하고 실제 설치 증거에 기록한다.
- [Claude reference](https://code.claude.com/docs/en/plugins-reference): `.claude-plugin/plugin.json`, plugin `/plugin-name:skill-name`, local scope. [OpenAI packaging](https://developers.openai.com/plugins/build/plugins): portable root `plugin.json`, OpenAI metadata는 `extensions.com.openai`, skills는 root `skills/`. schema 차이만으로 두 core를 만들지 않는다.
- [Codex hooks](https://learn.chatgpt.com/docs/hooks): install/enable과 current hook-definition hash 신뢰는 별개다. definition 신뢰는 script 내용 digest를 대체하지 않는다. [Codex skills](https://learn.chatgpt.com/docs/build-skills): `agents/openai.yaml`의 implicit invocation policy. [Codex plugins](https://learn.chatgpt.com/docs/plugins): IDE plugin 미지원과 standalone skills 지원을 구분한다.
- plugin root의 전체 skills 자동 발견이 선택 설치된 규범과 충돌할 위험이 있다. 실제 selection/phase 규범이 정본이어야 하며, Clean/FSD 실설치 검증은 이 경계를 포함한다. 필요하면 동일 source에서 좁은 discovery 산출물을 생성하되 중복 entry 본문을 수작업 유지하지 않는다.

### 재사용할 검증 기반과 한계

`tests/test_architecture_install_scenarios.py`에는 Clean 설치/전달(`136-182`), local 본문·참조 전달과 Clean 비강제(`185-229`), pending 비구조 허용/구조 차단(`232-278`), 누락 local reference 설치 실패/복구(`281-313`)가 있다. 환경 격리 fixture(`37-48`)도 재사용 후보다. 이들은 최종 package 실설치·native host 실행을 대신하지 않는다. 특히 현재 Clean 표본은 Python/Next.js 일부이며 presentation과 vendor/mixed scope 전체 커버리지를 주장하지 않는다.

기존 `.github/workflows/tests.yml`은 PR/main push/workflow_dispatch로 pytest와 frozen-invariants를 실행한다. 새 deterministic 설치 계약은 이 CI 경로에 반영하고 최종 SHA 결과를 남긴다. 기존 CI의 `AGENT_FLOW_SKIP_CODEX_TRUST=1`은 실제 host trust 증거가 아니므로 새 실설치/native host 인수에는 사용하지 않는다. mock/격리 검증과 실제 trust 검증을 별도 열로 기록한다. 기존 frozen 계약을 완화하거나 이름만 바꾸어 검사에서 빠뜨리지 않는다.

### 구현 분해와 완료 문턱

1. 공통 root 정책을 두 installer/자식 fallback/index/metadata 기록까지 연결하고 기존 receipt/import 보존을 구현한다.
2. 단일 source에서 native 배포와 명시 entry 발견 범위를 만든다. portable root의 전체 skills 탐색과 profile 선택의 충돌을 해결한 산출물만 수용한다.
3. 명시 invocation/재개와 Stop 참여 조회를 연결한다. 기존 보호 binding과 안내 참여의 차이를 유지하고, native hook을 불필요하게 추가하지 않는다.
4. 기존 entry에 운영 계약과 종료계약별 workflow 선택을 통합하고 default/full-feature setup 지시를 기록으로 전환한다.
5. 실제 CLI 독립 리뷰, profile gates, 동일 candidate 실설치/CI 인수표를 채운다. 실패 원인 수정 후 candidate가 달라지면 해당 실설치와 최종 CI를 재실행한다.

공유 파일별 구현 소유자는 plugin-optin이다. 사용자의 명시 `분리 구현 시작` 결정으로 시작 대기를 해소했다. package.json/pyproject.toml 배포 자산은 범위에 포함하되 version/release 작업은 선점하지 않는다. 자기 run의 design artifact부터 정상 절차로 진행하며 다른 run은 변경하지 않는다.

### 구체 설계 결정: 배포와 규범 발견

설계 선택은 **entry만 host discovery에 노출하는 생성형 plugin bundle**이다. source 저장소의 `skills/` 전체를 portable plugin root에 그대로 두면 Clean/FSD 선택 전 불필요한 규범이 host에 발견될 수 있다. 따라서 배포 staging root의 `skills/agent-flow/`에는 기존 entry source에서 생성한 사본과 host invocation metadata만 둔다. 전체 kit은 같은 bundle의 `kit/` 아래 기존 상대 경로를 유지해 담는다. `.claude-plugin/plugin.json`과 portable `plugin.json`은 bundle root에 둔다. 외부 symlink나 `../` component 경로로 payload를 참조하지 않는다.

이는 배포 산출물의 생성이지 두 번째 entry source나 host별 core가 아니다. 두 host는 같은 `kit/bin` installer, Python/YAML/profile/skill/hook body를 사용한다. 설치된 프로젝트의 selected skill index와 phase resolver가 실제 규범 선택을 결정한다. plugin root에는 default-agent, 자동 실행 hook, 전체 Clean skill catalog를 노출하지 않는다.

기존 npm CLI package와 native bundle을 같은 source commit에서 만들고 각각의 digest를 기록한다. 실행하지 않은 packing 호환성을 주장하지 않는다. package version은 현재 checkout의 `0.3.0`을 관찰한 값이며, 최신 GitHub Release `v0.2.12`와 같다고 취급하지 않는다. 이 작업에서 임의 version bump나 게시를 하지 않는다. 생성 bundle의 identity/version은 승인된 package metadata를 따르고 candidate SHA/digest로 미게시 변경을 구별한다.

### 구체 설계 결정: runtime 갱신과 참여

active-run upgrade 차단은 새 체계를 만들지 않고 기존 `core/installation.py:68-100`을 보존한다. 이 코드는 leader와 worktree runtime의 active run을 조사하고, lease 획득과 inherited descriptor 재검증 양쪽에서 설치를 거절한다. `lib/installer-shared.mjs:2370-2405`의 동일 진입점을 plugin project preparation도 사용한다. plugin cache update는 가능하지만 명시 project install/update는 active run 중 기존 차단을 받는다. plugin uninstall은 project runtime·hook·state 제거가 아니다.

안내 참여는 기존 보호 binding에 결속된 최소 추가 증거로 설계한다. 기존 보호 binding을 없애거나 status 조회의 보호 효과를 임의 변경하지 않는다. 대신 성공한 명시 start/run/continue에 해당하는 검증된 host command 결과만 안내 참여를 기록할 수 있게 한다. entry skill read, install/enable, status 조회, 단순 cwd 관측은 참여 기록을 생성하지 않는다. 기존 binding이 다른 run을 가리키면 기존 충돌 거절을 유지한다.

참여 기록은 같은 신뢰된 session/run/checkout identity와 결속하며 plugin cache 밖 기존 private runtime 영역에 둔다. 별도 phase/state machine이 아닌 안내 eligibility 정보다. 기존 binding 파일에 추가할지 부속 receipt로 둘지는 구현 시 기존 schema 호환성을 보고 더 작은 쪽을 선택한다. 오래된 binding은 보호에 계속 쓰되 참여 증거 없음으로 안내를 생략한다. Stop은 읽기 전용 검증 후 해당 active run만 표시하고, payload의 session id 누락/손상/종료/stale 상태에서는 조용히 생략한다. snapshot capture·binding 생성·최신 run fallback은 하지 않는다.

### 구체 설계 결정: fixture와 증거 레코드

- `clean`: Next.js UI 변경으로 Clean core/React platform/presentation 규범을 실제 선택한다. backend-only Python 경로는 presentation n/a인 유효 대조군이다. 지원 profile 전체가 이 두 표본으로 검증됐다고 주장하지 않고 실제 실행한 profile/host 조합을 표에 적는다.
- `outsourced-fsd`: `skills/architecture/SKILL.md`와 `requires_docs`를 가진 local FSD 계약. feature/public API 의존 방향 및 transport 소유 규칙을 명시한다. Clean usecase/repository/UiModel을 강요하면 실패다.
- `vendor-fsd`: 사용자 후속 결정 `기존 root 안에서 검증`에 따라 계약 root는 `skills/architecture/SKILL.md`를 유지한다. vendor별 skill name, reference 이름/경로, scope와 실제 정책을 외주 표본과 다르게 검증한다. 최초 예시 `engineering/contracts/frontend/SKILL.md`는 현재 parser가 허용하지 않으므로 철회하며 임의 root 지원을 추가하지 않는다. reference 삭제는 설치 실패 후 원본 복구/재설치가 관찰돼야 한다.
- `mixed-company`: 명시 local umbrella 계약 아래 frontend FSD와 backend service-layer 규범을 각각 scoped reference로 선언한다. frontend-only/backend-only/both/shared/unmatched 변경을 나눠 원문 전달과 비누출을 검증한다. 하나의 repo가 동시에 두 전역 architecture mode를 가진다는 새 모델을 만들지 않는다. `tests/test_mixed_architecture_delivery.py:32-78`의 scope별 본문 포함/배제 패턴을 확장한다.

실설치 증거는 case/profile/host/architecture/install-state별 레코드로 남긴다. 필수 필드: candidate commit/version/package digest, target root, isolated HOME/cache/config, actual command/cwd/start/end/exit/log path, selected contract 및 reference digest, before/after root·사용자 hook hash, run identity/pin/evidence 보존, discovery/registration/trust/execution 각각의 관찰 상태, CI head SHA/run/job URL. `pass`, `fail`, `not-run`, `unsupported`를 분리하고 `not-run`/`unsupported`를 pass 분모에 포함하지 않는다. host 인증 정보나 API key는 로그/fixture에 복사하지 않는다.

## 근거와 미확정 사항

현재 checkout에서 확인: `skills/agent-flow/SKILL.md:14-25`의 모든 세션 status와 bare run, `bootstrap/AGENTS.md.template:4-18` 운영 계약, `scripts/hooks/show-phase-status.sh:5-12,35-48` 설치 존재 기반 안내, `lib/installer-shared.mjs:1595-1614,3202-3439` import/receipt 보존, 네 짧은 YAML의 `completion_disposition: local-handoff`, 앞서 기록한 CLI 선행 setup.

역사적 조사: `/var/folders/81/308ppjhd4_175x6vnb5flwmr0000gn/T/agent-flow-plugin-phase-research-rceeetcj/assessment.md`, fable-review.md/astra-review.md. Fable의 Vercel 수치를 Agent Flow 실측으로 전용하지 않고, manifest 차이로 단일 archive 불가능을 단정하지 않으며, OMP plugin 미지원을 확정하지 않는다.

미확정 사실: 기존 PR225 담당/잔여 소유권 및 기준 SHA native reviewer provenance. 사용자가 그 확인 대기를 대체해 `ad8d9d8…`에서 분리 구현을 승인했다. 구현 세부사항: 로컬 host CLI의 생성 bundle 수용, 참여 eligibility schema 배치, migration 옵션은 구현/실설치에서 검증한다. PR225 head와 planning base CI는 각각 success이며 새 작업의 독립 리뷰/gates/최종 실설치 완료를 의미하지 않는다.

## 구현 진입 중 관측한 blocker

사용자 `분리 구현 시작` 승인 뒤 자기 run에 design.md를 제출하고 출력된 정상 continue 명령을 실행했다. exit 2로 다음 leader-drift가 보고됐다.

- 경로: `.agents/local/graphify-out/cache/last_query_stamp`
- gained stamp: `17 5c59019cc28d2573`
- lost stamp: `18 c7807d4c98fa1a59`
- plugin-optin은 graphify를 실행하지 않았으며 변경 주체는 미확인이다.
- Main과 당시 같은 repo의 read-only 연구 agent `wB:p2M`에 경위 확인/추가 캐시 쓰기 방지를 알렸다. 해당 agent를 변경 주체로 단정하지 않았다.
- leader 파일 복원/삭제/stash/clean 및 `--accept-leader-drift`는 실행하지 않았다. 사용자 금지에 따라 baseline을 초기화하지 않는다.

이 blocker는 이전 소유권 시작 조건과 별개였다. 이 관측 시점에는 source 구현이 없었고, 별도 복구 승인까지 pause를 유지했다. 이후 결정과 실행은 아래에 기록한다.

### 승인된 복구와 구현 진입

Main 전달에 따르면 검토 담당은 leader cwd에서 graphify query 1회를 실행했다고 보고했고, 설치된 graphify query의 `_touch_query_stamp` 경로가 atomic write를 수행한다. 현재 gained byte/hash와 일치하지만 syscall 관측이 없으므로 마지막 writer PID는 확정하지 않는다.

사용자가 이 단일 status-axis 변경에만 공식 acknowledgement 1회를 별도 승인했다. 자기 run에서 출력된 continue 명령에 `--accept-leader-drift`를 붙여 1회 실행한 결과 exit 0, `2026-09-16T12:16:32.294362+00:00`에 phase `design`, path `last_query_stamp` 한 개의 ack가 기록됐다. 이후 slice-plan 사용자 review pause를 거쳤고 사용자가 `진행해`로 승인했다. 출력된 phase approval token을 사용해 worktree 기록 단계를 완료하고 implement에 진입했다. 이 승인은 사용 완료이며 추가 drift에 재사용하지 않는다.

### 전달받은 local 규범 검토의 범위

기준 ad8d9d8…의 저장된 Python probe 결과는 `references/frontend.md` 허용, `architecture/frontend.md`와 `../architecture/frontend.md` 거부 및 author/reviewer의 frontend/backend/mixed 6개 동일 입력 scope 선택이다. 누적 scope 전이, 실제 installer/host, plugin bundle roundtrip 증거가 아니다.

정확한 `architecture/*.md` 지원을 위한 공통 validator 최소 확장(B)은 채택 후보일 뿐 현재 SPEC에 추가하지 않는다. 기존 유효 local 계약 및 Python parser/requires_docs/pathGlobs/digest를 재사용하고, author 누적 scope와 reviewer captured snapshot의 차이를 보존한다. FSD 독립 mode나 임의 root 지원을 가정하지 않는다.

Fable 요청은 API 429로 추론 전 실패했고 Astra는 HTTP 400 newer Codex required로 실제 리뷰가 없었다. 모델 합의/불일치, 독립 리뷰 승인으로 계산하지 않는다. entry+payload 배치의 discovery와 규범 activation을 구분하고 상대 참조·필수 본문·digest·git-tracked 계약 운반은 최종 설치에서 별도로 증명한다.

### Vendor root 범위 결정

구현 중 `_parse_contract_path`가 `skills/architecture/SKILL.md`만 허용함을 확인했다. 사용자에게 기존 root 내 검증과 별도 root 확장 SPEC 중 선택을 요청했고 `기존 root 안에서 검증`을 선택했다. vendor 및 mixed 실설치 fixture도 그 root를 사용한다. SPEC-30의 경로 차이는 지원되는 required reference 경로와 적용 source scope 차이로 검증한다. 이 결정은 resolver의 계약 root 제한을 제거하거나 reference validator를 확장하지 않는다.
