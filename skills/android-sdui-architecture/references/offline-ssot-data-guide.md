# Offline SSOT Data Guide

Source: PART 1-8, plus PART 6 (whole).

## Rule

Local storage is the single source of truth for the state a screen observes when
that state must survive a process restart. Durability is the whole test: a
durable value observed by one screen belongs in storage exactly as much as one
observed by five. Sharing changes only the consequence — when two or more screens
observe the same value they observe the same table, which is what makes storage a
single source rather than a per-screen cache. Structure, queries, and lists live
in Room; settings, toggles, and small scalars live in DataStore. The storage
engine follows the shape of the data, not a rule about Room.

```text
API response -> validate/parse -> storage write -> Flow emit -> state holder -> Compose
```

Three reasons: offline render from the last successful response, cross-screen
state consistency because every screen observes the same table, and instant
render through stale-while-revalidate.

## Outside storage

These are observed state and still do not belong in storage:

- refresh/progress flags and cold-load failure state
- one-shot effects (navigate, toast, scroll) and command responses
- server clock offset and staleness computed from it
- cursor pages held for the current scroll
- auth credentials and media bytes

Excluding them from storage does not excuse a shortcut: data that bypasses
storage still crosses a repository and arrives as a domain model. The UI never
subscribes to a DTO or an API response, in any case.

## Storage strategy

- Screen structure: store the node JSON whole, so schema changes stay cheap.
- Volatile state: normalized tables (like, cart, filter selection), so one write
  updates every screen showing that item.
- Render time: `combine` the two.

## Room schema

- `screens(screenId PK, schemaVersion, version, rootJson, actionsJson,
  cachePolicyJson, fetchedAt)`.
- `sections(screenId + sectionId PK, position, nodeJson, updatedAt)` — one row
  per section so a patch touches one section, not the screen.
- `product_states(token PK, liked, inCart, syncedAt)` and
  `filter_states(filterId PK, selectedId, updatedAt)` — normalized volatile state.
- `pending_operations(id PK, idempotencyKey, method, endpoint, bodyJson,
  createdAt, retryCount)` — the offline write queue. `idempotencyKey` is what
  makes a retry safe.

## Patch operations are one transaction

The following is a partial transaction skeleton for `UPSERT` and `REMOVE`. Production code must handle `REPLACE`, `MOVE`, and `PATCH_ITEM` in the same exhaustive transaction.

```kotlin
@Transaction
suspend fun applyOperations(screenId: String, ops: List<SectionOperationEntity>) {
    ops.forEach { op ->
        when (op) {
            is Upsert -> {
                val pos = op.anchorId?.let { anchorId ->
                    val anchorPos = positionOf(screenId, anchorId) ?: return@let null
                    val target = if (op.after) anchorPos + 1 else anchorPos
                    shiftPositions(screenId, target)
                    target
                } ?: ((maxPosition(screenId) ?: -1) + 1)
                upsertSections(listOf(op.toEntity(screenId, pos)))
            }
            is Remove -> deleteSection(screenId, op.sectionId)
        }
    }
}
```

Without `@Transaction` a partially applied patch leaves duplicate or gapped
positions and list order stops being deterministic. An `UPSERT` with a missing
anchor appends rather than failing.

## Repository: three-way merge

```kotlin
override fun observeScreen(screenId: String): Flow<ObserveScreenResult> =
    combine(
        dao.observeScreenWithSections(screenId),  // structure
        dao.observeProductStates(),               // like / cart
        dao.observeFilterStates(),                // filter selections
    ) { screenData, productStates, filterStates ->
        runCatching<ObserveScreenResult?> {
            screenData?.toModel(
                json = json,
                productStates = productStates,
                filterStates = filterStates,
            )?.let(ObserveScreenResult::Content)
        }.getOrElse { ObserveScreenResult.Failure(it.toDomainError()) }
    }
        .filterNotNull()
        .flowOn(dispatcher)
        .distinctUntilChanged()
```

- `refresh` and `refreshSections` return `Result<Unit>`, never a screen. Callers
  read the flow.
- The repository converts transport and storage failures before returning refresh
  results. `runCatching` maps per-emission parsing or mapping failures to
  `ObserveScreenResult.Failure` without terminating observation.
- A filter selection is written locally first so the chip reacts immediately,
  then the patch request goes out.
- Discard a patch whose `version` is older than the stored version before
  applying it.
- The merge walks the whole tree on every state emission, so keep it on a
  background dispatcher via `flowOn` and terminate with `distinctUntilChanged`.

## Merging state into the tree

A single recursive `UiNode.transform { }` rebuilds containers by mapping their
children and returns leaves unchanged. State merging is then one `transform`
that rewrites the matching semantic nodes:

```kotlin
private fun Screen.mergeProductStates(states: List<ProductStateEntity>): Screen {
    val map = states.associateBy { it.token }
    return copy(root = root.transform { node ->
        when (node) {
            is UiNode.ProductCard -> map[node.data.token]
                ?.let { node.copy(data = node.data.copy(liked = it.liked, inCart = it.inCart)) }
                ?: node
            else -> node
        }
    })
}
```

Every new container node type must be added to `transform`, otherwise its
subtree silently stops receiving merged state.

## Offline writes

- Write locally first so the UI updates immediately.
- On transport failure do **not** roll back. Queue the operation with an
  idempotency key and schedule a network-constrained worker. "Send later" is the
  offline-first stance.
- Roll back only when the server explicitly rejects the write (out of stock,
  permission denied), and then surface a message.

## State holder is unidirectional

```kotlin
val uiState: StateFlow<ScreenUiState> =
    screenRepository.observeScreen(SCREEN_ID)
        .map<ObserveScreenResult, ScreenUiState> { result ->
            when (result) {
                is ObserveScreenResult.Content -> ScreenUiState.Success(
                    screenUiMapper.toUiModel(result.screen)
                )
                is ObserveScreenResult.Failure -> ScreenUiState.Error(
                    screenErrorMapper.toUiModel(result.error)
                )
            }
        }
        .onStart { emit(ScreenUiState.Loading) }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), ScreenUiState.Loading)
```

```text
        +------------------ network ------------------+
        v                                             |
     [Room] --Flow--> [Repository] --Flow--> [state holder] --StateFlow--> [Compose]
        ^                                             ^                        |
        +--------- [ActionExecutor] <-- onEvent(...) <-------------------------+
```

- The state holder injects repositories and the action executor, never the
  network data source.
- One-shot UI behavior (navigate, toast, scroll) leaves through a buffered
  `Channel`, not `StateFlow`.
- Refresh is a command into the repository; the resulting UI change always
  arrives back through Room.
