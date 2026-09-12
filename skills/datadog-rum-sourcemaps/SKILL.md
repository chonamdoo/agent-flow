---
name: datadog-rum-sourcemaps
description: Implement or review web Datadog RUM and browser-error sourcemap generation, authorized upload, release matching, and deobfuscation evidence. Use for Datadog browser sourcemap changes, not generic logging, React Native tooling selection, or unrelated deployment work.
---

# Datadog RUM Sourcemaps

Make a browser error resolve against the artifacts that actually produced its deployed JavaScript. Upload success, release success, and working deobfuscation are separate claims.

## Establish the artifact and authority boundary

Inspect the existing bundler configuration, installed RUM SDK and uploader/plugin versions, release metadata source, artifact lifecycle, and CI policy. Identify:

- Browser bundles and their corresponding maps, including lazy chunks and multiple browser outputs. Server-only bundles are not browser RUM artifacts merely because they share an output directory.
- The selected matching mechanism. For service/version matching, compare runtime `service` and `version` with upload service/release identity and the minified URLs in actual error frames.
- The authorized Datadog organization/account, site, effective intake endpoint, and source-disclosure policy. Inspect endpoint overrides as well as default site settings.
- Who owns upload execution, credential injection, artifact retention, release failure handling, and deployment approval.

Read [uploader and artifact matching details](references/upload-and-matching.md) when changing bundler output, CLI/plugin configuration, path prefixes, site variables, or debug-ID matching. Verify the installed version's supported behavior before adopting a current documentation example.

Stop the dependent action when source-transfer permission or destination authority is missing. Static inspection and local artifact analysis can continue; neither this skill nor possession of an API key grants permission to upload, create keys, deploy, or trigger production errors.

## Keep release matching exact

For the service/version path described in the [Datadog upload guide](https://docs.datadoghq.com/real_user_monitoring/guide/upload-javascript-source-maps/):

- The uploader's `--service` matches the error event's runtime `service`; `--release-version` matches its runtime `version`. A branch name, an unrelated package version, or a successful upload from another release is not an acceptable substitute.
- Derive both runtime and upload identity from the same established immutable release metadata. Environment names may be additional tags, but do not replace the matching identity.
- The upload root plus each artifact's relative path must map through the minified path prefix to the URL reported in the browser stack frame. Account for CDN/base paths, nested assets, and lazy chunks without assuming that a local directory name appears in the public URL.
- When the same bytes are served from multiple hosts, a supported absolute path prefix can be valid. Do not demand a full URL when a host-independent mapping is intentional and unambiguous.
- Upload for each relevant service when several services use the artifacts. Do not relabel unrelated runtime events to make one upload appear to work.

If the repository already uses a supported debug-ID path, preserve that matching architecture and follow the reference's version checks. Do not impose service/version matching on a mechanism that identifies artifacts differently, or migrate to debug IDs solely because a development-branch document mentions them.

## Preserve artifacts without exposing source

Generate maps for the final production browser bundles, with valid mappings and the original source content required by the chosen Datadog integration. `sourcesContent` can contain proprietary code or accidentally embedded secrets; minification and a private build directory do not authorize third-party disclosure.

Upload only the approved artifact set. Check whether repository metadata is also transferred, including remote URLs, commit identity, and source paths. A metadata opt-out does not remove original code from sourcemaps. If required source cannot be disclosed, state the deobfuscation limitation and follow the approved alternative; do not silently upload it or promise full source context from incomplete maps.

Keep the exact bundle/map pair available until upload processing succeeds, with protected retention sufficient for the release's retry or rollback policy. An uploader's zero exit status with no matching artifacts is not proof of successful processing. Do not delete needed maps before upload or regenerate them from a later build while retaining the old release identity.

Public map hosting is not required for an authorized Datadog upload. Use the bundler's supported separate/hidden map output as appropriate, and keep maps out of the public deployment when policy requires it. Hidden map comments alone are not access control: a map file still deployed to a public server can remain fetchable. Preserve private build artifacts separately from the deployment package when needed.

## Make failure policy explicit

Uploader credentials belong in the authorized server/CI secret channel, never browser code, public environment substitution, uploaded diagnostics, or command transcripts. A browser RUM client token is not the source-map uploader's API key.

Follow the release's stated policy for missing credentials, partial uploads, processing failures, and unavailable network access:

- A release that requires symbolication blocks at its defined gate until that requirement is satisfied or an authorized exception is recorded.
- An explicitly permitted degraded release may proceed through the owner's normal release process, with the upload failure and recovery requirement visible. Preserve the exact artifacts needed for recovery.
- If no policy exists, report the missing decision instead of choosing unconditional warning-only success or an unconditional deployment block. Do not make that decision by catching every error or silently skipping when a secret is absent.

Build plugins can upload during an otherwise ordinary build. Establish permission and configure the supported non-upload mode before running such a build for local inspection. Do not assume a build command is side-effect-free.

## Verify through the deployed artifact

Separate these evidence levels and run only those authorized:

1. **Artifact evidence:** inspect the selected build's bundle/map pairs, meaningful mappings, required source content, matching metadata, and public-package exclusions. Include changed lazy chunks, not just the entry bundle.
2. **Upload evidence:** in the authorized account/site, record the actual uploader result and processed artifact identity. A dry run establishes local discovery/configuration only; accepted uploads are not yet proof of deobfuscation.
3. **Runtime evidence:** use an authorized existing error or a controlled non-production error from the exact deployed build. Inspect its runtime tags, frame URL or supported debug identity, and Datadog's resolved original file, line, and source context where available. Compare to the original source for that build, not merely to a plausible-looking filename.
4. **Policy evidence:** confirm the configured failure branch and protected artifact recovery path. If public map exclusion is required and an authorized deployed surface is available, verify that maps cannot be retrieved there; source-map comment omission alone is insufficient.

If runtime events are absent, first distinguish SDK initialization, collection consent, sampling, filtering, and ingestion from symbol lookup. Do not modify these policies simply to produce a green check. If upload succeeds but frames remain minified, compare exact release identity and artifact URLs before repeating uploads; the guide warns that re-uploading a map under an unchanged version does not overwrite the existing one.

Report the selected matching mechanism, changed artifact/upload path, authorized destination, observed evidence level, release-policy outcome, and unverified steps. Redact secrets and source content from evidence. An upload for another release cannot count as working symbolication for the current release.

## Sources and applicability

Synthesized from Datadog's [JavaScript sourcemap upload guide](https://docs.datadoghq.com/real_user_monitoring/guide/upload-javascript-source-maps/) and linked first-party CLI/plugin and bundler documentation, consulted on 2026-09-11. That date records consultation, not a pinned tool release. Artifact ownership, protected retention, and evidence separation are operational judgments; release authority and failure policy remain project inputs.
