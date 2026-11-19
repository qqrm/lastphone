#!/usr/bin/env python3
"""Package the isolated ARM64 test app, its instrumentation, and the USB runner.

Build with assembleHardwareDebug assembleHardwareDebugAndroidTest first. This
checks signatures/identities and packages a test kit; it never reports a device pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
APPLICATION = "io.github.qqrm.lastphone.hardwaretest"
BASELINE_SHA = "bc18a4d0322056ee040ad3d0524460ca309117db"
BASELINE_APK_SHA256 = "e7371806d010ebaf3914cb5054c0f78b7fb4e1dabc929c59769adacdfba975fc"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(*args: object) -> str:
    return subprocess.check_output([str(item) for item in args], text=True, stderr=subprocess.STDOUT)


def certificate(apksigner: Path, apk: Path) -> str:
    result = command(apksigner, "verify", "--verbose", "--print-certs", apk)
    matches = re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$", result, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"Expected one verified signing certificate for {apk.name}")
    return matches[0].lower()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/lastphone-nubia-hardware-kit")
    parser.add_argument("--sdk", type=Path, default=os.environ.get("ANDROID_HOME"))
    parser.add_argument("--source-sha", help="Published commit containing this exact kit source")
    args = parser.parse_args()
    if args.sdk is None:
        parser.error("Set ANDROID_HOME or pass --sdk")
    if args.output.exists():
        parser.error("Output already exists; choose a new directory to preserve previous artifacts")
    source_sha = args.source_sha or command("git", "-C", ROOT, "rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        parser.error("source-sha must be a complete commit SHA")
    # A source override is useful when the exact tree was published through the
    # GitHub API. Record the source files separately so provenance is checkable.
    if not args.source_sha and command("git", "-C", ROOT, "status", "--porcelain").strip():
        parser.error("Commit the sources or provide the exact published --source-sha")
    app = ROOT / "mobile/android/app/build/outputs/apk/hardware/debug/app-hardware-debug.apk"
    tests = ROOT / "mobile/android/app/build/outputs/apk/androidTest/hardware/debug/app-hardware-debug-androidTest.apk"
    build_tools = args.sdk / "build-tools/36.0.0"
    app_cert = certificate(build_tools / "apksigner", app)
    if certificate(build_tools / "apksigner", tests) != app_cert:
        raise ValueError("Application and instrumentation must have the same signing certificate")
    from run_hardware_tests import read_apk_manifest
    # The same parser is used on Windows before any device installation.
    for apk, expected in ((app, APPLICATION), (tests, APPLICATION + ".test")):
        metadata = read_apk_manifest(apk)
        if metadata.get("package") != expected:
            raise ValueError(f"Unexpected APK identity: {apk.name}: {metadata}")
    test_metadata = read_apk_manifest(tests)
    if test_metadata.get("targetPackage") != APPLICATION:
        raise ValueError("The test APK must target only the isolated hardware application")

    args.output.mkdir(parents=True)
    app_name = "lastphone-hardware-debug.apk"
    test_name = "lastphone-hardware-debug-androidTest.apk"
    shutil.copy2(app, args.output / app_name)
    shutil.copy2(tests, args.output / test_name)
    copies = {
        "tools/android/run_hardware_tests.py": "run_hardware_tests.py",
        "tools/android/run_hardware_tests.ps1": "run_hardware_tests.ps1",
        "docs/NUBIA_HARDWARE_TEST.md": "README.md",
        "docs/NUBIA_HARDWARE_REPORT_TEMPLATE.md": "NUBIA_HARDWARE_REPORT_TEMPLATE.md",
        "docs/ANDROID_VALIDATION.md": "ANDROID_VALIDATION.md",
        "mobile/android/LICENSE": "LICENSE",
        "mobile/android/THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    }
    for source, name in copies.items():
        shutil.copy2(ROOT / source, args.output / name)
    validation = args.output / "ANDROID_VALIDATION.md"
    validation.write_text(validation.read_text(encoding="utf-8")
        .replace("../mobile/android/README.md", f"https://github.com/qqrm/lastphone/blob/{source_sha}/mobile/android/README.md")
        .replace("(LASTPHONE_PARITY.md)", f"(https://github.com/qqrm/lastphone/blob/{source_sha}/docs/LASTPHONE_PARITY.md)"),
        encoding="utf-8")
    source_paths = sorted(set(copies) | {
        "tools/android/package_hardware_kit.py", "mobile/android/app/build.gradle.kts",
        "mobile/android/app/src/hardware/AndroidManifest.xml",
        "mobile/android/app/src/androidTestHardware/java/org/lastphone/app/HardwareAcceptanceTest.kt",
    })
    manifest = {
        "schema": 1,
        "source_sha": source_sha,
        "source_url": f"https://github.com/qqrm/lastphone/tree/{source_sha}",
        "baseline_release": "v0.1.0-alpha.1",
        "baseline_source_sha": BASELINE_SHA,
        "baseline_apk_sha256": BASELINE_APK_SHA256,
        "provenance_note": "Separate ARM64 hardware-test build. This is not the byte-identical alpha release APK.",
        "hardware_validation": "not_run",
        "app": {"file": app_name, "sha256": digest(app), "package": APPLICATION},
        "test": {"file": test_name, "sha256": digest(tests), "package": APPLICATION + ".test"},
        "signing_certificate_sha256": app_cert,
        "source_files_sha256": {path: digest(ROOT / path) for path in source_paths},
    }
    (args.output / "hardware-test-bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    audit = command(sys.executable, ROOT / "tools/android/verify_apk.py", app,
                    "--sdk", args.sdk, "--abis", "arm64-v8a",
                    "--report", args.output / "apk-verification.json")
    if json.loads(audit)["errors"]:
        raise ValueError("ARM64 APK audit failed")
    checksums = "".join(f"{digest(path)}  {path.name}\n" for path in sorted(args.output.iterdir()) if path.is_file())
    (args.output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    archive = args.output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(args.output.iterdir()):
            bundle.write(path, path.name)
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size,
                      "sha256": digest(archive), "hardware_validation": "not_run"}, indent=2))


if __name__ == "__main__":
    main()
