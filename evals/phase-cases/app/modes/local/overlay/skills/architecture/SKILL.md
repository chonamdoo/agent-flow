---
name: architecture
description: Shop app architecture contract. Use for every change under lib/ — feature folder layout, library structure, repositories, models, and screen state holders — in design, implementation, and review.
---

# Shop App Architecture Contract

This repository does not use layered (domain/data/presentation) architecture. These rules
replace it for all code under `lib/`.

## Rules

1. **One flat folder per feature.** A feature lives in `lib/<feature>/` with no subfolders.
   Never create `core`, `domain`, `data`, `presentation`, `features`, or `api` directories
   anywhere under `lib/`. A feature's models, repository, state, and state holder sit side by side.
2. **No abstract or interface classes.** Do not declare `abstract class`, `interface class`, or
   `abstract interface class`. A repository is a concrete class that receives its transport as a
   function value (for example `Future<List<Map<String, Object?>>> Function()`); tests substitute
   the function, not the class. Closed state families use `sealed class`.
3. **One library per feature.** `lib/<feature>/<feature>.dart` is the feature's only library
   file: it contains every `import` the feature needs and one `part '<file>.dart';` directive for
   each other file in the folder. Every other file in the folder starts with
   `part of '<feature>.dart';` and contains no `import` or `export` directives. Code outside the
   feature (including tests) imports only `<feature>.dart`.
4. **Decode where consumed.** Raw backend records are decoded by a factory on the model itself
   (for example `Order.fromRecord`). Separate DTO or mapper types are not used.

## Review

A change that adds layer folders, an abstract repository, or a sibling import inside a feature
folder violates this contract, regardless of other qualities.
