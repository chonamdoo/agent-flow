# Project Instructions

- 응답은 한국어로 작성한다.
- 코드/명령/식별자는 원문 영어를 유지한다.
- 요청하지 않은 리팩터, 문서화는 추가하지 않는다.
- 코드 생성·수정 작업은 `code-generation-discipline` 기준을 적용한다. 언어별 guide 선택과 주석 규칙은 `code-generation-discipline`의 Before Starting 체크리스트를 따른다.

## Architecture

```text
bin/agent-flow-kit.mjs  ← JS runner (phase routing, install, artifact validation)
src/agent_flow/         ← Python CLI (runner, adapters, gates, multi-review, worktrees)
src/agent_flow/workflows/  ← Source YAML (full-feature, bugfix, review 등). 정본은 여기 한 벌뿐이다
src/agent_flow/profiles/   ← Stack별 profile (android, nextjs, python 등)
skills/                 ← Source skills (install 시 target .agent-flow/skills/로 복사)
templates/              ← Review angle templates (_shared/review/)
bootstrap/              ← AGENTS/CLAUDE.md template
scripts/hooks/          ← PreToolUse/PostToolUse/Stop hooks (guard-protected-branch, comment-checker, record-skill-read)
.Codex/agents/          ← code-reviewer.md (리뷰 기준)
```

## Key Files

- `src/agent_flow/workflows/*.yaml`: phase 순서와 routes의 단일 진실 소스. 루트에 사본을 두지 않는다 — 두 벌이면 둘을 맞추는 검사가 따로 필요해진다. JS는 Python `workflow export` JSON을 소비한다 (`bin/agent-flow-kit.mjs:385`).
- `bin/agent-flow-kit.mjs`: JS runner. `nextPhaseIndex()`가 phase의 routes로 다음 phase index를 계산하고 fix-loop round cap을 적용한다.
- `src/agent_flow/runner.py`: Python runner. YAML routes 파싱, fix-loop round cap.
- `src/agent_flow/profiles/_schema.yaml`: profile 필드 스키마 (gates, branching, worktree).
- `skills/code-generation-discipline/SKILL.md`: 코드 생성 기준의 canonical source.

## Gotchas

- `full-feature`는 `gates` fail → `fix-loop` → `gates` 순환. `default`의 gates는 `implement` completion marker로 강제한다.
- `multi-review`는 활성 host(현재 사용 중인 CLI)의 sub-agent 2개가 필수. 활성 host가 아닌 추가 provider는 optional이며, 2+ 독립 reviewer 없이는 approve 불가.
- `architecture-review`의 `request-changes`/`blocked` verdict → `refactor`로 라우팅.
- worktree는 `agent-flow worktree create --name feat-<slug>`로 만든다. 기본 자리는 `~/.agent-flow/worktrees/<repo-id>/<name>`이다 — 프로젝트 폴더 안에 두면 leader를 열어 둔 IDE가 worktree 작업에 반응해 leader의 `.gradle/` 같은 캐시를 건드리고, leader tripwire가 그것을 오염으로 보고해 남은 phase가 exit 2로 막힌다. 이전 자리(`<repo>.worktrees/<name>`, `.agent-flow/worktrees/<name>`)의 checkout은 계속 인식된다. leader worktree에서 `git checkout`/`git switch`로 브랜치를 바꾸지 않는다.
- 그 밖의 linked worktree(Orca `~/orca/workspaces/<repo>/<slug>` 등)는 `agent-flow worktree adopt --path <checkout>`으로 채택해야 인식된다. 채택 전에 그 안에서 `run`/`start`를 치면 blocker다 — git 등록만으로 인가하면 워커가 `git worktree add`로 자기 권한을 만들 수 있다. install은 언제나 leader checkout에서만 한다(linked worktree에서 실행하면 차단된다).

Workflow Contract와 Context Economy는 아래 agent-flow 블록이 canonical source다. 여기에 중복으로 적지 않는다.

<!-- agent-flow:start -->
## Agent Flow

이 프로젝트는 agent-flow 워크플로우 킷이 설치되어 있습니다.

모든 작업은 `/agent-flow`로 시작합니다:

```text
/agent-flow <task>
```

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
`/agent-flow <task>`를 보면 프로젝트 루트에서 `agent-flow run "<task>"`를 실행하세요.
git repo에서는 이 명령이 `~/.agent-flow/worktrees/<repo-id>/feat-<slug>/` worktree를 만들고 그 안에서 run을 시작합니다.
`/agent-flow`만 입력되면 프로젝트 루트에서 `agent-flow status`로 active run을 확인하세요.
active run이 있으면 status 출력의 `next_command`를 그대로 실행하고, 없으면 `/agent-flow <task>` 형식으로 작업을 요청하세요.
활성 workflow와 current phase는 항상 `agent-flow status` 출력 기준입니다. `agent-flow continue`나 `agent-flow run advance`를 추측하지 마세요.

### Workflow Contract

