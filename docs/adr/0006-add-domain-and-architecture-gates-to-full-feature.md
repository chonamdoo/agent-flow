# Add domain and architecture gates to full-feature

## Status

Accepted

## Context

The workflow kit already enforces phase order through artifacts. Feature work still needs earlier domain/product shaping and later architecture review so agents do not jump from vague ideas to code.

## Decision

Add domain, product, plan review, DDD design, and architecture review phases only to the installed `full-feature` workflow.

Keep the CLI as a gatekeeper. It validates artifact presence and review verdicts, but does not judge product quality or architecture quality.

## Consequences

- `full-feature` becomes stricter for new feature work.
- Smaller workflows remain light.
- Agent behavior is guided by installed skills and prompts while phase order remains CLI-enforced.
