---
name: android-cli
description: Operates the Android command-line tool for project inspection, SDK and emulator management, deployment, device UI inspection, screenshots, and journey evaluation. Use when a task explicitly involves the `android` CLI, Android SDK setup, AVDs, APK deployment, device interaction, or Android journey tests.
---

# Android CLI

Use the installed CLI as the source of truth because available subcommands and
flags may vary by version. Keep device-changing actions narrow and observable.

## Quick start

1. Run `command -v android`, `android --version`, and `android info`.
2. Inspect the project with `android describe --project_dir=<path>` when build
   artifacts or modules are involved.
3. Run `android help <command>` and the target subcommand's `--help` before use.
4. Select an explicit device when more than one emulator or device is present.
5. Execute the smallest command that satisfies the request, then verify its
   result with project output, `android layout`, or a screenshot.

If `android` is unavailable, report the missing prerequisite and use the
environment's approved provisioning process. Do not execute a remote installer
or update the CLI without user authorization.

## Task routing

- Read [commands.md](references/commands.md) for SDKs, project creation,
  documentation, emulators, deployment, Studio integration, and diagnostics.
- Read [device-interaction.md](references/device-interaction.md) before
  inspecting or manipulating a running app.
- Read [journey-evaluation.md](references/journey-evaluation.md) before running
  an XML journey test.

## Operating constraints

- Treat current `--help` output as authoritative; do not guess flags.
- Prefer `android layout` for UI state and `android layout --diff` after an
  action. Use screenshots when layout data is insufficient.
- Visually inspect every captured screenshot before using it to choose an
  interaction target.
- Do not install or remove SDK packages, create or delete AVDs, update tools, or
  alter project files unless the task authorizes that state change.
- Record commands and observed results when evaluating a journey.
- Do not turn a journey failure into an implementation task unless requested.

## Completion check

Report the selected device, commands run, resulting artifact or UI state, and
any command that could not be executed. For journey tests, use the result schema
in the journey reference.
