# JSON Schema Guide

Source attribution retained from the supplied bundle: PART 5 and PART 1-3's action
dictionary. Original-source identity, version, locator, effective date, and author
authority are unverified. Vocabulary below records the supplied design contract;
it is not an Android-wide standard or a requirement to rename an adopted schema.

## Screen envelope

The envelope declares screen identity, parser `schemaVersion`, response-ordering
`version`, typed `root`, screen-level `actions`, and `cachePolicy`. Define TTL and
stale-while-revalidate from actual freshness requirements. Nodes reference actions
by id so multiple nodes can share behavior. A schema version does not replace the
ordering version used to reject late patches.

## Node shape

Nodes carry `id`, `type`, and declared modifier, visibility, event, and
accessibility fields. Containers own children; semantic components own typed
payloads. Define requiredness and defaults consistently in schema and parser.
Styling uses tokens; dimensions cannot silently accept raw numbers while the
contract claims token-only styling. Structural sizing modes, aspect ratios,
weights, and bounded opacity controls are separate declared schema values under
[ui-node-model-guide.md](ui-node-model-guide.md).

## Events and triggers

The supplied base trigger set is `onClick`, `onLongPress`, `onVisible`,
`onScrollEnd`, `onRefresh`, `onLoad`, `onDismiss`, `onTimer`, and `onScreenResult`.
Each references an action; `onVisible` additionally declares `threshold` and
`once`. A semantic component may expose extra events only through a versioned,
declared component contract understood by both schema and client capability
negotiation. An undeclared event is not implicitly supported.

## Finite action vocabulary

Use the project's finite typed action catalog and exhaustive interpreter. The
supplied vocabulary has the following field relationships; `?` means optional,
not an instruction to invent a payload or endpoint:

| Action | Contract |
|---|---|
| `NAVIGATE` | Required `uri`; optional `expectResult`, registered before navigation |
| `DISMISS_WITH_RESULT` | Required `resultKey` and typed `payload` |
| `API_CALL` | Required `method`, `endpoint`, `body`; optional `optimistic`, `onSuccess`, `onError` |
| `REFRESH_SECTIONS` | Required `sectionIds` and `context`; optional `allowInsert` |
| `UPDATE_LOCAL_STATE` | Required approved `path` and typed `value` |
| `SCROLL_TO` | Required `sectionId`; optional `highlight` |
| `OPEN_URL` | Required approved `url` and `external` behavior |
| `TRACK` | Required declared `event` and `props` |
| `TOAST` | Required display `message` |
| `SEQUENCE` | Required ordered `steps` of typed actions |
| `CONDITION` | Required `if` and `then`; optional `else` |
| `NOOP`, `UNKNOWN` | No operational fields; safe no-op behavior |

Unknown future actions safely degrade on older clients. This tolerance does not
permit a server to promise a capability the target client lacks. Preserve the
actual adopted field contracts rather than silently widening them.

Explicit business rejection may reverse an optimistic local write and surface a
message. Transport failure keeps it pending for synchronization under
[offline-ssot-data-guide.md](offline-ssot-data-guide.md).

The supplied condition operators are `EQ`, `NEQ`, `GT`, `LT`, `AND`, `OR`, `NOT`,
and `EXISTS`. Enforce the adopted complexity/depth bound; move more complex logic
to composition or native capabilities rather than introducing arbitrary evaluation.

## Binding context and authority

Allow only declared typed references within the binding context:

| Root | Meaning |
|---|---|
| `$.item` | Component data |
| `$.state` | Screen-local state |
| `$.section` | Owning section metadata |
| `$.filter` | Filter selection |
| `$.screen` | Screen parameters |
| `$.user` | Coarse allowed flags, never secrets |
| `$.result` | Typed result from another screen |
| `$.env` | Allowed app/network environment values |

Substitution is bounded reference resolution, not `eval`, scripting, reflection,
or arbitrary member access. Define unresolved-reference and type-mismatch behavior;
do not silently substitute unrelated result data or an empty string.

Derived authority rule: a finite action type alone does not authorize its target.
Validate navigation destinations, external URLs, API operations, and writable
local-state paths against client-approved scope and the existing authorization
policy. Server data cannot grant privileges beyond that policy. Discover actual
allowlists; do not invent endpoint names or permissions.

## App-shell config

Native code owns the navigation skeleton. Server configuration may select the
declared top-bar/tab composition and supported native or SDUI destinations.
Configuration versions and defaults follow the existing contract. Always retain
bundled safe navigation defaults that work without network or cache: failed,
empty, or unusable configuration cannot remove essential navigation.

## Patch response

Patches carry an ordering `version` and a finite operation list:
`UPSERT`, `REPLACE`, `REMOVE`, `MOVE`, and `PATCH_ITEM`. Define target section,
anchor position/identity, and replacement or item payload according to the
operation. `UPSERT` replaces an existing section or inserts at the anchor when
absent; the server need not know the client's current section presence.

Apply all operations and ordering metadata atomically. Reject stale versions and
use the declared missing-anchor fallback; the storage guide preserves the
supplied append fallback. Full reload and patch results must agree within the
same user/request/template scope.
