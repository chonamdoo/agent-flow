# BO: form ownership

Apply only to `apps/bo/src/app/demo/orders/new/**` in addition to BO route rules. This scope declaration is synthetic metadata; the original form frontmatter was not observed.

## One value owner

One screen has one top-level useForm and FormProvider. Children receive control/name or registration capability; they subscribe with useController, useWatch, or useFormState only to the field or preview region they need. Do not create child forms, duplicate draft stores, or synchronization effects between field/preview owners. Whole-form watch for every keystroke is not a substitute for scoped subscriptions.

The fixture's OrderForm owns values and a last-submitted payload. The submitted snapshot has a distinct purpose from editable draft values and is not synchronized back into the form. It is explicitly local, not a saved record or a simulated request. ReferenceField watches only reference and field errors; NoteField subscribes only to its note path. Successful submission validates and transforms the draft into the visible payload.

## RHF stops above primitives

Connect native fields with register; do not invent per-screen controller wrappers for ordinary inputs. Non-native controlled controls such as Select/RadioGroup/date widgets need a section-owned Controller adapter forwarding value, change, and ref, not a copy per screen. A primitive accepts native input props and ref without importing RHF or domain concepts.

Schema and payload transformation belong in pure _lib. Ordinary RHF types belong in _types or form-aware _components/_ui, never in general _lib. This fixture uses _types/form.ts for field capabilities and _components for its approved owner/field composition.

## Narrow path exception

Only `apps/bo/src/app/demo/orders/new/_lib/rhf-path.ts` may depend on RHF types for the discriminated-union array leaf-path constraint. It may not import RHF values, React, or Next, and the exception does not extend to neighboring files or other routes. Constrain paths to real schema keys so a misspelled leaf still fails compilation.

The verified original rule isolates a cast for an RHF discriminated-union array inference limitation at that location. A new cast requires the same demonstrated cause. This fixture represents the union note leaf with a constrained type-only helper; it does not add an unnecessary cast or claim that the pinned RHF version reproduces the original inference failure. Tests of the narrow type-import permission must not imply blanket _lib RHF access.

The separate React Compiler register/reset workaround uses `use no memo` only on a component with that demonstrated root cause, with a reason comment. No such failure is demonstrated here, so no directive is added. Neither exception is an automatic template for new fields.

## Pure validation and conversion

Form Zod schemas describe editable drafts, not server response shapes. Keep cross-field invariants in pure schema code so they can run without rendering. Distinguish z.input from validated output; trim, convert, and map payload names in _lib rather than screen JSX. Response parsing belongs to shared/api, which uses the shared technical parser and owns BO response validation. Never duplicate JSON parsing in a form field or merge response and draft schemas because both use Zod.

These order fields and payload names are synthetic examples, not recovered product rules. Review meaningful state ownership, subscription scope, validation semantics, response/draft separation, and the precise reason for a workaround. Import lint and useForm counts cannot replace this review.
