# Subscriptions and Validation Cost

Read when changing RHF subscription placement, reactive previews, input latency, validation timing, or resolver work. Determine the installed version and actual consumers before optimizing.

## Subscription Location

- `watch('field')` narrows watched values, not the rendering boundary: it can update the `useForm` root. `useWatch` isolates updates at the component calling it; calling it at the root still updates the root. Put the subscription at the consuming leaf/section when a broader update creates real cost.
- `getValues()` reads an event-time snapshot without a reactive subscription. It is appropriate for an action that needs current values; it cannot replace a live preview's subscription. For `useWatch`, account for subscription setup order if a value is written before the subscriber mounts; use the installed API's current-value/default semantics rather than assuming missed notifications replay.
- Subscribe to errors/touched/dirty fields where they are displayed with `useFormState` or field state, using supported name/exact options where appropriate. `formState` uses a Proxy: read the properties required for rendering so the subscription is established; short-circuit conditional access can omit a needed subscription.
- `isDirty` concerns the form's values compared with its default baseline, not “ever touched” or an interchangeable count of `dirtyFields`. Values restored to their defaults can become clean. `dirtyFields` is field-level information; `touchedFields` records interaction. Supply complete meaningful defaults for comparisons and respect RHF limits for files/custom objects. Programmatic writes and reset options can change dirty bookkeeping; consult their actual options before interpreting it.

A whole-form unsaved-change banner consuming `isDirty`, or a small form whose root genuinely consumes its watched values, is normal. An unused broad subscription alone is not evidence of user-visible jank.

## Validation Timing and Cost

- Current RHF v7 documents `mode: 'onSubmit'` and `reValidateMode: 'onChange'` by default. The latter concerns error revalidation after submission. Inspect the installed version and actual configuration, `trigger`, programmatic `setValue` validation, and resolver calls; Zod does not intrinsically validate every keystroke.
- Explicit `onChange`/`onBlur`/`onTouched` modes are valid product choices. Preserve requested feedback timing; wired blur events matter for controlled inputs.
- Separate subscription notification, schema parse/refinement work, and React render/commit cost. Moving to `useWatch` does not shrink the resolver's computation.
- `trigger('field')` can isolate RHF render notification for a single name; array/whole-form triggering differs. It does not guarantee that an external resolver parses only that field. Cross-field refinements still need their inputs and correct error paths. Confirm the installed resolver implementation before claiming field-only computation or splitting a schema.
- Profile the actual slow interaction and resolver work when making a performance claim. Choose local subscription, validation scheduling, or schema changes according to the measured cause; memoization, schema splitting, and fixed render-count targets are not default requirements.

## Primary Sources

- [watch](https://react-hook-form.com/docs/useform/watch), [useWatch](https://react-hook-form.com/docs/usewatch), [getValues](https://react-hook-form.com/docs/useform/getvalues).
- [useFormState](https://react-hook-form.com/docs/useformstate), [formState](https://react-hook-form.com/docs/useform/formstate), [setValue](https://react-hook-form.com/docs/useform/setvalue).
- [useForm mode and resolver](https://react-hook-form.com/docs/useform), [trigger](https://react-hook-form.com/docs/useform/trigger), [Zod resolver implementation](https://github.com/react-hook-form/resolvers/blob/master/zod/src/zod.ts).
- [React Profiler](https://react.dev/reference/react/Profiler).
