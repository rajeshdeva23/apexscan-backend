"""Versioned deployment bundle: build + integrity-verify (DEPLOY-3A / B3).

A deployment bundle carries ONLY Compose deployment metadata for one exact
reviewed Git SHA — never secrets, never application source. The manifest binds
the bundle to that SHA and to each file's SHA-256 so Stage B can prove
``requested == eligible == bundle source SHA`` and that no file was tampered
with before it is installed into a versioned release directory. Extraction is
hardened against path traversal and symlink escape. No ``docker compose up``
happens here — this only makes a bundle independently verifiable.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from collections.abc import Mapping
from pathlib import Path

BUNDLE_SCHEMA = "apexscan-deploy-bundle/1"
MANIFEST_NAME = "manifest.json"
# The only deployment file a production bundle carries: the production Compose
# authority. Secrets and durable state stay external (never bundled).
PRODUCTION_COMPOSE_FILE = "docker-compose.production.yml"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")  # flat names only; no directories


class BundleError(ValueError):
    """Raised when a bundle is malformed, tampered, or fails its SHA binding."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(source_sha: str, files: Mapping[str, bytes]) -> dict[str, object]:
    """Build the manifest binding a bundle to its source SHA and file hashes."""
    if not _FULL_SHA.match(source_sha):
        raise BundleError("source_sha must be a full 40-char git SHA")
    return {
        "schema": BUNDLE_SCHEMA,
        "source_sha": source_sha,
        "files": {name: _sha256(data) for name, data in sorted(files.items())},
    }


def create_bundle(source_sha: str, files: Mapping[str, bytes]) -> bytes:
    """Create a deterministic ``.tar.gz`` bundle of ``files`` plus a manifest.

    Args:
        source_sha: The exact reviewed Git SHA the deployment files come from.
        files: Mapping of flat filename -> bytes (e.g. the two compose files).

    Returns:
        The gzipped tar archive bytes.
    """
    for name in files:
        if name == MANIFEST_NAME or not _SAFE_NAME.match(name):
            raise BundleError(f"unsafe or reserved bundle filename: {name!r}")
    manifest = build_manifest(source_sha, files)
    payload: dict[str, bytes] = {
        **files,
        MANIFEST_NAME: json.dumps(manifest, sort_keys=True).encode("utf-8"),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in sorted(payload.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _safe_members(tar: tarfile.TarFile) -> dict[str, bytes]:
    """Extract regular-file members with flat, traversal-free names into memory."""
    out: dict[str, bytes] = {}
    for member in tar.getmembers():
        if not member.isreg():  # rejects symlinks, hardlinks, dirs, devices, FIFOs
            raise BundleError(f"non-regular tar member rejected: {member.name!r}")
        name = member.name
        if name.startswith("/") or ".." in name.split("/") or not _SAFE_NAME.match(name):
            raise BundleError(f"unsafe tar member name rejected: {name!r}")
        if name in out:
            raise BundleError(f"duplicate tar member rejected: {name!r}")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise BundleError(f"unreadable tar member: {name!r}")
        out[name] = extracted.read()
    return out


def verify_bundle(archive: bytes, *, expected_sha: str) -> dict[str, bytes]:
    """Verify a bundle's integrity and SHA binding; return its deployment files.

    Checks: safe extraction (no traversal/symlink/non-regular members), manifest
    schema, ``manifest.source_sha == expected_sha``, and that the manifest lists
    exactly the non-manifest members with matching SHA-256. Raises
    :class:`BundleError` on any mismatch (fail closed).
    """
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = _safe_members(tar)
    if MANIFEST_NAME not in members:
        raise BundleError("bundle missing manifest.json")
    manifest = json.loads(members[MANIFEST_NAME].decode("utf-8"))
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleError("unsupported bundle schema")
    if manifest.get("source_sha") != expected_sha or not _FULL_SHA.match(expected_sha):
        raise BundleError("bundle source SHA does not match the expected SHA")
    declared = manifest.get("files")
    if not isinstance(declared, dict):
        raise BundleError("manifest files section is malformed")
    files = {name: data for name, data in members.items() if name != MANIFEST_NAME}
    if set(files) != set(declared):
        raise BundleError("bundle files do not match the manifest file set")
    for name, data in files.items():
        if _sha256(data) != declared[name]:
            raise BundleError(f"file hash mismatch for {name!r}")
    return files


def production_bundle(source_sha: str, root: Path) -> bytes:
    """Build a production deployment bundle from the reviewed repo tree.

    Contains only the production Compose authority (``docker-compose.production.yml``)
    and the manifest binding it to ``source_sha``. No secrets, no source tree.
    """
    data = (root / PRODUCTION_COMPOSE_FILE).read_bytes()
    return create_bundle(source_sha, {PRODUCTION_COMPOSE_FILE: data})


def main(argv: list[str] | None = None) -> int:
    """CLI: build a production bundle (create) or check one (verify)."""
    import argparse

    parser = argparse.ArgumentParser(description="Build/verify an ApexScan deployment bundle.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--source-sha", required=True)
    create.add_argument("--root", default=".", help="Repo root holding the production compose.")
    create.add_argument("--out", required=True, help="Output .tar.gz path.")
    check = sub.add_parser("verify")
    check.add_argument("--archive", required=True)
    check.add_argument("--expected-sha", required=True)
    args = parser.parse_args(argv)

    if args.command == "create":
        Path(args.out).write_bytes(production_bundle(args.source_sha, Path(args.root)))
        print(f"wrote {args.out} for {args.source_sha}")
        return 0
    files = verify_bundle(Path(args.archive).read_bytes(), expected_sha=args.expected_sha)
    print(f"verified bundle for {args.expected_sha}: {sorted(files)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
