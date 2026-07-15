# Project Instructions

- 응답은 한국어로 작성한다.
- 코드/명령/식별자는 원문 영어를 유지한다.
- 요청하지 않은 리팩터, 문서화는 추가하지 않는다.
- 코드 생성·수정 작업은 `code-generation-discipline` 기준을 적용한다. 언어별 guide 선택과 주석 규칙은 `code-generation-discipline`의 Before Starting 체크리스트를 따른다.

## Architecture

```text
bin/agent-flow-kit.mjs  ← JS runner (phase routing, install, artifact validation)
src/agent_flow/         ← Python CLI (runner, adapters, gates, multi-review, worktrees)
workflows/              ← Source YAML (full-feature, bugfix, review 등)
profiles/               ← Stack별 profile (android, nextjs, python 등)
skills/                 ← Source skills (install 시 target .agent-flow/skills/로 복사)
templates/              ← Review angle templates (_shared/review/)
bootstrap/              ← AGENTS/CLAUDE.md template
scripts/hooks/          ← PreToolUse/Stop hooks (guard-worktree, guard-protected-branch)
.Codex/agents/          ← code-reviewer.md (리뷰 기준)
```

## Key Files

- `bin/agent-flow-kit.mjs`: PHASES 배열이 phase 순서의 단일 진실 소스. `nextPhaseIndex()`가 모든 라우팅 결정.
- `src/agent_flow/runner.py`: Python runner. YAML routes 파싱, fix-loop round cap.
- `profiles/_schema.yaml`: profile 필드 스키마 (gates, branching, worktree).
- `skills/code-generation-discipline/SKILL.md`: 코드 생성 기준의 canonical source.

## Gotchas

- `full-feature`는 `gates` fail → `fix-loop` → `gates` 순환. `default`의 gates는 `implement` completion marker로 강제한다.
- `multi-review`는 활성 host(현재 사용 중인 CLI)의 sub-agent 2개가 필수. 활성 host가 아닌 추가 provider는 optional이며, 2+ 독립 reviewer 없이는 approve 불가.
- `architecture-review`의 `request-changes`/`blocked` verdict → `refactor`로 라우팅.
- worktree phase는 `git worktree add -b <branch> <path> main` 필수. leader worktree에서 `git checkout`/`git switch`로 브랜치를 바꾸지 않는다.

Workflow Contract와 Context Economy는 아래 agent-flow 블록이 canonical source다. 여기에 중복으로 적지 않는다.

<!-- agent-flow:start -->
## Agent Flow

작업을 시작하거나 재개하기 전에 `agent-flow status`를 실행한다.

- 활성 run이 없으면 `agent-flow run "<task>"`로 시작한다.
- 활성 run이 있으면 출력된 `next_command`를 그대로 실행한다.
- phase 이동 명령이나 다음 phase를 추측하지 않는다.
- install은 프로젝트당 한 번만 수행하며 새 세션마다 다시 설치하지 않는다.
- 현재 workflow와 phase는 항상 `agent-flow status` 출력을 기준으로 판단한다.
- 현재 phase에 필요한 skill만 로드하고 모든 skill이나 guide를 한꺼번에 읽지 않는다.
- worktree에서는 install, skill index 재생성, skill link 재생성을 수행하지 않는다.
- 보호 브랜치와 leader checkout을 보호하는 hook을 우회하지 않는다.

사용자 응답은 짧게 유지하고 필요한 경우에만 current phase, action, `next_command`, blocker를 전달한다.

이 구간은 agent-flow가 관리하므로 직접 수정하지 않는다.

<!-- agent-flow:end -->
