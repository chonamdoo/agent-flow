# Backend verification contract

The Android app is a transport and consent surface. The relying-party backend
is the verifier and must reject the transaction unless every required check
succeeds.

## Required checks

1. Parse the OpenID4VP response and select the credential matching the requested
   DCQL identifier and format.
2. For the Google `UserInfoCredential` flow in this skill, require `iss` to
   equal `https://verifiablecredentials-pa.googleapis.com` exactly and resolve
   verification keys only from
   `https://verifiablecredentials-pa.googleapis.com/.well-known/vc-public-jwks`.
   These are protocol endpoint literals, not documentation links. Never derive
   the trusted issuer or JWK endpoint from an unverified credential. Reject
   every other issuer and any algorithm outside the backend allowlist.
3. Verify the SD-JWT issuer signature and every disclosure digest.
4. Verify the Key Binding JWT and its `cnf` key relationship so the presenter
   proves possession of the credential-bound key.
5. Compare the returned nonce to the server record using exact bytes and mark
   it consumed atomically. Reject missing, expired, reused, wrong-session, or
   wrong-purpose nonces.
6. Validate issuance/expiry constraints and the expected
   `UserInfoCredential` type.
7. Require `email` to be syntactically valid and `email_verified` to be true.
8. Apply provider policy to `hd` and account class; unsupported Workspace or
   supervised accounts must use another flow.
9. Return only the verified claims needed by the client and an opaque server
   transaction/account result.

Use a maintained standards-compliant SD-JWT/OpenID4VP verification library.
Do not implement signature, disclosure, or key-binding verification with custom
string splitting or generic JWT decoding.

## Trust and freshness

A “verified” claim means the issuer asserts that it checked the value; the
relying party still decides whether it trusts that issuer and verification
process. Consumer Gmail addresses have authoritative account status at share
time. For a consumer Google Account backed by another email provider, ownership
may have changed since account creation. Require an additional freshness
challenge such as OTP when current mailbox control matters.

Verified identity also does not prove that mail can currently reach the inbox.
Use a delivery challenge when deliverability is a product/security requirement.

## Replay and transaction safety

- Generate and store nonce state on the backend where possible.
- Bind nonce to session/user, relying party, requested operation, and expiry.
- Consume it in the same transaction that provisions or authorizes the action.
- Reject duplicate account creation or recovery completion idempotently.
- Keep raw presentations out of logs, analytics, crash reports, and URLs.
- Redact provider errors and personally identifying claims from client-visible
  diagnostics.

## Backend response

Return a small result such as:

```json
{
  "transaction_id": "<opaque-id>",
  "email": "user@example.com",
  "display_name": "User",
  "account_state": "created"
}
```

The app may display these server-verified values. It must not overwrite them
with values from its earlier client-only preview.

## Failure policy

Fail closed on malformed JSON, unknown credential IDs/formats, key-fetch or key
rotation ambiguity, signature failure, disclosure mismatch, invalid key
binding, nonce mismatch, time failure, unsupported account type, or an
unverified email. Route the user to a safe retry or fallback rather than
partially provisioning an account.
