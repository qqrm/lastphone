#!/usr/bin/env python3
"""Verify the signed APK, ZIP alignment, and every packaged native ELF library."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

ARCHITECTURES = {"arm64-v8a": "AArch64", "x86_64": "Advanced Micro Devices X86-64"}


def run(*args: object) -> str:
    return subprocess.check_output([str(arg) for arg in args], text=True, stderr=subprocess.STDOUT)


def native_files(package: zipfile.ZipFile, prefix: str = ""):
    """Inspect ELF magic, including versioned .so files and Chaquopy .imy ZIPs."""
    for entry in package.infolist():
        if entry.is_dir():
            continue
        name = prefix + entry.filename
        if name.endswith((".gguf", ".zim")):
            raise ValueError(f"The application APK embeds test or user data: {name}")
        with package.open(entry) as stream:
            magic = stream.read(4)
            if magic == b"\x7fELF":
                yield name, magic + stream.read()
            elif name.endswith((".imy", ".zip")):
                archive = io.BytesIO(magic + stream.read())
                with zipfile.ZipFile(archive) as nested:
                    yield from native_files(nested, name + "!/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--sdk", type=Path, default=os.environ.get("ANDROID_HOME"))
    parser.add_argument("--abis", default="arm64-v8a")
    parser.add_argument("--minimum-load-alignment", type=int, choices=(4096, 16384), default=16384,
                        help="16 KB for phone APKs; 4 KB only for the separate x86_64 emulator APK")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.sdk is None:
        parser.error("Set ANDROID_HOME or pass --sdk")
    expected_abis = set(args.abis.split(","))
    if not expected_abis or not expected_abis <= ARCHITECTURES.keys():
        parser.error("Only the supported ARM64 and x86_64 ABIs may be selected")
    if args.minimum_load_alignment < 16384 and expected_abis != {"x86_64"}:
        parser.error("Only the separate x86_64 emulator APK may use 4 KB alignment")
    build_tools = args.sdk / "build-tools/36.0.0"
    readelf = args.sdk / "ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"
    apk = args.apk.resolve()
    run(build_tools / "zipalign", "-c", "-P", "16", "4", apk)
    signature = run(build_tools / "apksigner", "verify", "--verbose", "--print-certs", apk)
    report = {"apk": str(apk), "zip_alignment_kib": 16,
              "required_load_alignment": args.minimum_load_alignment,
              "signature": signature.strip(), "libraries": [], "errors": []}
    with tempfile.TemporaryDirectory(prefix="lastphone-apk-") as temporary, zipfile.ZipFile(apk) as package:
        actual_abis = set()
        for index, (name, data) in enumerate(native_files(package)):
            path = Path(temporary) / f"library-{index}.so"
            path.write_bytes(data)
            tagged_abis = {abi for abi in ARCHITECTURES if abi in name}
            if len(tagged_abis) != 1:
                raise ValueError(f"Packaged ELF lacks an unambiguous ABI directory or archive: {name}")
            abi = tagged_abis.pop()
            actual_abis.add(abi)
            header = run(readelf, "--file-header", path)
            if ARCHITECTURES[abi] not in header:
                report["errors"].append(f"Wrong ELF architecture: {name}")
            program_headers = run(readelf, "--program-headers", "--wide", path)
            alignments = [int(line.split()[-1], 16) for line in program_headers.splitlines()
                          if line.lstrip().startswith("LOAD ")]
            if not alignments or min(alignments) < args.minimum_load_alignment:
                report["errors"].append(f"Insufficient ELF page alignment: {name}: {alignments}")
            report["libraries"].append({"path": name, "abi": abi, "bytes": len(data),
                                        "sha256": hashlib.sha256(data).hexdigest(),
                                        "minimum_load_alignment": min(alignments) if alignments else None})
            path.unlink()
        if actual_abis != expected_abis:
            report["errors"].append(f"APK ABIs {actual_abis} differ from expected {expected_abis}")
    rendered = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered, end="")
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
