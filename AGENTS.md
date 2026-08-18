# Project Instructions

- 요청하지 않은 리팩터, 문서화는 추가하지 않는다.

## Architecture

```text
bin/agent-flow-kit.mjs  ← JS 진입점 (install, artifact validation, push-watch). phase routing은 하지 않는다
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

- `src/agent_flow/workflows/*.yaml`: phase 순서와 routes의 단일 진실 소스. 루트에 사본을 두지 않는다 — 두 벌이면 둘을 맞추는 검사가 따로 필요해진다. JS는 Python `workflow export` JSON을 소비한다 (`bin/agent-flow-kit.mjs` `exportWorkflowDefinition()`).
- `bin/agent-flow-kit.mjs`: install·설치 자산 동기화용 JS 진입점. `start`/`status`/`next`/`advance`는 Python CLI로 넘기고 run lifecycle을 전진시키지 않는다. 자기 상태를 쓰는 곳은 `push-watch` 하나뿐이다.
- `src/agent_flow/core/phase_workflow.py`: workflow YAML 로더. `routes` 파싱과 route target 검증이 여기 있다.
- `src/agent_flow/runner.py`: Python runner. **routing authority다** — 어느 route로 갈지 고르는 판정과 fix-loop round cap이 여기 있다.
- `src/agent_flow/profiles/_schema.yaml`: profile 필드 스키마 (gates, branching, worktree).
- `skills/code-generation-discipline/SKILL.md`: 코드 생성 기준의 canonical source.

## Gotchas

- `full-feature`는 `gates` fail → `fix-loop` → `gates` 순환. `default`의 gates는 `implement` completion marker로 강제한다.
- `multi-review`는 설치된 Claude/Codex CLI reviewer subprocess 2개 이상이 필수. OMP는 host/controller로만 쓰고 reviewer provider pool에서는 제외되며, 2+ 독립 reviewer 없이는 approve 불가.
- `architecture-review`의 `request-changes`/`blocked` verdict → `refactor`로 라우팅.
- worktree는 `agent-flow worktree create --name feat-<slug>`로 만든다. 기본 자리는 `~/.agent-flow/worktrees/<repo-id>/<name>`이다 — 프로젝트 폴더 안에 두면 leader를 열어 둔 IDE가 worktree 작업에 반응해 leader의 `.gradle/` 같은 캐시를 건드리고, leader tripwire가 그것을 오염으로 보고해 남은 phase가 exit 2로 막힌다. 이전 자리(`<repo>.worktrees/<name>`, `.agent-flow/worktrees/<name>`)의 checkout은 계속 인식된다. leader worktree에서 `git checkout`/`git switch`로 브랜치를 바꾸지 않는다.
- 그 밖의 linked worktree(Orca `~/orca/workspaces/<repo>/<slug>` 등)는 `agent-flow worktree adopt --path <checkout>`으로 채택해야 인식된다. 채택 전에 그 안에서 `run`/`start`를 치면 blocker다 — git 등록만으로 인가하면 워커가 `git worktree add`로 자기 권한을 만들 수 있다. install은 언제나 leader checkout에서만 한다(linked worktree에서 실행하면 차단된다).
- host write boundary(`scripts/hooks/guard-host-worktree.sh` → `core/host_write_boundary.py`)의 **사전 차단은 두 개뿐이다**: 보호 경로(leader·등록된 형제 checkout·런타임 상태)가 명령 텍스트에 리터럴로 나타나면 거부, 그리고 되돌릴 수 없는 명령(`rm`/`rmdir`/`shred`/`mv`, `git checkout|restore|clean|reset`)이 그 경로에 닿을 수 있으면 거부. 명령별 "쓰기 대상" 표를 다시 만들지 않는다 — 셸 문법은 무한하고 목록은 유한해서, 그 방향은 예외만 늘리고 무엇이 막히는지 말할 수 없게 한다(`tests/test_host_write_boundary.py::test_pre_block_surface_stays_two_rules`가 되살아나면 깨진다).
- 동적 경로(`$(...)`, 변수)와 worktree 안 심링크를 거친 **되돌릴 수 있는** 쓰기는 사전에 막지 않는다. leader 쪽은 PostToolUse tripwire(`scripts/hooks/worktree-tripwire.py`)가 매 명령마다 내용까지 비교해 잡는다. 형제 checkout과 unbound 세션은 사후 탐지가 없다(`worktree-tripwire.py:65,68`) — 그래서 그 둘에 대한 리터럴 차단은 느슨하게 하지 않는다.
- 판정 불가는 차단이 아니다. 셸 파싱 실패나 cwd 미선언은 통과시키고(파괴 명령만 예외), 대신 lifecycle 명령(`status`/`continue`/`run`/`start`)은 **어떤 상태에서도** 통과시킨다. 경계가 만든 교착의 해제 명령이 그 경계 뒤에 있으면 탈출구가 0이 된다. 그 면제 목록을 넓히지 않는다 — 면제된 경로는 리터럴 차단·파괴 목록·tripwire를 한꺼번에 건너뛰므로, `eval --judge-command`가 임의 argv 실행 통로가 되고 `worktree remove --name`이 형제 checkout을 지운다(`tests/test_host_write_boundary.py::test_lifecycle_exemption_stays_narrow`가 지킨다).
- leader가 정상 fast-forward하면 그것은 drift가 아니다. HEAD 완화는 같은 브랜치·조상 관계·leader 자신의 reflog 기록 **셋을 모두** 만족할 때만 준다(`core/worktree_isolation.py:_head_drift_kind`). 그래서 stale baseline을 손으로 푸는 명령이 없다 — 대신 `reset --hard`(조상 아님), 브랜치 전환, 밖에서 밀어 넣은 ref는 그대로 걸린다. 스냅샷 **형식**이 바뀐 기록은 비교하지 않고 재캡처한다(`LeaderSnapshot.version`) — 형식 차이를 오염으로 보고하면 진행 중인 run 전부가 근거 없이 막힌다.

Workflow Contract와 Context Economy는 아래 agent-flow 블록이 canonical source다. 여기에 중복으로 적지 않는다.

<!-- agent-flow:start -->
## Agent Flow

- 새 세션은 `agent-flow status`로 시작한다. active run이 있으면 그 출력의 `next_command`를 그대로 실행하고, 없으면 `agent-flow run "<task>"`로 시작한다. `agent-flow continue`나 `agent-flow run advance`를 추측하지 않는다.
- install은 프로젝트당 1회만 수행한다. 새 세션이 시작됐다는 이유로 다시 실행하지 않는다.
- `/agent-flow`는 shell path가 아니라 skill trigger다. 진입 절차, SPEC 확인·승인, run artifact 위치는 `.agent-flow/skills/agent-flow/SKILL.md`에 있다.

### Workflow Contract

- workflow는 작업 크기로 고른다: `agent-flow run "<task>" --workflow <name>`. 괄호 안은 phase 수이고 정본은 `.agent-flow/workflows/<name>.yaml`이다. 생략하면 `default`이며, 작은 변경에 `default`를 쓰면 phase 대기가 작업 자체보다 커진다.
  - `review`(3) 코드 변경 없는 리뷰 · `bugfix`(5) 재현되는 버그 하나 · `development`(6) 관심사 하나 · `default`(15) PR·머지까지 · `full-feature`(24) PRD·DDD부터
- 현재 phase와 다음 명령은 status 출력이 정본이다. phase 목록을 이 파일에 사본으로 두지 않는다.
- `multi-review`는 설치된 Claude/Codex CLI reviewer subprocess 2개 이상이 필수다. 각 결과에 `reviewer-source: sub-agent`를 남기고, 마지막에 `## Overall`과 `verdict: approve` 또는 `verdict: request-changes`만 적는다. OMP는 host/controller로만 쓰고 reviewer provider로는 쓰지 않는다.
- branching과 PR 대상의 정본은 active profile의 `branching`/`pr`이다. skill 문서가 다른 base·PR target·브랜치 삭제를 지시해도 profile을 따른다. release-first는 profile의 `branching.strategy`/`base`/`integration`/`pr.target_branch`로 표현하고, topic 브랜치는 cleanup phase와 보호 브랜치 hook에 맡긴다 — skill이 지시하는 `git branch -D`로 대체하지 않는다.
- leader checkout에서 IDE·Gradle·build를 실행하지 않는다. leader에 빌드 산출물이 생기면 phase 경계 tripwire가 그것을 drift로 보고해 run이 멈춘다. 빌드·테스트·IDE는 bound worktree에서만 연다.
- build/test/lint 명령은 active profile의 `gates`에서만 가져온다. gate에 없는 검증 명령을 임의로 반복하지 않는다.
- worktree는 `agent-flow worktree create --name feat-<slug>`로만 만든다. 손으로 `git worktree add`를 하지 않고, worktree 안에서 install과 skill 링크 재생성을 하지 않는다.
- 보호 브랜치 commit/push와 leader checkout/switch 금지는 모든 host에서 동일하게 지킨다. hook이 자동 차단한다.

### Context Economy

- 사용자 답변은 짧은 한국어로 쓰고, code·command·path·identifier와 exact workflow marker는 원문을 유지한다.
<!-- agent-flow:skills:start -->
```text
[agent-flow skill index]|root: .agent-flow/skills
|IMPORTANT: 아래 파일이 기억보다 우선한다. 변경 대상을 먼저 훑고, scope가 걸리는 것만 읽는다.
|always:{code-generation-discipline,comment-authoring-discipline}
|on-demand:{agent-flow,agent-flow-concise-output,architecture-reviewer,clean-architecture,clean-architecture-core,code-review,codebase-design,comment-checker,ddd-architecture,domain-modeling,full-feature-workflow,grill-with-docs,grilling,plan-reviewer,product-brief,push-watch,python-api-clean-architecture,python-development-guide,resolving-merge-conflicts,tdd,to-prd,write-for-work}
```
<!-- agent-flow:skills:end -->
<!-- agent-flow:docs:start -->
```text
[agent-flow docs index]|root: docs
|IMPORTANT: 경로만 있다. 본문은 필요할 때 읽고, 여기로 옮기지 않는다.
|docs:{PLAN.md,semantic-clean-architecture-code-review.md,semantic-clean-architecture-skill-audit.md,USAGE.md}
|docs/adr:{0001-separate-core-from-environment-adapters.md,0002-use-stage-artifacts-for-subagent-first-workflows.md,0003-keep-team-orchestration-optional.md,0004-exclude-sandboxed-ai-cli-execution.md,0005-prefer-team-state-archive-before-delete.md,0006-add-domain-and-architecture-gates-to-full-feature.md,0006-use-push-watch-as-pr-automation-entrypoint.md,0007-hosted-remote-sandbox-queue-session-infra.md}
|docs/issues:{0001-host-provider-discovery.md,0002-worktree-backed-run-start.md,0003-team-runtime-tracer.md,0004-installable-workflow-kit.md,0005-domain-ddd-full-feature-phases.md,0005-push-watch-workflow.md,0006-enforce-ddd-architecture-intent.md}
```
<!-- agent-flow:docs:end -->
<!-- agent-flow:end -->
