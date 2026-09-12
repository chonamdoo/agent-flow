# Good and Bad Tests

## Good Tests

**Integration-style:** test through real interfaces, not mocks of internal parts.

A caller performs a supported action under a stated condition, then checks its observable result through the public interface. This proves the capability rather than the collaboration used to implement it.

Characteristics:

- Tests behavior users or callers care about
- Uses the public API
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

## Bad Tests

### Internal collaboration instead of outcome

**Bad case:** a test substitutes an internal collaborator and checks only that it was called once. The public result could be wrong while the test passes. Changing the internal collaboration without changing behavior could also break it.

**Valid exception:** the count or order of an externally observable side effect is itself a requirement, such as preventing duplicate submissions or ensuring authorization precedes an external write. Observe that effect at the external seam and check the required outcome; this does not justify asserting unrelated internal call counts.

Red flags:

- Mocking internal collaborators
- Testing private methods
- Asserting internal call counts or ordering that callers cannot observe
- Breaking on refactoring without behavior change
- Naming HOW instead of WHAT
- Verifying through a side channel instead of the agreed interface

### Storage bypass instead of public retrieval

**Bad case:** after creating a record through a public operation, a test queries private database storage to prove success. This couples the test to schema details and misses failures in the public retrieval path.

**Good case:** create through the agreed interface, retrieve through its public query, and compare the observable data with independently supplied input. Test storage directly only when storage itself is the agreed public contract, not as a shortcut around another interface.

### Expected value copied from implementation

**Bad case:** the expected result is recomputed with the same algorithm as the implementation. The same defect can occur in both computations, so agreement provides no independent evidence.

**Good case:** obtain the expected result from a worked example, known-good literal, or specification independent of the implementation. Choose inputs that would expose a plausible incorrect result rather than asserting a constant equals itself.