- `default.yaml`: design → slice-plan → worktree → implement → comment-authoring → final-review → gates ↔ fix-loop → comment-authoring → final-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge → cleanup
- `full-feature.yaml`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → comment-authoring → multi-review → architecture-review → gates ↔ fix-loop → comment-authoring → multi-review → architecture-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
- `multi-review`는 현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수다. agent-flow는 두 sub-agent를 검증된 worktree에 바인딩한 별도 OS sandbox subprocess로 병렬 실행한다. 각 결과에 `reviewer-source: sub-agent`를 기록한 뒤 sub-agent를 닫는다. 마지막에 `## Overall`과 `verdict: approve` 또는 `verdict: request-changes`만 기록한다. 활성 host가 아닌 추가 provider는 optional이다.
- branching과 PR 대상의 정본은 active profile의 `branching`/`pr`이다. 코드가 그 값을 읽는다. skill 문서는 프롬프트 텍스트이므로 profile과 어긋나는 base·PR target·브랜치 삭제 지시를 하지 않는다. release-first가 필요한 프로젝트는 profile의 `branching.strategy`/`base`/`integration`/`pr.target_branch`로 표현하고, topic 브랜치 정리는 kit의 cleanup phase와 보호 브랜치 hook을 따른다 — skill이 지시하는 `git branch -D`로 대체하지 않는다.

### Context Economy

- User-facing 답변은 짧은 한글이 기본입니다.
- 코드/명령/식별자는 영어 그대로 유지합니다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, action, `next_command`, blocker를 요약합니다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽습니다.
- 프로젝트 skill은 `skills/<name>/SKILL.md` 또는 private `.agent-flow/local-skills/<name>/SKILL.md`에 둡니다.
<!-- agent-flow:skills:start -->
```text
[agent-flow skill index]|root: .agent-flow/skills
|IMPORTANT: 아래 파일이 기억보다 우선한다. 변경 대상을 먼저 훑고, scope가 걸리는 것만 읽는다.
|always:{code-generation-discipline,comment-authoring-discipline}
|on-demand:{agent-flow,agent-flow-concise-output,architecture-reviewer,clean-architecture,clean-architecture-core,code-review,codebase-design,comment-checker,ddd-architecture,domain-modeling,full-feature-workflow,grill-with-docs,grilling,plan-reviewer,product-brief,push-watch,python-api-clean-architecture,python-development-guide,resolving-merge-conflicts,tdd,to-prd}
```
<!-- agent-flow:skills:end -->
- 인덱스에 없는 skill은 이 프로젝트에 설치돼 있지 않습니다. 런타임에 설치를 묻지 말고 `agent-flow skills sync`에 맡깁니다.
- Claude/Codex/OMP 프로젝트 skill 경로는 leader checkout의 install 결과를 따릅니다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않습니다.

코드 생성·수정·코드리뷰 phase에서는 `code-generation-discipline`을 적용합니다. active profile, installed skill index, changed files, task scope로 필요한 profile skill만 결정하고, 관련 없는 platform skill은 요구하지 않습니다.
phase 프롬프트가 이번 변경에 필요한 skill을 나열합니다 — required는 경로와 함께, 범위에 걸린 것은 이름으로. 그 목록은 active profile의 skill 어휘, 이 머신에 설치된 skill, 변경 파일, task 문구로 매 run 결정됩니다. required는 읽고, 범위에 걸린 것은 변경이 실제로 닿을 때만 이름으로 부릅니다. 설치된 외부 skill의 이름은 어느 설정에도 열거돼 있지 않습니다 — 이름 대신 어휘가 선언돼 있고, upstream이 skill을 추가·삭제·rename해도 선언을 고칠 일이 없습니다. 필요한 skill이 현재 host에 없으면 설치하지 말고 `missing local <group>: <skill>`와 source URL을 사용자에게 알려 설치를 요청합니다.

진행 중인 run은 자동 감지됩니다:

```text
/agent-flow          # 이어서 진행
/agent-flow status   # 진행 상태 확인
```
`/agent-flow`는 shell path가 아니라 agent-flow skill trigger로 취급하세요.

run의 status가 SPEC 추가·수정·삭제를 보고하면 변경 목록만 사용자에게 보여 확인을 요청합니다. 사용자가 현재 대화에서 명확히 동의하면 agent가 status에 출력된 `agent-flow spec confirm --run-dir <run-dir>`을 실행합니다.
`manual` verify 항목도 사용자에게 현재 대화에서 확인한 뒤 agent가 `agent-flow spec approve <spec-id> --run-dir <run-dir>`을 실행합니다.
정확한 승인 문구나 사용자 터미널 명령 실행을 요구하지 않습니다.

### Artifact

run artifact는 `.agent-flow/runs/<run-id>/`에 저장됩니다. 컨텍스트가 끊겨도 artifact 기반으로 정확히 재개됩니다.

### Worktrees

git worktree는 `agent-flow worktree create --name feat-<slug>`로 만듭니다. 손으로 `git worktree add`를
하지 않습니다 — 그 경로는 creation lock, base 결정, 채택 기록, worktree setup을 모두 건너뜁니다.
기본 자리는 `~/.agent-flow/worktrees/<repo-id>/feat-<slug>/`, 기본 브랜치는 `feat/<slug>`입니다.
프로젝트 폴더 **안**에 두지 않습니다: leader를 열어 둔 IDE가 worktree 작업에 반응해 leader 쪽
캐시(`.gradle/` 등)를 건드리고, leader tripwire가 그것을 오염으로 보고해 남은 phase가 멈춥니다.
이전 자리(`<repo>.worktrees/<name>/`, `.agent-flow/worktrees/<name>/`)의 checkout은 그대로 인식됩니다.
브랜치만 만들지 않습니다 — 물리적 worktree가 병렬 작업 격리에 필수입니다.
Claude/Codex/OMP hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킵니다.

### Memory / Lore

`.agent-flow/memory/lore/`에 작성된 lore는 자동으로 인덱싱·검색·인용됩니다.
`agent-flow lore list` / `agent-flow lore search "<query>"`로 조회합니다.
<!-- agent-flow:end -->
