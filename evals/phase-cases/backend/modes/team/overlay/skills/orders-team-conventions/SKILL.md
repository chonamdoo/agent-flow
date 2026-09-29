---
name: orders-team-conventions
description: Ledger team conventions for Python service code in this repository. Use when writing or reviewing any Python module under src/ or tests/.
pathGlobs: ["**/*.py"]
---

# Ledger team conventions

## Record types are NamedTuple

- Every record type under `src/` (a class with a fixed set of named fields: domain
  records, stored-row records, command or result records) is a `typing.NamedTuple`
  subclass.
- `dataclasses` is banned under `src/`: no `import dataclasses`, no
  `from dataclasses import ...`, no `@dataclass`. We rely on tuple immutability,
  cheap equality, and `_replace()`; a dataclass in a diff is a blocking review finding.
- Plain dicts stand in for records only at the edges: the HTTP query and response
  body, and the raw stored rows handed to `src/app/container.py`, which converts
  them into NamedTuple records before anything else sees them.
