# Android client flow

## Dependencies and client

Use one project-managed version for both artifacts; resolve the actual version
from the version catalog or trusted dependency tooling rather than copying a
stale number:

```kotlin
dependencies {
    implementation("androidx.credentials:credentials:${libs.versions.credentials.get()}")
    implementation(
        "androidx.credentials:credentials-play-services-auth:${libs.versions.credentials.get()}"
    )
}
```

Create `CredentialManager` from the Activity or application context. Invoke the
request from an Activity because the system must present consent UI.

## Nonce and request

Generate at least 128 bits with `SecureRandom`, encode with base64url without
padding, and store a server-side record bound to the session, purpose, and a
short expiry. Interpolate only the encoded nonce into JSON; constructing the
request with a JSON library is safer than string concatenation.

The request shape is:

```json
{
  "requests": [
    {
      "protocol": "openid4vp-v1-unsigned",
      "data": {
        "response_type": "vp_token",
        "response_mode": "dc_api",
        "nonce": "<base64url-nonce>",
        "dcql_query": {
          "credentials": [
            {
              "id": "user_info_query",
              "format": "dc+sd-jwt",
              "meta": {
                "vct_values": ["UserInfoCredential"]
              },
              "claims": [
                {"path": ["email"]},
                {"path": ["email_verified"]},
                {"path": ["name"]},
                {"path": ["given_name"]},
                {"path": ["family_name"]},
                {"path": ["picture"]},
                {"path": ["hd"]}
              ]
            }
          ]
        }
      }
    }
  ]
}
```

Remove optional claims the product does not use. Data minimization reduces
consent friction and exposure.

```kotlin
val option = GetDigitalCredentialOption(requestJson = requestJson)
val request = GetCredentialRequest(listOf(option))
```

Digital Credentials has no equivalent to “prefer immediately available.” If no
eligible credential exists, the system may show a no-options surface; provide a
usable fallback afterward.

## Retrieve and hand off

Call from a lifecycle-owned coroutine. Catch Credential Manager's typed
exceptions, not a broad `Exception` that could swallow coroutine cancellation.

```kotlin
suspend fun requestVerifiedEmail(
    activity: Activity,
    request: GetCredentialRequest,
): String {
    return try {
        val result = credentialManager.getCredential(activity, request)
        val credential = result.credential
        require(credential is DigitalCredential) {
            "Unexpected credential type: ${credential.type}"
        }
        credential.credentialJson
    } catch (error: GetCredentialCancellationException) {
        throw VerifiedEmailCancelled(error)
    } catch (error: NoCredentialException) {
        throw VerifiedEmailUnavailable(error)
    } catch (error: GetCredentialInterruptedException) {
        throw VerifiedEmailRetryable(error)
    } catch (error: GetCredentialException) {
        throw VerifiedEmailFailed(error)
    }
}
```

Treat user cancellation, no credential, retryable interruption, provider
configuration, and unknown provider errors as distinct UI states. Preserve a
plain-email path.

Send the full, unchanged `credentialJson`, the server-issued nonce identifier,
and the flow purpose to the backend. Do not send only decoded claims.

## Optional client preview

For non-security UI only, parse the outer JSON to locate
`vp_token.user_info_query[0]` and decode with a maintained SD-JWT parser. Mark
all such values unverified. Do not create an account, authorize an action, or
persist trusted profile data until the backend returns verified claims.

Never log `credentialJson`, the raw SD-JWT, disclosures, key-binding JWT, nonce,
or returned personal data.
