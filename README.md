# Lastphone

**Lastbook on Android: a portable offline library, maps, videos and an on-device assistant.**

Lastphone embeds the original Lastbook Python engine and web interface in an Android APK. It connects the platform-specific pieces to Android PDF rendering, libkiwix, ONNX Runtime and an in-process llama.cpp runtime. Reading, search, maps and AI work without a server, Termux, root or a custom ROM.

The first target is the Nubia Z60 Ultra (`cerro`, Snapdragon 8 Gen 3), with an ARM64 Android build. The device tree supplied for this project is [qqrm/android_device_nubia_cerro](https://github.com/qqrm/android_device_nubia_cerro). The app runs within Android; it does not flash or change the phone's operating system.

## Status and delivery

**[Download Lastphone 0.1.0 alpha 1 for ARM64](https://github.com/qqrm/lastphone/releases/tag/v0.1.0-alpha.1).** The release contains the APK, SHA-256 and verification reports from [successful build 44325291474](https://github.com/qqrm/lastphone/actions/runs/44325291474), source revision `bc18a4d0322056ee040ad3d0524460ca309117db`. All **18 Android instrumentation tests** passed with networking disabled. See the [exact validation results and limits](docs/ANDROID_VALIDATION.md).

Implementation and automated validation are tracked in [Android build and device tests](https://github.com/qqrm/lastphone/actions/workflows/android.yml). A successful run produces the **tested ARM64 APK**, its SHA-256 and test reports. Android emulator tests exercise real JNI inference and native document handling. GPU speed, thermal behavior and battery use require measurements on the physical phone; an emulator result does not establish those properties.

The initial artifact is a **debug-signed development build**. A production release must use a persistent owner-controlled signing key; the repository does not contain a private signing key. See [Android build and validation](mobile/android/README.md) and [functional coverage](docs/LASTPHONE_PARITY.md).

## Install with a portable folder

1. Install the ARM64 APK on Android 10 or newer.
2. Copy an existing Lastbook data folder to the phone, or use **Settings → Library downloads and repair** to install a published pack.
3. Choose **Settings → Open a library folder**. **Use this folder in place** keeps it portable and requests Android file access. **Import a separate copy** works through Android's document picker and needs space for the copy.
4. Choose **Settings → Local AI** to import/select a GGUF model, set its context and CPU threads, and select CPU or Vulkan. Library use does not require a model.

A compatible folder looks like this:

```text
LastphoneData/
  library-data/
    catalog.json
    search.sqlite
    documents/ …
    pages/ …
    videos/ …
    transcripts/ …
  derived/
    geo/ …
    models/bge-small-en-v1.5/ …
    index/ …
  models/
    your-model.gguf
```

`LastbookData` folders retain their original name and format. Imported program code is never executed. An app-managed copy is deleted by Android if you uninstall the app; keep your original portable folder or a separate backup. Existing library packs are large, and installer staging requires additional free space: the app computes the requirement before starting.

## What is carried over

| Area | Android implementation |
| --- | --- |
| Library, guides, shelves, collections, reading history, bookmarks | Original Lastbook engine and UI |
| Keyword search, trust weighting, citations and tool loop | Original FTS5/BM25, assistant and retrieval code |
| Semantic search | Original index and reciprocal-rank fusion; BGE WordPiece tokenizer and Android ONNX Runtime |
| PDFs and archived pages | Android page rendering; original text and sanitized page routes |
| ZIM encyclopedias | libkiwix/libzim through the original archive routes |
| Videos and captions | Local range requests and original HTML media player, transcript search and timed links |
| Maps, address search, terrain, aerial imagery and 3D buildings | Original SQLite data formats and original route implementations |
| Local AI | In-process llama.cpp, selected GGUF, CPU/Vulkan, cancellation, serialized model ownership and idle unload |
| Setup, resumable downloads, checksums, repair and rollback | Original installer with Android folder/lifecycle integration |
| Wi-Fi library sharing | Explicitly started foreground service and per-session access link |
| Updates | Manual check of this repository's Android releases |

Data coverage is inherited from the installed packs. A port cannot add countries, transcripts, image-only PDF text or missing source material that the original pack does not contain. Different Android devices also support different media codecs.

## Privacy and execution model

The WebView uses an intercepted local HTTPS origin; no listening port is opened for normal app use. Native messages are accepted only from its main frame. Library pages receive the original sandbox/content-security policy, foreign WebView requests are blocked, and POST requests retain the original per-launch key checks.

Network work is explicit: downloads, a manual update check, or user-started Wi-Fi sharing. There is no analytics, boot service, periodic update job or model-provider fallback. CPU/GPU inference stays in the APK process. A foreground notification lets the user stop ongoing work without deleting downloaded bytes.

## Build and test

The Android project is under [`mobile/android`](mobile/android). Toolchain versions, dependency revisions and test-fixture hashes are pinned. A bootstrap script installs the public vendor toolchain in a directory you choose:

```bash
python3 tools/android/bootstrap_linux.py --help
bash mobile/ipad/scripts/prepare_dependencies.sh
python3 tools/android/fetch_test_fixtures.py
python3 tools/android/make_fixture.py
cd mobile/android
./gradlew assemblePhoneDebug assembleEmulatorDebug assembleEmulatorDebugAndroidTest \
  lintPhoneDebug testPhoneDebugUnitTest
./gradlew connectedEmulatorDebugAndroidTest
```

The model, PDF, video, ZIM and SQLite fixtures used by tests are kept out of the application APK. Tests do not substitute a fake inference engine for the native smoke test. See the Android README for the SDK, Python environment, hardware test command and signing configuration.

## Origin and licenses

Based on [Immortal-Protocols/lastbook](https://github.com/Immortal-Protocols/lastbook), revision `9542e80fbdb6e6ee2a0472beabab0bbd706f0c15`. Original documentation is preserved as [README.upstream.md](README.upstream.md). The upstream Android preview [PR #200](https://github.com/Immortal-Protocols/lastbook/pull/200), revision `434673fce63f5bb4e833a5651688564d5c3e02ff`, supplied a useful manifest snapshot and Gradle wrapper reference; Lastphone uses the shared Python engine rather than that preview's partial Kotlin engine.

Original Lastbook code retains its [MIT license](LICENSE). The combined Android distribution and new Android integration are [GPL-3.0-or-later](mobile/android/LICENSE), because libkiwix is GPL software. Dependencies, data and model weights retain their own licenses; the MIT license does not cover all library content. See [Android third-party notices](mobile/android/THIRD_PARTY_NOTICES.md) and the upstream [notices](THIRD_PARTY_NOTICES.md).
