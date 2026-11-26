"""Offline build/runtime contract for the pinned native Trace Processor."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from backend.startup import trace_analysis as analysis

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fetch_startup_tp", ROOT / "scripts" / "fetch-trace-processor.py")
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


class TraceProcessorBuildTests(unittest.TestCase):
    def test_pinned_manifest_has_official_urls_size_and_sha256_for_all_release_targets(self):
        manifest = json.loads(fetch.MANIFEST.read_text())
        self.assertEqual(set(manifest["platforms"]), {"mac-arm64", "mac-x64", "windows-x64"})
        self.assertEqual(manifest["version"], "v58.2")
        self.assertIn(manifest["source_commit"], manifest["manifest_source"])
        for name, spec in manifest["platforms"].items():
            self.assertTrue(spec["url"].startswith(fetch.ARTIFACT_HOST + manifest["version"] + "/"))
            self.assertEqual(len(spec["sha256"]), 64)
            self.assertGreater(spec["file_size"], 1000000)

    def test_download_is_atomic_and_bad_checksum_never_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tool"
            path.write_bytes(b"keep-existing")
            spec = {"url": fetch.ARTIFACT_HOST + "v58.2/example", "file_size": 8,
                    "sha256": hashlib.sha256(b"verified").hexdigest()}
            with self.assertRaisesRegex(ValueError, "SHA256"):
                fetch.fetch_binary(path, spec, opener=lambda *_a, **_k: io.BytesIO(b"tampered"))
            self.assertEqual(path.read_bytes(), b"keep-existing")
            fetch.fetch_binary(path, spec, opener=lambda *_a, **_k: io.BytesIO(b"verified"))
            self.assertEqual(path.read_bytes(), b"verified")
            self.assertEqual(list(Path(directory).glob(".trace-processor-*")), [])

    def test_verify_only_never_uses_network_and_rejects_missing_asset(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(fetch.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(ValueError, "Missing Trace Processor"):
                fetch.main(["--platform", "mac-arm64", "--destination", directory, "--verify-only"])


class TraceProcessorRuntimeTests(unittest.TestCase):
    def test_platform_mapping_does_not_treat_windows_arm_or_linux_as_supported(self):
        self.assertEqual(analysis.trace_processor_platform("Darwin", "arm64"), "mac-arm64")
        self.assertEqual(analysis.trace_processor_platform("Darwin", "x86_64"), "mac-x64")
        self.assertEqual(analysis.trace_processor_platform("Windows", "AMD64"), "windows-x64")
        self.assertIsNone(analysis.trace_processor_platform("Windows", "ARM64"))
        self.assertIsNone(analysis.trace_processor_platform("Linux", "x86_64"))

    def test_frozen_resource_resolution_and_manifest_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            frozen = Path(directory)
            binary = frozen / "trace_processor" / "mac-arm64" / "trace_processor_shell"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"test-only")
            manifest = frozen / "trace_processor" / "trace-processor-manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "version": "v58.2", "platforms": {}}))
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(analysis.sys, "_MEIPASS", str(frozen), create=True):
                self.assertEqual(analysis.resolve_trace_processor(target="mac-arm64"), binary.resolve())
                self.assertEqual(analysis._manifest(binary)["version"], "v58.2")

    def test_checksum_failure_never_executes_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "trace_processor_shell"
            binary.write_bytes(b"tampered")
            manifest = {"version": "v58.2", "platforms": {"mac-arm64": {"sha256": "0" * 64, "file_size": 8}}}
            with mock.patch.object(analysis, "trace_processor_platform", return_value="mac-arm64"), mock.patch.object(analysis, "_manifest", return_value=manifest), mock.patch.object(analysis, "_command", side_effect=AssertionError("must not execute")):
                self.assertEqual(analysis.verify_trace_processor(binary)["status"], "CHECKSUM_MISMATCH")

    def test_csv_preserves_zero_quoted_names_and_null(self):
        tables = analysis._csv_tables('"name","time_ms","extra"\n"a,b",0,"[NULL]"\n\n"count"\n0\n')
        self.assertEqual(tables, [[{"name": "a,b", "time_ms": 0, "extra": None}], [{"count": 0}]])

    def test_ios_never_claims_android_or_native_system_startup_metrics(self):
        with mock.patch.object(analysis, "verify_trace_processor", side_effect=AssertionError("not needed")):
            result = analysis.analyze_startup_trace("missing.trace", "com.example.app", platform_name="ios")
        self.assertEqual(result["analysis_status"], "UNAVAILABLE")
        self.assertIn("iOS WDA", result["reason"])

    def test_partial_trace_analysis_states_window_fallback_instead_of_fabricating_ttid(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            trace.write_text("{}")
            tool = {"available": True, "path": "/native/local/tool"}
            def query(_trace, sql, **kwargs):
                if "android.startup" in sql:
                    return [[]]
                if "trace_bounds" in sql:
                    return [[{"start_ts": 0, "end_ts": 1000}]]
                if "FROM slice" in sql:
                    return [[{"name": "activityStart", "duration_ms": 0}]]
                return [[]]
            with mock.patch.object(analysis, "verify_trace_processor", return_value=tool), mock.patch.object(analysis, "query_trace", side_effect=query):
                result = analysis.analyze_startup_trace(trace, "com.example.app")
            self.assertEqual(result["analysis_status"], "PARTIAL")
            self.assertEqual(result["window_source"], "trace_bounds_fallback")
            self.assertEqual(result["main_thread_slices"][0]["duration_ms"], 0)
            self.assertNotIn("ttid_ms", result)

    def test_real_bundled_native_binary_offline_smoke(self):
        if analysis.resolve_trace_processor() is None:
            self.skipTest("Build assets not downloaded on this test host")
        with tempfile.TemporaryDirectory() as directory, mock.patch("urllib.request.urlopen", side_effect=AssertionError("runtime network forbidden")):
            trace = Path(directory) / "startup.json"
            trace.write_text(json.dumps({"traceEvents": [
                {"ph": "M", "name": "process_name", "pid": 10, "tid": 10, "args": {"name": "com.example.startup"}},
                {"ph": "M", "name": "thread_name", "pid": 10, "tid": 10, "args": {"name": "main"}},
                {"ph": "X", "name": "bindApplication", "pid": 10, "tid": 10, "ts": 1000, "dur": 5000},
            ]}))
            tool = analysis.verify_trace_processor()
            self.assertTrue(tool["available"], tool)
            self.assertTrue(tool["verified"])
            result = analysis.analyze_startup_trace(trace, "com.example.startup")
            self.assertEqual(result["analysis_status"], "PARTIAL")
            self.assertEqual(result["main_thread_slices"][0]["name"], "bindApplication")
            self.assertEqual(result["main_thread_slices"][0]["duration_ms"], 5.0)


if __name__ == "__main__":
    unittest.main()
