# Android Data Layer Notes

Before applying this reference, read the required
[clean-architecture-core](../../clean-architecture-core/SKILL.md) and
[android-clean-architecture](../../android-clean-architecture/SKILL.md) in full.
Their repository, cache, mapping, error, and Hilt contracts govern these Android
checks; this reference does not override either contract.

## Android-specific checks

- Retrofit, Room, DataStore, file storage, SDK clients, and platform APIs stay in
  data/infrastructure adapters.
- `Flow`, `suspend`, paging, and dispatcher choices must match existing project
  conventions at the repository/data-source boundary.
- Apply the required core's **Mapping Boundary** to Room entities and Retrofit
  DTOs at their owning Android adapters.
- Offline, paging, and cache invalidation policy must be explicit when local
  storage or disk cache is involved.
