#!/usr/bin/env python3
"""Run the isolated Lastphone hardware test APK on an explicitly selected Nubia.

Requires Python 3.10+ and Android platform-tools. No root, Gradle, SDK build tools,
network download, phone reset, package uninstall, or radio-setting change is used.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any
import uuid
import zipfile

APP = "io.github.qqrm.lastphone.hardwaretest"
TEST_APP = APP + ".test"
RUNNER = TEST_APP + "/androidx.test.runner.AndroidJUnitRunner"
HARDWARE_CLASS = "org.lastphone.app.HardwareAcceptanceTest"
SMOKE_CLASSES = ",".join((
    "org.lastphone.app.EngineIntegrationTest",
    "org.lastphone.app.nativebridge.NativeAdaptersTest",
    "org.lastphone.app.nativebridge.NativeEmbeddingsTest",
    HARDWARE_CLASS + "#hardwareSmoke",
))
PROPERTIES = (
    "ro.product.manufacturer", "ro.product.brand", "ro.product.model",
    "ro.product.device", "ro.product.vendor.device", "ro.product.cpu.abi",
    "ro.product.cpu.abilist", "ro.soc.manufacturer", "ro.soc.model",
    "ro.board.platform", "ro.build.version.release", "ro.build.version.sdk",
    "ro.build.version.security_patch", "ro.build.fingerprint", "ro.kernel.qemu",
    "ro.boot.qemu",
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Blocked(RuntimeError):
    """A prerequisite was not satisfied; no hardware pass is possible."""


class HardwareUnavailable(Blocked):
    """The selected physical device cannot be reached through ADB."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _string_pool(data: bytes, base: int, header_size: int, size: int) -> list[str]:
    if header_size < 28 or size < header_size:
        raise ValueError("Malformed APK manifest string pool")
    count, _, flags, start, _ = struct.unpack_from("<IIIII", data, base + 8)
    if count > 100000 or header_size + count * 4 > size:
        raise ValueError("Malformed APK manifest string count")
    utf8 = bool(flags & 0x100)
    strings: list[str] = []

    def length(offset: int, wide: bool) -> tuple[int, int]:
        unit = 2 if wide else 1
        if offset + unit > base + size:
            raise ValueError("Truncated APK manifest string")
        first = int.from_bytes(data[offset:offset + unit], "little")
        offset += unit
        high = 0x8000 if wide else 0x80
        if first & high:
            if offset + unit > base + size:
                raise ValueError("Truncated APK manifest length")
            second = int.from_bytes(data[offset:offset + unit], "little")
            offset += unit
            first = ((first & ~high) << (16 if wide else 8)) | second
        return first, offset

    for index in range(count):
        relative = struct.unpack_from("<I", data, base + header_size + index * 4)[0]
        offset = base + start + relative
        if offset < base + header_size or offset >= base + size:
            raise ValueError("APK manifest string outside pool")
        text_length, offset = length(offset, not utf8)
        if utf8:
            text_length, offset = length(offset, False)
        byte_length = text_length if utf8 else text_length * 2
        if offset + byte_length > base + size:
            raise ValueError("Truncated APK manifest text")
        strings.append(data[offset:offset + byte_length].decode("utf-8" if utf8 else "utf-16-le"))
    return strings


def read_apk_manifest(path: Path) -> dict[str, str]:
    """Read package and instrumentation identity from Android binary XML.

    This deliberately small reader avoids requiring Android SDK build tools on
    the user's PC. Android itself verifies APK signatures during installation.
    """
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("AndroidManifest.xml")
        if info.file_size > 2 * 1024 * 1024:
            raise ValueError("APK manifest exceeds the inspection limit")
        data = archive.read(info)
    if len(data) < 8 or struct.unpack_from("<H", data)[0] != 3:
        raise ValueError("APK does not contain an Android binary XML manifest")
    _, header, total = struct.unpack_from("<HHI", data)
    if header < 8 or total != len(data):
        raise ValueError("Malformed APK manifest header")
    strings: list[str] = []
    identity: dict[str, str] = {}
    offset = header
    while offset < total:
        if offset + 8 > total:
            raise ValueError("Truncated APK manifest chunk")
        kind, chunk_header, size = struct.unpack_from("<HHI", data, offset)
        if chunk_header < 8 or size < chunk_header or offset + size > total:
            raise ValueError("Malformed APK manifest chunk")
        if kind == 1:
            strings = _string_pool(data, offset, chunk_header, size)
        elif kind == 0x102:
            if size < 36:
                raise ValueError("Truncated APK manifest element")
            name_index = struct.unpack_from("<I", data, offset + 20)[0]
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, offset + 24)
            first = offset + 16 + attr_start
            if attr_size < 20 or first + attr_count * attr_size > offset + size:
                raise ValueError("Malformed APK manifest attributes")
            element = strings[name_index]
            for number in range(attr_count):
                attr = first + number * attr_size
                key_index, raw = struct.unpack_from("<II", data, attr + 4)
                value_type = data[attr + 15]
                value_data = struct.unpack_from("<I", data, attr + 16)[0]
                value = strings[raw] if raw != 0xFFFFFFFF else (
                    strings[value_data] if value_type == 3 else str(value_data)
                )
                key = strings[key_index]
                if element == "manifest" and key == "package":
                    identity["package"] = value
                elif element == "instrumentation" and key in ("name", "targetPackage"):
                    identity[key] = value
        offset += size
    return identity


