# Validation and Submission

Applies to v1-style APIs. Confirm installed declarations; sources and limits are recorded in [Examples and provenance](examples-and-provenance.md).

## Schema Input Is Not Parsed Output

Use a Standard Schema-compatible version of the project's schema library, or keep supported function validators. A library name alone does not establish compatibility, and incompatibility is not permission to upgrade it.

TanStack Form validates the schema but passes **input** values to `onSubmit`. For Zod, type defaults with `z.input<typeof schema>`; the parsed save value has `z.output<typeof schema>`. Other schema libraries have equivalent input/output distinctions. Typed defaults and `formOptions` usually preserve inference better than manually reproducing every Form generic; valid explicit generics and inline defaults are not inherently defects.

Author-written boundary illustration, not a complete form:

```ts
const quantitySchema = z.object({
  quantity: z.string().regex(/^\d+$/).transform(Number),
})
const initialQuantity: z.input<typeof quantitySchema> = { quantity: '' }
const parsedQuantity: z.output<typeof quantitySchema> =
  quantitySchema.parse({ quantity: '12' })
```

Here the controlled draft remains a string; the parsed quantity is a number. In `onSubmit`, parse its actual `value` before passing output to the save boundary. Use `await schema.parseAsync(value)` or `await schema.safeParseAsync(value)` for async refinements/transforms; handle parse failure as validation feedback, not successful save. Put asynchronous schemas in `onChangeAsync`, `onBlurAsync`, or `onSubmitAsync`, not synchronous slots. Repeated validation/parsing can repeat refinements, so keep them free of writes and assess expensive remote checks separately. Do not treat `parseValuesWithSchema`/`parseValueWithSchema` error inspection as an automatic state update or transformed save value.

## Timing, Dependencies, and Errors

- Select validation timing from the interaction contract. v1 dynamic validation needs a matching `validationLogic`, normally `revalidateLogic()`, as well as `validators.onDynamic`. Its default is submit-first, then change; configure modes explicitly when the product differs. Group-local behavior is covered in [Composition](composition-arrays-and-wizards.md).
- Sync validation runs before the corresponding async path; failure normally skips async work. `asyncAlways` deliberately changes that. Choose debounce from measured cost and UX; `asyncDebounceMs` and trigger-specific overrides control frequency, not result ownership.
- Forward a supplied cancellation signal to requests that support it. Establish how cancellation differs from service failure in the installed implementation. Do not assume abort cancels external effects, suppress all `AbortError`s into valid results, or infer a current library race from old source/issue reports.
- For linked fields, `onChangeListenTo`/`onBlurListenTo` revalidates dependencies supported by the installed API. Field-local linkage is not a substitute for whole-payload invariants when fields can unmount.
- Form validators can return `{ form, fields }`, with full field paths such as `'contact.email'` or `'lines[0].sku'`. A field validator for the same trigger can override a form-derived field error; combine deliberately rather than assume both appear.
- Error values follow validator types. String validators, custom objects, and Standard Schema issue arrays need distinct rendering. A form-level schema error map can be a record of issue arrays; render their messages rather than joining objects into `[object Object]`. Shared adapters must safely handle unknown errors without a cast that invents their shape.
- `canSubmit` can be true before interaction even if validation will later fail. It is a submission affordance, not proof of valid or saved data. Do not disable pristine submissions unless the product actually requires an edit first.

## Controlled Widget Contract

| Control | v1 connection | Boundary to decide |
| --- | --- | --- |
| Text/textarea | `value={field.state.value}`, change to `field.handleChange(event.target.value)`, `onBlur={field.handleBlur}` | Empty string versus absent value |
| Numeric text | Keep a string draft or deliberately map `valueAsNumber` | Empty input, `NaN`, zero, locale, output conversion |
| Select | `value`, `onValueChange={field.handleChange}` | Empty selection and composite blur/focus behavior |
| Boolean checkbox | `checked`, change mapping to boolean | Preserve false; map an indeterminate state only if the domain is binary |
| Multi-select/checkbox set | Membership plus array helpers | Stable option identity and duplicates |

Map disabled, name/FormData participation, and ref/focus as required by the real widget. Headless TanStack Form does not supply the markup or focus policy. Use unique control IDs associated with labels; an ID need not equal the field path. Associate rendered error/help nodes with the control, expose `aria-invalid` according to the chosen touched/submission policy, and provide suitable error announcements. A composite widget needs a real blur transition if blur validation is promised.

On invalid submit, focus the intended form's first usable invalid control via its ref/registry or a **form-scoped** query. Reveal its step/section before focusing and wait for the control to mount; skip disabled/nonfocusable nodes. A global document query may focus another form. If using `aria-disabled` instead of native `disabled`, enforce the interaction guard; the attribute alone blocks nothing.

## Save Outcomes

For a client-driven form, prevent the native submit and invoke `form.handleSubmit()` once. Preserve keyboard/Enter submission and avoid a submit button that calls it again in `onClick`. Handle a rejected submission promise at the existing error boundary. For native actions, follow [SSR](ssr.md) instead.

Return/await `mutateAsync` or the existing save promise inside `onSubmit`; fire-and-forget `mutate()` cannot represent its pending lifetime. Classify returned business failures as well as thrown failures. If an error is caught and the callback returns normally, Form may consider its lifecycle completed: keep business-success effects in the explicit accepted-result branch. Preserve the draft on rejection; only a current accepted result may reset, notify success, or navigate.

A read-only remote validator can return field errors from `onSubmitAsync`; persistence belongs in the save boundary, not validators. Map persistence validation failures through the installed typed error API (for example an appropriate `setErrorMap` server error shape). Keep conflict/network/domain feedback visible even if a field is unmounted. Route only classified common failures through the existing AppShell; do not turn every permission error into session expiry.

Observe the complete correction-and-resubmit path, including empty/zero/false, async validation failure, server rejection, pending interaction, and focus. A successful validation request is not a successful write.
