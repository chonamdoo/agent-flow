# Context Maintenance

## Hot Context Rule

- `CONTEXT.md` must stay under 200 lines.
- It contains current vocabulary, lifecycle relationships, current/future split, and hard operating rules only.
- Long examples, rationale, decision history, PRD/issue detail, and phase procedure live outside hot context.

## Split Rules

- Expanded vocabulary: `domain-glossary-full.md`
- Research process: `research-context.md`
- Paper/spec/runtime detail: `paper-runtime-context.md`
- Phase loading policy: `agent-flow-context-map.md`

## Gate

Run:

```bash
python3 scripts/check_context_docs.py
```

The gate checks hot context size, conflict markers, absolute path leaks, current/future vocabulary drift, and Agent Flow artifact path policy.
