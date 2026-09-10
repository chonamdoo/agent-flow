# Draft Lifecycle and Identity

Read when changing async defaults, record transitions, refetch/reset, conditional fields, arrays, or save-result application. The skill's submission/safety contract applies throughout.

## Baseline and Reset Policy

`defaultValues` are cached, not a live subscription to props. Distinguish these transitions before applying data:

| Transition | Required decision |
|---|---|
| Initial async arrival | Initialize the intended form only; preserve or prevent edits made while loading according to the product policy. |
| A-to-B record change | Establish B's baseline after resolving A's unsaved-edit policy; never merge A's dirty values into B automatically. |
| Same-record background refetch | Keep, reconcile, or explicitly replace the draft. A new object reference is not a reason to discard edits. |
| User cancellation | An intentional reset to the chosen baseline is valid. |
| Accepted save | Update baseline/draft only for the current target and accepted request; preserve edits newer than the submitted snapshot. |

- `reset(values)` normally replaces values and their default baseline. `keepDefaultValues` preserves the original comparison baseline; it does not mean “adopt the server response as clean.”
- `keepDirty` preserves dirty bookkeeping, not dirty input values. `keepDirtyValues` preserves dirty field values while updating other fields; current RHF requires a `dirtyFields` subscription. `keepValues` preserves all input values. Check installed-version semantics rather than treating these options as synonyms.
- Reactive `values` and async `defaultValues` can cause internal resets; configure their reset policy deliberately. Passing every refetch through `values` is not automatically safer than explicit `reset`.
- Perform reset after form subscription setup in a supported effect/event lifecycle, not during render. Controlled fields need appropriate form defaults for reset. Do not use an `isSubmitSuccessful` effect as a substitute for actual business acceptance and request identity.

## Late Results

Use the existing request/state pattern, not a new generic concurrency framework. Correlate target record plus form/edit generation and request identity; raw object identity and RHF row keys do not identify a saved business record. If relevant, tenant/session changes invalidate the target too.

A response for A arriving after navigation to B cannot reset B, attach A's field errors, or navigate away from B. Within A, old validation errors must not attach to subsequently corrected values, a late success must not erase newer edits, and an old cleanup must not clear a newer request's pending state. Cancellation can suppress irrelevant UI application but cannot prove server rollback. Global common-error classification follows the existing session/error contract independently of whether local draft application is stale.

## Conditional Fields

Treat CSS-hidden, mounted-but-hidden, unmounted, and unregistered as different states. Decide separately whether a branch retains draft values, participates in validation, and appears in the command/wire payload.

`unregister` removes registration/value state according to its options; it does not rewrite a Zod schema. A still-required schema field can fail even after its input is removed. Align the branch schema and submitted shape with the product policy. A wizard retaining hidden-step data for later return/submission is valid; a branch whose old values must be omitted needs an explicit schema/projection policy. Read [Schema and adapters](schema-and-adapters.md) when that policy changes parsing or payload construction.

## Field Arrays

- Use the RHF-generated `field.id` as the React key (or its configured key property supported by the installed version), not index or database ID. Keep business identity separate; do not accidentally submit the generated key as a persisted ID. Ordinary domain lists still use domain IDs.
- `fields` describes the row structure, defaults, and generated keys; it is not a live snapshot of every edited value. Read current values through a correctly located subscription for display or `getValues` for an event snapshot.
- `update` unmounts/remounts the updated row. If focus, local state, or uncontrolled input continuity must survive, use supported leaf `setValue` operations instead. Intentional remount is valid; it is the lost contract, not the method name, that warrants a finding.
- Reorder/remount interacts with `shouldUnregister`; do not enable unregister-on-unmount on controlled array rows as a generic cleanup rule. Follow the installed RHF support contract and verify value/focus identity through reorder and deletion.

## Primary Sources

- [useForm defaults and values](https://react-hook-form.com/docs/useform), [reset options](https://react-hook-form.com/docs/useform/reset).
- [unregister](https://react-hook-form.com/docs/useform/unregister), [Controller shouldUnregister](https://react-hook-form.com/docs/usecontroller/controller).
- [useFieldArray](https://react-hook-form.com/docs/usefieldarray), [setValue](https://react-hook-form.com/docs/useform/setvalue), [handleSubmit](https://react-hook-form.com/docs/useform/handlesubmit).

Record/edit/request correlation above is a design rule for preserving the application contract, not a claim that RHF implements server-write cancellation or business idempotency.
