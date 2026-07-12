---
name: verified-email
description: Implements secure Android verified-email retrieval with Credential Manager DigitalCredential, OpenID4VP, DCQL, SD-JWT handoff, backend verification, fallback verification, and optional passkey enrollment. Use when adding OTP-less sign-up, account recovery, or sensitive-action reauthentication that requests a cryptographically verifiable email on Android.
---

# Verified Email on Android

This skill covers Android client integration and the client/server contract. It
does not replace backend cryptographic verification. Client-parsed claims are
display-only until the server validates the complete credential response.

## Prerequisites

- Target Android 9 / API 28 or newer.
- Confirm the device environment has Google Play services 25.49.x or newer.
- Resolve a compatible Credential Manager version that supports
  `GetDigitalCredentialOption` and keep the core and Play-services artifacts on
  the same version.
- Confirm a backend endpoint can issue/store nonces and verify the returned
  OpenID4VP SD-JWT presentation.
- Provide a manual email/OTP fallback before replacing an existing flow.

## Quick start

1. Locate sign-up, recovery, or reauthentication UI and its ViewModel/backend
   contract.
2. Generate a unique cryptographically secure nonce and bind it to one attempt.
3. Build the `openid4vp-v1-unsigned` DCQL request for
   `UserInfoCredential` and request only required claims.
4. Call Credential Manager from the Activity and accept only
   `DigitalCredential`.
5. Send the untouched `credentialJson` and original nonce to the backend.
6. Provision or reauthenticate only after the backend verifies issuer,
   signature, key binding, nonce, time validity, credential type, and email.
7. Offer fallback verification for unavailable, unsupported, stale, or
   mismatched credentials.
8. Optionally offer passkey creation after successful account provisioning.

## Progressive references

- Read [client-flow.md](references/client-flow.md) for dependencies, request
  JSON, Credential Manager handling, and response handoff.
- Read [server-security.md](references/server-security.md) before connecting the
  result to account creation, recovery, or authorization.
- Read [ux-passkeys-and-verification.md](references/ux-passkeys-and-verification.md)
  for supported accounts, fallback UX, WebView, passkeys, and final tests.

## Hard constraints

- Never trust an SD-JWT decoded only on the Android client.
- Never reuse a nonce or log the raw presentation.
- Never silently remove OTP for non-Gmail addresses; they lack a freshness
  guarantee and may require an additional challenge.
- Do not treat email verification as email deliverability proof.
- Do not assume Workspace or supervised accounts are supported.
