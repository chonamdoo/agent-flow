# Hilt Guide

Use the target project's DI style first. For Hilt projects:

- Constructor inject use cases, repositories, mappers, and data sources.
- Bind repository interfaces to implementations in data modules.
- Keep `@Singleton` for truly app-wide objects only.
- Scope ViewModels with `@HiltViewModel`.
- Avoid injecting Android `Context` unless the dependency explicitly needs it;
  prefer `@ApplicationContext` over activity context for long-lived objects.

## Review Checks

- Does each binding live in the module that owns the implementation?
- Are scopes consistent with object lifetime?
- Are test replacements possible?
- Are feature modules avoiding app-level DI leaks?

