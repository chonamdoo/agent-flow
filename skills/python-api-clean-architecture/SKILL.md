---
name: python-api-clean-architecture
description: Python API-service Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for FastAPI/Django/Flask API handlers, app container/factory DI, usecase/repository/source/mapper boundaries, and Python API architecture review without a UI presentation layer.
---

# Python API Clean Architecture

Load `clean-architecture-core` first. This skill adds Python API-service layout
and DI details only.

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
