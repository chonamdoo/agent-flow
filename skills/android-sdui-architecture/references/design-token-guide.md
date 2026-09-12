# Design Token Guide

Source attribution retained from the supplied bundle: PART 4-4. Original-source
identity, version, locator, effective date, and author authority are unverified.

## Rule

The server states **meaning**; the client owns the **value**. Styling fields in
server JSON reference semantic tokens. Raw dp dimensions, hex colors, and other
literal design values violate the schema. Structural sizing modes and bounded
ratios are a distinct, explicitly declared value class; see
[ui-node-model-guide.md](ui-node-model-guide.md) for that distinction.

Judge a payload by its structured field meaning: a spacing or surface token is
acceptable only if it belongs to the client catalog; a literal pretending to be
a token is not. Do not turn incidental token names or numerical tables into a
required application design system.

## Resolution ownership

The client design-system boundary resolves spacing, color, typography,
shape/radius, elevation, and icon tokens. Other layers consume those meanings
without duplicating their concrete values.

Each resolver defines a safe fallback for an absent, unknown, or malformed token.
The fallback must avoid a crash and preserve a usable surface where possible;
its neutral behavior follows the actual component contract, not a universal
numeric constant or parser recipe.

## Fallback is not permission

Server schema validation rejects raw styling literals before delivery. Client
resilience handles bad payloads that still arrive. These are separate contracts:
an existing tolerant numeric fallback is not permission for the server to send
numbers, nor a requirement to add numeric parsing to every resolver.

## Theme and window behavior

Token resolution can support dark mode and larger windows only when the client
resolver and components actually use the active theme and window conditions.
Tokenization alone does not prove adaptation. Design revisions belong at the
client design-system boundary rather than in every stored screen template.
