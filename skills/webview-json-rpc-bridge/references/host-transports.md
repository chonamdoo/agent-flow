# Host Transports and Sender Evidence

Read the branch for the host actually in use. These public APIs were consulted on 2026-09-11; documentation retrieval is not runtime testing. Resolve the installed package, platform availability, native wrapper, and both transport directions before applying an API claim. A wrapper can narrow a native API's capabilities.

## Direct Android WebView

[Android's native bridge security guidance](https://developer.android.com/privacy-and-security/risks/insecure-webview-native-bridges) explains that `addJavascriptInterface` exposes an injected object to all frames and does not let the application verify the calling frame's origin. Checking the WebView's current top-level URL therefore does not authenticate a caller in an embedded frame.

- Inspect the actual exposed native method signature. A handler accepting JSON text requires serialization; a method's name or the word WebView does not establish a universal string transport.
- Keep the interface limited to registered, necessary methods and content controlled by the adopted trust policy. Loading remote HTTPS content alone does not make its scripts or embedded frames trusted.
- For untrusted content, use an interface-free browsing surface or the platform's supported removal/lifecycle handling before exposing that content. Verify the effective timing on the installed host rather than assuming removal takes immediate effect in an already loaded document.
- If the application needs caller-origin-aware messaging, identify the concrete supported message-channel API, feature availability, origin restriction, and callback metadata. Do not attribute origin evidence to `addJavascriptInterface` or to a generic message port that does not supply it.

The Android guidance also warns that message channels without origin control can accept malicious senders. Configure the supported target origin precisely where the API provides it, and establish trust when creating/transferring a channel. An outbound target-origin restriction alone is not proof of the sender's authority for a native method.

## Direct WebKit

[`WKScriptMessage`](https://developer.apple.com/documentation/webkit/wkscriptmessage) carries `body`, `frameInfo`, and the originating WebView. Its [`body`](https://developer.apple.com/documentation/webkit/wkscriptmessage/body) is not universally a string: WebKit supports bridged values including dictionaries, arrays, strings, numbers, and null.

For a registered structured-object bridge, validate that structured body directly. Do not add `JSON.stringify` merely to resemble another platform. For a wrapper that explicitly accepts JSON text, honor that text contract instead. Restrict accepted values to the JSON-compatible protocol schema even if WebKit can bridge additional native types.

Use host-provided [`WKFrameInfo`](https://developer.apple.com/documentation/webkit/wkframeinfo) and its security origin/main-frame information for the adopted sender policy, with availability checked for the deployed platform. Being the main frame is not itself origin authorization; being same-origin does not automatically grant every native capability. The frame information object is transient, not a stable frame identity across delegate calls.

Register/remove handlers and reply through the actual WebKit API or wrapper in use. Availability of `WKScriptMessageHandlerWithReply` in documentation does not mean an older deployment or a third-party wrapper exposes it. Preserve an existing supported reply transport when a native reply-handler migration is not requested.

## React Native WebView

The [maintainer API reference](https://github.com/react-native-webview/react-native-webview/blob/master/docs/Reference.md#onmessage) documents the web-to-native entry point:

- Supplying `onMessage` installs `window.ReactNativeWebView.postMessage` in the page.
- Its `data` argument must be a string and is delivered through `event.nativeEvent.data`. Encode a JSON-RPC envelope as JSON text once, then parse and validate once at the receiver's boundary.

This is specifically the React Native WebView web-to-native signature. It is not the browser's `window.postMessage` contract and not direct WebKit's structured-body contract. Verify native-to-web delivery separately from the installed wrapper rather than treating both directions as the same callback.

The same reference documents important navigation limits:

- [`originWhitelist`](https://github.com/react-native-webview/react-native-webview/blob/master/docs/Reference.md#originwhitelist) controls allowed navigation origins; it is not per-message calling-frame authentication.
- [`onShouldStartLoadWithRequest`](https://github.com/react-native-webview/react-native-webview/blob/master/docs/Reference.md#onshouldstartloadwithrequest) is not called on Android's first load. Validate the configured initial source independently; cover subsequent redirects, new windows, and frames with supported mechanisms rather than asserting that one callback covers everything.
- Static HTML can require `originWhitelist` to be `['*']` under the documented wrapper behavior. That compatibility setting is not a native-privilege grant. Keep the HTML's provenance, scripts, frame policy, navigation limits, and available capabilities independently constrained.
- Frame-only script injection settings describe where code is injected; they do not prove that every bridge message comes from an authorized frame. Do not invent `event.origin` on the native message event.

The linked `master` documentation is mutable, not a release pin. Check the installed release's source/types when callback fields, injection timing, or platform behavior matter. Preserve safe capability-unavailable/browser fallback rather than using user-agent branding as authentication or reliable feature detection.

## Other browser or host channels

When a bridge actually uses browser `window.postMessage`, consult its own [message contract](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage) for structured-clone payloads, exact target origins, and received `origin`/`source`. Those properties do not automatically exist on a native WebView callback with the same method name.

If the available callback cannot distinguish the caller required by the native permission model, state that limit explicitly. Choose an enforceable content boundary or an authorized transport redesign before enabling privileges; serialization or a claimed origin in JSON cannot repair missing host trust evidence.
