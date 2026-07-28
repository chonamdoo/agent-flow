# Agent Flow Hot Context

항상 로드되는 hot context다. 긴 도메인/워크플로우 설명은 `.Codex/rules/context/`에서 phase별로만 읽는다.

## 작업 원칙

- 응답은 한국어. 코드/명령/식별자는 영어 원문 유지.
- 요청하지 않은 리팩터, 문서화, 에러 핸들링 금지.
- 코드 주석 규칙은 `code-generation-discipline` 참조.
- 모르면 읽고 확인. 추측 금지.
- 큰 변경 전 짧은 계획 제시. 단순 수정은 바로 실행.
- 파괴적 작업은 사전 확인.
- workflow/agent artifact에는 repo-relative path만 기록.

## Current Vocabulary

- **Workflow Kit**: 여러 프로젝트에 설치되는 재사용 agent workflow 패키지.
- **Workflow**: 작업이 진행되는 phase graph. project-specific command나 runtime state는 포함하지 않는다.
- **Project Profile**: stack별 gates, profile detection, defaults.
- **Phase**: Workflow 내부 단계. 이 repo의 current 용어는 Stage가 아니라 Phase다.
- **Role**: phase가 요구하는 책임. concrete subagent/model/provider가 아니다.
- **Adapter**: 현재 환경에서 role을 실행 가능한 방식으로 바꾸는 전략.
- **Provider**: Codex, Claude 같은 외부 실행 대상 wrapper.
- **Run**: 하나의 Workflow 실행 인스턴스. git branch나 전체 session이 아니다.
- **Artifact**: phase 결과/검증/리뷰를 저장하는 재사용 record file. raw log 저장소가 아니다.
- **Handoff**: 다음 phase가 필요한 결정, 리스크, 관련 파일, 남은 일을 요약하는 artifact.
- **Gate**: build, typecheck, lint, test, context-lint 같은 자동 검증 명령.
- **Personal Workflow**: lead가 Run과 phase 전환을 소유하는 current 기본 실행 모드.
- **Verified Worktree**: canonical path, branch, git common dir 정합성을 확인한 non-leader linked checkout.
- **Sandbox Capability**: 현재 host가 write 경계를 실제로 강제할 수 있음을 증명한 결과. 없으면 spawn을 거부한다.
- **Protected Roots**: 절대 바뀌면 안 되는 닫힌 경로 집합. leader checkout, 다른 linked checkout, git common metadata, 다른 run의 상태 디렉터리.
- **Sandbox Policy**: 하나의 Verified Worktree에 묶인 write 규칙. allow 기본값 위에 Protected Roots를 deny하고, 그 spawn 자기 경로를 다시 열고, 그 안에 남는 pointer 파일을 다시 닫는 3단 순서다. 열어 준 entry 자체와 shared object database 안쪽은 따로 unlink를 막아, grant를 symlink로 바꿔치기하거나 history를 지우는 길을 닫는다.
- **Sandboxed Spawn**: agent-flow가 정책 아래에서 시작한 외부 프로세스 하나.
- **Unbounded Spawn**: 보호할 바깥 checkout이 없어 경계 없이 시작했다고 호출 지점이 명시한 spawn. 빠뜨린 것과 구분하기 위해 이유를 함께 기록한다.
- **Reuse Consent**: 지금 서 있는 checkout을 그대로 쓰겠다는 명시적 확인.

## Current Lifecycle

1. `agent-flow run "<task>"`가 Run을 만들고 git repo에서는 `.agent-flow/worktrees/feat-<slug>/` worktree에서 시작한다.
2. `agent-flow status` 또는 직전 phase 출력의 `next_command`가 다음 실행 명령을 결정한다.
3. agent는 phase context map에 맞는 문서만 읽는다.
4. artifact 작성 후에도 runner가 출력한 `next_command`로만 전환한다.
5. gates/review/PR comment 실패는 fix-loop로 돌아간다.
6. push/pr-watch는 checks와 review threads가 green일 때만 merge로 간다.

## Future Vocabulary

Team Orchestration은 optional future module이다. current Personal Workflow와 섞지 않는다.

- **Worker**: future Team Orchestration의 장기 실행 참여자.
- **Task**: future Worker가 claim 가능한 작업 단위.
- **Team State**: future team coordination state.
- **Mailbox**: future Worker/lead 비동기 메시지 큐.
- **Heartbeat**: future Worker liveness record.
- **Worktree**: 독립 변경을 만들기 위한 git worktree. 프로젝트 내부 `.agent-flow/worktrees/feat-<slug>/`를 사용하고 브랜치는 `feat/<slug>`를 기본값으로 둔다.

## 금지어 / 혼동어

- current Personal Workflow에서 Worker/Task/Team State를 active runtime처럼 쓰지 않는다.
- Phase와 Stage를 혼용하지 않는다. 사용자-facing current 문맥은 Phase.
- Artifact를 raw log, manifest, source change와 혼동하지 않는다.
- Gate와 phase completion marker를 혼동하지 않는다.
- Adapter와 Provider를 혼동하지 않는다.
- Worktree separation과 Provider Process Isolation을 같은 보안 경계로 표현하지 않는다.
- Sandbox Policy 없이 실행된 hosted/embedded process를 sandboxed라고 부르지 않는다.

## Context Loading

- 기본: `CONTEXT.md`만 로드.
- 긴 용어/근거: `.Codex/rules/context/domain-glossary-full.md`
- research/paper runtime: 해당 phase에서만 context map에 따라 로드.
- implementation: changed files + relevant context only.
- review/fix/pr-watch: diff, gate result, PR checklist 중심.

## Artifact Policy

- 기본 설치는 `.agent-flow/`를 gitignore한다. 그 아래 run artifact는 커밋 대상이 아니다.
- `.agent-flow/`를 추적하는 프로젝트에 한해 commit 가능: final summary artifact, `gate-results.json`, review decision.
- 최소화/제외: raw logs, 반복 phase logs, local absolute path가 들어갈 수 있는 manifest.
- artifact에는 repo-relative path만 저장한다. gate 출력의 절대 경로는 `write_gate_results`가 상대화한다.
- context-lint는 gitignore되지 않은 artifact만 검사한다. ignore된 파일은 커밋될 수 없어 경로 누출 경로가 없다.
