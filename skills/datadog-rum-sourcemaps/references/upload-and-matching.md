# Uploader and artifact matching

Read only the branch used by the repository. Discover versions and commands from its lockfile, installed CLI help, and existing build/CI configuration; these examples describe public options, not a required project layout.

## Service/version CLI uploads

The [first-party CLI command reference](https://github.com/DataDog/datadog-ci/blob/master/packages/base/src/commands/sourcemaps/README.md) documents `datadog-ci sourcemaps upload`, with the artifact directory as its positional argument and `--service`, `--release-version`, and `--minified-path-prefix` for service/version matching.

Resolve each argument before execution:

| Input | Evidence needed |
| --- | --- |
| Artifact directory | Contains the final corresponding minified JS and maps. The relative directory structure matches served assets. Avoid a repository-wide scan that collects unrelated source. |
| Service/release | Exactly matches the selected runtime event's `service` and `version`, not a separately guessed value. |
| Minified prefix | Combined with a file's relative path, yields its actual deployed URL or the supported absolute path equivalent. |
| Credentials/site | The uploader's effective configuration resolves to the authorized account/site. Do not substitute browser SDK credentials. |
| Git metadata | Disclosure of repository URL, commit, and related paths is authorized, or the installed uploader's supported metadata opt-out is selected. |

The currently published guide describes co-located JS/map pairs and `.js.map` support, warns that accepted `.mjs.map` files do not unminify stack traces, and limits each map plus its minified file to 500 MB. Recheck support and limits for the installed version when relevant. Upload acceptance alone does not establish support for a different extension, and merely renaming a map is not proof that its corresponding deployed bundle is correct.

The CLI documents `--dry-run` as omitting the final upload while performing other checks. `--disable-git` suppresses repository metadata collection; it does not sanitize `sourcesContent`. Supplying repository/commit overrides can change metadata discovery behavior, so inspect the selected version before using them as a substitute for Git access.

### Site and credential differences

The upload guide documents `DD_API_KEY` for the CLI and site-specific `DATADOG_SITE`/`DATADOG_API_HOST` settings. The CLI repository reference also documents `DD_SITE` and `DATADOG_SOURCEMAP_INTAKE_URL`. Treat this documentation difference as a reason to inspect the installed version's effective configuration, not to set every variable or allow a silent default to the US site. Full intake URL overrides deserve the same authorization check as the nominal site.

Do not echo secret values to establish presence. An unavailable credential prevents an authorized live upload, not local inspection of an existing artifact set.

## Build-plugin uploads

When the project already uses a Datadog build plugin, consult its installed-version documentation and the [build-plugin sourcemap guide](https://docs.datadoghq.com/real_user_monitoring/application_monitoring/browser/build_plugins/source_maps/).

The published service/version configuration uses `errorTracking.sourcemaps.service`, `releaseVersion`, and `minifiedPathPrefix`. It documents `dryRun` for a non-uploading run and `bailOnError` for failing the build on upload error. The documented `bailOnError` default is `false`; that default is not the project's release policy. Translate the explicit policy into the supported option rather than inheriting it accidentally.

Plugin authentication is documented through `auth.apiKey` or `DATADOG_API_KEY`, with `auth.site` and supported environment overrides. Do not assume CLI and plugin credential variable names are interchangeable. The plugin uploads maps but does not generate them, and may include Git metadata. Avoid a second unconditional CLI upload step when the existing plugin already owns the same release artifacts.

## Map generation and public delivery

Use the selected bundler's production configuration and confirm the emitted artifacts. A TypeScript intermediate source map does not by itself prove that the final minifier emitted a usable map; chained transforms must preserve mappings through to the original source.

For Vite, [the build options reference](https://vite.dev/config/build-options.html#build-sourcemap) documents `build.sourcemap: true` for separate map files, `'inline'` for data embedded in the bundle, and `'hidden'` for separate maps without a source-map comment in the bundle. Choose the supported mode that satisfies upload and public-exposure policy. A hidden map still needs exclusion from a public package if source must remain private. Do not switch minifiers or change unrelated framework settings solely to copy a vendor example.

For another bundler or framework, read its installed-version map generation and deployment controls. Keep original sources available to the approved processing path; dropping all source content to avoid disclosure can remove the source context the integration needs. Where disclosure is prohibited, state the limitation rather than claiming an equivalent result.

## Existing debug-ID matching

The CLI's development-branch reference consulted on 2026-09-11 documents `sourcemaps inject` and `sourcemaps upload --debug-id`. This is not evidence that every released CLI or RUM SDK supports that path. If it is already configured, verify the installed CLI, SDK, plugin, and receiving service's support and use their actual matching contract. If support cannot be established, retain the existing artifacts and report the uncertainty rather than mixing matching modes.

For the documented path, injection modifies bundle bytes and adjusts mappings. The upload reads the authoritative debug ID from the JavaScript bundle; the map's top-level `debug_id` alone is not end-to-end proof. Deploy and upload the same injected artifacts. Injection precedes compression, SRI hashes, signatures, and checksums that depend on bundle bytes; regenerate those outputs if needed.

The referenced CLI treats injected debug-ID uploads and service/version uploads as mutually exclusive and rejects injected bundles in service/version mode. It also preserves an existing injected ID on rerun; reinjection is not a way to detect arbitrary edits made after injection. After source/configuration changes, rebuild unmodified outputs through the normal build process before injection. Do not patch deployed JavaScript after producing matching maps.

A local debug-ID lookup only finds an artifact. It cannot establish that Datadog received it or that a runtime error resolves through it. Preserve accurate service/release tags for diagnosis even when the supported lookup key differs.

## Diagnostic counterexamples

- Correct hidden maps kept privately and uploaded to an approved account are valid; public hosting is not a requirement.
- A correctly authorized warn-only/degraded release is not a defect merely because upload failure does not fail deployment. A silent skip without such a policy is different.
- Matching runtime tags with a wrong CDN prefix still fails service/version lookup. Matching filenames from a different build are not equivalent artifacts.
- A valid absolute path prefix can serve several hosts. Requiring one host-specific upload per hostname can add unnecessary work.
- A successful dry run, empty successful scan, or accepted upload cannot substitute for an original-source frame observed from the actual release.

All linked sources are first-party documentation, consulted on 2026-09-11. Development-branch CLI behavior is explicitly conditional and must not be presented as an established minimum released version.
