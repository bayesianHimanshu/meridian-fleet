"""inputs_manifest.json: size and sha256 of every staged file, written by the host.

The staged cut is uploaded to a writable path inside the sandbox, so the worker checks
every file against this manifest before reading any data. A missing, extra or changed
file (including an incomplete upload) is refused.
"""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from meridian.ledger.hashing import canonical_json

MANIFEST_FILE = "inputs_manifest.json"
_CHUNK = 1 << 20


class InputsManifestError(Exception):
    pass


class ManifestEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str  # relative to the staged folder, "/"-separated
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InputsManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[ManifestEntry, ...]

    def dumps(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    def sha256(self) -> str:
        return hashlib.sha256(self.dumps().encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*") if (p.is_file() or p.is_symlink()) and p.name != MANIFEST_FILE
    )


def build_manifest(root: Path) -> InputsManifest:
    """Every file under root except the manifest itself, sorted by relative path."""
    entries = []
    for path in _files(root):
        if path.is_symlink():
            raise InputsManifestError(f"symlink in staged inputs: {path.relative_to(root)}")
        entries.append(
            ManifestEntry(
                path=path.relative_to(root).as_posix(),
                size=path.stat().st_size,
                sha256=_sha256(path),
            )
        )
    return InputsManifest(files=tuple(entries))


def write_manifest(root: Path) -> InputsManifest:
    manifest = build_manifest(root)
    (root / MANIFEST_FILE).write_text(manifest.dumps(), encoding="utf-8")
    return manifest


def verify_manifest(root: Path) -> InputsManifest:
    """Raise InputsManifestError unless the files under root are exactly the manifest's."""
    path = root / MANIFEST_FILE
    try:
        manifest = InputsManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise InputsManifestError(f"cannot read {MANIFEST_FILE}: {exc}") from exc
    expected = {e.path: e for e in manifest.files}
    present = {p.relative_to(root).as_posix(): p for p in _files(root)}
    missing = sorted(set(expected) - set(present))
    extra = sorted(set(present) - set(expected))
    if missing:
        raise InputsManifestError(f"missing staged files: {missing}")
    if extra:
        raise InputsManifestError(f"unexpected staged files: {extra}")
    for rel, entry in expected.items():
        file = present[rel]
        if file.is_symlink():
            raise InputsManifestError(f"symlink in staged inputs: {rel}")
        if file.stat().st_size != entry.size or _sha256(file) != entry.sha256:
            raise InputsManifestError(f"staged file changed: {rel}")
    return manifest
