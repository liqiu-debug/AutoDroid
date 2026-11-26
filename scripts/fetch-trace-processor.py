#!/usr/bin/env python3
"""Fetch pinned official Perfetto binaries at build time, never at user runtime.

  python scripts/fetch-trace-processor.py --platform mac-arm64
  python scripts/fetch-trace-processor.py --platform all --verify-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).with_name("trace-processor-manifest.json")
ARTIFACT_HOST = "https://commondatastorage.googleapis.com/perfetto-luci-artifacts/"


def host_platform():
    system, machine = platform.system().lower(), platform.machine().lower()
    if system == "darwin":
        return "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
    if system == "windows" and machine in {"amd64", "x86_64", "x64"}:
        return "windows-x64"
    raise ValueError(f"Unsupported build host {system}/{machine}; pass --platform explicitly")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path, spec):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Missing Trace Processor: {path}")
    if path.stat().st_size != spec["file_size"]:
        raise ValueError(f"Trace Processor size mismatch: {path}")
    if sha256_file(path) != spec["sha256"]:
        raise ValueError(f"Trace Processor SHA256 mismatch: {path}")
    return path


def fetch_binary(destination, spec, *, timeout=60, opener=urllib.request.urlopen):
    """Validate an existing file or atomically replace it after hash verification."""
    destination = Path(destination)
    if destination.exists():
        try:
            return verify_file(destination, spec)
        except ValueError:
            pass
    if not spec["url"].startswith(ARTIFACT_HOST):
        raise ValueError("Trace Processor download must use the pinned official Perfetto artifact host")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".trace-processor-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "AutoDroid-build/1"})
        with os.fdopen(fd, "wb") as output:
            with opener(request, timeout=timeout) as response:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > spec["file_size"]:
                        raise ValueError("Trace Processor download exceeds pinned file size")
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
        verify_file(temporary, spec)
        temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=["mac-arm64", "mac-x64", "windows-x64", "all"], default=None)
    parser.add_argument("--verify-only", action="store_true", help="Check size and SHA256 without network access")
    parser.add_argument("--destination", type=Path, default=ROOT / "assets" / "trace_processor")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    target = args.platform or host_platform()
    targets = list(manifest["platforms"]) if target == "all" else [target]
    if not args.verify_only:
        args.destination.mkdir(parents=True, exist_ok=True)
    for name in targets:
        spec = manifest["platforms"][name]
        binary = args.destination / name / spec["file_name"]
        if args.verify_only:
            verify_file(binary, spec)
        else:
            fetch_binary(binary, spec, timeout=args.timeout)
        print(f"Verified Perfetto {manifest['version']} {name}: {binary} (sha256 {spec['sha256']})")
    # Ship exactly the same provenance and checksum metadata beside the binaries.
    # verify-only never creates/repairs missing resources and never downloads.
    if not args.verify_only:
        (args.destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        license_source = Path(__file__).with_name("trace-processor-LICENSE.txt")
        if license_source.is_file():
            shutil.copyfile(license_source, args.destination / "LICENSE")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, urllib.error.URLError) as exc:
        raise SystemExit(f"Trace Processor preparation failed: {exc}") from exc
