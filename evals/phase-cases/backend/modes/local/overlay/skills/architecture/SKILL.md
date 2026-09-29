---
name: architecture
description: Approved project architecture for the orders service. Flat feature packages, no Clean layers.
---

# Flat feature packages

This service does not use Clean Architecture layers. Apply these rules instead of
any domain/data/presentation or core/domain split.

## Topology

- Each feature is exactly one flat package directly under `src/`, e.g. `src/orders/`.
  A feature package has no subpackages.
- Do not create layer packages anywhere under `src/`: `domain`, `data`, `app`,
  `api`, `core`, `features`, `application`, `infrastructure`, `adapters`,
  `ports`, `repositories`, `presentation`, `usecases`.
- Do not introduce repository ports, `Protocol`/`ABC` interfaces, or classes named
  `*Repository`/`*Port`, and do not add domain model classes for stored rows.
- The service module receives its row source as a plain zero-argument callable
  (for example `load_rows: Callable[[], Iterable[Mapping[str, Union[str, int]]]]`),
  filters and orders the raw rows, and maps them to response-ready dicts itself.
- The HTTP module only validates the query and wraps the service result in the
  response; it does not read or map rows.
- The package `__init__.py` is the composition point: it binds the row source to
  the handler and re-exports the feature's public entry functions.

## Module naming

- Every module inside a feature package other than `__init__.py` is named with the
  `ord_` prefix followed by its role, e.g. `ord_service.py`, `ord_http.py`.
  Modules without the prefix are rejected in review.
