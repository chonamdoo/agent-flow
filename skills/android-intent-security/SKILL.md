---
name: android-intent-security
description: Audits and hardens Android component and Intent boundaries against redirection, unauthorized access, mutable PendingIntent abuse, and unsafe exported components. Use when changing or reviewing AndroidManifest.xml components, incoming or nested Intents, PendingIntent creation, broadcast receivers, services, or ContentProviders.
license: Complete terms in LICENSE.txt
metadata:
  author: Google LLC
  upstream-commit: 57ff3c7d02a53781954f9ce2df92f14b7fbb2ded
---

# Android Intent Security

Apply the smallest relevant part of the pinned official guidance. Treat every
external component entry point and caller-controlled Intent field as untrusted.

## Quick start

1. Inventory exported activities, services, receivers, and providers in every
   affected manifest, including merged-library declarations.
2. Trace incoming Intents, nested Intent extras, URI grants, caller identity,
   and every subsequent launch or privileged action.
3. Prefer explicit internal Intents and immutable PendingIntents. Require an
   explicit component or package for any legitimately mutable PendingIntent.
4. Sanitize nested Intents with an allowlist. If modern sanitization is not
   available, verify package, component, export state, and URI-grant flags.
5. Protect exported components with the narrowest export setting, signature
   permission, caller UID/package/signature verification, or URI grant policy.
6. Add negative tests for an untrusted caller, unexpected extras, redirected
   components, mutable PendingIntent injection, and unauthorized provider data.

## Progressive reference

Read [official-guidance.md](references/official-guidance.md) for the complete
pinned Google guidance, decision tables, implementation patterns, test cases,
and component-specific requirements. Use only sections matching the affected
boundary; do not load unrelated examples into the working context.

## Review constraints

- Never launch a nested Intent directly from an untrusted extra.
- Never create an implicit mutable PendingIntent.
- Do not assume `android:exported="false"` when intent filters or library
  manifests participate in the merged manifest.
- Do not trust a package name without verifying the caller or signing identity
  where partner-app authorization is required.
- Do not grant provider URI access more broadly or longer than the use case.
- Keep platform Intent and component types outside domain models.

## Completion check

Record the audited entry points, trust decisions, mitigations, and negative
tests. Build the affected Android modules and inspect the merged manifest before
declaring the boundary secure.
