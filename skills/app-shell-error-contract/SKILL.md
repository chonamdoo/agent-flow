---
name: app-shell-error-contract
description: Shared semantic contract for app-wide error classification, queue identity, acknowledgement, retry, and metadata preservation. Use when Android, iOS, Flutter, React Web, or React Native AppShell handles session, maintenance, or product-defined common errors above feature UI.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [app shell, appshell, global error, common error, session expired, root reset]
requires: [clean-architecture-core]
---

# AppShell Common Error Contract

Apply this semantic contract before the matching platform AppShell skill. Platform skills own host placement and navigation APIs; this file owns classification, queue, acknowledgement, and metadata meaning.

## Classification

- `notify(error) == true` means AppShell owns the common error and the feature does not also render it locally.
- `notify(error) == false` means the feature owns the local inline error state.
- Common errors include `SessionExpired`, `Maintenance`, `Forbidden`, and product-defined server-wide codes such as `COMMON_*` unless the product flow explicitly classifies one locally.
- Classify the product's error meaning; HTTP 403 alone does not imply a common
  error, expired session, or permission to reset navigation.

## Queue and acknowledgement

- Give each queued error a stable `id`; deduplicate equivalent pending or handling errors by a stable semantic key.
- Expose pending errors as observable state and present one current error at a time.
- On user confirmation, move the current error from `pending` to `handling` before starting the AppShell side effect.
- Move `handling` to `consumed` only after the navigation, root-flow, or maintenance side effect succeeds.
- If the side effect fails or is cancelled, return the error to `pending` or retain an explicit retryable failure state. Never lose the recovery opportunity.
- A consumed error cannot remain in the pending queue or be shown again without a new semantic occurrence.
- State survives a collector pause only within its declared owner lifetime;
  an in-memory queue does not promise recovery after process loss. When the
  product requires restart recovery, define persistence, replay, and idempotent
  recovery separately. UI acknowledgement is not a transactional outbox or an
  external-side-effect delivery guarantee; UI-free servers do not need this queue.

## Ownership and metadata

- AppShell owns global dialogs, snackbars, toasts, banners, maintenance UI, and root-flow changes.
- Features emit common-error intents and keep local errors in feature state; data/network/interceptor layers never present UI or reset navigation.
- The notifier/queue port belongs to `shared-presentation-contract`, not Core
  Domain or an AppShell implementation. Use the project's neutral contract
  boundary and map its actual location in the architecture profile; no prescribed
  folder or extra module is required. Features consume the port; AppShell wires
  and observes its implementation at composition.
- Map transport metadata into domain error fields at the data boundary. Preserve `code`, `title`, `message`, and `requestId` through domain/application mapping, queue storage, and common UI models when supplied; never pass transport DTO or exception types through those layers.

## Review and tests

Request changes when classification can render the same failure both globally and locally, deduplication can double-show it, acknowledgement can lose it before recovery succeeds, or metadata disappears.

Tests must cover common-versus-local classification, stable-key deduplication, `pending → handling → consumed`, failure/cancellation returning to a retryable state, and metadata preservation.

## Completion gate

Use only the markers supplied by the active phase.
