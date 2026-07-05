---
name: python-api-clean-architecture
description: Python API-service platform adapter for `clean-architecture-core` that maps the core contract to FastAPI/Django/Flask handlers, app containers/factories, framework DI at the API edge, and repository/source/mapper boundaries. Use when applying Clean Architecture to Python API services or reviewing API/data/DI boundaries without a UI presentation layer.
---

# Python API Clean Architecture

This is not a standalone Clean Architecture guide. Load [`clean-architecture-core`](../clean-architecture-core/SKILL.md) first; this skill adds Python API-service layout, framework-edge DI, and adapter-boundary details only.

## Quick start

1. Apply the layer, dependency-direction, and review rules from `clean-architecture-core`.
2. Use this adapter only to translate those rules into Python API packages, app container/factory wiring, framework-edge dependencies, and response/data-source boundaries.
3. If the task is UI presentation architecture, use the relevant platform presentation skill instead; this adapter is for API-service boundaries without a UI layer.

## Package Shape

```text
apps/api/
src/example_app/
  app/
    api/
    container.py
  core/
    domain/home/
    data/home/
    network/
    platform/
    routing/
tests/{unit,integration,architecture}/
```

Small services may collapse folders, but the import boundary must remain
explicit.

## DI Shape

- FastAPI/Django/Flask dependency mechanisms belong at app/API adapter edge.
- Domain, use case, and repository contracts must not import framework DI.
- App shell owns factory/provider construction in `app/container.py`,
  `app/di.py`, or equivalent.
- API handlers call use cases and map domain/application results to response
  models at the adapter boundary.

## Generic Sample

```python
class HomeRepository(Protocol):
    async def get_home_feed(self) -> HomeFeed: ...


class HomeRepositoryImpl:
    def __init__(self, remote_data_source: HomeRemoteDataSource) -> None:
        self._remote_data_source = remote_data_source

    async def get_home_feed(self) -> HomeFeed:
        dto = await self._remote_data_source.get_home_feed()
        return to_home_feed(dto)
```

```python
def create_container() -> Dependencies:
    api_client = HomeApiClient(http_client=create_http_client())
    remote_data_source = HomeRemoteDataSourceImpl(api_client)
    repository = HomeRepositoryImpl(remote_data_source)
    return Dependencies(get_home_feed=GetHomeFeedUseCase(repository))
```

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
