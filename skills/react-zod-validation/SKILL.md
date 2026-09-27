---
name: react-zod-validation
description: React Web and Next.js Zod schema design and review at form, response, and server-action boundaries. Use when changing Zod parsing, transforms, inferred input/output types, field errors, or schema placement; not for generic React code, an unrelated form library, or React Native-only work.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Zod, z.safeParse, z.object]
---

# React Zod Validation

This skill is installed with the React Web profile, but applies only when the task actually uses or adopts Zod. Skill installation is not permission to add the `zod` npm dependency, rewrite a working validator, or assume Zod is used by every screen. Read the project's installed Zod, TypeScript, form-library, and framework versions and its existing schema placement before changing an API. Another project's Zod usage is not a measured ecosystem adoption rate or a universal folder contract.

## Own the boundary

- Choose a schema where untrusted data enters: network response, submitted form payload, server action, or persisted state, according to the target project's selected architecture. A TypeScript annotation alone does not validate runtime input. Parse once at the correct trust boundary and keep the parsed result as the validated value; avoid maintaining a duplicate schema/type declaration for the same shape.
- Distinguish `z.input<typeof schema>` (what a transform/coercion accepts) from `z.output<typeof schema>` or `z.infer<typeof schema>` (what parsing returns). A form's editable draft may retain input shape while the save command uses parsed output. Never assert a transformed type without actually parsing the submitted value.
- Use `parse` when throwing is the caller's established error contract, `safeParse` when validation failure is an ordinary branch; use their async variants when the schema has async refinements or transforms. An API response schema does not replace HTTP error and business-result handling.
- Keep independent domain invariants, authorization, and tenant/object/field permission checks at their server owner. Browser-side Zod validation is user feedback, not access control. In a server handler, use the validated output rather than the original untrusted object when acting on a request.
- Preserve nested paths and form-level issues when mapping `ZodError.issues` to fields. `z.flattenError` is for shallow fields in Zod 4; nested fields need issue-path traversal or a suitable nested formatter. Inspect the installed major version before choosing error helpers, defaults, coercion, or unknown-key policy.

## Form and cache branches

- With TanStack Form, read `react-tanstack-form` for Standard Schema integration, draft ownership, subscriptions, and submit lifecycle. Form validation alone does not replace input with transformed output; explicitly parse when the save command needs that output.
- With React Hook Form and a Zod resolver, read `react-hook-form-zod` for resolver input/output, dirty state, arrays, and field adapters. Preserve an existing RHF form; this skill does not mandate migrating it to TanStack Form.
- With TanStack Query, read `react-tanstack-query` when parsed API data, errors, keys, or cache writes change. Zod parsing does not choose a QueryClient lifetime or invalidate a server cache.
- Keep schemas that are used by both server and client free of server-only imports and secrets. Inspect the actual bundling boundary before re-exporting them through a client-imported public API.

## Completion evidence

Exercise the changed input at its actual caller: accepted input, an invalid or nested value, and a transformed value when present; observe the parsed payload, field/form error feedback, and whether a failed validation prevents the write. For server-side input, confirm the same checks run without browser involvement. For an API response, confirm invalid data is a failure rather than a successfully cached result. A typecheck or schema definition alone is not behavior proof.

| Case | Expected decision |
| --- | --- |
| Supported | A form with a coercing schema sends the parsed output while the form retains its editable input shape. |
| Valid alternative | An existing RHF/Zod form stays on RHF; a form with another working validator need not install Zod. |
| Defect | A server action trusts an unparsed submitted payload, or a nested field error disappears under shallow flattening. |
| Non-target | A TSX style change with no Zod schema or adoption decision does not select this skill's rules. |

## Sources and limits

[Zod parsing and input/output types](https://zod.dev/basics), [Zod error formatting](https://zod.dev/error-formatting), [TanStack Form validation](https://tanstack.com/form/latest/docs/framework/react/guides/validation), and the repository's `react-tanstack-form` and `react-hook-form-zod` skills. Version-sensitive methods in linked rolling docs are examples; installed exports and the selected project architecture govern actual code. Shipping this skill with the React Web profile is a kit packaging choice, not a verified prevalence study.
