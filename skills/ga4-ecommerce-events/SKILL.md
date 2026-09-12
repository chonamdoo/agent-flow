---
name: ga4-ecommerce-events
description: Implement or review web GA4 ecommerce payloads, gtag or GTM delivery, transaction and item identity, and event firing. Use for ecommerce measurement changes, not generic logging, native-only analytics, or unrelated React work.
---

# GA4 Ecommerce Events

Make each ecommerce event describe a real business occurrence, carry the selected GA4 contract, and reach its intended destination without stale fields or unintended duplicates. This skill owns analytics semantics, not checkout behavior, authentication, or a new application architecture.

## Establish the measurement contract

Inspect the existing analytics adapter, tag initialization, GTM container configuration when available, and the changed business flow. Record:

- The approved business occurrence and corresponding GA4 event, including the point at which the occurrence is known to have happened.
- The authoritative source for transaction and item identity, item quantities, prices, currency, discounts, and any optional dimensions.
- The sending path: direct `gtag`, GTM data-layer messages, or an existing wrapper around one of them; identify the destination property and other senders of the same occurrence.
- Consent/CMP ownership, permitted data, and the authorized test environment. A payload fix does not authorize production conversions, GTM publication, or consent-policy changes.

Read [transport and consent details](references/transport-and-consent.md) when changing tag delivery, data-layer isolation, parameter scope, or consent ordering. For each selected event, read its entry in the [current GA4 event reference](https://developers.google.com/analytics/devguides/collection/ga4/reference/events); a purchase schema is not a universal schema for all ecommerce events.

Proceed when each changed event has a known occurrence, source, and destination. If the business mapping is missing, identify that gap rather than inventing it from a button label or route name. Without container access, inspect available exports and application code, but leave container firing and receipt unverified.

## Bind events to occurrences and identity

- Emit at the measured occurrence, not because a component rendered or data happened to load. `purchase` represents a completed purchase, not a checkout click or a failed payment attempt. Use the product's authoritative completion signal.
- Use stable transaction identity for the same purchase across retries, reloads, and completion-page revisits. Different purchases need different identifiers. Do not generate a new transaction ID merely to make a repeated delivery visible.
- Preserve item identity across list, detail, cart, and purchase measurements. Prefer the established catalog ID; do not substitute list position, translated display text, or an unrelated entity's ID. Where the selected GA4 schema permits an item name without an ID, preserve that valid fallback instead of inventing an ID.
- Find duplicate ownership in handlers, reactive effects, route listeners, tag triggers, and parallel client/server senders. Keep one accountable delivery path per intended destination. Purchase deduplication is not a general exactly-once guarantee for every event.
- Distinguish repeated delivery from a new legitimate occurrence. A second successful addition of the same item to a cart is not a duplicate merely because the item ID is unchanged. Any deduplication boundary must match the event's business identity and lifetime.
- Snapshot event-specific fields from the occurrence. Later cart mutations or deferred callbacks must not turn a completed purchase into a description of the current cart.

These are implementation judgments derived from the occurrence and identity requirements, not a mandate for a particular hook, storage mechanism, or analytics library.

## Preserve the public payload contract

For `purchase`, the [purchase reference](https://developers.google.com/analytics/devguides/collection/ga4/reference/events#purchase) requires:

| Field | Contract and judgment |
| --- | --- |
| `transaction_id` | A string identifying the actual transaction. Preserve meaningful leading zeros and do not use an empty or invented fallback when identity is unavailable. |
| `items` | An array of item objects, not a JSON string, keyed object, or spread of the order object. Each item requires at least one of `item_id` or `item_name`. |
| `value` | A number when supplied; use the sum of item `price * quantity`, excluding separately reported `tax` and `shipping`. Do not send formatted money strings. |
| `currency` | Required when `value` is supplied, as a three-letter ISO 4217 currency code. Put it at GA4 event scope, not only inside individual items. |
| Item `price`, `discount`, `quantity` | Preserve number types and units. `price` is the discounted unit price when discounted; `discount` is the unit discount. GA4 defaults an omitted `quantity` to 1; include the known quantity rather than silently losing multiple units. |
| Optional fields | Supply only supported facts. For example, omit `customer_type` when unknown instead of assuming guest checkout means a new customer. Event-level and item-level coupons are independent. |

For other events, apply their own required fields, item selection, scope, and precedence. A removal event describes removed items, not automatically the entire cart. A refund refers to the original transaction and the actual refunded scope. Do not copy purchase revenue or item rules into list and promotion events without checking their contracts.

The ecommerce guide currently permits up to 200 items and 27 additional custom parameters per item. Recheck current limits when the changed path approaches them; agree on measurement treatment rather than silently truncating or splitting a transaction into misleading purchases.

Keep internal payloads behind an explicit mapping to GA4 fields. Preserve valid existing mappings and wrappers; do not forward raw customer/order objects. Exclude PII, secrets, and sensitive free-form content from parameters, identity fields, URLs, and titles. An opaque transaction ID is not authorization to send the associated customer's identity.

## Verify observable behavior

Use synthetic transactions in an authorized test property/session, or intercept outbound traffic locally when live collection is not authorized. Exercise the actual business path rather than manually pushing an unrelated sample and calling the integration verified.

1. Observe the measured occurrence and its emitted event, including item-level values and their types. A GTM queue entry is not proof that a GA4 tag sent it.
2. Send two distinct ecommerce occurrences in sequence. The second must contain only its intended event data while shared container/consent state remains intact.
3. Exercise a failed or cancelled action and an unrelated action. Neither may create a successful purchase or other occurrence that did not happen.
4. Exercise the applicable retry, rerender, revisit, or repeated-action boundary. Confirm duplicate prevention without suppressing a legitimate second occurrence.
5. Check the configured consent states and outgoing data against policy. When live receipt is authorized and available, use Tag Assistant/GTM Preview as appropriate and GA4 DebugView to inspect the event and item parameters in the intended property. Debug mode does not make production measurement harmless.

The [GA4 validation guide](https://developers.google.com/analytics/devguides/collection/ga4/validate-ecommerce) explains DebugView evidence. Distinguish local payload correctness, tag firing, outbound delivery, and GA4 receipt. An HTTP response or a console object alone does not establish correct reporting.

Report changed occurrences and transport, evidence actually observed, and any missing container/property access or unresolved business rule. Do not claim receipt, deduplication, or consent compliance from static inspection alone.

## Sources and applicability

Guidance is synthesized from the [GA4 ecommerce guide](https://developers.google.com/analytics/devguides/collection/ga4/ecommerce), [event reference](https://developers.google.com/analytics/devguides/collection/ga4/reference/events), and linked transport/privacy documentation, consulted on 2026-09-11. This is the consultation date, not an API release pin. Recheck the selected event and installed tag/CMP behavior before relying on version-sensitive details.
