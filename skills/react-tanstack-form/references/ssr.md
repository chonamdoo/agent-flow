# SSR and Server Boundaries

## Select One Submission Transport

| Client-driven mutation | Native/framework server action |
| --- | --- |
| Prevent native submit, run Form validation, await the save promise | Submit named successful DOM controls as FormData through the framework action |
| Form callback owns the awaited save lifecycle | Framework action/navigation owns its pending and response lifecycle |
| Apply a current accepted result to cache/draft/navigation | Return safe validation state or redirect, then merge/restore according to the framework contract |

SSR rendering alone does not require a native action; a hydrated client-driven form is valid. Do not combine a native action with a separate client persistence callback and accidentally write twice. Calling `form.handleSubmit()` without preventing or coordinating native submission does not make its async validation an authorization gate for the native POST. Keep server validation authoritative in either transport.

## Match the Adapter and Framework Versions

The v1.33.5 sources use standalone `@tanstack/react-form-nextjs` and `@tanstack/react-form-start` adapters. Older examples may use different import paths; inspect the installed package exports and server/client boundaries before changing imports. Match React/Next/Start APIs as well as Form. Source examples using `useActionState` are not a compatibility promise for every React version.

**Next.js:** the referenced pattern shares safe `formOptions`, validates FormData with `createServerValidate` in a server action, returns `ServerValidateError.formState` for validation failures, and merges returned state with `useTransform`/`mergeForm` in a client component. Use framework action pending for that transport; do not assume Form's `isSubmitting` spans the action request. Bind merged results to the intended form/edit identity so a late action cannot overwrite newer work.

**TanStack Start:** the referenced native POST pattern submits to a server-function URL. In the published 1.33.5 adapter, validation failure is transported through cookie-backed state and a redirect; `getFormData` consumes that state for the next render. Inspect the installed implementation's cookie size, sensitivity, integrity, lifetime, and cross-tab/form isolation before adopting that mechanism. The research did not establish its suitability for sensitive or large payloads. Native navigation pending is not automatically Form callback pending.

`useTransform` dependencies determine when new server state is merged; `mergeForm` is a state integration API, not a sanitizer or authorization check. Share only client-safe shape/options across the boundary. Keep database access, secrets, and server-only policy out of client bundles.

## Decode, Validate, Authorize, Persist

1. **Decode the real payload.** Native controls need `name`; controlled React state alone does not serialize them. Test custom selects, unchecked/disabled checkboxes, empty numbers, files, and nested arrays against actual FormData encoding. TypeScript defaults do not prove that an incoming value has that runtime type.
2. **Validate and obtain the intended output.** The referenced Next adapter decodes FormData, validates, and returns decoded values; do not assume schema transformation output replaced them. If the command requires transformed output, parse at the server boundary using the schema's sync/async contract. Server validator callbacks are not client FormApi instances.
3. **Authorize current state.** Check actor, tenant, object and field permissions and domain invariants independently of client validators, visibility, or a submitted record ID. Keep the existing CSRF/origin and concurrency protections appropriate to the transport.
4. **Await persistence.** Return success/redirect only after the actual write is accepted. Treat validation, conflict, permission, infrastructure failure, and unknown write outcome according to the application's result contract.
5. **Return safe feedback.** Preserve useful draft/error state without returning passwords, tokens, unnecessary private values, or internal exception details. An adapter's ability to return every submitted value is not permission to do so. Scope state transfer to the intended form/session.

Hydration needs compatible server/client initial values and associated IDs. Observe the actual POST/action-to-error-render round trip, correction and resubmission, pending behavior, and success navigation. A guide whose persistence is only a comment or console output does not prove a production save path.

## Sources

- [v1.33.5 SSR guide](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/docs/framework/react/guides/ssr.md)
- [v1.33.5 Next server validator](https://github.com/TanStack/form/blob/b865ef335a69aa08a2f160895258f13e03773467/packages/react-form-nextjs/src/createServerValidate.ts)
- [Published Next adapter 1.33.5](https://unpkg.com/@tanstack/react-form-nextjs@1.33.5/dist/esm/createServerValidate.js)
- [Published Start validator 1.33.5](https://unpkg.com/@tanstack/react-form-start@1.33.5/dist/esm/createServerValidate.js) and [state consumer](https://unpkg.com/@tanstack/react-form-start@1.33.5/dist/esm/getFormData.js)

Server authorization, safe feedback, and transport isolation are this skill's security recommendations. The example integrations do not implement or certify them for an application.
