# Composition, Arrays, and Wizards

## Choose the Smallest Composition Boundary

| Need | v1 API | Contract |
| --- | --- | --- |
| Concrete form or custom one-off control | `useForm` + `form.Field` | Keep the real form's inferred shape |
| Repeated design-system adapters | `createFormHookContexts` + `createFormHook` | The hook and field consumers share the same context instances; `AppField`/`AppForm` provide them |
| Split one large typed form | `withForm` | Its defaults describe types, not runtime initialization of the parent |
| Reuse fields across forms or nested paths | `withFieldGroup` | Map fields through a string path or supported mapping; reusable field errors may be unknown |
| Independent step validation/submission | `form.FormGroup` | Own validation and submission lifecycle, with group-relative error paths |

These APIs are present in the v1.33.5 evidence; check availability in an older installation. `withFieldGroup` reuses fields; it is not `FormGroup`'s independent validation lifecycle. A UI library's `FieldGroup` is a layout/semantics component, neither TanStack API.

Start from a working concrete form and extract recurring contracts, not a universal form builder. Typed defaults and `formOptions` preserve path/value inference; context type parameters do not perform runtime validation. For hook-using `withForm` render functions, follow the named-component pattern and existing Hooks lint rules. Typed context fallback cannot prove that its provider has the asserted form type; prefer a directly typed form boundary when available. Deep `extendForm` chains may increase TypeScript cost; measure rather than impose an arbitrary layer count.

## Arrays Have Three Identities

- **Domain identity:** the persisted record ID, if one exists.
- **UI identity:** a stable React key for the row instance, including new unsaved rows.
- **Field path:** the current index, such as `lines[${index}].quantity`.

For dynamic rows, use stable UI keys while updating index-based field paths as positions change. Do not generate a fresh key on each render. If UI-only IDs live in draft rows, project them out of the server payload when the command excludes them. An immutable append-only list can have different identity requirements from a reorderable editor; the official index-key array demo is not evidence that index keys preserve row-local state under reorder.

Use `mode="array"` for the array container in v1 and supported helpers such as `pushValue`, `removeValue`, `moveValue`, or their FormApi equivalents. These helpers coordinate structure and metadata; raw array replacement is not automatically equivalent. Stable React keys alone do not prove field errors/touched state/focus follow the intended row.

Observe deletion of an invalid middle row, moving a row, removing the final row and adding again, and any relevant reset or async validation during removal. Values, messages, and focus should still belong to the intended logical row. Investigate installed-version behavior before adding metadata workarounds or claiming an upstream regression.

## Conditional Fields: Four Separate Decisions

For each conditional branch, decide:

1. **Visibility:** whether controls are mounted or merely hidden.
2. **Retention:** whether returning to the branch restores the draft.
3. **Validation:** whether the branch is currently applicable to the domain.
4. **Payload:** whether its data may be submitted.

v1 field unmount does not itself guarantee value deletion. It also does not guarantee that field-local validation continues while unmounted. Retaining a temporarily hidden draft is valid. If the branch is domain-inapplicable, use a conditional/discriminated schema and explicit command/DTO projection to exclude it, or deliberately delete the field if discarding its draft is the intended behavior. `deleteField` is not a mandatory unmount hook. Hidden/disabled DOM controls do not define the client object's payload or the server's allowed fields.

## Multi-Step Forms

A parent form can own the complete draft while mounted `FormGroup`s own step validation. In supported v1 versions, `group.handleSubmit()` validates/submits that group; `form.handleSubmit()` handles the whole form. Group validator `fields` keys are relative to the group. When using dynamic group validation, configure the appropriate `revalidateLogic` and the group's own `onDynamic`; parent submission attempts do not stand in for group attempts.

- **Next:** validate the applicable step before advancing. A successful step is not a persisted form.
- **Back/step selection:** define navigation policy and use `type="button"` for non-submit buttons. Avoid nested HTML forms even though groups are logical subforms.
- **Finish:** validate all applicable retained data and cross-step constraints at a parent schema/save boundary, including data from unmounted steps. Field-local validators alone cannot guarantee this. A valid alternative is separate forms with an explicit final aggregate validation boundary.
- **Invalid finish:** reveal the relevant step and focus its control after mount; preserve the draft.
- **Save:** return/await the parent save promise if it must determine group pending completion. Do not advance or announce success merely because a step callback returned.

Check keyboard Enter, direct step selection, and route-driven entry as well as Next clicks. The final whole-payload boundary must remain authoritative when navigation bypasses step-local checks.

## Sources

- [v1.33.5 composition](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/docs/framework/react/guides/form-composition.md)
- [v1.33.5 Form Groups](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/docs/framework/react/guides/form-groups.md)
- [v1.33.5 array guide](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/docs/framework/react/guides/arrays.md)
- [v1.33.5 FieldApi lifecycle](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/packages/form-core/src/FieldApi.ts)
- [React list keys](https://react.dev/learn/rendering-lists#keeping-list-items-in-order-with-key)

The four-way conditional policy and UI/domain identity separation are engineering recommendations derived from these contracts, not extra library-enforced rules.
