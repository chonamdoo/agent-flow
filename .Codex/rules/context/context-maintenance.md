# Context Maintenance

## Hot Context Rule

- `CONTEXT.md`는 hot context다. 짧게 유지하되 게이트가 줄 수를 강제하지 않는다.
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
node scripts/check-context-docs.mjs
```

The gate checks conflict markers, absolute path leaks, current/future vocabulary drift, and Agent Flow artifact path policy.
