# Schema Input, Output, and Field Adapters

Read when changing a resolver, transform/coercion, conditional payload, or controlled field adapter. Inspect installed RHF/resolver/Zod versions and the existing wire contract; preserve library choice.

## Type and Validation Boundaries

- `z.input<typeof schema>` is what parsing accepts; `z.output<typeof schema>` is what successful parsing produces. `z.infer` denotes output. Transforms, coercion, and defaults can make input and output differ, including an optional input becoming a required output.
- RHF draft fields and `defaultValues` belong to the input side; the successful resolver submit callback normally receives parsed output. Let a compatible resolver infer types or use the installed `useForm<Input, Context, Output>` contract. Do not silence mismatches by casting output types onto raw inputs. Resolver `raw` options can return raw rather than transformed values; the callback type and command construction must match the actual configuration.
- Keep parsed form data, application command, and transport DTO semantically distinct. Transform/serialize where required, including wire absence semantics. If they genuinely have the same compatible shape, an identity projection is enough; separate copying types/files are not proof of a boundary.
- Zod refinements/transforms may be async; use a compatible async resolver/parse path. Do not choose synchronous mode to reduce cost if the schema requires async work. Form timing, resolver execution mode, and delayed error display are separate settings.
- A hidden/unregistered input does not remove a schema requirement. Model branch participation explicitly, for example with the existing conditional/discriminated schema or boundary projection. Preserve cross-field validation and error paths. Sending only visible inputs is not universally correct for wizards or preserved drafts.

## Empty, Zero, Null, and Missing

Decide product meanings before coercing. Browser number inputs do not make raw values reliable domain numbers: string values remain strings unless converted, and `valueAsNumber` can produce `NaN` for an empty/invalid value. `z.coerce.number()` follows JavaScript `Number`: empty string, whitespace, and `null` can become zero. Those are not necessarily equivalent business inputs.

| Input | Decision to preserve |
|---|---|
| `''` or whitespace | Required error, explicit absence, or valid text; normalize only under that field's policy. |
| `0` / `'0'` | Preserve valid zero. Falsy-to-undefined normalization can lose it. |
| `null` | Explicit null is not omitted/undefined, particularly for PATCH clear semantics. |
| Missing / `undefined` | Optional input, default application, or required error depends on the schema and wire contract. |
| `NaN` / invalid numeric text | Reject or report as invalid; do not silently manufacture zero. |

Normalize at one intentional boundary; account for `setValueAs`/`valueAsNumber` options already applied before the resolver to avoid conflicting double conversion. Schema defaults fill parse-time output, not necessarily the visible RHF default baseline.

RHF `register` value conversion runs before validation and does not transform `defaultValue` or `defaultValues`. When `valueAsNumber` or `valueAsDate` is enabled, `setValueAs` is ignored. Establish where defaults and reset values are prepared instead of assuming user-input conversion normalizes every initialization path; choose conversion according to the field's meaning and installed API.

## Controlled and Native Inputs

- Native inputs can use `register`; do not convert every input to `Controller`. External controlled widgets may use `Controller` or `useController` through a presentation field adapter.
- Preserve `field.value`, `field.onChange`, `field.onBlur`, `field.name`, and the actual focusable input `field.ref`, adapting nonstandard prop/event shapes intentionally. `onBlur` drives touched/blur validation; a ref attached only to a wrapper cannot focus the errored input.
- Register a field once. Spreading both `register(name)` and Controller's field registration creates competing ownership. Let the field's event contract update RHF rather than duplicating every change through `setValue`.
- Provide a supported controlled default and cleared value; avoid `undefined` for Controller values/defaults or `onChange`. Choose empty string or null only when the widget and schema support that meaning.
- Respect disabled-value omission versus read-only retention under the installed RHF/browser contract. Neither is an authorization boundary. Associate labels and errors, and announce meaningful validation changes without moving every keystroke into a global UI event queue.

A valid native `register` implementation, a schema without transforms, and a same-shape application command need no additional Controller, mapper class, or model suffix.

## Primary Sources

- [RHF resolver types/options](https://github.com/react-hook-form/resolvers#typescript), [Zod input/output](https://zod.dev/basics), [Zod coercion and transforms](https://zod.dev/api).
- [register value conversion](https://react-hook-form.com/docs/useform/register), [Controller contract](https://react-hook-form.com/docs/usecontroller/controller), [handleSubmit disabled behavior](https://react-hook-form.com/docs/useform/handlesubmit).
- [JavaScript Number conversion](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Number#number_coercion), [React controlled inputs](https://react.dev/reference/react-dom/components/input).
