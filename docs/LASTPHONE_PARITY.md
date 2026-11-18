# Lastphone functional coverage and acceptance

Lastphone's scope is the full Lastbook application. The Android shell embeds the
original route handler and web UI instead of maintaining a second subset of
endpoints. This document separates implementation coverage from proof that a
scenario has been exercised on a particular runtime.

## Shared implementation

The Gradle build packages `server.py`, `assistant.py`, `retrieval.py`, `llm_local.py`,
the `engine` package, `app/bridge.py`, `web` and `content` from the same repository
revision. It excludes development/vendor code and never imports anything from a
portable data folder. Library, topic, collection, video, map and assistant routes
dispatch through the original handler. Native-dependent operations are supplied
by explicit Android adapters.

| Contract | Verification |
| --- | --- |
| Catalog records, filters, shelves, collections, source metadata | Original core tests; Android fixture API and UI tests |
| FTS5/BM25, Unicode terms and page provenance | Actual Chaquopy SQLite search against a real FTS5 fixture |
| Semantic search, indexing and fusion | WordPiece differential tests; Android ONNX adapter tests; original retrieval implementation |
| PDF page/text/full-document view | Real PDF rendered through Android; PNG signature/content; invalid page rejection |
| Saved HTML and ZIM | Original rewriting/CSP; real archive bridge tests; foreign-network and script blocking |
| Video range requests, captions and seeking | Exact byte-range comparison, timed VTT and WebView media metadata/seek |
| Map layers and 3D buildings | Non-empty SQLite geometry and exact binary building payload |
| Tools and streaming chat | Authenticated POST through actual WebView transport; streamed responses; original tool loop/citation logic |
| Model inference | Real tiny GGUF through JNI; cancellation and subsequent reuse; ARM64 build and native library inspection |
| Setup/repair/download integrity | Original TLS, Range, checksum, interrupted download, traversal and rollback tests |
| Switching data folders | Engine request drain, native handle closure and cache reload; original folder remains intact |
| Android lifecycle | Activity recreation, persistent web storage and user-started foreground work |
| LAN sharing | Explicit start/stop; per-session key; original remote setup/update restrictions |
| APK contents | Signature, ABI, ZIP alignment, native ELF load alignments including embedded Python wheels |

## Physical cerro acceptance

Automated emulator results cover Android integration and CPU execution. Before
calling a release production-ready for the Nubia Z60 Ultra, run the same tests on
the physical device and record firmware, RAM, page size, model hash, backend and
context. The device tree alone is not evidence of the installed firmware or RAM.

Required measurements: cold/warm model load, prompt and generation token rates,
peak process/RSS and available memory, sustained generation under thermal load,
cancellation latency, background/foreground transitions, lock-screen behavior,
free-space exhaustion, airplane-mode cold start, real pack media codecs, 3D map
rendering and Vulkan fallback on driver failure. Keep thermal services enabled.

Full Small/Medium/All pack checks require the actual packs. A synthetic fixture
establishes the route/format contract and catches regressions; it is not a claim
that every document, codec or archive in hundreds of gigabytes has been opened.

## Release gates

The GitHub workflow fails if host adapter tests, Android compilation, lint, unit
tests, package validation or emulator instrumentation fails. The tested ARM64 APK
is uploaded only after required jobs pass. Reports are retained even when a job
fails. Physical-device performance figures are never filled in from emulator
measurements. Production signing is a separate release configuration.
