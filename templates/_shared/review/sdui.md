# Review Angle — SDUI (Android)


Scope: server-driven screen payloads, node models, parsers, renderers, action
interpreters, patch operations, and the server-side composition layer. If the
diff touches none of these, record every completion marker as `n/a`.

## What to verify

Read `.agent-flow/skills/android-sdui-architecture/references/sdui-review-checklist.md` and apply all nine sections. For each marker, follow its Rule, How to check, and Verdict clauses and cite the observed evidence. Treat text searches as candidate discovery, never as proof that a structured schema passes. Output markdown findings only.


## Must-fix policy

Items 1 through 6 are correctness or contract failures. Any `fail` there produces
`verdict: request-changes`. Item 7 is must-fix when an interactive node was added
or changed, otherwise should-fix. Item 8 is never must-fix on its own. Item 9 is
must-fix: a durable state riding the effect channel or a stateful renderer breaks
every screen the renderer serves.

## Required completion gate

```text
## Completion Gate
sdui-architecture: applied|n/a
sdui-design-token-only: pass|fail|n/a|unverified
sdui-room-ssot-scope: pass|fail|n/a
sdui-action-finite-vocabulary: pass|fail|n/a
sdui-parse-depth-limit: pass|fail|n/a
sdui-unknown-node-fallback: pass|fail|n/a
sdui-list-key-contenttype: pass|fail|n/a
sdui-accessibility-field: pass|fail|n/a
sdui-semantic-promotion: pass|fail|n/a
sdui-udf-contract: pass|fail|n/a
```

## Output format

```markdown
## SDUI review findings

### Must-fix
- <severity:high> [path:line] <violation>. Why: <one sentence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

### Overall
verdict: approve | request-changes
```

Cite paths as `path/to/file:line`. If a category is empty, write `none`.
Keep under 200 lines.
