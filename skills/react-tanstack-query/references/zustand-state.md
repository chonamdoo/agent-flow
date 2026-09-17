# Zustand Lifetime and Persistence

Read for actual React Web Zustand store lifetime, SSR initialization, persistence/restoration, or persisted draft checkpoints. This is a standalone reference: Zustand-only work does not require TanStack Query, its skill entrypoint, or a new form library. It does not prescribe a general state architecture or authorize dependency installation.

## Identify the Real Lifetime

Read the installed Zustand, React, middleware/storage-adapter, and framework versions from manifests and lockfiles; check the installed types and source for version-sensitive behavior. Locate the store factory, every instance owner/provider, consumers, asynchronous actions, and any storage namespace. Determine where code actually executes, not just whether its file says `'use client'`.

| Actual lifetime | Decision |
| --- | --- |
| Browser application | An app-wide store is valid for shared client state such as theme or panels. Preserve it when route changes should retain that state. |
| Browser route, record, or session | Place or replace the instance at the intended boundary, or use a deliberate reset/rebind policy. A provider in a retained layout does not necessarily remount on navigation. |
| Server request rendering user-specific state | Create a request-isolated instance with request-appropriate initial data. A module-scoped mutable store shared across server requests can leak one user's state into another's. |
| RSC data access | Keep server data in the server's established data-access boundary. RSCs should not use a client Zustand store as a mutable request/session authority. |

Context can distribute an instance; it does not itself create request isolation, reset behavior, or narrow subscriptions. Keep an instance stable through ordinary rerenders within its chosen lifetime. Do not convert a valid browser singleton into a per-component store solely because the Next.js guide warns against server global stores.

This discovery is complete when instance creation, disposal/reset, rendered consumers, and delayed work all have identified owners. A reset invoked through a store's public API by a session coordinator is valid; inspect competing state policies, not the caller's filename.

## SSR and Restoration Timing

Server output and the first client hydration render must agree. Initialize both from a compatible snapshot or use the framework's supported client-only boundary for genuinely browser-only content. A lazy initializer reading local storage is not automatically hydration-safe in an SSR Client Component.

For `persist`, inspect the actual storage contract:

- Synchronous storage can restore during store creation; asynchronous storage restores later. Both are valid, but the initial UI must distinguish a not-yet-restored value from an authoritative empty or signed-out value when that distinction affects behavior.
- Use `skipHydration` plus deliberate `rehydrate()` only when the installed API and chosen rendering strategy need manual timing. Automatic restoration is valid when its first render and race behavior satisfy the contract; asynchronous restoration is not inherently unsafe.
- `hasHydrated()` is a snapshot getter, not a React subscription. Use the existing reactive readiness state or supported hydration listeners when the rendered UI must change as restoration completes, and dispose listeners with their owner.
- Handle unavailable storage, malformed or obsolete data, and migration/restore errors according to the product's retention policy. Do not report a checkpoint restored or saved when the storage operation failed.
- Default persistence merging is shallow. For partial nested data, decide how missing fields and current values are preserved; do not blindly apply a generic deep merge or let an old snapshot replace newer edits.

Keep loading or hydration gating as narrow as the affected content. A parent mount effect does not establish that every streaming descendant has hydrated. Inspect actual server HTML, first client output, and post-restore rendering before claiming hydration safety.

## Persisted Identity and Disposal

Persist only the fields whose durable storage has a product purpose, using `partialize` or the existing equivalent. Identify namespace and record/principal/tenant dimensions where those change the state's meaning; public preferences may remain unscoped. Follow existing privacy and credential policy rather than persisting server-owned secrets or treating restored identity as authorization.

Define schema version and migration/discard behavior, expiry where retention requires it, and deletion on logout, account removal, or record completion as applicable. Zustand's version mechanism does not supply an application expiry or permission check.

For an account, tenant, or record switch, cover both memory and storage:

1. Detach, gate, or rebind consumers of the old identity; reset or replace its instance under the adopted policy.
2. Bind asynchronous actions, restore completions, and callbacks to their originating instance and generation. A late A result must not replace B's state, even if A's request could not be cancelled.
3. Stop old subscriptions and delayed checkpoint writes from repopulating a deleted namespace or saving A's values under B's name. Inspect the storage adapter's completion and ordering semantics rather than assuming an option change is atomic with pending work.
4. Remove or retain stored state according to policy. `persist.clearStorage()` clears its storage key, not the in-memory store; changing `name` with `setOptions` is not by itself a memory reset, restore cancellation, or identity transition.

