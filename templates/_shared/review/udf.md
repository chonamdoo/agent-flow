# Review Angle — UDF (Android)

You are reviewing this change through
`skills/android-clean-presentation-architecture/SKILL.md`. Its `ViewModel Rule`,
`UiState Rule`, `Compose Screen Rule`, and `Review Checklist` sections hold the
rule text behind every item below; this angle only says how to judge it. Output
markdown findings only. Do not propose code unless asked.

Scope: screen state holders (`ViewModel` or plain state holder), `UiState` /
`UiAction` / `UiEvent` contracts, `stateIn` pipelines, one-shot event channels,
route and content composables, and presentation mappers. If the diff touches
none of these, mark the completion gate `n/a`.

Server-driven screens are in scope. Read
`## Server-Driven Screen Exception` in the presentation skill first: only items 2
and 7 become `n/a` there. Items 3, 5, 6, and 8 still hold — a renderer has to be
stateless for the same reason a hand-written screen does.

## What to verify

1. **`udf-immutable-state-exposure`** — the public surface is an immutable
   `StateFlow`; every `MutableStateFlow` is private. Check that the public
   property type is `StateFlow<...>` (via `asStateFlow()` or `stateIn`), and that
   no mutable holder, `MutableList`, or Compose `MutableState` escapes the state
   holder.

2. **`udf-explicit-state-modeling`** — every state the screen can actually reach
   is a named member of a `@Stable sealed interface`, per the skill's
   `## UiState Rule` member list. Check by listing the states this screen can
   enter, then diffing against the type's members; a fake domain default (empty
   list standing in for loading, `-1`, blank string) instead of a member is the
   failure. `n/a` on a server-driven screen whose state type is owned by the
   shared renderer.

3. **`udf-event-direction`** — input travels UI to state holder, one-shot effects
   travel state holder to UI, and nothing durable rides the effect path. **Judge
   by direction, not by name**: a project may call the upward type `ScreenEvent`
   and the downward type `UiEffect`. Confirm one-shot effects leave through
   `Channel(...).receiveAsFlow()` or another deliberate single-consumer model,
   never `StateFlow`, and that no transient event is the only record of state the
   screen must redraw after recreation.

4. **`viewmodel-statein-initial-load`** — flow-backed state terminates in one
   shared `stateIn` value, not a `stateIn` per call site or per collector, and
   initial loading starts when the route collects rather than in `init`. Where the
   skill's `SharingStarted` guidance applies, confirm no caller reads the stale
   `.value` as a fresh source after the subscriber timeout.

5. **`udf-stateless-content-composable`** — screen and content composables do not
   call `hiltViewModel()`, `viewModel()`, `collectAsStateWithLifecycle()`, or a
   navigation API. They receive `uiState` plus callbacks and emit actions upward.

6. **`udf-route-owns-collection`** — the screen entry composable (a `*Route`, or
   the stateful overload of a same-named screen) owns state-holder acquisition,
   state collection, one-shot event collection, and navigation or platform calls.
   Everything below it gets state values and callbacks only. A multi-holder screen
   may receive holders the navigation entry created as parameters; the constraint
   is where acquisition happens, not the call shape. One-shot commands are
   collected with `collect`, not `collectLatest`.

7. **`udf-uimodel-boundary`** — `UiState` carries presentation types, not mutable
   domain or data entities, and mapping happens before state reaches Compose.
   `n/a` when the screen's state carries a server-authored node tree; that mapping
   boundary belongs to the node parser.

8. **`udf-state-holder-purity`** — the state holder holds none of the platform and
   navigation types the skill's `## ViewModel Rule` forbids, and route keys carry
   serializable data only. Check the constructor, the properties, and the route
   key declaration against that list rather than re-deriving it.

9. **`derived-state-precomputed`** — the cross-item display flags the skill's
   `## Derived Display State` names are fields on the item model, computed in the
   state holder or mapper. A composable deriving one from `items[index ± 1]`,
   `index == lastIndex`, or unrelated screen state is the failure.

## Must-fix policy

Items 1, 3, 4, 5, and 8 are contract failures: any `fail` there produces
`verdict: request-changes`. Items 2, 6, 7, and 9 are should-fix — each has a
measured counter-example in a working codebase, so a `fail` is a finding, not a
block.

## Required completion gate

```text
## Completion Gate
udf-architecture: applied
udf-immutable-state-exposure: pass|fail|n/a
udf-explicit-state-modeling: pass|fail|n/a
udf-event-direction: pass|fail|n/a
viewmodel-statein-initial-load: pass|fail|n/a
udf-stateless-content-composable: pass|fail|n/a
udf-route-owns-collection: pass|fail|n/a
udf-uimodel-boundary: pass|fail|n/a
udf-state-holder-purity: pass|fail|n/a
derived-state-precomputed: pass|fail|n/a
```

## Output format

```markdown
## UDF review findings

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
