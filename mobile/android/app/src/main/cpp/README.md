# Android native adapters

The inference backend uses the same pinned llama.cpp source as the original
iPad port: `dde879bc4f954b809892fd0d660515f42536394a`.
Run `mobile/ipad/scripts/prepare_dependencies.sh` from the repository root
before building. CMake checks the exact commit and rejects edits to that source
checkout.

The Android build links the actual CPU and Vulkan implementations. Vulkan uses
the NDK shader compiler and pinned Khronos headers, not a mock backend. Runtime
capabilities list detected devices; selecting an unavailable Vulkan device fails
explicitly. Android settings can select CPU instead.

## Bounded build concurrency

The pinned upstream Vulkan generator launches up to 16 compiler children for
each shader custom command, and Ninja can run many custom commands together.
This exhausts the RAM of smaller CI hosts. `cmake/prepare_bounded_ggml.cmake`
creates a build-local copy of the pinned `ggml` directory and applies two narrow
build changes:

- Vulkan shader custom commands share a Ninja pool of one generator.
- A generator launches at most `LASTPHONE_SHADER_JOBS` shader compilers
  (default: two).

The ordinary native compiler pool is `LASTPHONE_NATIVE_JOBS` (default: two),
with one concurrent linker. All Vulkan shader variants and runtime code remain
unchanged. The original pinned checkout remains unmodified; the generated copy
and compiled outputs are ignored build artifacts. The patch validates its
expected source anchors and fails if a dependency upgrade changes them. The
copied upstream code retains its MIT license and notices.

## Verification

Android instrumentation tests use a checksum-pinned TinyStories GGUF for actual
CPU decode, prefix reuse, cancellation, Unicode input, model-load errors and
backend switching. The Vulkan branch runs real inference when a Vulkan device
is exposed; otherwise it records the unavailable-device branch and verifies CPU
fallback. This does not substitute for testing Adreno/Mali driver compatibility,
sustained temperature or throughput on a physical phone.

The content tests render a generated two-page PDF with Android's renderer, read
a checksum-pinned real ZIM through libzim, and execute a small deterministic ONNX
graph through ONNX Runtime to verify CLS selection and normalization. Test
fixture provenance and download checksums are in
`tools/android/fetch_test_fixtures.py`.
