---
name: app-shell-error-contract
description: Shared semantic contract for app-wide common-error classification, queue identity, acknowledgement, retry, and metadata preservation. Use when an Android, iOS, React Web, or React Native AppShell handles SessionExpired, Maintenance, Forbidden, or server-wide errors above feature UI.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, session expired, root reset, 공통 에러, 전역 에러, 세션 만료]
requires: [clean-architecture-core]
---

# AppShell Common Error Contract

Apply this semantic contract before the matching platform AppShell skill. Platform skills own host placement and navigation APIs; this file owns classification, queue, acknowledgement, and metadata meaning.

## Classification

- `notify(error) == true` means AppShell owns the common error and the feature does not also render it locally.
- `notify(error) == false` means the feature owns the local inline error state.
- Common errors include `SessionExpired`, `Maintenance`, `Forbidden`, and product-defined server-wide codes such as `COMMON_*` unless the product flow explicitly classifies one locally.

## Queue and acknowledgement

- Give each queued error a stable `id`; deduplicate equivalent pending or handling errors by a stable semantic key.
- Expose pending errors as observable state and present one current error at a time.
- On user confirmation, move the current error from `pending` to `handling` before starting the AppShell side effect.
- Move `handling` to `consumed` only after the navigation, root-flow, or maintenance side effect succeeds.
- If the side effect fails or is cancelled, return the error to `pending` or retain an explicit retryable failure state. Never lose the recovery opportunity.
- A consumed error cannot remain in the pending queue or be shown again without a new semantic occurrence.

## Ownership and metadata

- AppShell owns global dialogs, snackbars, toasts, banners, maintenance UI, and root-flow changes.
- Features emit common-error intents and keep local errors in feature state; data/network/interceptor layers never present UI or reset navigation.
- The notifier and queue port is a Shared Presentation Contract, not a Core Domain or platform AppShell implementation type. Place the interface in a shared presentation-contract module that neither AppShell nor feature presentation owns. Features depend on that interface; AppShell wires and observes its implementation at the composition root.
- Map transport metadata into domain error fields at the data boundary. Preserve `code`, `title`, `message`, and `requestId` through domain/application mapping, queue storage, and common UI models when supplied; never pass transport DTO or exception types through those layers.

## Review and tests

Request changes when classification can render the same failure both globally and locally, deduplication can double-show it, acknowledgement can lose it before recovery succeeds, or metadata disappears.

Tests must cover common-versus-local classification, stable-key deduplication, `pending → handling → consumed`, failure/cancellation returning to a retryable state, and metadata preservation.

## Completion gate

Use only the markers supplied by the active phase.
