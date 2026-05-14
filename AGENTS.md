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
bootstrap/              ← AGENTS/CLAUDE/GEMINI.md template
scripts/hooks/          ← PreToolUse/Stop hooks (guard-worktree, guard-protected-branch)
.Codex/agents/          ← code-reviewer.md (리뷰 기준)
```

## Key Files

- `bin/agent-flow-kit.mjs`: PHASES 배열이 phase 순서의 단일 진실 소스. `nextPhaseIndex()`가 모든 라우팅 결정.
- `src/agent_flow/runner.py`: Python runner. YAML routes 파싱, fix-loop round cap.
- `profiles/_schema.yaml`: profile 필드 스키마 (gates, branching, worktree).
- `skills/code-generation-discipline/SKILL.md`: 코드 생성 기준의 canonical source.

## Phase Chain

domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → gates → (fix-loop ↔ gates) → multi-review → architecture-review → commit → push-pr → pr-watch → (pr-comment-fix / pr-ci-fix → pr-watch) → merge-approval → merge → handoff

## Gotchas

- `gates` fail → `fix-loop` → `gates` 순환. 3회 초과 시 사용자 에스컬레이션.
- `multi-review`는 2+ 독립 reviewer 필수. 1개만으로 approve 불가.
- `architecture-review`의 `blocked` verdict → `refactor`로 라우팅.
- worktree phase는 `git worktree add -b <branch> <path> main` 필수. leader worktree에서 `git checkout`/`git switch`로 브랜치를 바꾸지 않는다.

<!-- agent-flow:start -->
## Agent Flow

Before feature work, run:

```bash
agent-flow run "<task>"
```

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
Follow the CLI output exactly. Git projects start inside `.agent-flow/worktrees/feat-<slug>/`; continue with the printed `next_command`.

<!-- agent-flow:end -->
