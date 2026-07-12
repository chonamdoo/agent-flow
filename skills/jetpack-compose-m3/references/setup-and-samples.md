# Setup and version-matched samples

## Establish versions

Inspect `libs.versions.toml`, module build files, the dependency lock state, and
the resolved Gradle graph. Use the project's trusted dependency-update tool to
identify the latest stable `androidx.wear.compose:compose-material3`; exclude
qualifiers such as `alpha`, `beta`, and `rc` unless requested. Keep Foundation
and Navigation artifacts compatible with that resolved version.

For Kotlin 2.0 and newer, apply the Compose compiler plugin:

```kotlin
plugins {
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    defaultConfig { minSdk = 25 }
}
```

Do not retain the old `kotlinCompilerExtensionVersion` path in a Kotlin 2.x
migration. After any catalog or build-file update, run the project sync or an
equivalent Gradle dependency resolution before source changes.

## Mandatory sample extraction

Wear Compose publishes version-matched sample source JARs. These are the source
of truth for parameters, slot types, defaults, interaction behavior, and
component nesting.

Use a project-local cache so all hosts share the same location without writing
to a global skill installation:

```text
<PROJECT_ROOT>/.agent-flow/cache/wear-compose-m3/<VERSION>/<ARTIFACT>/
```

Procedure:

1. Resolve `<GRADLE_USER_HOME>` from the environment or Gradle configuration.
2. Check whether the project-local cache already contains `.kt` files for the
   exact version and artifact.
3. On a miss, inspect only the targeted artifact directory:

```bash
find <GRADLE_USER_HOME>/caches/modules-2/files-2.1/androidx.wear.compose/compose-<ARTIFACT>/<VERSION>/ \
  -name '*samples-sources.jar'
```

4. Process `material3` and `foundation`; include `navigation3` when used.
5. Extract each matching JAR to its project-local artifact directory:

```bash
unzip -j <SAMPLES_JAR> -d <PROJECT_ROOT>/.agent-flow/cache/wear-compose-m3/<VERSION>/<ARTIFACT>/
```

6. Read the relevant sample `.kt` file before implementation and record which
   sample informed the change in the implementation notes.

Do not use a sample from another version, generated API guesses, or a generic
web snippet as a substitute. If Gradle did not fetch sample JARs, resolve the
dependency/environment problem first.

## Evidence to capture

- resolved dependency versions and Kotlin/plugin versions;
- successful sync or dependency-resolution command;
- exact sample JAR and extracted sample file used;
- any experimental opt-in required by the resolved API;
- baseline screenshots or previews before migration.
