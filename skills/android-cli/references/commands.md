# Android CLI command workflows

Always inspect `android help <command>` before relying on the examples below.
The installed CLI's help output overrides this reference.

## Environment and project inspection

```bash
android --version
android info
android describe --project_dir=.
android help
```

Use `android info <field>` when a script needs one environment value. Use
`describe` to locate modules and produced artifacts rather than guessing APK
paths.

## SDK management

Inspect first, then mutate only with authorization:

```bash
android sdk list --all
android sdk install platforms/android-35
android sdk install <package>[@<version>]...
android sdk update <package>
android sdk update
android sdk remove <package>
```

When a project declares a required `compileSdk`, build-tools version, or
platform, align the requested package with that declaration. Do not upgrade all
packages to solve a single missing dependency.

## Project creation

```bash
android create --list --name="Placeholder"
android create empty-activity --name="My App" --output=./my-app
android create <template> --name="My App" --minSdk=<api> --output=<directory>
```

Before creation, confirm the output directory, application name, minimum SDK,
template, and whether overwriting is allowed. Inspect generated Gradle files and
run the generated project's documented build command.

## Documentation search

Use the CLI knowledge base for Android-specific API guidance:

```bash
android docs search <keywords>
android docs fetch <result-or-document-id>
```

Good searches are migration names, exact API symbols, library names, and error
messages. Fetch only the result needed for the current decision.

## Emulator management

```bash
android emulator list
android emulator create --help
android emulator start --help
android emulator stop --help
android emulator remove --help
```

List existing AVDs before creating one. Wait until `start` reports the device is
ready before deploying. Removing an AVD is destructive and requires explicit
authorization.

## Run an APK

First discover artifacts with `android describe`, then deploy:

```bash
android run --apks=<app.apk> --device=<serial>
android run --apks=<app.apk> --activity=<activity> --device=<serial> --debug
```

If multiple APKs form one install set, pass the exact set expected by the
current `android run --help`. Verify the launched component with `android
layout` or device logs.

## Screen and layout

```bash
android layout --device=<serial> --pretty
android layout --device=<serial> --diff
android layout --device=<serial> --output=<path>
android screen capture --device=<serial> --output=<screen.png>
```

Flag spelling may differ by CLI version; confirm it before use. Resolve targets
from the current layout or screenshot rather than reusing stale coordinates.

## Studio and skill commands

```bash
android studio check
android studio find-declaration --help
android studio find-usages --help
android studio analyze-file --help
android studio render-compose-preview --help
android studio version-lookup --help
android skills list
android skills find <keyword>
```

Installing or removing a skill changes the host environment. Do it only when
the user asks, and verify the resulting list.

## Updates

`android update` changes the CLI installation. Inspect `android update --help`
and obtain authorization before invoking it. After an update, re-run
`android --version`, `android info`, and the relevant subcommand help.
