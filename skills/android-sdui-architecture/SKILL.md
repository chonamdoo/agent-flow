---
name: android-sdui-architecture
description: Hybrid Server-Driven UI architecture for Android — server layout tree vs client semantic components, design-token-only styling, storage-backed rendering for restart-durable state even with one observer, a finite action vocabulary, and crash-safe recursive renderers. Use when designing, implementing, or reviewing SDUI screens, UiNode trees, screen JSON schemas, action interpreters, section patch operations, or server-side screen composition on Android. Do not use for static native Compose screens, WebView-hosted dynamic content, or payload design no renderer consumes.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [sdui, server-driven ui, server driven ui, dynamic screen, json rendering, component catalog]
pathGlobs: ["**/sdui/**", "**/*UiNode*", "**/*ScreenComposer*", "**/*NodeRenderer*"]
requires: [android-clean-architecture, android-clean-presentation-architecture]
---

# Android SDUI Architecture

Server-Driven UI is a contract problem before it is a rendering problem. Load
[`android-clean-architecture`](../android-clean-architecture/SKILL.md) for module,
data, and DI boundaries and
[`android-clean-presentation-architecture`](../android-clean-presentation-architecture/SKILL.md)
for the UDF contract its `## Server-Driven Screen Exception` section scopes for
server-driven screens; this skill adds only the SDUI-specific axes, schema rules,
and failure modes.

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
2. **Offline-first** — local storage is the single source of truth for every
   state the screen must survive a process restart with, whether one screen or
   many observe it. `offline-ssot-data-guide.md` names what stays outside it.
3. **UDF** — state flows storage to UI, events flow UI to the state holder.

## Responsibility Boundaries

The supplied PART 3-2 architecture is expressed as roles, not required modules:

- Pure node/domain contracts declare typed screen, action, and patch meanings
  without Compose, storage, or network implementations.
- Client design-system code owns token values and their theme/window resolution.
- Transport/parsing adapters validate payloads and construct safe client values.
- Storage/repository adapters own persistence, observation, atomic patches, and
  offline synchronization.
- Presentation renderers and interpreters consume contracts, never concrete data
  implementations. Route/AppShell wiring executes navigation and platform effects.
- The composition root constructs adapters and injects their contracts.

Map these responsibilities to existing modules/source sets. Dependencies point
toward contracts; pure domain does not depend on the design system, database, or
renderer. Module count and paths are not acceptance criteria.

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

Read [`sdui-review-checklist.md`](references/sdui-review-checklist.md) and apply its nine marker rules to the actual change/review scope. That file owns semantics and evidence criteria. Report missing applicable evidence without mislabeling it as a pass, a confirmed failure, or an inapplicable check; preserve the marker values declared below.

## Required Markers

Include these in the completion artifact or review output:

```text
sdui-architecture: applied|n/a
sdui-design-token-only: pass|fail|n/a|unverified
sdui-room-ssot-scope: pass|fail|n/a
sdui-action-finite-vocabulary: pass|fail|n/a
sdui-parse-depth-limit: pass|fail|n/a
sdui-unknown-node-fallback: pass|fail|n/a
sdui-list-key-contenttype: pass|fail|n/a
sdui-accessibility-field: pass|fail|n/a
sdui-semantic-promotion: pass|fail|n/a
sdui-udf-contract: pass|fail|n/a
```

## Evidence Basis

- The supplied bundle attributes its adopted design policies to an internal SDUI
  source, PART 1 through PART 9 and PART 11. Original title, version, retrievable
  locator, effective date, and author authority are unverified; PART labels are
  retained for provenance, not proof that the original source was inspected.
- Android UI layer and offline-first guidance inform UDF and source-of-truth
  design; the specific durability policy here remains an adopted contract.
- Public Compose API facts are linked in the node, renderer, and review guides:
  strong skipping, bounded nested lists, and accessibility semantics. They do not
  certify this architecture's runtime behavior or every source-derived policy.
