#!/usr/bin/env python3
"""Install the locked Android toolchain in a user-owned Linux/WSL directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile


def download(spec: dict, downloads: Path) -> Path:
    path = downloads / Path(urllib.parse.urlparse(spec["url"]).path).name
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".part")
        try:
            print(f"Downloading {path.name}", flush=True)
            with urllib.request.urlopen(spec["url"], timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != spec["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {path.name}: {actual}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.home() / ".cache/lastphone-toolchain")
    parser.add_argument("--accept-sdk-licenses", action="store_true",
                        help="Accept Android SDK licenses for this development installation")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("This bootstrap supports Linux x86_64, including WSL2. Use Android Studio on other hosts.")
    versions = json.loads(Path(__file__).with_name("toolchain.lock.json").read_text())
    base = args.directory.expanduser().resolve()
    downloads = base / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    java = base / versions["jdk"]["directory"]
    if not (java / "bin/javac").is_file():
        archive = download(versions["jdk"], downloads)
        with tarfile.open(archive) as package:
            package.extractall(base, filter="data")
    sdk = base / "sdk"
    command_tools = sdk / "cmdline-tools" / versions["command_line_tools"]["version"]
    if not (command_tools / "bin/sdkmanager").is_file():
        archive = download(versions["command_line_tools"], downloads)
        parent = command_tools.parent
        parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                if not (parent / member.filename).resolve().is_relative_to(parent.resolve()):
                    raise ValueError(f"Unsafe archive member: {member.filename}")
            package.extractall(parent)
        (parent / "cmdline-tools").rename(command_tools)
        for executable in (command_tools / "bin").iterdir():
            executable.chmod(executable.stat().st_mode | 0o111)
    environment = os.environ | {"JAVA_HOME": str(java), "ANDROID_HOME": str(sdk)}
    subprocess.run([str(java / "bin/javac"), "-version"], check=True, env=environment)
    command = [str(command_tools / "bin/sdkmanager"), f"--sdk_root={sdk}", *versions["sdk_packages"]]
    subprocess.run(command, check=True, env=environment, text=True,
                   input="y\n" * 100 if args.accept_sdk_licenses else None)
    setup = base / "env.sh"
    setup.write_text(
        f"export JAVA_HOME={shlex.quote(str(java))}\n"
        f"export ANDROID_HOME={shlex.quote(str(sdk))}\n"
        "export CMAKE_BUILD_PARALLEL_LEVEL=2\n"
        f"export PATH={shlex.quote(str(java / 'bin'))}:{shlex.quote(str(sdk / 'cmake/3.22.1/bin'))}:"
        f"{shlex.quote(str(sdk / 'platform-tools'))}:\"$PATH\"\n"
    )
    print(f"Ready. Run: source {shlex.quote(str(setup))}")
    print("Then: cd mobile/android && ./gradlew assemblePhoneDebug lintPhoneDebug testPhoneDebugUnitTest")


if __name__ == "__main__":
    main()
