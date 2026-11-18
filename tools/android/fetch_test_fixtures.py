#!/usr/bin/env python3
"""Fetch and verify small upstream fixtures used only by instrumentation tests."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DESTINATION = ROOT / "mobile/android/app/src/androidTest/assets"
FIXTURES = (
    {
        "path": "models/stories260K.gguf",
        "url": "https://huggingface.co/ggml-org/models/resolve/499bc8821c6b12b4e53c5bffcb21ec206f212d81/tinyllamas/stories260K.gguf",
        "sha256": "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d",
        "size": 1_185_376,
    },
    {
        "path": "collections/small.zim",
        "url": "https://raw.githubusercontent.com/kiwix/java-libkiwix/379e3f067340ba27ed12c14af41d3106c2344a3e/lib/src/test/small.zim",
        "sha256": "39efd51432536669175c3a40b454571df9d69a9e8426810b48d748ee81054417",
        "size": 66_937,
    },
)


def verify(path: Path, fixture: dict) -> None:
    if path.stat().st_size != fixture["size"]:
        raise ValueError(f"Unexpected test fixture size: {path}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != fixture["sha256"]:
        raise ValueError(f"Test fixture SHA-256 mismatch: {path}: {actual}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION,
                        help="Instrumentation asset directory")
    args = parser.parse_args()
    for fixture in FIXTURES:
        destination = args.destination / fixture["path"]
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".part")
            try:
                with urllib.request.urlopen(fixture["url"], timeout=60) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                verify(temporary, fixture)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        verify(destination, fixture)
        print(f"Verified test fixture: {destination} ({fixture['size']} bytes; sha256:{fixture['sha256']})")


if __name__ == "__main__":
    main()
