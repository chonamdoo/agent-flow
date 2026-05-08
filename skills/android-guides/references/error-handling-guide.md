# Android Error Handling Guide

## Principles

- Do not swallow exceptions silently.
- Preserve coroutine cancellation.
- Translate transport/storage failures into domain or UI errors at one boundary.
- UI should render a recoverable error state when recovery is possible.

## Coroutine Rule

Always rethrow `CancellationException` when using broad catches:

```kotlin
catch (error: Throwable) {
    if (error is CancellationException) throw error
    // translate non-cancellation failure
}
```

## UI Error States

- Network unavailable
- Server or unknown failure
- Empty data
- Permission/auth failure
- Validation failure

Use the categories that exist in the target project.

