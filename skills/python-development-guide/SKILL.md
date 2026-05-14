---
name: python-development-guide
description: Python-specific implementation and review checklist. Use only when writing, modifying, or reviewing Python files (`*.py`) or Python project configuration. Apply as a secondary guide after repo patterns and task scope; do not use it to demand broad rewrites.
---

# Python Development Guide

Use this only for Python code in the changed scope. Do not score it.

## Write

- Preserve the repo's existing style, package layout, and test framework first.
- Keep functions small enough to test directly. Avoid broad framework changes for a narrow task.
- Prefer typed function signatures for new public or cross-module functions.
- Prefer `pathlib.Path` for new path logic unless the repo already standardizes on `os.path`.
- Prefer context managers for files, locks, network clients, and temporary resources.
- Avoid mutable default arguments. Use `None` plus initialization inside the function.
- Avoid broad `except Exception` unless the boundary requires it and the error is re-raised, wrapped, or logged with context.

## Test

- Add or update focused tests for new branches, edge cases, and bug regressions.
- For CLI or file behavior, test paths, missing input, malformed input, and non-zero exits when relevant.
- Prefer deterministic tests over sleeps, wall-clock assumptions, and network calls.

## Review

- Treat these as blocking only for real runtime bugs, data loss, security issues, type errors, failing tests, or project-rule violations.
- Treat style-only differences as suggestions unless they conflict with configured lint/format tools.
- Check that imports are used, resources close, exceptions preserve useful context, and tests cover the changed behavior.

## Sources

- Python PEP 8: style and readability conventions.
- Python typing docs: type hints support maintainability and static checks.
- Existing repo configuration (`pyproject.toml`, `ruff`, `mypy`, `pytest`) overrides generic advice.
