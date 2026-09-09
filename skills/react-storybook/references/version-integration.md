# Storybook Version Integration

Read when changing installation, framework/builder integration, addons, CSF syntax, or test imports. Preserve an existing supported setup rather than upgrading to match a documentation example.

1. Resolve installed Storybook core, React renderer, framework adapter, builder, addon, and test-runner versions from the manifest/lockfile and configuration. Establish whether the target is React Web rather than React Native or a different renderer.
2. Select the matching official integration documentation and existing project commands. Current documentation includes `storybook/test`, CSF 3, and experimental CSF Next; availability does not imply that older installations support those imports or that stable stories should migrate to an experimental API. Match `canvas`, `mount`, lifecycle cleanup, spies, and runner APIs to the installed version.
3. Reuse necessary decorators/providers and the existing dependency seam. A pure component needs no entire-app provider tree. Next router/server module mocks can reproduce a client-facing state but do not turn Storybook into the real server runtime.
4. For explicit adoption, choose only the renderer/framework/addons necessary for the requested states and evidence. Respect authorization before installing or launching anything; use the project's declared scripts/gates rather than inventing commands. A paid visual service is optional.

Completion for an integration change is the changed story loading and exercising its intended evidence path under the actual selected renderer/builder/runner. Import resolution alone is not proof that play, a11y, or visual comparison ran. A supported older CSF setup is a normal control, not a failure to follow current docs.

## Primary Sources

- [Storybook framework integrations](https://storybook.js.org/docs/get-started/frameworks), [configuration](https://storybook.js.org/docs/configure).
- [Component Story Format](https://storybook.js.org/docs/api/csf), [interaction tests and lifecycle hooks](https://storybook.js.org/docs/writing-tests/interaction-testing).
- [Next integration](https://storybook.js.org/docs/get-started/frameworks/nextjs), [Vitest addon](https://storybook.js.org/docs/writing-tests/integrations/vitest-addon).

These links identify the authoritative API families; select the version matching the project before applying version-specific instructions.
