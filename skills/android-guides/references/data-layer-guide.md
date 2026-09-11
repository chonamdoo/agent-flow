# Android Data Layer Notes

Canonical layer, repository, cache, mapper, and value-separation rules live in
[clean-architecture-core](../../clean-architecture-core/SKILL.md). Apply
[android-clean-architecture](../../android-clean-architecture/SKILL.md) for
Android adapter details; this reference does not override either contract.

## Android-specific checks

- Retrofit, Room, DataStore, file storage, SDK clients, and platform APIs stay in
  data/infrastructure adapters.
- `Flow`, `suspend`, paging, and dispatcher choices must match existing project
  conventions at the repository/data-source boundary.
- Convert Room entities and Retrofit DTOs to domain/application values at the
  owning boundary. No separate mapper class or identity copy is required when
  safe representations already coincide.
- Hilt modules or manual DI belong at composition. Pure domain policy imports
  no Hilt types; adopted application `@Inject constructor` wiring metadata is
  allowed under the Android adapter's explicit exception.
- Network, database, and cache error types are translated before crossing into
  domain/application contracts.
- Offline, paging, and cache invalidation policy must be explicit when local
  storage or disk cache is involved.
