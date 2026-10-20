# Backend QA

- Exercise the endpoint the way a client does: a real request through the project's integration or
  contract tests, or against a locally started server — not by calling the handler function.
- From the Spec Items, cover: the success response shape and status, validation failures (status and body),
  not found, unauthorized or forbidden where the endpoint is protected, and idempotency where it is promised.
- Compare field names, types and number formats to the contract, not to what the implementation returns.
- Start what you need, stop it afterwards, and use test data only. Never point QA at production services.