def verify_bundle(manifest_path: Path) -> tuple[dict[str, Any], Path, Path]:
    """Hash both APKs and check actual IDs before invoking adb install."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != 1:
            raise ValueError("Unsupported bundle manifest schema (expected schema: 1)")
        if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_sha", "")):
            raise ValueError("Bundle must record its exact source_sha")
        verified: list[Path] = []
        for key, expected in (("app", APP), ("test", TEST_APP)):
            entry = manifest[key]
            name = entry["file"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.apk", name):
                raise ValueError("APK filenames must be plain .apk basenames inside the bundle")
            if entry.get("package") != expected or not SHA256.fullmatch(entry.get("sha256", "")):
                raise ValueError(f"Invalid {key} package identity or SHA-256 pin")
            path = manifest_path.resolve().parent / name
            if sha256_file(path) != entry["sha256"]:
                raise ValueError(f"SHA-256 mismatch for {name}; do not install this bundle")
            identity = read_apk_manifest(path)
            if identity.get("package") != expected:
                raise ValueError(f"APK package {identity.get('package')!r} is not isolated package {expected}")
            if key == "test" and (identity.get("targetPackage") != APP or
                                  identity.get("name") != "androidx.test.runner.AndroidJUnitRunner"):
                raise ValueError("The test APK does not target the isolated hardware application")
            verified.append(path)
        return manifest, verified[0], verified[1]
    except (OSError, ValueError, KeyError, TypeError, IndexError, struct.error, zipfile.BadZipFile) as error:
        raise Blocked(f"Invalid hardware test bundle: {error}") from error


def validate_device(properties: dict[str, str], page_size: str, serial: str) -> dict[str, Any]:
    if serial.startswith("emulator-") or any(properties.get(key) == "1" for key in ("ro.kernel.qemu", "ro.boot.qemu")):
        raise Blocked("An emulator cannot satisfy Nubia hardware acceptance")
    abi = properties.get("ro.product.cpu.abi", "")
    if abi != "arm64-v8a":
        raise Blocked(f"Expected ARM64 phone, found {abi!r}")
    identity = " ".join(properties.get(key, "") for key in (
        "ro.product.model", "ro.product.device", "ro.product.vendor.device",
    )).lower()
    if not re.search(r"\bnx721j(?:\b|_)|\bcerro\b", identity):
        raise Blocked("Selected device is not the expected Nubia NX721J/cerro")
    try:
        api = int(properties.get("ro.build.version.sdk", "0"))
        pages = int(page_size.strip())
    except ValueError as error:
        raise Blocked("Cannot read Android API level or memory page size") from error
    if api < 29:
        raise Blocked(f"Android API {api} is below the supported minimum 29")
    if pages not in (4096, 16384):
        raise Blocked(f"Unrecognized ARM64 page size: {pages}")
    return {"serial": serial, "properties": properties, "api_level": api,
            "page_size_bytes": pages, "execution_on_16k_device": pages == 16384}


def parse_instrumentation(text: str, returncode: int, expected: int) -> dict[str, Any]:
    """ADB exit 0 is not proof of instrumentation success."""
    codes = [int(value) for value in re.findall(r"^INSTRUMENTATION_STATUS_CODE:\s*(-?\d+)\s*$", text, re.M)]
    final_codes = re.findall(r"^INSTRUMENTATION_CODE:\s*(-?\d+)\s*$", text, re.M)
    counts = [int(value) for value in re.findall(r"^OK \((\d+) tests?\)\s*$", text, re.M)]
    expected_counts = [int(value) for value in re.findall(r"^INSTRUMENTATION_STATUS: numtests=(\d+)\s*$", text, re.M)]
    failures: list[str] = []
    if returncode != 0:
        failures.append(f"ADB exited with code {returncode}")
    if any(code not in (0, 1, 2) for code in codes):
        failures.append("A test failed, errored, was ignored, or was skipped")
    if re.search(r"FAILURES!!!|INSTRUMENTATION_FAILED|INSTRUMENTATION_ABORTED|Process crashed|^Error:", text, re.M):
        failures.append("Instrumentation reported failure or process death")
    if final_codes != ["-1"]:
        failures.append("Missing or unsuccessful final instrumentation result")
    if counts != [expected] or codes.count(0) != expected:
        failures.append(f"Expected exactly {expected} completed passing tests")
    if not expected_counts or any(count != expected for count in expected_counts):
        failures.append("Instrumentation's declared test count differs from the selected suite")
    records: dict[str, str] = {}
    completed: list[str] = []
    for line in text.splitlines():
        match = re.match(r"INSTRUMENTATION_STATUS: (class|test)=(.*)", line)
        if match:
            records[match[1]] = match[2]
        if line.startswith("INSTRUMENTATION_STATUS_CODE:"):
            code = int(line.split(":", 1)[1])
            if code == 0 and "class" in records and "test" in records:
                completed.append(records["class"] + "#" + records["test"])
            records = {}
    if len(completed) != expected or len(set(completed)) != expected:
        failures.append("Missing or duplicate completed test identities")
    return {"passed": not failures, "expected_tests": expected, "completed_tests": completed,
            "reported_counts": counts, "failures": failures}


class Adb:
    def __init__(self, executable: str, serial: str):
        self.executable = executable
        self.serial = serial

    def command(self, *args: str) -> list[str]:
        return [self.executable, "-s", self.serial, *args]

    def run(self, *args: str, timeout: float = 15, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(self.command(*args), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        if check and result.returncode:
            raise RuntimeError((result.stdout + result.stderr).strip() or f"ADB failed: {args[0]}")
        return result

    def shell(self, *args: str, timeout: float = 15, check: bool = True) -> subprocess.CompletedProcess[str]:
        # adb shell joins arguments into a remote shell command. Quote there too;
        # subprocess shell=False alone does not protect a remote command.
        return self.run("shell", "-T", shlex.join(args), timeout=timeout, check=check)


def probe(adb: Adb, *args: str, timeout: float = 10) -> dict[str, Any]:
    try:
        result = adb.shell(*args, timeout=timeout, check=False)
        return {"ok": result.returncode == 0, "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)}


def inventory(adb: Adb, output: Path) -> dict[str, Any]:
    try:
        state = adb.run("get-state", timeout=15)
        if state.stdout.strip() != "device":
            raise HardwareUnavailable("Selected device is not authorized and online")
        result = adb.shell("getprop", timeout=15)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise HardwareUnavailable(f"Cannot access the selected USB device {adb.serial}: {error}") from error
    all_properties = dict(re.findall(r"^\[([^]]+)\]: \[(.*)\]\s*$", result.stdout, re.M))
    properties = {key: all_properties.get(key, "") for key in PROPERTIES}
    page_size = adb.shell("getconf", "PAGE_SIZE").stdout.strip()
    device = {"serial": adb.serial, "properties": properties, "page_size_reported": page_size}
    write_json(output / "device.json", device)
    device.update(validate_device(properties, page_size, adb.serial))
    current_user = adb.shell("am", "get-current-user").stdout.strip()
    device["android_user"] = current_user
    write_json(output / "device.json", device)
    if current_user != "0":
        raise Blocked("Run the test from Android's owner profile (user 0). Secondary/work-profile execution is not supported by this runner.")
    radio = {}
    for key in ("airplane_mode_on", "wifi_on", "mobile_data"):
        radio[key] = adb.shell("settings", "get", "global", key).stdout.strip()
    device["radio_settings"] = radio
    device["offline_radios_ready"] = radio == {"airplane_mode_on": "1", "wifi_on": "0", "mobile_data": "0"}
    device["connectivity"] = probe(adb, "dumpsys", "connectivity")
    write_json(output / "device.json", device)
    return device


def offline_after_run(adb: Adb) -> dict[str, Any]:
    """Record the end state too; a radio change must not leave an offline PASS."""
    state: dict[str, Any] = {"passed": False}
    try:
        state["radio_settings"] = {key: adb.shell("settings", "get", "global", key).stdout.strip()
                                  for key in ("airplane_mode_on", "wifi_on", "mobile_data")}
        state["connectivity"] = probe(adb, "dumpsys", "connectivity")
        active = re.search(r"Active default network:\s*([^\r\n]+)", state["connectivity"].get("stdout", ""))
        state["active_default_network"] = active[1].strip() if active else "unavailable"
        state["passed"] = (
            state["radio_settings"] == {"airplane_mode_on": "1", "wifi_on": "0", "mobile_data": "0"}
            and state["connectivity"]["ok"] and active is not None
            and state["active_default_network"].lower() in ("none", "null", "0", "-1")
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        state["error"] = str(error)
    return state


def snapshots(adb: Adb, output: Path, phase: str) -> None:
    folder = output / "snapshots"
    folder.mkdir(exist_ok=True)
    commands = {
        "memory": ("cat", "/proc/meminfo"), "app-memory": ("dumpsys", "meminfo", APP),
        "battery": ("dumpsys", "battery"), "thermal": ("dumpsys", "thermalservice"),
        "webview": ("dumpsys", "webviewupdate"), "vulkan": ("cmd", "gpu", "vkjson"),
        "app-exits": ("dumpsys", "activity", "exit-info", APP),
    }
    for name, args in commands.items():
        write_json(folder / f"{phase}-{name}.json", probe(adb, *args))


def telemetry(adb: Adb, output: Path, stop: threading.Event) -> None:
    started = time.monotonic()
    with (output / "telemetry.jsonl").open("w", encoding="utf-8") as stream:
        while not stop.is_set():
            sample = {"timestamp": utc_now(), "elapsed_seconds": round(time.monotonic() - started, 3)}
            for name, command in (
                ("app_memory", ("dumpsys", "meminfo", APP)),
                ("battery", ("dumpsys", "battery")),
                ("thermal", ("dumpsys", "thermalservice")),
            ):
                if stop.is_set():
                    break
                sample[name] = probe(adb, *command, timeout=5)
            stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
            stream.flush()
            stop.wait(10)


def install(adb: Adb, app: Path, test: Path, output: Path) -> None:
    for name, path in (("app", app), ("test", test)):
        print(f"Installing isolated {name} APK: {path.name}", flush=True)
        result = adb.run("install", "-r", "-t", str(path), timeout=300, check=False)
        (output / f"install-{name}.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode or "Success" not in result.stdout.splitlines():
            raise Blocked(f"Isolated {name} APK installation failed. See install-{name}.txt. "
                          "Signature conflicts require an owner decision; no package was uninstalled.")
    instruments = adb.shell("pm", "list", "instrumentation").stdout
    expected = f"instrumentation:{RUNNER} (target={APP})"
    if expected not in instruments.splitlines():
        raise Blocked("Installed instrumentation does not target the isolated app")


def rotate_evidence(adb: Adb, run_id: str) -> None:
    adb.shell("am", "force-stop", APP)
    for folder in ("hardware-evidence", "test-evidence"):
        command = (f"mkdir -p files; if [ -d files/{folder} ]; then "
                   f"mv files/{folder} files/{folder}-previous-{run_id}; fi; mkdir -p files/{folder}")
        adb.shell("run-as", APP, "sh", "-c", command)


def upload_model(adb: Adb, path: Path, output: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Blocked(f"Model file does not exist: {path}")
    with path.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise Blocked("--model must point to a real GGUF file")
    size = path.stat().st_size
    disk = adb.shell("df", "-k", "/data").stdout
    (output / "model-device-space.txt").write_text(disk, encoding="utf-8")
    try:
        free = int(disk.splitlines()[-1].split()[3]) * 1024
    except (ValueError, IndexError) as error:
        raise Blocked("Cannot establish free space for the model copy") from error
    if free < size + 512 * 1024 * 1024:
        raise Blocked(f"Not enough free space for model staging: {free} bytes free, "
                      f"need {size + 512 * 1024 * 1024}")
    digest = sha256_file(path)
    print(f"Copying {path.name} ({size:,} bytes) to the isolated test app; SHA-256 {digest}", flush=True)
    with path.open("rb") as source:
        result = subprocess.run(
            adb.command("shell", "-T", shlex.join(("run-as", APP, "sh", "-c",
                        "cat > files/hardware-model.gguf.partial"))),
            stdin=source, capture_output=True, timeout=1800,
        )
    if result.returncode:
        raise RuntimeError("Model upload failed: " + result.stderr.decode("utf-8", errors="replace"))
    checksum_output = adb.shell("run-as", APP, "sha256sum", "files/hardware-model.gguf.partial", timeout=600).stdout.split()
    if not checksum_output or not SHA256.fullmatch(checksum_output[0]):
        raise RuntimeError("Device did not return a valid SHA-256 for the model copy")
    actual = checksum_output[0]
    if actual != digest:
        raise RuntimeError("Device model SHA-256 differs from the source file; benchmark was not started")
    adb.shell("run-as", APP, "mv", "files/hardware-model.gguf.partial", "files/hardware-model.gguf")
    root = adb.shell("run-as", APP, "pwd").stdout.strip()
    if not re.fullmatch(r"/data/(?:user/\d+|data)/" + re.escape(APP), root):
        raise Blocked(f"Unexpected isolated app private directory: {root!r}")
    model = {"name": path.name, "bytes": size, "sha256": digest,
             "device_path": root + "/files/hardware-model.gguf"}
    write_json(output / "model.json", model)
    return model


def extract_evidence(archive_path: Path, destination: Path) -> None:
    """Never use tar.extractall on device-controlled paths or links."""
    total = 0
    with tarfile.open(archive_path) as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in member.name or ":" in member.name:
                raise ValueError("Unsafe device evidence path")
            if len(relative.parts) < 2 or relative.parts[:2] not in (
                ("files", "hardware-evidence"), ("files", "test-evidence"),
            ):
                raise ValueError("Unexpected device evidence path")
            if member.isdir():
                continue
            if not member.isfile() or member.size > 64 * 1024 * 1024:
                raise ValueError("Unsupported evidence entry or oversized file")
            total += member.size
            if total > 512 * 1024 * 1024:
                raise ValueError("Device evidence archive exceeds size limit")
            target = destination.joinpath(*relative.parts[1:])
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Device evidence path escaped its output directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Unreadable device evidence entry")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def collect_evidence(adb: Adb, output: Path) -> dict[str, Any]:
    archive_path = output / "device-evidence.tar"
    try:
        with archive_path.open("wb") as stream:
            result = subprocess.run(
                adb.command("exec-out", "run-as", APP, "tar", "-cf", "-",
                            "files/hardware-evidence", "files/test-evidence"),
                stdout=stream, stderr=subprocess.PIPE, timeout=60,
            )
        if result.returncode:
            return {"ok": False, "error": result.stderr.decode("utf-8", errors="replace")}
        extract_evidence(archive_path, output / "evidence")
        return {"ok": True}
    except (OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)}


def stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_suite(adb: Adb, args: argparse.Namespace, output: Path, run_id: str,
              model: dict[str, Any] | None) -> dict[str, Any]:
    method = "hardwareSmoke" if args.mode == "smoke" else "productionBenchmark"
    expected = 19 if args.mode == "smoke" else 1
    selection = SMOKE_CLASSES if args.mode == "smoke" else HARDWARE_CLASS + "#" + method
    command = ["am", "instrument", "-w", "-r", "-e", "class", selection,
               "-e", "hardwareRunId", run_id]
    if model:
        values = {
            "hardwareModel": model["device_path"], "hardwareModelSha256": model["sha256"],
            "hardwareContext": args.context, "hardwareThreads": args.threads,
            "hardwareTokens": args.tokens, "hardwareDurationSeconds": args.duration_seconds,
            "hardwareRepeats": args.repeats, "hardwareBackendOrder": args.backend_order,
        }
        for key, value in values.items():
            command += ["-e", key, str(value)]
    command.append(RUNNER)
    timeout = args.timeout_seconds or (max(1200, 2 * args.duration_seconds + 900) if model else 1200)
    print(f"Running {expected} {args.mode} test(s); total deadline {timeout} seconds. Evidence: {output.resolve()}", flush=True)
    stop = threading.Event()
    worker = threading.Thread(target=telemetry, args=(adb, output, stop), daemon=True)
    uid_text = adb.shell("cmd", "package", "list", "packages", "-U", APP).stdout
    match = re.search(r"^package:" + re.escape(APP) + r" uid:(\d+)\s*$", uid_text, re.M)
    if not match:
        raise Blocked("Cannot determine the isolated app UID for private log collection")
    timed_out = False
    returncode = -1
    with ExitStack() as cleanup:
        log = cleanup.enter_context((output / "logcat.txt").open("wb"))
        log_process = subprocess.Popen(adb.command("logcat", "--uid=" + match[1], "-v", "threadtime", "-T", "1"),
                                       stdout=log, stderr=subprocess.STDOUT)
        cleanup.callback(stop_process, log_process)
        worker.start()

        def stop_worker() -> None:
            stop.set()
            worker.join(timeout=20)

        cleanup.callback(stop_worker)
        with (output / "instrumentation.txt").open("wb") as stream:
            process = subprocess.Popen(adb.command("shell", "-T", shlex.join(command)),
                                       stdout=stream, stderr=subprocess.STDOUT)
            cleanup.callback(stop_process, process)
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_process(process)
                probe(adb, "am", "force-stop", APP)
        stop_worker()
    transcript = (output / "instrumentation.txt").read_text(encoding="utf-8", errors="replace")
    result = parse_instrumentation(transcript, returncode, expected)
    result["timeout_seconds"] = timeout
    result["timed_out"] = timed_out
    if timed_out:
        result["passed"] = False
        result["failures"].append("Instrumentation exceeded its deadline; isolated app was stopped")
    result["evidence_collection"] = collect_evidence(adb, output)
    try:
        evidence = json.loads((output / "evidence" / "hardware-evidence" / f"{method}.json").read_text(encoding="utf-8"))
        if not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or evidence.get("test") != method:
            raise ValueError("Hardware evidence has an unexpected schema or test identity")
        if evidence.get("status") != "passed" or evidence.get("run_id") != run_id:
            raise ValueError("Hardware evidence is not a passed record from this run")
        result["hardware_evidence"] = f"evidence/hardware-evidence/{method}.json"
    except (OSError, ValueError, TypeError) as error:
        result["passed"] = False
        result["failures"].append(f"Missing valid hardware evidence: {error}")
    if not result["evidence_collection"]["ok"]:
        result["passed"] = False
        result["failures"].append("Device evidence collection failed")
    result["offline_after_run"] = offline_after_run(adb)
    if not result["offline_after_run"]["passed"]:
        result["passed"] = False
        result["failures"].append("Offline state could not be verified at the end of the test")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="mode", required=True)
    default_manifest = Path(__file__).resolve().with_name("hardware-test-bundle.json")
    for mode in ("preflight", "smoke", "benchmark"):
        child = sub.add_parser(mode)
        child.add_argument("--serial", required=True, help="Exact USB serial from adb devices -l")
        child.add_argument("--adb", default="adb", help="Path to adb or adb.exe")
        child.add_argument("--output", type=Path, help="New, empty directory for diagnostic evidence")
        if mode != "preflight":
            child.add_argument("--manifest", type=Path, default=default_manifest,
                               help="Pinned APK bundle manifest (default: beside this script)")
            child.add_argument("--timeout-seconds", type=int,
                               help="Explicit total test deadline, 60..7200 seconds; raise for high repeat counts")
        if mode == "benchmark":
            child.add_argument("--model", type=Path, required=True, help="Local GGUF; no model is downloaded")
            child.add_argument("--context", type=int, default=2048)
            child.add_argument("--threads", type=int, default=4)
            child.add_argument("--tokens", type=int, default=128)
            child.add_argument("--duration-seconds", type=int, default=300, help="Minimum sustained period PER backend (0..1800)")
            child.add_argument("--repeats", type=int, default=3)
            child.add_argument("--backend-order", choices=("cpu,vulkan", "vulkan,cpu"), default="cpu,vulkan")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if not args.serial or args.serial.startswith("-") or any(character.isspace() for character in args.serial):
        arguments.error("--serial must be one exact device serial")
    if args.mode == "benchmark" and not (
        256 <= args.context <= 32768 and args.context % 256 == 0 and 1 <= args.threads <= 16 and
        8 <= args.tokens <= 1024 and args.tokens < args.context // 2 and
        0 <= args.duration_seconds <= 1800 and 1 <= args.repeats <= 50
    ):
        arguments.error("Invalid benchmark bounds: context 256..32768 in multiples of 256, threads 1..16, tokens 8..1024 and <context/2, duration 0..1800, repeats 1..50")
    if args.mode != "preflight" and args.timeout_seconds is not None and not 60 <= args.timeout_seconds <= 7200:
        arguments.error("--timeout-seconds must be between 60 and 7200")
    run_id = uuid.uuid4().hex
    output = args.output or Path.cwd() / ("lastphone-nubia-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + run_id[:8])
    if output.exists() and any(output.iterdir()):
        arguments.error("--output must be a new or empty directory; existing evidence is preserved")
    output.mkdir(parents=True, exist_ok=True)
    print(f"Evidence directory: {output.resolve()}", flush=True)
    summary: dict[str, Any] = {
        "schema_version": 1, "run_id": run_id, "mode": args.mode, "started_at": utc_now(),
        "status": "RUNNING", "hardware_acceptance": "pending_manual",
        "app_package": APP, "release_binary_tested": False,
        "manual_checks_pending": ["Published alpha installation and UI", "Intended complete content pack",
                                  "Background/screen-off and restart recovery", "Battery behavior under controlled power conditions"],
        "note": "The isolated hardware variant is not the published APK. A passing automated suite does not complete manual acceptance.",
    }
    adb = Adb(args.adb, args.serial)
    device_ready = False
    exit_code = 1
    try:
        bundle = None
        if args.mode != "preflight":
            bundle, app_path, test_path = verify_bundle(args.manifest)
            summary["bundle"] = bundle
        device = inventory(adb, output)
        summary["device"] = device
        device_ready = True
        snapshots(adb, output, "before")
        if args.mode == "preflight":
            summary["status"] = "READY"
            summary["next_step"] = "Set airplane mode on and turn Wi-Fi/mobile data off, then run smoke." if not device["offline_radios_ready"] else "Run smoke with the pinned hardware bundle."
            exit_code = 0
        else:
            if not device["offline_radios_ready"]:
                raise Blocked("Offline run requires airplane mode ON, Wi-Fi OFF, and mobile data OFF. Change these manually, then rerun. No phone settings were changed.")
            connectivity = device["connectivity"].get("stdout", "")
            active = re.search(r"Active default network:\s*([^\r\n]+)", connectivity)
            if active and active[1].strip().lower() not in ("none", "null", "0", "-1"):
                raise Blocked("Android reports an active default network; disconnect it before offline hardware testing")
            install(adb, app_path, test_path, output)
            summary["installed"] = True
            rotate_evidence(adb, run_id)
            model = upload_model(adb, args.model, output) if args.mode == "benchmark" else None
            if model:
                summary["model"] = model
                summary["benchmark_options"] = {key: getattr(args, key) for key in (
                    "context", "threads", "tokens", "duration_seconds", "repeats", "backend_order",
                )}
            summary["tests"] = run_suite(adb, args, output, run_id, model)
            summary["status"] = "PASS_AUTOMATED" if summary["tests"]["passed"] else "FAIL"
            exit_code = 0 if summary["tests"]["passed"] else 1
    except HardwareUnavailable as error:
        summary.update(status="HARDWARE_UNAVAILABLE", error=str(error))
        exit_code = 2
    except Blocked as error:
        summary.update(status="BLOCKED", error=str(error))
        exit_code = 2
    except KeyboardInterrupt:
        summary.update(status="INTERRUPTED", error="Interrupted by the operator; no pass recorded")
        probe(adb, "am", "force-stop", APP)
        exit_code = 130
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        summary.update(status="FAIL", error=str(error))
        exit_code = 1
    finally:
        if summary.get("installed") and not summary.get("tests", {}).get("evidence_collection"):
            summary["evidence_collection"] = collect_evidence(adb, output)
        if device_ready:
            snapshots(adb, output, "after")
        summary["finished_at"] = utc_now()
        write_json(output / "summary.json", summary)
        with zipfile.ZipFile(output / "report.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output.rglob("*")):
                if path.is_file() and path.name not in ("report.zip", "device-evidence.tar"):
                    archive.write(path, path.relative_to(output))
        print(f"{summary['status']}: {output.resolve()}")
        if summary.get("error"):
            print(summary["error"], file=sys.stderr)
        print(f"Evidence: {(output / 'report.zip').resolve()}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
