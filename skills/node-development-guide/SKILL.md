---
name: node-development-guide
description: Reviews and guides Node.js server and tooling changes. Use when modifying JavaScript or TypeScript Node.js services, CLIs, package scripts, async I/O, or process lifecycle code.
---

# Node.js Development Guide

Use this only for Node.js runtime code in the changed scope.

## Quick start

1. Confirm the file runs under Node.js rather than a browser or React Native runtime.
2. Preserve the repository's module format, supported Node version, package manager, and test framework.
3. Trace async I/O, shutdown, and error propagation before changing behavior.
4. Run the narrow test, typecheck, or CLI invocation that exercises the changed path.

## Write

- Keep ESM/CommonJS boundaries explicit; do not mix `require` and `import` without an existing interoperability boundary.
- Await promises or deliberately own fire-and-forget failures.
- Close files, streams, servers, workers, and child processes on success, failure, and termination.
- Validate untrusted file paths, command arguments, environment values, and parsed payloads at their boundary.
- Prefer argument arrays over shell interpolation for child processes.
- Preserve exit codes and actionable error context in CLIs.

## Review

- Block lost promise rejections, hanging handles, command injection, path traversal, double responses, and incompatible module exports.
- Check cancellation and partial-failure behavior for parallel work.
- Treat formatting preferences as non-blocking unless configured tooling requires them.
