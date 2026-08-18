# ADR 0008: Reject Live External Event Injection Into Running Sessions

## Status

Proposed

## Context

Xirp(Chirp) 0.14.0 해부 결과를 `docs/xirp-chirp-teardown-and-agent-flow-gap.md`에 기록했다.
그중 `pr-watch` 계열 기능에 대해 채택 여부를 결정해야 한다.

Xirp는 실행 중인 에이전트 세션에 PR 이벤트를 실시간 주입한다.

- `channels/channel-server.js` — stdio MCP 서버가 daemon WebSocket의 `channel:push`를
  받아 `notifications/claude/channel`로 살아있는 세션에 밀어넣는다.
- `channels/injector.js` `EVENT_TEMPLATES` — 주입 문자열의 `{detail}`이 임의 GitHub
  사용자가 작성한 코멘트 본문이다.
- `channels/instructions.js` — "changes_requested / inline_comment: read the feedback,
  address it, push a fix".
- `agent/command.js` — `--dangerously-skip-permissions`, `--yolo`,
  `--dangerously-bypass-approvals-and-sandbox`를 같은 경로에서 조립한다.

우회 플래그의 기본값은 모두 false로 확인됐다(`EMPTY_AGENT_DEFAULTS`). 그러나 전역
토글 `defaultSkipPermissions`가 claude 세션의 기본값을 뒤집을 수 있고, 코드가 그
조합 자체를 막지는 않는다. 세 요소가 동시에 성립하면 PR에 코멘트를 달 수 있는
사람이 그 저장소에서 임의 코드 실행과 push를 유발할 수 있다. 주입 텍스트는
`<channel source="chirp-channel" event_type="...">` 태그로 감싸지지만 태그는 신뢰
경계가 아니다.

agent-flow의 현재 설계는 `pr-watch ↔ pr-comment-fix / pr-ci-fix`가 별개 phase이고
phase 전이를 사람이 `next_command`로 진행한다.

## Decision

- 외부 이벤트를 실행 중 세션에 자동 주입하지 않는다. phase 경계를 유지한다.
- 외부에서 유입된 텍스트(PR 코멘트, CI 로그, 리뷰 본문)는 지시가 아니라 데이터로
  취급한다. `{source, author, url, body}` 구조화 봉투로 감싸고 본문은 데이터 필드에만 둔다.
- 외부 이벤트에서 유도된 작업의 산출물은 diff까지다. push는 별도 phase에서 수행한다.
- 외부 이벤트 유입이 활성인 실행에서는 권한 우회 플래그를 코드로 금지한다. 설정
  항목으로 두지 않는다.
- 구분자·태그 기반 격리에 의존하지 않는다. 봉투의 본문 필드는 항상 비신뢰로 처리한다.

## Consequences

완전 무인 PR 수정은 하지 않는다. 이미 `merge-approval` phase가 사람 승인을 요구하므로
철학적으로 일관된다.

`pr-watch`의 반응 지연은 phase 전이 주기만큼 남는다. 이 지연을 줄이려고 주입
자동화를 도입하는 변경은 이 ADR을 먼저 뒤집어야 한다.

우회 플래그 금지를 코드로 강제하려면 실행 컨텍스트가 "외부 이벤트 유입 여부"를
알아야 한다. 그 플래그를 run 메타데이터에 두는 후속 slice가 필요하다.
