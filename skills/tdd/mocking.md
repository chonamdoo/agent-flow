# When to Mock

Mock at **system boundaries** only:

- External APIs
- Databases (sometimes; prefer a test database)
- Time and randomness
- File system (sometimes)
- Owned remote services when an adapter at the network seam isolates the behavior under test

Do not mock in-process classes or internal collaborators merely because they are easy to replace. Ownership alone does not determine the seam: an owned remote service can need a transport adapter, while an internal collaborator should normally run for real.

## Designing for Mockability

At system boundaries, design interfaces that are easy to substitute.

### Dependency injection

**Good decision:** accept the external collaborator at the seam so a test can supply a controlled adapter and observe the module's outcome.

**Bad decision:** create the external client inside the operation under test, forcing tests to depend on credentials, global configuration, or client-construction internals. The defect is hidden dependency creation, not the choice of provider.

### Operation-specific contracts

**Good decision:** expose each meaningful external operation with its own input, result, and error contract. A test substitute can describe that operation directly.

**Bad decision:** make behavior tests dispatch on raw endpoints and transport options through a generic fetch mock. The mock must reproduce routing logic before it can express a result. A generic transport can remain behind the adapter; it need not become the behavior-level test interface.

Operation-specific contracts keep result shapes and type expectations local, make the exercised external action visible, and avoid conditional routing logic in test setup.
