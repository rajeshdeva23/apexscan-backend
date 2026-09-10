"""Deployment bundle: integrity, SHA binding, hardened extraction (DEPLOY-3A/B3)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from deploy.bundle import (
    BUNDLE_SCHEMA,
    PRODUCTION_COMPOSE_FILE,
    BundleError,
    create_bundle,
    production_bundle,
    verify_bundle,
)

_ROOT = Path(__file__).resolve().parents[3]

_SHA = "6" * 40
# Neutral fixtures for the generic bundle machinery (not real repo files).
_FILES = {
    "alpha.yml": b"services:\n  backend: {}\n",
    "beta.yml": b"services:\n  backend: {image: x}\n",
}


def test_roundtrip() -> None:
    files = verify_bundle(create_bundle(_SHA, _FILES), expected_sha=_SHA)
    assert files == _FILES


def test_wrong_expected_sha_rejected() -> None:
    with pytest.raises(BundleError, match="source SHA"):
        verify_bundle(create_bundle(_SHA, _FILES), expected_sha="5" * 40)


def test_invalid_source_sha_rejected() -> None:
    with pytest.raises(BundleError):
        create_bundle("nothex", _FILES)


def test_reserved_manifest_filename_rejected() -> None:
    with pytest.raises(BundleError):
        create_bundle(_SHA, {"manifest.json": b"x"})


def _tamper(archive: bytes, name: str, data: bytes, *, keep_manifest: bool = True) -> bytes:
    """Rebuild an archive replacing/adding a member's bytes (manifest untouched)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as src:
        members = {m.name: src.extractfile(m).read() for m in src.getmembers() if m.isreg()}  # type: ignore[union-attr]
    members[name] = data
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for n, d in members.items():
            info = tarfile.TarInfo(n)
            info.size = len(d)
            tar.addfile(info, io.BytesIO(d))
    return out.getvalue()


def test_tampered_file_rejected() -> None:
    bad = _tamper(create_bundle(_SHA, _FILES), "alpha.yml", b"evil: true\n")
    with pytest.raises(BundleError, match="hash mismatch"):
        verify_bundle(bad, expected_sha=_SHA)


def test_extra_file_not_in_manifest_rejected() -> None:
    bad = _tamper(create_bundle(_SHA, _FILES), "extra.yml", b"x")
    with pytest.raises(BundleError, match="manifest file set"):
        verify_bundle(bad, expected_sha=_SHA)


def _malicious_tar(name: str, *, symlink: bool = False) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        if symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        else:
            data = b"x"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


@pytest.mark.parametrize("name", ["../evil.yml", "/etc/passwd", "sub/dir.yml"])
def test_path_traversal_and_nested_rejected(name: str) -> None:
    with pytest.raises(BundleError):
        verify_bundle(_malicious_tar(name), expected_sha=_SHA)


def test_symlink_member_rejected() -> None:
    with pytest.raises(BundleError, match="non-regular"):
        verify_bundle(_malicious_tar("docker-compose.yml", symlink=True), expected_sha=_SHA)


def test_hardlink_member_rejected() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo("docker-compose.yml")
        info.type = tarfile.LNKTYPE
        info.linkname = "manifest.json"
        tar.addfile(info)
    with pytest.raises(BundleError, match="non-regular"):
        verify_bundle(out.getvalue(), expected_sha=_SHA)


def test_duplicate_member_rejected() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for data in (b"a", b"b"):
            info = tarfile.TarInfo("docker-compose.yml")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(BundleError, match="duplicate"):
        verify_bundle(out.getvalue(), expected_sha=_SHA)


def test_manifest_schema_enforced() -> None:
    out = io.BytesIO()
    manifest = json.dumps({"schema": "wrong", "source_sha": _SHA, "files": {}}).encode()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    with pytest.raises(BundleError, match="schema"):
        verify_bundle(out.getvalue(), expected_sha=_SHA)


def test_production_bundle_contains_only_production_compose() -> None:
    files = verify_bundle(production_bundle(_SHA, _ROOT), expected_sha=_SHA)
    assert set(files) == {PRODUCTION_COMPOSE_FILE}
    content = files[PRODUCTION_COMPOSE_FILE]
    assert b"APEXSCAN_IMAGE" in content  # digest-pinned image variable
    # No secrets or dev source bind mount carried in the bundle.
    assert b"password" not in content.lower() and b"./backend:/app" not in content


def test_bundle_schema_is_versioned() -> None:
    manifest = json.loads(
        next(
            data
            for name, data in _read_members(create_bundle(_SHA, _FILES)).items()
            if name == "manifest.json"
        )
    )
    assert manifest["schema"] == BUNDLE_SCHEMA == "apexscan-deploy-bundle/1"


def _read_members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isreg()}  # type: ignore[union-attr]
