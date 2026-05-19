# ADR 0007: Hosted Remote Sandbox Queue Session Infra

## Status

Proposed

## Context

agent-flow는 local/leader checkout에서 workflow identity를 유지하고, worktree에서는 root install state를 참조한다. hosted remote sandbox는 이 규칙을 깨지 않고 queue/session/artifact 동기화만 외부 실행 환경으로 옮겨야 한다.

## Decision

- Sandbox lifecycle은 `queued -> starting -> running -> stopping -> stopped|failed|timed_out` 상태를 가진다.
- Queue job은 workflow id, run id, worktree name, command, timeout, cost cap, artifact sync allowlist를 기록한다.
- Session persistence는 prompt 전문이 아니라 run path, context contract path, artifact path, provider metadata를 저장한다.
- Artifact sync는 `context.md`, `events.jsonl`, `sources/`, `tool_outputs/`, `scratch/`, stage artifacts만 대상으로 한다.
- Timeout/cost cap 초과는 gate failure로 기록하고 기존 fix-loop 또는 recovery artifact로 연결한다.

## Consequences

P2 구현은 local skeleton과 spec에서 멈춘다. 실제 hosted provider, durable queue, remote file sync는 후속 slice에서 adapter 단위로 붙인다.
