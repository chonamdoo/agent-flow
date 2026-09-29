---
name: architecture
description: Approved architecture contract for this Next.js app — one flat folder per feature, role-named files, T-prefixed types, and no Clean layering. Apply it to every placement, dependency, and naming decision under src/.
---

# Flat feature folders

This project deliberately does **not** use Clean Architecture layering. A feature is small enough
to read in one folder, so transport calls, payload mapping, business rules, and view state live
side by side. This contract replaces the profile's Clean roles wherever they differ.

## Placement

- Each feature owns exactly one flat folder `src/<feature>/` (for example `src/orders/`).
  A feature folder has no subdirectories.
- Never create `core/`, `domain/`, `data/`, `presentation/`, or `features/` directories anywhere
  under `src/`. There is no separate entity layer and no data layer.
- No repository interfaces, ports, adapters, use-case objects, or `class` declarations. Export plain
  functions; callers pass the transport function (`getJson(path)`) in as an argument.
- A feature folder never imports from another feature folder.

## Naming

- Files are named `<role>.<feature>.ts` (or `.tsx`), where the role is one lowercase word:
  `api.orders.ts`, `view.orders.ts`, `sort.orders.ts`.
- Declare types with `type`, never `interface`. Every exported type name starts with a capital `T`
  followed by the name in PascalCase: `TOrder`, `TOrderStatus`, `TOrdersView`. Non-exported local
  types follow the same rule.

## Review

A change that adds a layer directory, a repository/port abstraction, a class, a nested folder inside a
feature, a file outside the `<role>.<feature>.ts` pattern, or an exported type without the `T`
prefix does not follow this contract.
