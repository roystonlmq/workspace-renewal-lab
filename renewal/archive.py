"""Recoverable, lossless component archives. This module never deletes a source."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


def sha(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def entries(source: Path):
    result = []
    paths = [source, *source.rglob("*")] if source.is_dir() else [source]
    for path in sorted(paths):
        relative = str(path.relative_to(source.parent))
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(source.resolve()):
                raise ValueError("External symlinks need an explicit owning-source archive")
            import os
            result.append({"path": relative, "type": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            result.append({"path": relative, "type": "file", "sha256": sha(path), "bytes": path.stat().st_size})
        elif path.is_dir():
            result.append({"path": relative, "type": "directory"})
        else:
            raise ValueError("Special files are not eligible for archival")
    return result


def create(source: Path, archive: Path, now: str | None = None):
    source = source.absolute()
    archive = archive.absolute()
    if not source.exists() or source.is_symlink():
        raise ValueError("Source must exist and cannot be a symlink")
    if archive.exists() or archive.with_suffix(archive.suffix + ".manifest.json").exists():
        raise ValueError("Archive must be a new path")
    if archive.is_relative_to(source):
        raise ValueError("Archive cannot be stored within its source")
    before = entries(source)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz", dereference=False) as tar:
        tar.add(source, arcname=source.name, recursive=True)
    if before != entries(source):
        archive.unlink()
        raise RuntimeError("Source changed during archival, no retirement allowed")
    manifest = {"schema": 1, "created_at": now or dt.datetime.now(dt.timezone.utc).isoformat(),
                "source": str(source), "source_name": source.name, "archive_sha256": sha(archive),
                "entries": before, "status": "recoverable copy, source retained", "last_use": None,
                "deletion": "disabled until off-laptop storage and all retirement conditions are verified"}
    manifest_path = archive.with_suffix(archive.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    archive.chmod(0o600)
    manifest_path.chmod(0o600)
    verify(archive)
    return manifest


def verify(archive: Path):
    manifest = json.loads(archive.with_suffix(archive.suffix + ".manifest.json").read_text())
    if sha(archive) != manifest["archive_sha256"]:
        raise ValueError("Archive hash mismatch")
    expected = {e["path"]: e for e in manifest["entries"]}
    with tarfile.open(archive, "r:gz") as tar:
        actual = tar.getmembers()
        if set(m.name for m in actual) != set(expected):
            raise ValueError("Archive member mismatch")
        for member in actual:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != manifest["source_name"]:
                raise ValueError("Unsafe archive path")
            e = expected[member.name]
            if member.isfile():
                h = hashlib.sha256()
                f = tar.extractfile(member)
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
                if e["type"] != "file" or h.hexdigest() != e["sha256"]:
                    raise ValueError("Archived content hash mismatch")
            elif member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts or e.get("target") != member.linkname:
                    raise ValueError("Unsafe archived symlink")
            elif not member.isdir():
                raise ValueError("Unsupported archive member")
    return manifest


def restore(archive: Path, destination: Path, now: str | None = None):
    manifest = verify(archive)
    target = destination / manifest["source_name"]
    if target.exists() or target.is_symlink():
        raise ValueError("Restore refuses to overwrite an existing target")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(destination, filter="data")
    if entries(target) != manifest["entries"]:
        raise ValueError("Restored content does not match manifest")
    manifest["last_use"] = now or dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["status"] = "restored, retirement eligibility reset"
    archive.with_suffix(archive.suffix + ".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return target


def record_use(archive: Path, now: str | None = None):
    """Renewed use restarts the recovery window without restoring a second copy."""
    manifest = verify(archive)
    manifest["last_use"] = now or dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["status"] = "renewed use, retirement eligibility reset"
    archive.with_suffix(archive.suffix + ".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def deletion_eligibility(manifest: dict, now: dt.datetime, *, storage_verified=False,
                         dependencies_clear=False, active=False, dirty=False, unpushed=False,
                         exceptions_clear=False, use_evidence_complete=False):
    """Explain eligibility. Unknown use and unknown upstreams must be passed as unsafe."""
    origin = manifest.get("last_use") or manifest["created_at"]
    age = now - dt.datetime.fromisoformat(origin)
    reasons = []
    for condition, reason in [(age >= dt.timedelta(days=30), "30 day recovery window not complete"),
        (storage_verified, "off-laptop archive storage not verified"), (dependencies_clear, "dependencies unresolved"),
        (not active, "active work"), (not dirty, "uncommitted work"), (not unpushed, "unpushed or unknown upstream work"),
        (exceptions_clear, "exceptions unresolved"), (use_evidence_complete, "use evidence incomplete")]:
        if not condition:
            reasons.append(reason)
    return {"eligible": not reasons, "reasons": reasons, "deletion_performed": False}
