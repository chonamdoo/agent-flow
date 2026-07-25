---
name: android-sdui-architecture
description: Hybrid Server-Driven UI architecture for Android — server layout tree vs client semantic components, design-token-only styling, Room-as-single-source-of-truth offline rendering, a finite action vocabulary, and crash-safe recursive renderers. Use when designing, implementing, or reviewing SDUI screens, UiNode trees, screen JSON schemas, action interpreters, section patch operations, or server-side screen composition on Android. Do not use for static native Compose screens, WebView-hosted dynamic content, or payload design no renderer consumes.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [sdui, server-driven ui, server driven ui, 서버 드리븐, 서버드리븐, 서버 주도 ui, 동적 화면, json 렌더링, 컴포넌트 카탈로그]
pathGlobs: ["**/sdui/**", "**/*UiNode*", "**/*ScreenComposer*", "**/*NodeRenderer*"]
dependencies: [android-clean-architecture]
---

# Android SDUI Architecture

Server-Driven UI is a contract problem before it is a rendering problem. Load
[`android-clean-architecture`](../android-clean-architecture/SKILL.md) first for
module, data, and DI boundaries; this skill adds only the SDUI-specific axes,
schema rules, and failure modes.

## Quick start

1. Draw the hybrid boundary first: per surface, decide whether the server owns a
   free layout tree or the client owns a fixed semantic component. Decide with
   the axes in `hybrid-boundary-guide.md`, never with a label.
2. Fix the contract second: node schema, token vocabulary, action vocabulary.
   The client implements a closed set; the server only recombines it.
3. Write renderer, parser, and action interpreter last, and make each fail soft:
   unknown type, depth overflow, stale patch, and offline must degrade, never
   crash.

## Do not use for

- Static native Compose screens with no server-authored layout.
- WebView or embedded-runtime dynamic content; trade-offs differ.
- Checkout, payment, and complex forms. Those stay native.
- Payload design that no renderer consumes.

## Core Axes

Three axes hold the design together (PART 3-1):

1. **Hybrid rendering** — layout tree for free composition, semantic components
   for fixed design.
2. **Offline-first** — Room is the single source of truth; UI observes Room only.
3. **UDF** — state flows storage to UI, events flow UI to the state holder.

## Module Shape

Compressed from PART 3-2:

```text
app/                # Application, root activity, root navigation
core/model/         # UiNode, NodeModifier, SduiAction, Screen, SectionOperation
core/designsystem/  # spacing/color/typography/shape/icon tokens
core/network/       # DTOs, mappers with Unknown fallback, capability header
core/database/      # Room entities, DAOs, patch transactions
core/data/          # offline-first repositories, sync worker
core/sdui/render/   # screen renderer, recursive node renderer, modifier mapper
core/sdui/action/   # action executor, expression resolver, result registry
feature/<name>/     # SDUI-hosted and native screens
```

```text
feature:* -> core:sdui -> core:data -> core:database
                                    -> core:network
every module -> core:model, core:designsystem
```

`core/model` stays pure Kotlin. `core/designsystem` owns every literal dp,
color, and text style; no other module resolves a token value — it is the line
the server cannot cross.

## Reference index

Read only the matching file.

- [hybrid-boundary-guide.md](references/hybrid-boundary-guide.md) — read when
  splitting server-owned layout from client-owned components.
- [ui-node-model-guide.md](references/ui-node-model-guide.md) — read when
  defining nodes, `NodeModifier`, or modifier order.
- [design-token-guide.md](references/design-token-guide.md) — read when JSON
  carries styling or a token table changes.
- [json-schema-guide.md](references/json-schema-guide.md) — read when writing
  screen JSON, actions, app-shell config, or patches.
- [offline-ssot-data-guide.md](references/offline-ssot-data-guide.md) — read
  when touching Room, patch transactions, repositories, or offline writes.
- [renderer-action-guide.md](references/renderer-action-guide.md) — read when
  writing renderer, parser, interpreter, results, or app-shell boundary.
- [bff-contract-guide.md](references/bff-contract-guide.md) — read when building
  the screen composer, capability negotiation, or domain mapping.
- [sdui-review-checklist.md](references/sdui-review-checklist.md) — read when
  reviewing; holds evidence and verdict rules for the markers below.

## Review Checklist

1. **Design tokens only** — styling JSON carries token names, never raw dp or hex.
2. **Room is the source of truth** — state holders observe storage, not responses.
3. **Finite action vocabulary** — every action `type` is in the sealed vocabulary.
4. **Parse depth limit** — the recursive parser caps depth and falls back past it.
5. **Unknown node fallback** — unsupported types render a fallback, never crash.
6. **List key and contentType** — lazy rendering supplies stable keys and types.
7. **Accessibility field** — interactive nodes carry label/role mapped to semantics.
8. **Semantic promotion** — a combination repeated in three or more places
   becomes a semantic component.

## Required Markers

Include these in the completion artifact or review output:

```text
sdui-architecture: applied
sdui-design-token-only: pass|fail
sdui-room-ssot: pass|fail|n/a
sdui-action-finite-vocabulary: pass|fail|n/a
sdui-parse-depth-limit: pass|fail|n/a
sdui-unknown-node-fallback: pass|fail|n/a
sdui-list-key-contenttype: pass|fail|n/a
sdui-accessibility-field: pass|fail|n/a
sdui-semantic-promotion: pass|fail|n/a
```

## Evidence Basis

- Internal SDUI design source, PART 1 through PART 9 and the PART 11
  design-decision section; each reference names its source parts.
- Android UI layer and offline-first data layer docs for single source of truth
  and unidirectional data flow.
- Compose list, stability, and semantics docs for keys and accessibility.
