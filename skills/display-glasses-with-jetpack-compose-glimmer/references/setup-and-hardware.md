# Project setup and hardware

## Projected Activity

The projected Activity runs on the host but renders and receives interaction on
the connected glasses. Declare it for the `xr_projected` display category and
launch it only with projected Activity options.

```kotlin
@OptIn(ExperimentalProjectedApi::class)
fun Activity.launchGlassesActivity() {
    if (!glassesConnected) return
    val intent = Intent(this, GlassesActivity::class.java)
    val options = ProjectedContext.createProjectedActivityOptions(this)
    startActivity(intent, options.toBundle())
}
```

Keep a discoverable host-phone action such as “Launch on Glasses”; disable it
when disconnected. Automatic launch is acceptable only when it matches the
product flow. Never fall back to launching the projected Activity on the phone.
Notification entry points must use the same projected Activity options and show
a safe host-phone fallback when glasses are disconnected.

## Choose the correct context

- Inside a projected Activity, the Activity context is already projected.
- From a phone Activity or service, call
  `ProjectedContext.createProjectedDeviceContext(context)` and handle
  `IllegalStateException` when no device is connected.
- From a projected Activity that needs phone hardware, call
  `ProjectedContext.createHostDeviceContext(context)`.
- Do not select hardware through `applicationContext`; its device identity can
  reflect whichever Activity was most recently foregrounded.

```kotlin
@OptIn(ExperimentalProjectedApi::class)
fun projectedContextOrNull(context: Context): Context? =
    try {
        ProjectedContext.createProjectedDeviceContext(context)
    } catch (error: IllegalStateException) {
        null
    }
```

Observe `ProjectedContext.isProjectedDeviceConnected(...)`. On disconnect,
stop and release every camera, recorder, sensor, and service created from the
projected context. On reconnect, obtain a new context and recreate resources;
the old context is invalid.

## Glasses-scoped permissions

Declare only required permissions in the manifest. A phone grant does not grant
the equivalent capability on glasses.

From a projected Activity, register
`ProjectedPermissionsResultContract` and launch a list of
`ProjectedPermissionsRequestParams` with a concise rationale:

```kotlin
private val permissionLauncher =
    registerForActivityResult(ProjectedPermissionsResultContract()) { result ->
        if (result[Manifest.permission.CAMERA] == true) startCamera()
        else showPermissionDeniedState()
    }

private fun requestCamera() {
    permissionLauncher.launch(
        listOf(
            ProjectedPermissionsRequestParams(
                permissions = listOf(Manifest.permission.CAMERA),
                rationale = "Camera access is needed for this glasses feature.",
            )
        )
    )
}
```

Do not use the ordinary single-permission Activity Result contract from a
glasses Activity; it can create a non-interactable dialog. From a phone
Activity, request permission for the projected context's `deviceId`. Handle
grant, denial, user cancellation, and disconnected-device paths.

## Microphone

Use a projected-context `AudioRecord` for multiple microphones and XR-specific
processing. Supply the projected context to `AudioRecord.Builder.setContext`,
use a 16 kHz mono or stereo input format, choose the audio source for the use
case, and call both `stop()` and `release()`.

```kotlin
val recorder = AudioRecord.Builder()
    .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
    .setAudioFormat(audioFormat16Khz)
    .setBufferSizeInBytes(bufferSize)
    .setContext(projectedContext)
    .build()
```

Bluetooth HFP is a separate, phone-scoped alternative for a standard single
microphone. It requires phone Bluetooth/audio permissions and explicit audio
routing; do not mix its permission model with projected-context recording.

## Camera

Create `ProcessCameraProvider` with the projected context. Within that context,
`CameraSelector.DEFAULT_BACK_CAMERA` maps to the glasses' outward camera.
Check `hasCamera`, choose a supported resolution, bind `ImageCapture` or
`VideoCapture` to a lifecycle owner, and unbind on teardown.

Conservative glasses targets:

| Use case | Resolution | Frame rate |
|---|---:|---:|
| Video communication | 1280×720 | 15 FPS |
| Computer vision | 640×480 | 10 FPS |
| AI video streaming | 640×480 | 1 FPS |

Higher resolution and frame rates increase battery and thermal pressure. Test
on the target hardware rather than assuming phone-camera capabilities.
