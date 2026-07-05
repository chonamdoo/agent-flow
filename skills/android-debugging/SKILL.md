---
name: android-debugging
description: Android diagnosis workflow for crashes, ANRs, incorrect UI state, coroutine or Flow bugs, data loading failures, and Compose rendering issues. Use when an Android app is broken, throwing, hanging, rendering incorrectly, or needs root-cause analysis; do not use as a general code-review checklist.
---

# Android Debugging

Do not patch symptoms first. Establish the failing path, locate the cause, then
make the smallest fix that proves the hypothesis.

## Quick start

1. Reproduce or precisely describe the failing Android path before editing.
2. Capture the narrowest useful evidence: stack trace, logcat, UI state, network response, or trace.
3. Trace from symptom to the first wrong state or exception, then form one falsifiable hypothesis.
4. Apply one minimal fix and rerun the reproduction path plus the narrowest relevant gate.

## Non-goals

- Do not use this for routine Android review after the cause is already known; use `android-code-review`.
- Do not use it for React Web, React Native JavaScript/TypeScript, Python, or generic TypeScript bugs unless the failing path crosses Android native code.
- Do not patch symptoms without proving the call chain.

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

- [error-handling-guide.md](../android-guides/references/error-handling-guide.md) when the failure involves user-visible errors, retries, app-wide errors, or failure states.
- [kotlin-concurrency-guide.md](../android-guides/references/kotlin-concurrency-guide.md) when coroutines, Flow, dispatchers, cancellation, or lifecycle collection may be causal.
- [compose-performance-guide.md](../android-guides/references/compose-performance-guide.md) when recomposition, stability, lazy lists, or frame-time work may be causal.
- [testing-guide.md](../android-guides/references/testing-guide.md) when choosing a narrow regression test or verification gate.

