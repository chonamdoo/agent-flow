# App QA

- Use the project's harness: Maestro or Detox (React Native), `flutter test integration_test` (Flutter),
  `connectedAndroidTest` (Android), the UI test target via `xcodebuild test` (iOS).
- These need a booted emulator, simulator or device. If none is available here, stop with `status: blocked`
  naming it; do not substitute unit tests and call the app verified.
- One device at a time per checkout; another run on the same device makes results meaningless.
- Cover the changed screen's states (loading, empty, error, content), navigation in and back out, and
  permission or system dialogs the flow raises.
- Record which device, OS version and build were used.
