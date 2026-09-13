---
name: react-hook-form-zod
description: React Web React Hook Form creation, review, subscription performance, draft initialization, conditional fields, controlled adapters, and submission correctness; Zod resolver input/output at the form boundary. Not generic TSX, server-only Zod, other form libraries, or React Native-only work.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [react-hook-form, react hook form, zodResolver, RHF form]
---

# React Hook Form and Zod

Use for an actual React Web RHF form or a Zod resolver connected to one. A bare `watch`, `reset`, `Controller`, or TSX extension is not sufficient. Preserve existing form libraries; RHF-only work does not require adding Zod. Discover installed RHF, resolver, Zod, and React versions and the existing save/error boundary before choosing APIs. Skill selection is not permission to install dependencies, execute writes, or bypass runtime approval and workflow gates.

## Single State Owner

- RHF owns input draft, dirty/touched state, and field errors. Existing RSC/query-cache boundaries own server data and refetch; the URL owns shareable filters; the screen state holder owns business progress, allowed actions, and domain-error presentation. Keep focus/hover state close to its consumer and use shared client state only when genuinely shared.
- Do not mirror RHF values or field state into a ViewModel `uiState` or global store for synchronization. A custom hook shares logic, not state instances; Context alone guarantees neither lifetime nor render isolation.
- Reusable UI primitives receive narrow label/value/error/callback/ref contracts. RHF field adapters may use `control`, `useWatch`, `useFormState`, or `useController` inside presentation. A small schema + form + existing save boundary is valid; file count and naming are not architecture evidence.
- Keep draft input, parsed resolver output, application command, and wire DTO meanings distinct. Convert only where meaning differs; identical compatible shapes need no artificial model copies. In Clean mode, domain policy stays free of RHF/Zod/Next dependencies. Follow the selected architecture contract for screen ownership and dependency wiring (`react-clean-presentation-architecture` in Clean mode).

## Submission and Safety

- Pass validated output through the existing application action or typed port. Return/await the actual async save in `handleSubmit`; preserve its pending lifetime and handle both returned business failure and thrown failure. Promise completion and `isSubmitSuccessful` are not independent proof of business success.
- Bind async initialization, field errors, save responses, reset, navigation, and pending cleanup to the intended record, form/edit generation, and request. An old response for A must not change B; an older request's `finally` must not clear a newer request's pending state. Include tenant/session identity when it changes the target's meaning.
- If editing continues during save, an accepted response must not erase edits made after its submitted snapshot. Choose the baseline/draft policy explicitly. Abort/unmount is not evidence that the server rolled back a write; do not blindly retry an unknown write outcome.
- Treat visibility, draft retention, schema participation, and submitted payload as separate decisions. A hidden/disabled input and successful client parsing are not authorization. The server independently validates format, current business state, actor/tenant/object/field permissions, and persistence invariants for direct, stale, or tampered requests.
- Keep field/domain errors local and route only classified common errors through the existing AppShell contract. If changing global classification or root navigation, read `react-app-shell-error-handling`; do not infer session expiry from every HTTP 403 or present one error in both local and global hosts.
- Preserve label/error association, blur/touched behavior, and focus on the actual input. Correct data submission without usable error feedback is not a complete form interaction.

## Conditional Detail

Read only the branch being changed or reviewed:

- Subscription location, live previews, input latency, validation timing, or resolver cost: [Subscriptions and cost](references/subscriptions.md).
- Async defaults, record changes, refetch/reset, conditional unmount/unregister, field arrays, or response-driven draft updates: [Lifecycle and identity](references/lifecycle.md).
- Resolver transforms, coercion, conditional schema/payload, or controlled input integration: [Schema and adapters](references/schema-and-adapters.md).

## Completion Evidence

Use the project's authorized runtime and declared verification commands, not invented scripts or a new form scaffold. Exercise the actual changed form path; report observed behavior separately from source reasoning and unexecuted scenarios. Choose relevant boundaries: dirty refetch, A-to-B change, editing during pending save, stale failure/success, empty/zero/null, hidden-field return, array reorder, blur/focus, and failed submit followed by correction and resubmit. Performance claims need observed input latency and separated parse/render costs, not an arbitrary render-count target. Story fixtures that inject `error` props prove presentation only, not this form pipeline.

## Primary Sources

- [RHF useForm](https://react-hook-form.com/docs/useform), [handleSubmit](https://react-hook-form.com/docs/useform/handlesubmit), [formState](https://react-hook-form.com/docs/useform/formstate).
- [RHF resolver input/output](https://github.com/react-hook-form/resolvers#typescript), [Zod parsing](https://zod.dev/basics).
- [React state ownership](https://react.dev/learn/sharing-state-between-components), [custom hooks](https://react.dev/learn/reusing-logic-with-custom-hooks).

Official API details are version-sensitive; the conditional references explain where installed-version checks change a decision.
