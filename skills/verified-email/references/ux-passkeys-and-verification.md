# UX, passkeys, WebView, and verification

## Supported experience

Verified email fits sign-up, account recovery, and reauthentication for a
sensitive action. Consumer Google Accounts are supported; Workspace and
supervised accounts are not. A consumer account may use Gmail or an address
from another provider, but non-Gmail addresses need an extra freshness check
when current mailbox control matters.

Every screen needs:

- a primary action that opens Credential Manager;
- a visible “Verify another way” or equivalent fallback;
- a mismatch path when the returned email differs from an expected address;
- distinct cancellation, unavailable, retryable, and terminal failure states;
- a success state only after the backend verifies the presentation.

Do not claim the flow guarantees email delivery. Keep OTP/manual verification
available for unsupported devices, unavailable credentials, account mismatch,
non-Gmail freshness, and delivery-sensitive flows.

## Passkey enrollment after success

Passkey creation is separate from verified-email retrieval. Offer it after the
backend has provisioned the account:

1. Configure Digital Asset Links for the relying party.
2. Request fresh public-key creation options and challenge from the backend.
3. Build `CreatePublicKeyCredentialRequest` from the untouched server JSON.
4. Call `CredentialManager.createCredential` from the Activity.
5. Send the returned public-key credential to the backend.
6. Verify challenge, origin, RP ID, user verification, and attestation policy,
   then store the public key.
7. Notify the user of successful creation and handle cancellation separately.

The server-generated user ID must be opaque and contain no email or other PII.
Use `excludeCredentials` to avoid duplicate passkeys. Do not treat a successful
verified-email response as a passkey.

## WebView

When the product flow is hosted in WebView, confirm the project WebKit version
supports `WebViewFeature.WEB_AUTHENTICATION`, enable support through
`WebSettingsCompat`, and configure Digital Asset Links. If the architecture
uses a JavaScript bridge, expose a narrow allowlisted message contract that
starts native Credential Manager and returns only the backend-verified result.
Never expose the raw presentation or nonce to arbitrary origins.

Test the native app, web content, and backend as one transaction. Conditional
mediation may not be available in the WebView integration, so keep an explicit
user action and fallback.

## Verification matrix

- [ ] API 28 minimum behavior and supported Play-services environment.
- [ ] Eligible Gmail consumer account success.
- [ ] Non-Gmail consumer account routes through the configured freshness check.
- [ ] Workspace/supervised account or no credential uses fallback.
- [ ] User cancellation does not show a security error or consume the nonce.
- [ ] Retryable interruption can retry with a fresh/valid transaction.
- [ ] Expected-email mismatch offers another credential or manual verification.
- [ ] Tampered SD-JWT, disclosure, issuer, key binding, nonce, and expiry fail
  closed on the backend.
- [ ] Replayed nonce is rejected atomically.
- [ ] Client never trusts preview-parsed claims or logs raw credential data.
- [ ] Account creation/recovery occurs only after backend approval.
- [ ] Passkey prompt appears only after provisioning and has its own errors.
- [ ] WebView origin/feature checks and fallback work where applicable.

Run Android build, lint, unit/UI tests, backend verification tests, and an
end-to-end device test. Include negative cryptographic fixtures; a successful
Credential Manager bottom sheet alone does not prove a secure integration.
