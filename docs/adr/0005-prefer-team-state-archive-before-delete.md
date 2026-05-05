# 0005 Prefer Team State Archive Before Delete

## Status

Accepted

## Context

Team State records contain task history, worker identities, mailbox messages, heartbeats, shutdown signals, and import/export snapshots. Removing this state is destructive and can make later review or recovery harder.

Agent Flow should help users retire Team State without accidentally losing coordination history.

## Decision

Prefer archive-before-delete for Team State lifecycle management.

Future Team State cleanup should first provide an archive operation that moves or exports a Team State record into a recoverable location. A destructive delete command should remain separate, explicit, and guarded by confirmation outside the core state model.

## Consequences

- Team State history remains recoverable by default.
- Cleanup behavior stays compatible with import/export workflows.
- Destructive delete can be designed later without weakening current non-destructive import guarantees.
- The core remains state-only; no Team runtime, process supervision, provider execution, or sandbox execution is introduced.

## Alternatives Considered

### Add delete first

This would be simpler, but it would make accidental loss easier and conflict with the current non-destructive import posture.

### Never delete Team State

This avoids data loss, but long-lived projects need a way to retire old Team State records.
