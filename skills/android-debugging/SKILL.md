---
name: android-debugging
description: |
  Use when diagnosing Android crashes, ANRs, incorrect UI state, coroutine or
  Flow bugs, data loading failures, and Compose rendering issues. Enforces a
  reproduce -> trace -> hypothesis -> minimal fix -> verification loop.
---

# Android Debugging

Do not patch symptoms first. Establish the failing path, locate the cause, then
make the smallest fix that proves the hypothesis.

## Process

1. Symptom: define the exact expected and actual behavior.
2. Reproduction: record device/build variant/account/data conditions.
3. Evidence: collect stack trace, logcat, failing test, network response, or
   UI state trace.
4. Call chain: trace from symptom to first wrong state or thrown exception.
5. Compare: find a similar working implementation in the same repository.
6. Hypothesis: write "Changing X to Y should fix Z because ...".
7. Fix: apply one hypothesis at a time.
8. Verify: rerun the reproduction path plus the narrowest build/test gate.

If three hypotheses fail, stop and return to evidence collection.

## Android Evidence Sources

- Crash: stack trace, Crashlytics, logcat, exception cause chain.
- ANR: main-thread blocking, traces, synchronous IO, long composition work.
- Compose: state source, recomposition triggers, unstable models, lazy keys.
- Network/data: request, response, mapper, cache, repository source selection.
- Coroutine/Flow: cancellation handling, collection lifecycle, dispatcher use.

## Verification Report

```markdown
### Root Cause
- Symptom:
- Cause:
- Call chain:

### Fix
- Change:
- Why this fixes it:

### Verification
- Build:
- Test:
- Reproduction:
```

## References

- `../android-guides/references/error-handling-guide.md`
- `../android-guides/references/kotlin-concurrency-guide.md`
- `../android-guides/references/compose-performance-guide.md`
- `../android-guides/references/testing-guide.md`

