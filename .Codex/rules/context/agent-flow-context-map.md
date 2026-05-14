# Agent Flow Context Map

Phase별로 읽을 context source를 제한한다.

| Phase | Load |
| --- | --- |
| domain-grill | `CONTEXT.md` + `.Codex/rules/context/domain-glossary-full.md` + relevant ADRs |
| product-brief | `CONTEXT.md` + `.Codex/rules/context/research-context.md` + relevant brief/issue only |
| prd | `CONTEXT.md` + relevant issue/brief only |
| slice-plan | `CONTEXT.md` + PRD + dependency map if needed |
| ddd-design | `CONTEXT.md` + domain glossary + changed/target modules |
| red/green/refactor | `CONTEXT.md` + changed files only |
| gates | staged diff + gate command output summary only |
| multi-review/fix-loop | staged diff + relevant context only |
| push-pr/pr-watch | PR checklist, checks, review threads only |

## Rules

- Never load all context docs by default.
- Prefer repo-relative paths in artifacts.
- Keep raw logs out of artifacts; summarize failures.
- If phase needs more context, name the file and reason in the artifact.
