# Android Data Layer Notes

Canonical layer, repository, cache, mapper, DTO/entity/domain/UI separation, and
SOLID boundary rules live in `skills/clean-architecture/SKILL.md`. Do not repeat
or override them here.

Use this file only for Android-specific adapter details after
`clean-architecture` has defined the boundary.

## Android-specific checks

- Retrofit, Room, DataStore, file storage, SDK clients, and platform APIs stay in
  data/infrastructure adapters.
- `Flow`, `suspend`, paging, and dispatcher choices must match existing project
  conventions at the repository/data-source boundary.
- Room entities and Retrofit DTOs do not cross into domain/application or UI
  state without a mapper.
- Hilt modules or manual DI belong at the composition root; domain/application
  code does not import Hilt annotations.
- Network, database, and cache error types are translated before crossing into
  domain/application contracts.
- Offline, paging, and cache invalidation policy must be explicit when local
  storage or disk cache is involved.
