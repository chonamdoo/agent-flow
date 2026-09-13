---
name: python-api-clean-architecture
description: Python API-service Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for FastAPI/Django/Flask API handlers, app container/factory DI, usecase/repository/source/mapper boundaries, and Python API architecture review without a UI presentation layer.
requires:
  - clean-architecture-core
---

# Python API Clean Architecture

Load `clean-architecture-core` first. This skill adds Python API-service layout
and DI details only.

## Source Boundaries

Discover existing packages, app factories, entry adapters, imports, and active
architecture role mappings. Keep the adopted layout; semantic roles do not
prescribe a folder tree or require separate files for every responsibility.

Apply the required core's **Semantic Layers** to server boundaries. A server
without UI needs no presentation state holder or shared UI error queue.

## DI Shape

- FastAPI/Django/Flask dependency mechanisms belong at app/API adapter edge.
- Domain, use case, and repository contracts must not import framework DI.
- Use the project's protocol convention for repository/application ports;
  `typing.Protocol` supports structural conformance without inheritance.
- App shell owns factory/provider construction. Framework dependency resolution
  belongs to the API/composition edge, not pure domain policy.
- API handlers map inbound schemas to application commands and map results/errors
  to responses. Outbound provider DTOs and ORM entities stay in driven adapters.

## Review Additions

```text
python-api-clean-architecture: applied
api-handler-usecase-boundary: pass|fail
framework-di-edge-only: pass|fail
app-container-explicit: pass|fail
repository-impl-direct-api-client: pass|fail
remote-data-source-boundary: pass|fail
domain-framework-imports: pass|fail
response-model-boundary: pass|fail
```

## Evidence Basis

FastAPI dependency mechanism, Django view layer, Flask application factory,
Python Protocol typing.
