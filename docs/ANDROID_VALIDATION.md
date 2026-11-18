# Lastphone Android validation — 0.1.0 alpha 1

Validated on 2026-11-18. The installable preview is published in
[release v0.1.0-alpha.1](https://github.com/qqrm/lastphone/releases/tag/v0.1.0-alpha.1).

## Exact source and binary

- Tested source: [`bc18a4d0322056ee040ad3d0524460ca309117db`](https://github.com/qqrm/lastphone/commit/bc18a4d0322056ee040ad3d0524460ca309117db).
- Successful build and device tests: [run 44325291474](https://github.com/qqrm/lastphone/actions/runs/44325291474).
- Successful publication: [run 44326656996](https://github.com/qqrm/lastphone/actions/runs/44326656996).
- APK: `lastphone-0.1.0-alpha.1-arm64.apk`, **111,031,505 bytes**.
- APK SHA-256: `e7371806d010ebaf3914cb5054c0f78b7fb4e1dabc929c59769adacdfba975fc`.
- Application ID: `io.github.qqrm.lastphone`; Android 10/API 29 minimum, compile/target API 36.
- Signing: Android development certificate, valid APK Signature Scheme v2.

The publication workflow downloaded the successful build's ARM64 artifact,
verified its checksum, and attached that exact APK to the source-pinned tag.
The release also contains `SHA256SUMS` and `lastphone-verification-reports.zip`.
The report archive includes JUnit XML/HTML, lint reports, recursive APK audits,
device properties, per-test screenshots and guest/host diagnostics.

## Results

| Check | Result |
| --- | --- |
| Original Lastbook Python regression suite | 335 run: **331 passed, 4 skipped** |
| Android Python adapter suite | **23 passed** |
| Web route and Android branding regressions | **9 passed** |
| JVM unit tests, phone flavor | **3 passed** |
| JVM unit tests, emulator flavor | **3 passed** |
| Android instrumentation | **18 passed, 0 failed, 0 skipped** |
| Both APK builds and Android test APK | Passed |
| Lint, both flavors | Passed |
| ARM64 recursive ELF inspection | **97 ELF files**, ARM64 only, minimum PT_LOAD alignment **16 KiB** |
| x86_64 recursive ELF inspection | **97 ELF files**, x86_64 only, minimum PT_LOAD alignment **4 KiB** |
| APK signature and ZIP alignment | Both valid; ZIP alignment **16 KiB** |

Android tests ran on API 36/x86_64 with 4096-byte memory pages, airplane mode
enabled and Wi-Fi/mobile data disabled. Both native inference and the Python
engine ran inside the installed APK. No model-service substitute was used for
the actual GGUF tests.

## What the Android tests exercised

- Actual FTS5 search, including Cyrillic queries, and original catalog/page routes.
- Android PDF geometry and PNG rendering, plus the document viewer response.
- Real ZIM archive access and the libkiwix bridge.
- Map geometry and the exact binary 3D-building payload from SQLite fixtures.
- Exact video byte ranges, media metadata and seeking through timed VTT cues.
- Authenticated JavaScript POST requests and clean completion of streamed errors.
- Real CPU GGUF generation through Python, Java and C++; exact Cyrillic/CJK/emoji/NUL round trips in token events and return values.
- Native cancellation, subsequent generation, prompt reuse and invalid-model errors.
- Actual ONNX execution and normalized 384-dimensional results, including the original Python embedder crossing the Java array boundary.
- HTML sandbox policy, foreign-request blocking and traversal rejection.
- Activity recreation, persistent WebView storage and offline search.
- Real Python `None`/Java `null` command handling and LAN start/stop.

The tests found and fixed stale asynchronous page updates and the incorrect
non-null Kotlin contract for Python commands returning `None`. Regression tests
retain the original security assertions and document ticket's single-use rule.

## Vulkan result and physical-device boundary

CPU and Vulkan backends are compiled into both ABI builds. This emulator used
Lavapipe/ANGLE for graphics, but llama.cpp excludes Vulkan devices classified as
CPU by default. Its capability report therefore listed only the CPU backend.
The Vulkan test exercised the **explicit unavailable-device error and subsequent
CPU generation**. It did **not** execute LLM inference on a Vulkan device.

No physical Nubia Z60 Ultra was connected. Adreno Vulkan execution, throughput,
thermals, battery use, sustained large-model memory pressure, actual 16 KiB
ARM64 execution and full content-pack coverage remain device acceptance work.
Binary alignment checks are not a substitute for execution on a 16 KiB device.
The small GGUF, ONNX, ZIM and library fixtures validate integration contracts;
they are not benchmarks of production model quality or exhaustive pack tests.

Two prior emulator runs lost the device during the video scenario under the
legacy SwiftShader configuration. Host/guest diagnostics did not show memory
exhaustion or an application fatal signal. The supported Lavapipe configuration
completed the full suite; no tests or Vulkan code were disabled. The exact
earlier emulator crash cause was not established.

For physical acceptance commands and production signing, see
[Android build and validation](../mobile/android/README.md) and
[functional coverage](LASTPHONE_PARITY.md). Preserve the external data folder
when uninstalling a development build; app-managed copies are removed by Android.
