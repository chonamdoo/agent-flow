# Deepening

How to deepen a cluster of shallow modules safely, given its dependencies. Assumes the vocabulary in [SKILL.md](SKILL.md) — **module**, **interface**, **seam**, **adapter**.

## Dependency categories

When assessing a candidate for deepening, classify its dependencies. The category determines how the deepened module is tested across its seam.

### 1. In-process

Pure computation, in-memory state, no I/O. Merge when the modules share a coherent responsibility and ownership permits it, then test through the new interface directly. No transport adapter is needed merely because the modules were previously separate.

### 2. Local-substitutable

Dependencies that have local test stand-ins (PGLite for Postgres, in-memory filesystem). A stand-in can support deepening when it preserves the behavior under test and the responsibilities belong together. Test with it running in the suite. Keep a seam internal unless callers genuinely need a port at the external interface.

### 3. Remote but owned (Ports & Adapters)

Your own services across a network boundary (microservices, internal APIs). Define a **port** (interface) at the seam. The deep module owns the logic; the transport is injected as an **adapter**. Tests use an in-memory adapter. Production uses an HTTP/gRPC/queue adapter.

Recommendation shape: *"Define a port at the seam, implement an HTTP adapter for production and an in-memory adapter for testing, so the logic sits in one deep module even though it's deployed across a network."*

### 4. True external (Mock)

Third-party services (Stripe, Twilio, etc.) you don't control. The deepened module takes the external dependency as an injected port; tests provide a mock adapter.

## Seam discipline

- **Justify ports by variation, ownership, or isolation needs.** Production and test adapters often make the need concrete, but a single production adapter can still protect a real boundary. Do not add indirection solely to reach an adapter count.
- **Internal seams vs external seams.** A deep module can have internal seams (private to its implementation, used by its own tests) as well as the external seam at its interface. Don't expose internal seams through the interface just because tests use them.

## Testing strategy: replace, don't layer

- Identify the observable contracts and edge cases each old test actually defends. Retain or migrate unique behavior coverage before deleting its old test; a new interface test is not automatically equivalent coverage.
- Delete tests that assert only obsolete plumbing or internal mock interactions without requiring replacements. A test with no observable contract to protect does not earn a replacement merely because it existed.
- Put necessary replacement or new behavior tests at the deepened module's interface. The **interface is the test surface**.
- Tests assert on observable outcomes through the interface, not internal state.
- Tests should survive internal-only refactors. A deliberate change to the observable contract can require test changes; a change to internal arrangement alone should not.
