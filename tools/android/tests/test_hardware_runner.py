"""Host-side negative-path tests; none of these represents a phone test."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

RUNNER_PATH = Path(__file__).resolve().parents[1] / "run_hardware_tests.py"
SPEC = importlib.util.spec_from_file_location("lastphone_hardware_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def properties(**changes: str) -> dict[str, str]:
    value = {"ro.product.model": "NX721J", "ro.product.device": "cerro",
             "ro.product.cpu.abi": "arm64-v8a", "ro.build.version.sdk": "34",
             "ro.kernel.qemu": "0", "ro.boot.qemu": "0"}
    value.update(changes)
    return value


def transcript(count: int, result_code: int = 0) -> str:
    result = []
    for number in range(count):
        for code in (1, result_code):
            result.extend(("INSTRUMENTATION_STATUS: class=org.lastphone.Test",
                           f"INSTRUMENTATION_STATUS: test=test{number}",
                           f"INSTRUMENTATION_STATUS: numtests={count}",
                           f"INSTRUMENTATION_STATUS_CODE: {code}"))
    result.extend(("INSTRUMENTATION_RESULT: stream=", "Time: 1.2", f"OK ({count} tests)",
                   "INSTRUMENTATION_CODE: -1"))
    return "\n".join(result) + "\n"


def binary_manifest(package: str, target: str | None = None) -> bytes:
    strings = ["manifest", "package", package, "instrumentation", "targetPackage",
               target or "", "name", "androidx.test.runner.AndroidJUnitRunner"]
    offsets = []
    string_bytes = bytearray()
    for text in strings:
        encoded = text.encode("utf-8")
        offsets.append(len(string_bytes))
        string_bytes += bytes((len(text), len(encoded))) + encoded + b"\x00"
    string_bytes += b"\x00" * ((-len(string_bytes)) % 4)
    pool_size = 28 + len(offsets) * 4 + len(string_bytes)
    pool = struct.pack("<HHIIIIII", 1, 28, pool_size, len(strings), 0, 0x100,
                       28 + len(offsets) * 4, 0)
    pool += struct.pack("<" + "I" * len(offsets), *offsets) + string_bytes

    def element(name: int, attributes: list[tuple[int, int]]) -> bytes:
        contents = struct.pack("<II", 1, 0xFFFFFFFF)
        contents += struct.pack("<IIHHHHHH", 0xFFFFFFFF, name, 20, 20, len(attributes), 0, 0, 0)
        for key, value in attributes:
            contents += struct.pack("<IIIHBBI", 0xFFFFFFFF, key, value, 8, 0, 3, value)
        return struct.pack("<HHI", 0x102, 16, 8 + len(contents)) + contents

    body = pool + element(0, [(1, 2)])
    if target:
        body += element(3, [(4, 5), (6, 7)])
    return struct.pack("<HHI", 3, 8, len(body) + 8) + body


def create_bundle(root: Path, app_id: str = runner.APP,
                  test_target: str = runner.APP) -> Path:
    manifest = {"schema": 1, "source_sha": "a" * 40}
    for key, package, target in (("app", app_id, None), ("test", runner.TEST_APP, test_target)):
        path = root / f"{key}.apk"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AndroidManifest.xml", binary_manifest(package, target))
        manifest[key] = {"file": path.name, "package": runner.APP if key == "app" else runner.TEST_APP,
                         "sha256": runner.sha256_file(path)}
    path = root / "hardware-test-bundle.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class ManifestTests(unittest.TestCase):
    def test_valid_bundle_has_real_isolated_apk_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, app, test = runner.verify_bundle(create_bundle(root))
            self.assertEqual(runner.APP, runner.read_apk_manifest(app)["package"])
            self.assertEqual(runner.APP, runner.read_apk_manifest(test)["targetPackage"])
            self.assertEqual("a" * 40, manifest["source_sha"])

    def test_published_package_cannot_be_installed_even_with_matching_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(runner.Blocked, "not isolated package"):
                runner.verify_bundle(create_bundle(Path(directory), app_id="io.github.qqrm.lastphone"))

    def test_instrumentation_cannot_target_published_app(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(runner.Blocked, "does not target"):
                runner.verify_bundle(create_bundle(Path(directory), test_target="io.github.qqrm.lastphone"))

    def test_checksum_mismatch_prevents_installation_before_device_contact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = create_bundle(root)
            (root / "app.apk").write_bytes(b"modified")
            with mock.patch.object(runner.Adb, "run") as adb_run:
                status = runner.main(["smoke", "--serial", "NXTEST", "--manifest", str(manifest),
                                      "--output", str(root / "report")])
            self.assertEqual(2, status)
            adb_run.assert_not_called()
            summary = json.loads((root / "report/summary.json").read_text())
            self.assertEqual("BLOCKED", summary["status"])
            self.assertTrue((root / "report/report.zip").is_file())

    def test_manifest_cannot_escape_bundle_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_bundle(Path(directory))
            data = json.loads(path.read_text())
            data["app"]["file"] = "../somewhere/app.apk"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(runner.Blocked, "basenames"):
                runner.verify_bundle(path)


class DeviceTests(unittest.TestCase):
    def test_actual_4k_and_16k_results_are_distinguished(self):
        self.assertFalse(runner.validate_device(properties(), "4096", "NXTEST")["execution_on_16k_device"])
        self.assertTrue(runner.validate_device(properties(), "16384", "NXTEST")["execution_on_16k_device"])

    def test_emulator_is_rejected_even_with_spoofed_model(self):
        for serial, changes in (("emulator-5554", {}), ("NXTEST", {"ro.kernel.qemu": "1"})):
            with self.subTest(serial=serial, changes=changes), self.assertRaisesRegex(runner.Blocked, "emulator"):
                runner.validate_device(properties(**changes), "4096", serial)

    def test_wrong_phone_abi_api_or_page_size_is_rejected(self):
        cases = (({"ro.product.model": "Pixel", "ro.product.device": "panther"}, "4096"),
                 ({"ro.product.cpu.abi": "x86_64"}, "4096"),
                 ({"ro.build.version.sdk": "28"}, "4096"), ({}, "8192"))
        for changes, pages in cases:
            with self.subTest(changes=changes, pages=pages), self.assertRaises(runner.Blocked):
                runner.validate_device(properties(**changes), pages, "NXTEST")

    def test_commands_are_scoped_and_remote_arguments_are_quoted(self):
        adb = runner.Adb("C:/Android/adb.exe", "MY_SERIAL")
        self.assertEqual(["C:/Android/adb.exe", "-s", "MY_SERIAL", "get-state"], adb.command("get-state"))
        with mock.patch.object(adb, "run") as run:
            adb.shell("run-as", runner.APP, "sh", "-c", "cat > files/hardware-model.gguf.partial")
        remote = run.call_args.args[-1]
        self.assertIn("'cat > files/hardware-model.gguf.partial'", remote)

    def test_no_adb_device_leaves_diagnostic_zip_and_never_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with mock.patch.object(runner.Adb, "run", side_effect=FileNotFoundError("adb not found")), \
                    mock.patch.object(runner, "install") as install:
                status = runner.main(["preflight", "--serial", "NXTEST", "--output", str(output)])
            self.assertEqual(2, status)
            install.assert_not_called()
            with zipfile.ZipFile(output / "report.zip") as archive:
                summary = json.loads(archive.read("summary.json"))
            self.assertEqual("HARDWARE_UNAVAILABLE", summary["status"])
            self.assertEqual("pending_manual", summary["hardware_acceptance"])

    def test_preflight_is_read_only_and_can_report_radios_on(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            with mock.patch.object(runner, "inventory", return_value={"offline_radios_ready": False}), \
                    mock.patch.object(runner, "snapshots"), mock.patch.object(runner, "install") as install:
                status = runner.main(["preflight", "--serial", "NXTEST", "--output", str(output)])
            self.assertEqual(0, status)
            install.assert_not_called()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual("READY", summary["status"])
            self.assertEqual("pending_manual", summary["hardware_acceptance"])

    def test_signature_conflict_never_causes_uninstall_or_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = mock.Mock(spec=runner.Adb)
            adb.run.return_value = subprocess.CompletedProcess([], 1, "", "INSTALL_FAILED_UPDATE_INCOMPATIBLE")
            with self.assertRaisesRegex(runner.Blocked, "no package was uninstalled"):
                runner.install(adb, Path("app.apk"), Path("test.apk"), Path(directory))
            self.assertEqual(1, adb.run.call_count)
            self.assertEqual("install", adb.run.call_args.args[0])
            adb.shell.assert_not_called()

    def test_secondary_android_profile_is_blocked_before_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = mock.Mock(spec=runner.Adb)
            adb.serial = "NXTEST"
            adb.run.return_value = subprocess.CompletedProcess([], 0, "device\n", "")
            property_text = "\n".join(f"[{key}]: [{value}]" for key, value in properties().items())
            adb.shell.side_effect = [subprocess.CompletedProcess([], 0, value, "")
                                     for value in (property_text, "4096\n", "10\n")]
            with self.assertRaisesRegex(runner.Blocked, "owner profile"):
                runner.inventory(adb, Path(directory))

    def test_end_radio_change_cannot_retain_offline_pass(self):
        for wifi, active, expected in (("0", "none", True), ("1", "none", False),
                                       ("0", "101", False), ("0", "unavailable", False)):
            with self.subTest(wifi=wifi, active=active):
                adb = mock.Mock(spec=runner.Adb)
                adb.shell.side_effect = [subprocess.CompletedProcess([], 0, value, "")
                                         for value in ("1", wifi, "0")]
                connectivity = "Active default network: " + active if active != "unavailable" else "unknown format"
                with mock.patch.object(runner, "probe", return_value={"ok": True, "stdout": connectivity}):
                    self.assertEqual(expected, runner.offline_after_run(adb)["passed"])


class ProtocolTests(unittest.TestCase):
    def test_complete_19_test_transcript_is_accepted(self):
        result = runner.parse_instrumentation(transcript(19), 0, 19)
        self.assertTrue(result["passed"], result)
        self.assertEqual(19, len(result["completed_tests"]))

    def test_exit_zero_does_not_override_failure_skip_or_assumption(self):
        for code in (-1, -2, -3, -4):
            with self.subTest(code=code):
                self.assertFalse(runner.parse_instrumentation(transcript(1, code), 0, 1)["passed"])

    def test_missing_tests_crash_final_code_or_duplicate_identity_fail(self):
        cases = ("INSTRUMENTATION_CODE: -1\n", "OK (0 tests)\nINSTRUMENTATION_CODE: -1\n",
                 transcript(1) + "INSTRUMENTATION_RESULT: shortMsg=Process crashed.\n",
                 transcript(1).replace("INSTRUMENTATION_CODE: -1", "INSTRUMENTATION_CODE: 0"),
                 transcript(2).replace("test1", "test0"))
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(runner.parse_instrumentation(value, 0, 2 if "2 tests" in value else 1)["passed"])

    def test_nonzero_adb_exit_cannot_pass(self):
        self.assertFalse(runner.parse_instrumentation(transcript(19), 1, 19)["passed"])


class ArchiveTests(unittest.TestCase):
    def test_symlinks_and_windows_or_posix_traversal_are_rejected(self):
        for name, kind in (("files/hardware-evidence/../../evil", tarfile.REGTYPE),
                           ("/tmp/evil", tarfile.REGTYPE),
                           ("files/hardware-evidence/C:escape.txt", tarfile.REGTYPE),
                           ("files/hardware-evidence/log.json:stream", tarfile.REGTYPE),
                           ("files/hardware-evidence/link", tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive_path = root / "test.tar"
                with tarfile.open(archive_path, "w") as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.size = 1 if kind == tarfile.REGTYPE else 0
                    info.linkname = "/tmp/evil" if kind == tarfile.SYMTYPE else ""
                    archive.addfile(info, io.BytesIO(b"x") if info.size else None)
                with self.assertRaises(ValueError):
                    runner.extract_evidence(archive_path, root / "out")

    def test_normal_evidence_is_extracted_without_app_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "test.tar"
            with tarfile.open(archive_path, "w") as archive:
                info = tarfile.TarInfo("files/hardware-evidence/hardwareSmoke.json")
                info.size = 2
                archive.addfile(info, io.BytesIO(b"{}"))
            runner.extract_evidence(archive_path, root / "out")
            self.assertEqual("{}", (root / "out/hardware-evidence/hardwareSmoke.json").read_text())


class SuiteControlTests(unittest.TestCase):
    class Process:
        def __init__(self, timeout: bool = False):
            self.timeout = timeout
            self.returncode = None

        def wait(self, timeout=None):
            if self.timeout and self.returncode is None:
                raise subprocess.TimeoutExpired("adb", timeout)
            self.returncode = self.returncode if self.returncode is not None else 0
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def suite(self, directory: str, *, timeout: bool = False, evidence_run_id: str = "current"):
        output = Path(directory)
        adb = runner.Adb("adb", "NXTEST")
        params = argparse.Namespace(mode="smoke", timeout_seconds=None)
        processes = []

        def popen(command, stdout, **kwargs):
            instrument = "instrument" in command[-1]
            process = self.Process(timeout and instrument)
            processes.append(process)
            if instrument:
                stdout.write(transcript(19).encode("utf-8"))
                stdout.flush()
            return process

        def collect(_adb, _output):
            folder = output / "evidence/hardware-evidence"
            folder.mkdir(parents=True)
            (folder / "hardwareSmoke.json").write_text(json.dumps({"schema_version": 1, "test": "hardwareSmoke",
                                                                  "status": "passed", "run_id": evidence_run_id}))
            return {"ok": True}

        with mock.patch.object(adb, "shell", return_value=subprocess.CompletedProcess([], 0, f"package:{runner.APP} uid:10234\n", "")), \
                mock.patch.object(runner.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(runner, "telemetry"), mock.patch.object(runner, "probe") as probe, \
                mock.patch.object(runner, "collect_evidence", side_effect=collect), \
                mock.patch.object(runner, "offline_after_run", return_value={"passed": True}):
            result = runner.run_suite(adb, params, output, "current", None)
        return result, processes, probe

    def test_success_requires_current_run_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, _ = self.suite(directory)
            self.assertTrue(result["passed"], result)
        with tempfile.TemporaryDirectory() as directory:
            result, _, _ = self.suite(directory, evidence_run_id="previous")
            self.assertFalse(result["passed"])
            self.assertIn("from this run", " ".join(result["failures"]))

    def test_timeout_retains_transcript_stops_only_test_app_and_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            result, processes, probe = self.suite(directory, timeout=True)
            self.assertFalse(result["passed"])
            self.assertTrue(result["timed_out"])
            self.assertIn("OK (19 tests)", (Path(directory) / "instrumentation.txt").read_text())
            self.assertTrue(all(process.returncode is not None for process in processes))
            self.assertEqual(("am", "force-stop", runner.APP), probe.call_args.args[1:])


if __name__ == "__main__":
    unittest.main()
