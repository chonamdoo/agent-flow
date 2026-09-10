# Ktor Server Plugins

Read for routing, authentication, ContentNegotiation, RequestValidation, or StatusPages changes. Match the project's Ktor version and installed plugins; the current documentation's modules/DI facilities are not a minimum version requirement.

- Locate Application and route-scoped plugin installation and the affected route hierarchy. Nested routing or a new module must not accidentally bypass the authentication or validation scope. Resolve provider names and configuration in the actual application, not just in a test helper.
- Authentication verifies credentials; application authorization resolves allowed tenant, action, target, and fields. JWT parsing without the required signature/issuer/audience/claim validation is not trusted identity. Define challenges under the existing response contract; do not let another tenant's existence leak through inconsistent failures.
- ContentNegotiation determines supported serializers/media types. Check serializer configuration for explicit null, defaults, unknown keys and enum handling. Preserve PATCH presence separately from nullable value. The contract includes malformed content and unsupported content type, not only a happy-path DTO.
- RequestValidation applies to the registered types and receive path; it is not automatic domain or database validation. Keep constraints requiring current state in the action and enforce concurrent invariants in persistence.
- StatusPages maps exceptions/statuses at the transport edge. Keep cancellation propagating, map known failures once, and sanitize unexpected failures. Do not assume one catch-all handler can change a status after a streaming response was committed.
- Apply stable bounded pagination and field projection before serialization; returning an ORM object risks lazy I/O and field exposure. A route can remain thin without adding a separate source file for every mapping.

For authorized evidence, exercise the actual installed plugins and route with malformed input, a valid identity targeting another tenant, PATCH omission/null, and an application conflict. A plain function call around route logic does not demonstrate plugin behavior. Healthy module-function parameter injection is valid; client-only HttpClient configuration does not activate this reference.

## Official sources

- [Application modules](https://ktor.io/docs/server-modules.html), [routing](https://ktor.io/docs/server-routing.html).
- [Authentication](https://ktor.io/docs/server-auth.html) and [JWT](https://ktor.io/docs/server-jwt.html).
- [Serialization](https://ktor.io/docs/server-serialization.html), [RequestValidation](https://ktor.io/docs/server-request-validation.html), [StatusPages](https://ktor.io/docs/server-status-pages.html).