Keeping a non-sensitive browser theme across logout is valid. A user-specific server mutable singleton or restoring another account's private draft is not. Use only the transition mechanisms the application needs; do not impose universal clearing of every store.

## Long-Lived Draft Checkpoints

When a form needs refresh recovery, a persisted checkpoint can be a snapshot rather than a second live draft owner. For draft/reset/submission behavior, read the actual form skill conditionally: `react-tanstack-form` or `react-hook-form-zod`, including its lifecycle reference. Preserve their ownership rules rather than implementing a second form engine in Zustand.

For the checkpoint boundary, identify:

- The draft's record, principal/tenant scope when relevant, schema version, and saved revision/time needed to detect stale restoration.
- Which fields may persist, retention/expiry and privacy constraints, and deletion after successful completion or explicit discard.
- Who accepts a restored snapshot into the live form and how it conflicts with edits or newer server data that arrived while storage was loading.
- What prevents an older queued save or restore from resurrecting a completed draft or overwriting a newer checkpoint, including another tab if shared storage can produce that race.

Automatic/debounced checkpointing and asynchronous restoration are valid with those decisions. Possible conflict policies include deferring editing until restoration, offering the user a restore choice, or reconciling against an unchanged draft generation. Choose the established product policy; do not require user-triggered saves or restoration only before initialization. Once accepted, the live form owns subsequent edits; checkpoint updates are snapshots, not a continuous two-way synchronization loop.

## Updates and Selectors: Concrete Risks Only

- Update changed objects/arrays immutably so subscribers observe the new state and prior snapshots remain intact. Zustand's `set` merges only one level; preserve untouched nested fields explicitly. An existing Immer integration can provide immutable updates without manual spreads.
- A full replacement must preserve the complete store contract, including actions where they live in state. Inspect reset/replacement code for dropped fields or methods rather than banning intentional replacement.
- Subscribe to the values a consumer needs when a broad subscription causes avoidable work on the changed path. In Zustand v5's standard React binding, selectors producing a fresh object, array, or fallback function on every read can produce unstable snapshots and update loops. Use stable selections or supported `useShallow`/equality APIs where that risk exists; v4 and `zustand/traditional` differ. Do not blanket-memoize selectors or migrate equality APIs without installed-version evidence.

A concrete missed update, unstable snapshot, lost field, or measured render cost justifies a finding. Merely using a global browser store, a broad selector in a small consumer, or an older supported API does not.

## Completion Evidence

For implementation, use the project's authorized runtime and declared checks to observe the changed lifetime. Choose applicable boundaries: route retention/reset, two request-isolated server users, initial hydration, delayed restoration after editing, account switch while a restore/action completes, schema migration, expired checkpoint disposal, or a queued write after discard. Inspect visible state and actual storage effects without recording private payloads.

For review, tie each finding to a concrete factory/provider, subscription, merge, or delayed callback and describe the wrong visible or persisted result. Completion requires an inspectable lifetime decision, a restoration/conflict policy for every changed persisted state, and preserved valid exceptions. Report source reasoning separately from executed scenarios and unverified behavior; no browser/application means no claim of runtime hydration or isolation proof.

## Primary Sources and Limits

- [Zustand Next.js guide](https://github.com/pmndrs/zustand/blob/main/docs/learn/guides/nextjs.md): request isolation, matching initialization, and route-scoped providers. Its global-store warning concerns server request sharing; the guide also carries an upstream revision notice.
- [Persistence guide](https://github.com/pmndrs/zustand/blob/main/docs/reference/integrations/persisting-store-data.md) and [persist implementation](https://github.com/pmndrs/zustand/blob/main/src/middleware/persist.ts): storage, partialization, version/migration, merge, hydration listeners, and clearing. Compare installed source/types when documentation and API shapes differ; current `main` behavior is not proof of an older release's race handling.
- [Immutable updates](https://github.com/pmndrs/zustand/blob/main/docs/learn/guides/immutable-state-and-merging.md) and [v5 migration](https://github.com/pmndrs/zustand/blob/main/docs/reference/migrations/migrating-to-v5.md): shallow merging, replacement, stable selector outputs, and version-sensitive equality/persistence changes.

These are rolling upstream sources, not a minimum-version mandate. The identity, checkpoint, expiry, and conflict criteria above are engineering applications of the library mechanisms; Zustand does not automatically enforce the application's privacy, authorization, or draft reconciliation policy.
