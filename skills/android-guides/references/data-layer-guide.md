# Android Data Layer Guide

## Responsibilities

- API/database clients know transport and persistence details.
- Data sources wrap clients and expose raw data operations.
- Repositories combine data sources, own cache/source-of-truth policy, map DTOs
  to domain models, and translate low-level failures.
- Domain and presentation should not consume DTOs directly.

## Repository Rules

- Keep repository interfaces in domain when using Clean Architecture.
- Keep repository implementations in data.
- Return domain models or domain results, not Retrofit/Room types.
- Centralize retry, cache invalidation, and error translation where possible.

## Mapping

- DTO -> data model -> domain model is acceptable when the project separates
  network and persistence.
- Do not leak nullable transport fields into domain without an explicit domain
  meaning.
- Empty responses should map to a defined empty state or domain result.

