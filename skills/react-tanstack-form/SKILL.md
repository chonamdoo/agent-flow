---
name: react-tanstack-form
description: React Web TanStack Form creation and review, Standard Schema validation and submission, controlled adapters, subscriptions, remote drafts, arrays, composition, wizards, and SSR. Installed-version-aware; not RHF-only forms, generic TSX, server-only schemas, or React Native-only work.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [TanStack Form, tanstack-form, '@tanstack/react-form', 탄스택 폼]
---

# React TanStack Form

Use for an actual React Web TanStack Form task. Confirm dependencies and the touched form: a `Field` component, Zod schema, or TanStack Query dependency alone does not identify the form library. Preserve existing library choices; this skill does not authorize installation, migration, production writes, or workflow bypasses.

## Establish the Contract

1. Read the installed Form/core, React, schema-library, TypeScript, and applicable Query/framework adapter versions from manifests and lockfiles. Check the installed exports/types before choosing APIs. The references use **v1.33.5** evidence, not a minimum-version requirement; retain valid older APIs. In particular, an older supported `useStore` implementation need not become an unavailable `useSelector`. Do not mix v2 alpha examples with v1.
2. Identify draft input, validation output, save command, success/failure result, and server baseline. Standard Schema validation does **not** replace TanStack Form's input values with transformed output. Parse explicitly at the save boundary when output is required, using async parsing for async schemas.
3. Identify the form's lifetime, record/tenant identity, remote refresh policy, and submission transport. Read the matching references below before changing those boundaries. A small concrete form and the existing save action are sufficient; a new framework is not required.

This discovery is complete when the chosen APIs exist in the installed release and the owner of each state and effect is explicit. If source access is unavailable, work from installed declarations and established local patterns; report any compatibility claim that remains unverified.

## Shared Invariants

- **One draft owner.** TanStack Form owns input values and field metadata. Query/RSC owns server snapshots; the screen owns business progress and navigation. Avoid synchronized copies of form state in another store. Keep library adapters in presentation and domain policy behind the project's existing application boundary.
- **Reactive reads.** Render changing form state through a narrow `form.Subscribe` or the installed selector hook. A direct `form.state` read is a snapshot, useful in an event handler but not a render subscription.
- **Explicit adapters.** Connect value/checked, change, blur, and focus to the real control. Preserve empty, zero, false, and null meanings; render validator messages with associated labels and accessible feedback. A TanStack Field API is not an RHF field-props object.
- **Real completion.** Return/await the actual save from `onSubmit`. Only confirmed business success permits success feedback, reset, or navigation. A resolved callback or library success flag alone does not establish persistence.
- **Result ownership.** Delayed initialization, validation feedback, save errors, success, and pending cleanup must still belong to the intended record, edit generation, and request. Preserve edits made after a submitted snapshot unless the product explicitly locks editing.
- **Server authority.** Client validation and hidden/disabled fields are UX, not authorization. The server validates submitted data, current business state, actor/tenant/object/field permissions, and persistence invariants independently.

## Conditional References

Read the branch being implemented or reviewed:

| Branch | Reference |
| --- | --- |
| Schema input/output, async or dynamic validation, controlled widgets, errors/focus, save outcomes | [Validation and submission](references/validation-and-submission.md) |
| Render subscriptions, async defaults, Query refetch, reset/baseline, late responses | [Lifecycle and subscriptions](references/lifecycle-and-subscriptions.md) |
| Reusable fields, arrays, conditional retention/payload, multi-step forms | [Composition, arrays, and wizards](references/composition-arrays-and-wizards.md) |
| Next.js/Start server actions, native FormData, SSR state transfer | [SSR and server boundaries](references/ssr.md) |
| Choosing an upstream example, checking source versions, copying licensed material | [Examples and provenance](references/examples-and-provenance.md) |

For changes to TanStack Query keys, client/cache lifetime, mutations, or post-save synchronization, also read `react-tanstack-query`; the form references remain authoritative for draft/reset behavior. For Next.js submission paths, rewrites, upstream environments, or HTTP mock routing, read `nextjs-api-routing`. Neither branch requires changing the existing form library or introducing a BFF.

## Completion Evidence

Exercise the changed form path using the project's authorized runtime and declared checks. Observe displayed values, validation feedback, pending lifetime, outgoing payload, save result, and focus—not just internal state or compilation. Choose relevant boundaries from the reference in use; do not create a demo scaffold merely to satisfy this skill. Performance claims need measured interaction/render/validation cost, not an arbitrary render-count target. Report executed evidence separately from source reasoning and unrun scenarios. A visual fixture with injected errors proves presentation, not the submission pipeline.
