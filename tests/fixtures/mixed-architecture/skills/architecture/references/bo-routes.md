# BO: route ownership

Apply only to `apps/bo/**`. Route folders are modules; Main features/widgets/slice obligations do not apply.

## Placement and privacy

Keep pure schemas, constants, mappers, body builders, types without UI dependencies, and path utilities in route _lib. Route query/mutation/state hooks belong in _hooks. Route-only screen assembly normally belongs in a flat _ui. Introduce _modules only after at least three genuine repeated kinds such as Fields/Preview justify it, not because there are three files. Section-wide reusable field UI belongs in section _components; business-independent primitives belong in @/ui. Reusable calls across sections belong in shared/api by capability, with response parsing separate from form draft schemas.

A route may import its own private underscore folders. Other routes, sibling routes, and other sections may not. The only ancestor exception is a child route using its parent section's _components. It does not allow parent _lib, sibling _components, every ancestor underscore folder, or a parent reading a child's private files. Promote genuinely cross-section behavior to business-independent ui or an appropriate domain-aware shared module instead.

The synthetic orders page consumes its own _lib and _components. History and new consume the orders section title, but retain their own private logic. Accounts consumes only its own section internals. The form owner/fields are deliberately co-located in new/_components for this approved fixture; this is a synthetic placement choice, not a claim that an unseen original form tree used that directory.

## Pure logic and primitive dependencies

Pure _lib imports no React, form library, or Next dependencies. The fixture applies that blanket rule to value and type imports, including resolved alias/relative spellings. The original text did not literally spell out `import type`; type-edge enforcement is the explicit strict interpretation used here. Ordinary RHF types belong outside _lib. The single form-specific RHF type exception is described in bo-forms.md and does not relax other libraries or other _lib files.

UI primitives know native value/onChange/ref contracts, not RHF or order semantics. Form-aware field composition stays above primitives. Adding a form-library dependency to @/ui is a boundary change, not a convenience refactor.

Framework use is permitted outside pure _lib. The non-client orders page uses Next Metadata; moving the same dependency into _lib would violate purity. Pages/layouts own server composition and client UI is explicitly marked. Pure utility modules are not React components.

## Product wiring and review limits

Preserve URL folder contracts. In a real new section, menu/proxy/permission registration must be checked against actual infrastructure; this isolated source fixture does not claim to supply that infrastructure. Keep request mapping pure and response handling in its shared API owner. This fixture prepares a local payload rather than pretending a server exists.

Use one relative import convention within these route sections; aliases address app-wide shared/ui modules. Keep route tests together at route root and section-shared tests at section root rather than one suite per segment. Existing product modal-host, mock-removal, and permission policies require actual corresponding scope before claiming evidence; no fake modal host or network fallback belongs in this example.

Review private ownership, route-only wiring, pure logic, primitive independence, and genuine sharing in addition to lint. A new underscore folder does not prove correctness, and passing imports do not prove that shared UI is business-independent.
