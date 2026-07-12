---
name: camera1-to-camerax
description: Migrates legacy Android Camera1 or directly managed Camera2 implementations to lifecycle-aware CameraX for preview, focus, capture, rotation, and lens switching. Use when code imports `android.hardware.Camera`, manually owns Camera2 sessions, uses `SurfaceHolder.Callback`, or needs a CameraX migration in Views or Jetpack Compose.
---

# Camera1 to CameraX

Preserve camera behavior while replacing manual device and lifecycle management
with CameraX use cases. Do not mix the old and new pipelines after cutover.

## Quick start

1. Inventory permissions, preview surface, capture format, lens switching,
   rotation, focus, flash, analysis, lifecycle code, and existing camera tests.
2. Confirm the app's minimum SDK, Compose or Views UI toolkit, and currently
   supported CameraX version before changing dependencies.
3. Add only the CameraX artifacts required by the use cases.
4. Bind `Preview`, `ImageCapture`, and any other use cases through one
   `ProcessCameraProvider` and a `LifecycleOwner`.
5. Use `PreviewView` for Views or `CameraXViewfinder` for Compose.
6. Replace manual focus coordinates with a CameraX metering-point factory.
7. Verify permission denial, lifecycle transitions, rotation, repeated capture,
   and every supported lens on a real or virtual device.

Read [migration.md](references/migration.md) for dependencies, code patterns,
capture handling, switching, constraints, and the full verification matrix.

## Migration constraints

- Request runtime camera permission before binding use cases.
- Unbind before rebinding a changed use-case set or lens selector.
- Do not manually open or release a camera in `onResume` or `onPause`.
- Do not retain Camera1 orientation or focus matrix calculations.
- Close every `ImageProxy` in `finally`; a leaked proxy stalls capture.
- For new Compose code, use `CameraXViewfinder`, not `PreviewView` wrapped in
  `AndroidView`.
- Keep CameraX objects out of domain models and ViewModel state unless the
  project's architecture explicitly owns platform resources there.
- Preserve behavior not covered by the basic examples, including flash,
  analysis, video, aspect ratio, and output metadata.

## Completion check

The migration is complete only when no active legacy camera lifecycle remains,
the target module builds, camera tests pass, and device verification covers
preview, focus, capture, lens switching, rotation, background/foreground, and
permission denial.
