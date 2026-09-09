# Spring Operations

Read for Actuator/security exposure, service startup/shutdown, migration ownership, Batch, or consumers. Resolve actual Boot, Batch, messaging, driver, and deployment versions; defaults in a newer guide are not the project's runtime configuration.

- Distinguish an endpoint being enabled from being exposed and authorized. Limit Actuator access and redact health/configuration details. Public health summaries should not expose credentials, internal addresses, or unrestricted environment/configuration data.
- Match liveness to process recoverability and readiness to traffic capability. A database outage should not automatically cause restart storms. Check the actual probe path/port and whether it demonstrates the application's serving path, not just an independent management listener.
- Keep secrets in the established configuration/secret mechanism, not committed properties or unrestricted debug logs. Bound request, executor, connection pool, queue, retry, and timeout settings as one capacity budget.
- Identify migration execution ownership and locking. Validate compatibility of old/new application instances, migration ordering, and populated data. Backfills need bounded chunks, progress, restart, and safe concurrent read/write behavior; do not rely solely on empty-schema startup.
- Graceful shutdown must match the engine and deployment deadline. Stop readiness/admission and new job claims, drain accepted requests or persist recoverable work, then close clients/pools/executors. Check lifecycle phase ordering so workers do not use already-closed dependencies.
- For actual Spring Batch work, preserve job identity/parameters, durable JobRepository metadata, chunk transaction/checkpoint semantics, restart rules, and duplicate launch protection. An in-memory progress variable is not restartability. Do not add Batch for ordinary synchronous CRUD.
- For actual message consumers, document acknowledgement versus effect commit, redelivery/dedup, bounded retry and dead-letter recovery. A broker transaction cannot automatically cover an unrelated remote payment. Replay needs the same authorization/effect policy as initial execution.

Use authorized evidence for readiness behavior, interrupted accepted work, and restart/recovery at the relevant boundary. Configuration presence is not proof the deployed probe or shutdown path was exercised.

## Official sources

- [Boot production endpoints](https://docs.spring.io/spring-boot/reference/actuator/endpoints.html).
- [Boot graceful shutdown](https://docs.spring.io/spring-boot/reference/web/graceful-shutdown.html).
- [Boot database initialization](https://docs.spring.io/spring-boot/how-to/data-initialization.html).
- [Spring Batch](https://docs.spring.io/spring-batch/reference/index.html).
