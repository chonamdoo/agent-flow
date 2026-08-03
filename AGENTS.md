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
- `multi-review`는 설치된 Claude/Codex CLI reviewer subprocess 2개 이상이 필수. OMP는 host/controller로만 쓰고 reviewer provider pool에서는 제외되며, 2+ 독립 reviewer 없이는 approve 불가.
- `architecture-review`의 `request-changes`/`blocked` verdict → `refactor`로 라우팅.
- worktree는 `agent-flow worktree create --name feat-<slug>`로 만든다. 기본 자리는 `~/.agent-flow/worktrees/<repo-id>/<name>`이다 — 프로젝트 폴더 안에 두면 leader를 열어 둔 IDE가 worktree 작업에 반응해 leader의 `.gradle/` 같은 캐시를 건드리고, leader tripwire가 그것을 오염으로 보고해 남은 phase가 exit 2로 막힌다. 이전 자리(`<repo>.worktrees/<name>`, `.agent-flow/worktrees/<name>`)의 checkout은 계속 인식된다. leader worktree에서 `git checkout`/`git switch`로 브랜치를 바꾸지 않는다.
- 그 밖의 linked worktree(Orca `~/orca/workspaces/<repo>/<slug>` 등)는 `agent-flow worktree adopt --path <checkout>`으로 채택해야 인식된다. 채택 전에 그 안에서 `run`/`start`를 치면 blocker다 — git 등록만으로 인가하면 워커가 `git worktree add`로 자기 권한을 만들 수 있다. install은 언제나 leader checkout에서만 한다(linked worktree에서 실행하면 차단된다).

Workflow Contract와 Context Economy는 아래 agent-flow 블록이 canonical source다. 여기에 중복으로 적지 않는다.

<!-- agent-flow:start -->
## Agent Flow

Before feature work, check status first:

```bash
agent-flow status
```

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
Follow the CLI output exactly. If no run is active, start with `agent-flow run "<task>"`. If a run is active, continue with the printed `next_command`.

run의 status가 SPEC 추가·수정·삭제를 보고하면 변경 목록만 사용자에게 보여 확인을 요청합니다. 사용자가 현재 대화에서 명확히 동의하면 agent가 status에 출력된 `agent-flow spec confirm --run-dir <run-dir>`을 실행합니다.
`manual` verify 항목도 사용자에게 현재 대화에서 확인한 뒤 agent가 `agent-flow spec approve <spec-id> --run-dir <run-dir>`을 실행합니다.
정확한 승인 문구나 사용자 터미널 명령 실행을 요구하지 않습니다.

### Workflow Contract

- 활성 workflow와 current phase는 항상 `agent-flow status` 출력 기준이다.
- phase 이동은 status의 `next_command`를 그대로 따른다. `agent-flow continue`나 `agent-flow run advance`를 추측하지 않는다.
- `default.yaml`: design → slice-plan → worktree → implement → comment-authoring → final-review → gates ↔ fix-loop → comment-authoring → final-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge → cleanup
- `full-feature.yaml`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → comment-authoring → multi-review → architecture-review → gates ↔ fix-loop → comment-authoring → multi-review → architecture-review → gates → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
- `multi-review`는 설치된 Claude/Codex CLI reviewer subprocess 2개 이상이 필수다. 두 reviewer를 병렬 실행하고 각 결과에 `reviewer-source: sub-agent`를 기록한다. 마지막에 `## Overall`과 `verdict: approve` 또는 `verdict: request-changes`만 기록한다. OMP는 host/controller로만 쓰고 reviewer provider로는 쓰지 않는다.

### Context Economy

- Claude/Codex/OMP user-facing 답변은 기본적으로 짧은 한글로 한다.
- 코드/명령/식별자는 영어 그대로 유지한다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, action, `next_command`, blocker를 요약한다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽는다.
- 프로젝트 skill은 `skills/<name>/SKILL.md` 또는 private `.agent-flow/local-skills/<name>/SKILL.md`에 둔다.
<!-- agent-flow:skills:start -->
```text
[agent-flow skill index]|root: .agent-flow/skills
|IMPORTANT: 아래 파일이 기억보다 우선한다. 변경 대상을 먼저 훑고, scope가 걸리는 것만 읽는다.
|always:{code-generation-discipline,comment-authoring-discipline}
|on-demand:{agent-flow,agent-flow-concise-output,architecture-reviewer,clean-architecture,clean-architecture-core,code-review,codebase-design,comment-checker,ddd-architecture,domain-modeling,full-feature-workflow,grill-with-docs,grilling,plan-reviewer,product-brief,push-watch,python-api-clean-architecture,python-development-guide,resolving-merge-conflicts,tdd,to-prd}
```
<!-- agent-flow:skills:end -->
- 인덱스에 없는 skill은 이 프로젝트에 설치돼 있지 않다. 런타임에 설치를 묻지 말고 `agent-flow skills sync`에 맡긴다.
- Claude/Codex/OMP 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude/Codex/OMP hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킨다.
<!-- agent-flow:end -->
