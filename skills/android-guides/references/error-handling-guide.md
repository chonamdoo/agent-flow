# Android Error Handling Guide

## Principles

- Do not swallow exceptions silently.
- Preserve coroutine cancellation.
- Translate transport/storage failures to domain errors at the data boundary;
  presentation projects those errors into UI-facing values.
- UI should render a recoverable error state when recovery is possible.

## Coroutine Rule

When a broad catch is necessary, rethrow `CancellationException` before
translating recoverable non-cancellation failures at the owning boundary.
Cancellation is control flow, not a business or UI failure.

## UI Error States

- Network unavailable
- Server or unknown failure
- Empty data only when the product classifies absence as an error
- Permission/auth failure
- Validation failure

Use the categories that exist in the target project.

For common/global errors, session expiry, or root recovery, use
`android-appshell-error-handling` and `app-shell-error-contract` for
classification, acknowledgement, retry, and AppShell ownership. Do not invent
a second global policy here.

