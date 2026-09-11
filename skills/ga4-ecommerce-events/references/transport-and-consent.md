# Transport, event isolation, and consent

Read this when changing delivery, parameter inheritance, GTM data-layer behavior, or consent timing. Preserve the existing transport unless the assignment requires a migration.

## Direct Google tag

The public event API is `gtag('event', eventName, eventParameters)`. Ecommerce parameters such as `items`, `value`, and `currency` belong in `eventParameters`; the GTM-specific `ecommerce` envelope is not a substitute for the direct event contract.

The [Google tag API reference](https://developers.google.com/tag-platform/gtagjs/reference#parameter_scope) distinguishes event-scoped values from target-scoped `config` values and global `set` values. Precedence is `event` over `config` over `set`; overriding a field for one event does not delete its lower-scope value. Put occurrence-specific transaction, cart, and promotion data in the event rather than global `set` state. Inspect an existing wrapper's merging behavior too: the wrapper can introduce leakage independently of gtag.

The bootstrap can queue commands before the external script finishes loading. Keep the existing initialization and consent ordering; a locally callable `gtag` is not evidence of network delivery. Do not add a second tag installation to compensate for a missing observation.

## GTM data layer

A GTM `dataLayer.push` message with an `event` key is a container event, not itself a GA4 request. Confirm that the configured GA4 Event tag has the intended custom-event trigger, destination, and ecommerce parameter mapping. Either GTM's supported ecommerce data source or an existing explicit variable mapping can be valid; verify the effective outgoing fields instead of forcing one configuration layout.

In the [GTM ecommerce guide](https://developers.google.com/analytics/devguides/collection/ga4/ecommerce?client_type=gtm), the event name is at the message root and ecommerce parameters are under `ecommerce`. The guide clears the previous ecommerce object by pushing `{ ecommerce: null }` before pushing the new message containing both `event` and its complete `ecommerce` data. This is a scoped reset, not a new data layer.

Preserve the existing layer instance, queued messages, container lifecycle, consent, and unrelated shared variables. Replacing `window.dataLayer`, emptying the whole queue, or resetting the whole data model to solve stale ecommerce fields destroys unrelated state. If the installation uses a custom layer name or data shape, follow its actual container contract and isolate the corresponding event-scoped fields.

[GTM processes queued messages in FIFO order](https://developers.google.com/tag-platform/tag-manager/datalayer). Values pushed from a tag during another event's processing are not a synchronous update to that event. Send the new event and the data it needs in the same message, after any scoped clearing, and bind the tag to that event. Keep this pair together in the existing delivery boundary so unrelated work cannot interleave stale values.

Check the tag's resolved variables and outgoing payload on two successive events. Inspecting the raw push history alone cannot prove the container's merged data model is clean.

## Consent and disclosure

Follow the existing CMP and approved regional policy. [Google's consent guide](https://developers.google.com/tag-platform/security/guides/consent) allows basic and advanced implementations; neither the presence of a denied storage setting nor a missing cookie alone proves that no request may be sent. Basic blocking and authorized advanced cookieless measurement are different contracts. Do not disable a legitimate advanced implementation or enable it merely to recover lost events.

For direct gtag, applicable consent defaults precede commands that send measurement data; updates reflect actual choices, including revocation. For GTM consent templates, use the documented `setDefaultConsentState` and `updateConsentState` APIs rather than treating an ordinary asynchronous data-layer value as consent synchronization. If only ecommerce mapping is in scope, preserve the CMP's ownership instead of building a parallel consent mechanism.

[Google's PII guidance](https://support.google.com/analytics/answer/6366371?hl=en) covers URLs, titles, identifiers, and user-entered content as well as explicit payload fields. Send only approved analytics data; do not paste customer records into diagnostics or infer that hashing arbitrary PII makes an ecommerce field acceptable. Separately governed user-provided-data features are not permission to put that data in ordinary ecommerce parameters.

If policy is unavailable, complete non-disclosing local inspection and identify the missing decision. Do not test by enabling consent or changing the destination property on a real user's behalf.

## Counterexamples to overcorrection

- A direct gtag event with event-scoped parameters does not require a GTM `ecommerce: null` push when no GTM ecommerce data model consumes it.
- A correctly mapped GTM custom data shape need not be renamed just to match a documentation example. Its observable payload and state isolation still have to satisfy the GA4 contract.
- Multiple configured destinations are not automatically duplicate collection. Trace which sender reaches which property before removing tags.
- A genuinely new cart addition of the same item should still be measured. A global set of previously seen item IDs would suppress valid behavior.

Sources were consulted on 2026-09-11; transport/CMP choices and container configuration remain project inputs, not inferred facts.
