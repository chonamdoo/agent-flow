# CameraX migration reference

## Prerequisites and dependency selection

Confirm `android.permission.CAMERA` exists in the manifest and that runtime
permission is granted before calling `bindToLifecycle`. Preserve any feature
declaration such as camera hardware requirements.

The source migration floor is CameraX 1.3.0 for interoperability and 1.5.0 for
Compose extensions. Treat these as minimums, not recommendations: inspect the
project's version catalog and approved dependency source for the currently
supported compatible version.

Version catalog entries:

```toml
[versions]
camerax = "<supported-version>"

[libraries]
androidx-camera-core = { group = "androidx.camera", name = "camera-core", version.ref = "camerax" }
androidx-camera-camera2 = { group = "androidx.camera", name = "camera-camera2", version.ref = "camerax" }
androidx-camera-lifecycle = { group = "androidx.camera", name = "camera-lifecycle", version.ref = "camerax" }
androidx-camera-view = { group = "androidx.camera", name = "camera-view", version.ref = "camerax" }
androidx-camera-compose = { group = "androidx.camera", name = "camera-compose", version.ref = "camerax" }
```

Module dependencies:

```kotlin
implementation(libs.androidx.camera.core)
implementation(libs.androidx.camera.camera2)
implementation(libs.androidx.camera.lifecycle)
implementation(libs.androidx.camera.view) // Views only
implementation(libs.androidx.camera.compose) // Compose only
```

Do not add both UI artifacts unless the module genuinely supports both.

## Remove the legacy ownership model

After behavior is covered by tests or a manual baseline:

- Remove `android.hardware.Camera` objects and direct open/release calls.
- Remove `SurfaceView`, `SurfaceHolder.Callback`, and their surface lifecycle
  callbacks when they exist only for the old preview.
- Remove custom open/close logic from `onResume`, `onPause`, and configuration
  callbacks.
- Remove manual display-orientation and focus-coordinate matrices that CameraX
  now owns.
- Keep business behavior, capture naming, storage policy, analytics, permission
  UX, flash controls, and error presentation intact.

## Bind lifecycle-aware use cases

Construct use cases once per relevant configuration and bind them only after
permission is available:

```kotlin
val providerFuture = ProcessCameraProvider.getInstance(context)
providerFuture.addListener({
    val provider = providerFuture.get()
    val selector = CameraSelector.Builder()
        .requireLensFacing(lensFacing)
        .build()
    val preview = Preview.Builder().build()
    val imageCapture = ImageCapture.Builder()
        .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
        .build()

    provider.unbindAll()
    val camera = provider.bindToLifecycle(
        lifecycleOwner,
        selector,
        preview,
        imageCapture,
    )
    cameraControl = camera.cameraControl
}, ContextCompat.getMainExecutor(context))
```

Handle provider and binding failures through the app's existing error path.
Do not swallow `ExecutionException`, unavailable-lens errors, or permission
changes. Store use cases where capture and rotation updates can reach them, but
do not leak an Activity through long-lived state.

## Preview and tap-to-focus with Views

Use `androidx.camera.view.PreviewView`:

```kotlin
preview.setSurfaceProvider(previewView.surfaceProvider)

previewView.setOnTouchListener { _, event ->
    if (event.action == MotionEvent.ACTION_UP) {
        val point = previewView.meteringPointFactory.createPoint(event.x, event.y)
        val action = FocusMeteringAction.Builder(
            point,
            FocusMeteringAction.FLAG_AF,
        ).build()
        cameraControl?.startFocusAndMetering(action)
        true
    } else {
        false
    }
}
```

Preserve scaling behavior and aspect ratio from the old preview. Test taps near
all edges because crop and letterbox behavior affect metering coordinates.

## Preview and tap-to-focus with Compose

Use the Compose viewfinder and retain the latest `SurfaceRequest`:

```kotlin
var surfaceRequest by remember { mutableStateOf<SurfaceRequest?>(null) }
val preview = remember {
    Preview.Builder().build().apply {
        setSurfaceProvider { request -> surfaceRequest = request }
    }
}

surfaceRequest?.let { request ->
    CameraXViewfinder(
        surfaceRequest = request,
        coordinateTransformer = coordinateTransformer,
        modifier = Modifier.fillMaxSize(),
    )
}
```

Use the coordinate transformer associated with the rendered viewfinder before
creating the metering point:

```kotlin
val surfaceOffset = with(coordinateTransformer) { tapOffset.transform() }
val factory = SurfaceOrientedMeteringPointFactory(
    request.resolution.width.toFloat(),
    request.resolution.height.toFloat(),
)
val point = factory.createPoint(surfaceOffset.x, surfaceOffset.y)
val action = FocusMeteringAction.Builder(
    point,
    FocusMeteringAction.FLAG_AF,
).build()
cameraControl?.startFocusAndMetering(action)
```

Use the exact transformer construction required by the selected CameraX
Compose version. Do not copy coordinates directly from Compose into sensor
space.

Update target rotation when display configuration changes:

```kotlin
val rotation = view.display?.rotation ?: Surface.ROTATION_0
imageCapture.targetRotation = rotation
preview.targetRotation = rotation
```

Skip display access in edit-mode previews.

## Capture

For direct file output, prefer `OutputFileOptions` and
`OnImageSavedCallback`. For in-memory processing, always close the proxy even
when conversion fails:

```kotlin
imageCapture.takePicture(
    cameraExecutor,
    object : ImageCapture.OnImageCapturedCallback() {
        override fun onCaptureSuccess(image: ImageProxy) {
            try {
                processCapturedImage(
                    image = image,
                    rotationDegrees = image.imageInfo.rotationDegrees,
                    mirrorHorizontally =
                        lensFacing == CameraSelector.LENS_FACING_FRONT,
                )
            } finally {
                image.close()
            }
        }

        override fun onError(exception: ImageCaptureException) {
            reportCaptureFailure(exception)
        }
    },
)
```

Process the actual `ImageProxy` format; do not assume plane zero is always a
complete encoded image. Keep expensive conversion and file IO off the main
thread. Shut down owned executors with their owner lifecycle.

## Switch lenses

```kotlin
lensFacing = if (lensFacing == CameraSelector.LENS_FACING_BACK) {
    CameraSelector.LENS_FACING_FRONT
} else {
    CameraSelector.LENS_FACING_BACK
}
```

Check `hasCamera` for the new selector, unbind, and rebind the same required use
cases. Disable or hide switching when the requested lens is unavailable.

## Verification matrix

- Build, lint, and the narrowest camera tests pass.
- Permission grant and denial produce the expected UI; revocation does not
  crash a bound screen.
- Preview starts once, survives background/foreground, and stops with lifecycle.
- Portrait, landscape, and device rotation preserve preview and output rotation.
- Tap-to-focus works at center and edges for every preview scale type.
- Repeated in-memory captures do not stall, proving proxies are closed.
- Front capture follows the existing mirror contract; rear capture is unchanged.
- Lens switching handles devices with one or multiple cameras.
- Flash, analysis, video, storage, metadata, and error behavior are preserved if
  they existed in the legacy implementation.
- No live `android.hardware.Camera`, old surface callback, or manual camera
  open/release path remains.
