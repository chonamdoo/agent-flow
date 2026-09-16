# Lifecycle and Subscriptions

## Subscribe Where the Value Is Consumed

In the referenced v1.33.5 API, `useSelector(form.store, selector)` rerenders its consuming component; `form.Subscribe` limits the reactive UI boundary to its subtree. Earlier releases may use `useStore`; check exports and comparison-argument shape before migrating. Preserve a supported older implementation rather than forcing a dependency upgrade.

Author-written v1 illustration inside an existing form:

```tsx
<form.Subscribe selector={(state) => state.isSubmitting}>
  {(isSubmitting) => (
    <button type="submit" disabled={isSubmitting}>
      {isSubmitting ? 'Saving' : 'Save'}
    </button>
  )}
</form.Subscribe>
```

This only describes Form's submission lifecycle; native framework action pending needs its own source. Read other button state through the same narrow boundary when required. Render a live preview or conditional field from the specific selected value, not an unrelated parent render or an untracked `form.state.values` snapshot. Snapshot reads remain appropriate in handlers. Avoid subscribing a page layout to all values merely to update one badge.

Keep selectors narrow and use the installed equality options when selecting allocated objects/tuples warrants them. Measure before adding memoization. Validation cost and React rendering are separate: moving a subscription does not make an expensive schema parse cheaper. Form context can share a stable instance; that does not make raw state reads reactive or guarantee isolation for unrelated context values.

## Remote Baseline Versus Local Draft

Query/RSC data is a server snapshot, not a continuously authoritative replacement for an in-progress edit. Choose each transition explicitly:

| Transition | Required decision |
| --- | --- |
| Initial load | Gate form mounting until data is available, or permit editing a complete fallback and decide how arriving data is applied |
| Background refetch | Preserve the draft, request reconciliation, or merge using a documented field/conflict policy |
| Record/tenant change | Resolve unsaved work and establish a new form identity; old results cannot target it |
| Successful save | Choose the accepted server-normalized result or deliberate refetch result as the new baseline |
| Failed save | Preserve draft and useful feedback; do not reset as generic cleanup |

The referenced v1 implementation updates changed defaults according to touched state; it is **not** a field-by-field dirty merge engine. Blur can matter without changing a value. Inline defaults are not inherently wrong, and the official async-defaults fallback pattern is valid when its arrival/interactivity behavior matches the product. Conversely, `useEffect(() => form.reset(data), [data])` silently discards edits on refetch unless that replacement is intentional.

`reset(values)` normally changes values and the default baseline; `reset()` uses current configured defaults, with field defaults also relevant in v1. `keepDefaultValues` changes baseline semantics, not a universal race fix. Decide what Reset should mean after save and verify with the installed version and actual parent rerenders. Coordinate remote refresh with baseline acceptance instead of adding a workaround for an unreproduced reset theory.

The official Query example awaits mutation, refetch, then reset. Use it to understand the sequence, not as a complete conflict-resolution policy. A refetch can fail after a save succeeds: represent that as saved-with-refresh-failure when appropriate, not a failed write that invites duplicate mutation.

## Delayed Results and Edits During Save

At the save boundary, associate the submitted snapshot with the target record/tenant/session, edit generation, and request. Before applying errors, a new baseline, success navigation, or pending cleanup, check that the result still owns the affected state. Use existing identity/cancellation mechanisms rather than inventing a second framework.

- If editing is locked while saving, enforce that policy on all editing paths and restore controls on completion.
- If editing continues, an accepted older snapshot must not overwrite newer draft changes. Reconcile server normalization with those changes, or defer replacement and show the saved state separately.
- An older request's `finally` must not clear a newer request's pending state. For overlapping saves, client result guards protect UI ownership but do not order server writes; use the application's concurrency/version contract where that matters.
- Aborting a request or leaving a screen does not prove the server rolled back a write. Preserve an unknown-outcome state and use the established reconciliation/idempotency contract before retrying.

Observe initial load after early interaction, dirty refetch, record switching with a delayed response, editing during save, and reset after an accepted baseline. Claim a library defect only after reproducing it in the relevant installed release; a historical issue or source inference alone is not that evidence.

## Sources

- [v1.33.5 reactivity](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/docs/framework/react/guides/reactivity.md)
- [v1.33.5 async initial values](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/docs/framework/react/guides/async-initial-values.md)
- [v1.33.5 FormApi update/reset](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/packages/form-core/src/FormApi.ts)
- [TanStack Query mutation lifecycle](https://tanstack.com/query/latest/docs/framework/react/guides/mutations) (rolling documentation; match installed Query)

Result ownership and reconciliation above are this skill's engineering recommendations, not claims of built-in TanStack conflict resolution.
