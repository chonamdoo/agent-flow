# Offline SSOT Data Guide

Source attribution retained from the supplied bundle: PART 1-8 and PART 6.
Original-source identity, version, locator, effective date, and author authority
are unverified. Storage roles below replace implementation-specific schemas.

## Durability decides the source of truth

Local storage is authoritative for every rendered value that must survive a
process restart, whether one screen or many observe it. Shared observers read the
same durable value rather than independent screen caches. Choose storage by data
shape and query/transaction needs: Room supports relational structure and lists;
DataStore can support settings and small values. Neither is a mandatory engine
for every implementation.

```text
API response -> validate/parse -> storage write -> observation -> state holder -> Compose
```

This permits offline rendering from prior success, cross-screen consistency, and
stale-while-revalidate where the cache policy allows it.

## Outside the SDUI screen store

Transient refresh/progress, cold-load failure, one-shot UI effects, command
results, clock offsets/derived staleness, and current-scroll cursor pages need
not be durable screen storage. Auth credentials and media bytes belong outside
the SDUI screen store; use their appropriate secure/media stores, not a blanket
ban on persistence.

Transient data still crosses repository/domain boundaries. UI never subscribes
to a transport DTO or bypasses the repository through an API response.

## Storage responsibilities

- Preserve the screen structure and action dictionary with schema/order versions,
  cache policy, and fetch metadata. Storing validated node JSON is an option that
  avoids a table for each schema type.
- Preserve section identity, screen membership, and deterministic order where
  independently addressable patches require them.
- Normalize independently changing durable state so every affected surface
  observes the same authoritative value.
- Persist pending writes with operation identity, target/payload, synchronization
  status, and retry information required by the actual protocol. Idempotency keys
  help only when the receiving operation honors their semantics.
- Combine structure and durable state at the repository observation boundary;
  no fixed table names, business fields, or three-flow recipe is required.

## Atomic patch contract

Handle `UPSERT`, `REPLACE`, `REMOVE`, `MOVE`, and `PATCH_ITEM` exhaustively in one
storage transaction, including ordering metadata. Reject a patch older than the
stored `version` before applying it. A partial failure must not expose partially
updated sections, duplicate positions, or inconsistent order.

`UPSERT` replaces when present and inserts when absent. Under the supplied
contract, a missing insertion anchor appends rather than failing. Preserve that
fallback consistently with the server contract. Room `@Transaction` is one
implementation, not the only atomic storage mechanism.

## Observation and refresh

Refresh is a command, not a second source of rendered screen data. It may return
command success/failure using the established result type; authoritative durable
content reaches the UI only through storage observation.

Distinguish no cached content, initial loading, cold-load failure, usable cached
content, and per-emission parse/mapping failure. Do not filter out missing-cache
state and leave cold start loading forever. Recoverable parse failures may emit
a domain failure while observation remains available for later valid data.
Preserve coroutine cancellation and do not recast fatal failures as routine
business errors.

Repository mapping applies shared state consistently to all affected subtrees,
including every supported container variant. A new container must not silently
exclude its descendants from state updates. Whole-tree rebuilding is not required:
choose indexing or incremental projection when appropriate to the workload, avoid
expensive work on the main thread, and suppress only genuinely equivalent results.

## Offline writes

- Write locally first so visible state updates immediately.
- On transport failure, keep the local write pending rather than rolling it back.
  Queue it under the existing idempotency/retry contract and schedule synchronization
  when network constraints permit.
- Roll back only on explicit server/domain rejection under the business contract,
  then surface the appropriate message or common-error recovery.
- Local selection changes may update storage before requesting a section patch;
  later responses still obey version ordering.

## Unidirectional state holder

The state holder consumes repository contracts and an action interpreter, never
raw network sources. It projects observable content/failures to immutable UI
state and shares the stream with a lifecycle/freshness policy chosen for the
actual route, not a copied `stateIn` timeout.

Transient navigation, toast, and scrolling effects leave through a deliberate
single-consumer mechanism with explicit lifetime and cancellation behavior. A
buffered channel is not proof of no loss; critical results belong in durable state
under `android-clean-presentation-architecture`. Route/AppShell wiring performs
platform UI. Refresh commands change storage; storage observation changes rendering.
