# Spring Web and Security

Read for MVC/WebFlux binding, Security configuration, validation, or response/error mapping. Select the actual Spring Framework/Security version and servlet/reactive stack before applying APIs; this reference does not require moving between stacks.

- Trace the matching SecurityFilterChain or SecurityWebFilterChain, matcher ordering, fallback coverage, and the trusted principal. Security annotations only work with the corresponding enabled runtime configuration. Authentication does not prove object/tenant/field authorization; include list/count, bulk, and worker paths in application policy.
- Preserve the credential model. Cookie/session authentication and bearer-only APIs have different CSRF needs. CORS is browser cross-origin policy, not authentication. Avoid copying a sample that disables protections globally; scope any exception to the actual threat/credential model.
- Bind inbound DTOs instead of ORM entities. Check Bean Validation and nested validation targets, missing/null/default handling, and PATCH presence semantics. Kotlin use-site targets and serializer modules must match the installed compiler/framework; constructor syntax alone does not prove validation ran. Java work does not need Kotlin dependencies.
- Map expected application failures at the web adapter. In MVC this may be ControllerAdvice/ExceptionHandler; in WebFlux use its supported error boundary. Keep validation, authentication, forbidden/missing-resource, conflict, and unexpected failures distinct under the existing public contract. Avoid raw exception serialization and double mapping.
- Respect response commit/streaming behavior: once headers/body are committed, a later error cannot be represented as an ordinary fresh status/envelope. Specify cancellation and partial-response semantics for actual streaming endpoints.
- Scope projections/serialization and pagination to authorized fields and objects. An authorized endpoint returning a lazily serialized entity can still trigger queries or expose fields beyond policy.

Evidence should reach the actual security and binding chain, not invoke a controller as an ordinary object and claim end-to-end authorization. Use existing authorized gates; no new command is prescribed.

## Official sources

- [Spring Security servlet architecture](https://docs.spring.io/spring-security/reference/servlet/architecture.html) and [reactive configuration](https://docs.spring.io/spring-security/reference/reactive/configuration/webflux.html).
- [Method security](https://docs.spring.io/spring-security/reference/servlet/authorization/method-security.html), [CSRF](https://docs.spring.io/spring-security/reference/servlet/exploits/csrf.html).
- [MVC validation](https://docs.spring.io/spring-framework/reference/web/webmvc/mvc-controller/ann-validation.html) and [MVC exceptions](https://docs.spring.io/spring-framework/reference/web/webmvc/mvc-controller/ann-exceptionhandler.html).
