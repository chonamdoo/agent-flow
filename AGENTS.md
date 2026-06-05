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

## Workflow Contract

- 활성 workflow와 current phase는 항상 `agent-flow status` 출력 기준이다.
- phase 이동은 status의 `next_command`를 그대로 따른다. `agent-flow continue`나 `agent-flow run advance`를 추측하지 않는다.
- `default.yaml`: design → slice-plan → worktree → implement → final-review ↔ fix-loop → commit → push-pr → pr-watch → merge → cleanup
- `full-feature.yaml`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → gates ↔ fix-loop → multi-review → architecture-review → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff

## Context Economy

- Codex / Claude / Gemini user-facing 답변은 기본적으로 짧은 한글로 한다.
- 코드/명령/식별자는 영어 그대로 유지한다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, 수행한 action, `next_command`, blocker를 요약한다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽는다.

## Gotchas

- `full-feature`는 `gates` fail → `fix-loop` → `gates` 순환. `default`의 gates는 `implement` completion marker로 강제한다.
- `multi-review` 기본은 Codex sub-agent 2개. Claude/Gemini는 optional이며, 2+ 독립 reviewer 없이는 approve 불가.
- `architecture-review`의 `blocked` verdict → `refactor`로 라우팅.
- worktree phase는 `git worktree add -b <branch> <path> main` 필수. leader worktree에서 `git checkout`/`git switch`로 브랜치를 바꾸지 않는다.

<!-- agent-flow:start -->
## Agent Flow

Before feature work, check status first:

```bash
agent-flow status
```

install은 프로젝트당 1회만 수행합니다. 새 세션이 시작됐다는 이유로 install을 다시 실행하지 않습니다.
Follow the CLI output exactly. If no run is active, start with `agent-flow run "<task>"`. If a run is active, continue with the printed `next_command`.

### Workflow Contract

- 활성 workflow와 current phase는 항상 `agent-flow status` 출력 기준이다.
- phase 이동은 status의 `next_command`를 그대로 따른다. `agent-flow continue`나 `agent-flow run advance`를 추측하지 않는다.
- `default.yaml`: design → slice-plan → worktree → implement → final-review ↔ fix-loop → commit → push-pr → pr-watch → merge → cleanup
- `full-feature.yaml`: domain-grill → product-brief → prd → slice-plan → plan-review → ddd-design → worktree → run-start → red → green → refactor → gates ↔ fix-loop → multi-review → architecture-review → commit → push-pr → pr-watch ↔ pr-comment-fix/pr-ci-fix → merge-approval → merge → handoff
- `multi-review`는 현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수다. 두 sub-agent를 병렬 실행하고, `reviewer-source: sub-agent`를 기록한 뒤 sub-agent를 닫는다. 마지막에 `## Overall`과 `verdict: approve` 또는 `verdict: request-changes`만 기록한다. 활성 host가 아닌 추가 provider는 optional이다.

### Context Economy

- Codex / Claude / Gemini / Antigravity user-facing 답변은 기본적으로 짧은 한글로 한다.
- 코드/명령/식별자는 영어 그대로 유지한다.
- 긴 설명, 긴 로그, 전체 파일 붙여넣기 금지.
- 필요한 경우만 current phase, action, `next_command`, blocker를 요약한다.
- 모든 guide를 항상 로드하지 말고 변경 파일에 필요한 guide만 읽는다.
- 프로젝트 skill은 `skills/<name>/SKILL.md` 또는 private `.agent-flow/local-skills/<name>/SKILL.md`에 둔다.
- install/bootstrap 후 `.agent-flow/skills/index.json` metadata를 보고 필요한 skill만 읽는다. 모든 SKILL.md 전문을 항상 읽지 않는다.
- Claude/Codex/Gemini/Antigravity 프로젝트 skill 경로는 leader checkout의 install 결과를 따른다. worktree 안에서 install, index 재생성, skill link 재생성을 하지 않는다.
- Claude hook이 자동 차단하는 보호 브랜치 commit/push와 leader checkout/switch 금지는 Codex에서도 동일하게 지킨다.

<!-- agent-flow:end -->
