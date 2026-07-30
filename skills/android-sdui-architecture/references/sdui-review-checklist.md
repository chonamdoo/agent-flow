# SDUI Review Checklist

Evidence and verdict rule for each marker. Sources are named per item; the
review angle that consumes this lives at `templates/_shared/review/sdui.md`.

## 1. `sdui-design-token-only`

Source: PART 4-4, PART 7-6.

Rule: styling fields in server JSON carry token names. Raw dp numbers and hex
colors are schema violations, and the client is the only owner of values.

How to check: grep screen JSON, fixtures, and composer output for literals in
styling positions, for example
`"(padding|margin|color|background|radius|spacing|elevation)"` on the same line
as `#[0-9A-Fa-f]{3,8}` or `[0-9]+dp`. Then confirm token resolution exists only
in the design-system module.

Verdict: `fail` on any raw literal in a styling field. A numeric fallback inside
the token table is resilience, not permission — server-side schema validation
must still reject the literal.

## 2. `sdui-room-ssot-scope`

Source: PART 1-8, PART 6-3, PART 6-6.

Rule: state holders and UI observe a storage flow for every state that must
survive a process restart, whether one screen or many observe it. No component
subscribes to an API response, and refresh writes to storage before anything
renders. Progress flags, one-shot effects, command responses, server clock offset,
cursor pages, auth credentials, and media bytes stay outside storage — see
`offline-ssot-data-guide.md`.

How to check: confirm the durable screen flow originates from a DAO or DataStore
`Flow`; confirm no network data source is injected into a state holder; confirm
refresh functions return `Result<Unit>` rather than a screen; then name which
observed state the change treats as durable and which as ephemeral.

Verdict: `fail` if a durable rendered value reaches the UI without passing through
storage, or if a DTO is subscribed to directly. `n/a` when the change touches no
data path. Ephemeral state held outside storage is not a finding on its own.

## 3. `sdui-action-finite-vocabulary`

Source: PART 1-3, PART 5-4, PART 7-4.

Rule: every action `type` exists in the declared sealed vocabulary. Unlisted
types resolve to `Unknown` and are ignored. Expressions are resolved by regex
substitution only.

How to check: confirm the action model is a sealed type and the executor `when`
is exhaustive with no `else -> error(...)`; diff the JSON action types against
the sealed subtypes; grep for `eval`, script engines, or reflective dispatch on
a type string.

Verdict: `fail` on an action type with no sealed counterpart, on a non-exhaustive
executor, or on any dynamic evaluation. `n/a` when no action code or payload
changed.

## 4. `sdui-parse-depth-limit`

Source: PART 7-3, PART 7-6.

Rule: the recursive parser enforces a maximum depth and returns a fallback node
past it.

How to check: grep the parser for `MAX_DEPTH`; confirm the check happens before
child recursion and that recursion increments depth; confirm the exceeded branch
returns `Unknown`, not an exception.

Verdict: `fail` if recursion has no bound or the bound throws. `n/a` when the
parser was not touched and a bound already exists.

## 5. `sdui-unknown-node-fallback`

Source: PART 7-3, PART 7-2.

Rule: an unsupported component type and a malformed node both degrade to a
fallback node that renders without crashing.

How to check: confirm the parser `when` ends in `else -> UiNode.Unknown(id, type)`
and that the whole branch is wrapped so a field-level failure also yields
`Unknown`; confirm the renderer has a branch for `Unknown` and that its `when`
over node types cannot throw on an unmatched case.

Verdict: `fail` if either fallback is missing, or if the renderer's fallback path
throws in release builds.

## 6. `sdui-list-key-contenttype`

Source: PART 7-2, PART 7-6, PART 1-2.

Rule: lazy list rendering supplies a stable `key`, and the screen-level list also
supplies `contentType`.

How to check: grep the renderer for `items(` and verify each call passes
`key =`; verify the top-level list passes `contentType =`; verify keys come from
node or section ids rather than list indices.

Verdict: `fail` on a missing key, an index-derived key, or a missing
`contentType` on the screen-level list. Keys are what preserve scroll position
across a patch, so this is not cosmetic.

## 7. `sdui-accessibility-field`

Source: PART 7-6.

Rule: interactive nodes carry an accessibility label or role, and the renderer
maps them into `semantics`.

How to check: confirm the shared modifier builder applies `contentDescription`
and `role`; then check interactive node types in the payload — buttons, icon
buttons, and any node with a click event — for a label, a role, or an explicit
`hidden: true`.

Verdict: `fail` when an interactive node has no label, no role, and no explicit
hidden decision, or when the renderer never applies the field. `n/a` when no
interactive node was added or changed.

## 8. `sdui-semantic-promotion`

Source: PART 2-4.

Rule: a layout-tree combination that appears in three or more places is promoted
to a semantic component.

How to check: this is a repository-wide observation, not a diff-local one. Scan
stored screen templates and fixtures for repeated container-plus-leaf shapes and
compare the count against the existing semantic component list.

Verdict: `fail` only with a concrete third occurrence identified. `n/a` is the
correct verdict when the change is diff-local and repository-wide repetition was
not surveyed — say so rather than guessing.
