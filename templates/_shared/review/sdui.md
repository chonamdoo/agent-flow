# Review Angle — SDUI (Android)

You are reviewing this change through
`skills/android-sdui-architecture/SKILL.md`. Read
`references/sdui-review-checklist.md` from that skill for the evidence rule
behind each item. Output markdown findings only. Do not propose code unless
asked.

Scope: server-driven screen payloads, node models, parsers, renderers, action
interpreters, patch operations, and the server-side composition layer. If the
diff touches none of these, mark the completion gate `n/a`.

## What to verify

1. **`sdui-design-token-only`** — styling fields in server JSON carry token
   names; raw dp and hex values are schema violations, and the client owns every
   value. Check by grepping payloads, fixtures, and composer output for a
   styling key (`padding`, `margin`, `color`, `background`, `radius`, `spacing`,
   `elevation`) on the same line as `#[0-9A-Fa-f]{3,8}` or `[0-9]+dp`, then
   confirm token resolution lives only in the design-system module. A numeric
   fallback in the token table is resilience, not permission.

2. **`sdui-room-ssot`** — state holders and UI observe a storage flow, never an
   API response. Confirm the screen flow originates from a DAO `Flow`, that no
   network data source is injected into a state holder, and that refresh returns
   `Result<Unit>` instead of a screen.

3. **`sdui-action-finite-vocabulary`** — every action `type` in the payload has a
   counterpart in the sealed action model, the executor `when` is exhaustive with
   no throwing `else`, and unknown types are silent no-ops. Expressions resolve
   by regex substitution only; flag any `eval`, script engine, or reflective
   dispatch on a type string.

4. **`sdui-parse-depth-limit`** — the recursive parser bounds depth. Check by
   grepping the parser for `MAX_DEPTH` (or the project's equivalent constant),
   then verify the check runs before child recursion, that recursion increments
   depth, and that the exceeded branch returns a fallback node rather than
   throwing.

5. **`sdui-unknown-node-fallback`** — unsupported types and malformed nodes both
   degrade. Check that the parser's type `when` ends in
   `else -> UiNode.Unknown(id, type)`, that the branch is wrapped so a
   field-level failure also yields `Unknown`, and that the renderer's `when` over
   node types has an `Unknown` branch and no unmatched-case throw in release
   builds.

6. **`sdui-list-key-contenttype`** — lazy rendering keeps identity across
   patches. Check every `items(` call in the renderer for `key =`, confirm the
   screen-level list also passes `contentType =`, and confirm keys come from node
   or section ids rather than list indices. Keys are what preserve scroll
   position across a patch; this is not cosmetic.

7. **`sdui-accessibility-field`** — interactive nodes carry an accessibility
   label or role and the renderer maps them into `semantics`. Verify the shared
   modifier builder applies `contentDescription` and `role`, then verify buttons,
   icon buttons, and any node with a click event declare a label, a role, or an
   explicit `hidden` decision.

8. **`sdui-semantic-promotion`** — a layout-tree combination repeated in three or
   more places is promoted to a semantic component. This is a repository-wide
   observation beyond the diff. Report `fail` only with a concrete third
   occurrence identified; report `n/a` when repository-wide repetition was not
   surveyed, and say so rather than guessing.

## Must-fix policy

Items 1 through 6 are correctness or contract failures. Any `fail` there produces
`verdict: request-changes`. Item 7 is must-fix when an interactive node was added
or changed, otherwise should-fix. Item 8 is never must-fix on its own.

## Required completion gate

```text
## Completion Gate
sdui-architecture: applied
sdui-design-token-only: pass|fail
sdui-room-ssot: pass|fail|n/a
sdui-action-finite-vocabulary: pass|fail|n/a
sdui-parse-depth-limit: pass|fail|n/a
sdui-unknown-node-fallback: pass|fail|n/a
sdui-list-key-contenttype: pass|fail|n/a
sdui-accessibility-field: pass|fail|n/a
sdui-semantic-promotion: pass|fail|n/a
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
