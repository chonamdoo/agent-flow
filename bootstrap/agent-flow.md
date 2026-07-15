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
