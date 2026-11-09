"""Stage exactly what an agent may see: meta/trigger.json and the zone's cut areas.

The staged folder holds one folder per area, uploaded file by file into the sandbox's
folder for that area (/stage/<area>, created in the image; meridian.sandbox.policies):

* meta/trigger.json and meta/inputs_manifest.json (size and sha256 of every other staged
  file, written last), and
* the cut areas the zone may read: blinded/ and ops/ for a blinded zone, blinded/ only
  for the safety zone (with the safety database snapshot's blinded area).

It never holds unblinded/, the cut's manifest (which lists the unblinded files) or any
ground truth. A file is uploaded to its area's folder, not as a folder, because a folder
upload lands in a subfolder named after it and needs the parent to be writable; the
sandbox cannot even list /stage.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path

from meridian.data.cut_writer import BLINDED_AREA, OPS_AREA
from meridian.data.inputs_manifest import MANIFEST_FILE, write_manifest
from meridian.domain.events import Event
from meridian.ledger.hashing import canonical_json
from meridian.sandbox.policies import META, STAGE_ROOT, stage_path

STAGE_NAME = "cut"
TRIGGER_FILE = "trigger.json"
CUT_AREAS = (BLINDED_AREA, OPS_AREA)
SANDBOX_CUT_DIR = STAGE_ROOT
SANDBOX_TRIGGER = f"{stage_path(META)}/{TRIGGER_FILE}"
SANDBOX_MANIFEST = f"{stage_path(META)}/{MANIFEST_FILE}"


class StagingError(Exception):
    pass


def _copy_area(source: Path, target: Path, area: str) -> None:
    if not source.is_dir() or source.is_symlink():
        raise StagingError(f"{source} is not a directory")
    for entry in sorted(source.iterdir()):
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".parquet":
            raise StagingError(f"unexpected entry {entry} in {area}/")
        if (target / entry.name).exists():
            raise StagingError(f"{area}/{entry.name} would be staged twice")
        shutil.copyfile(entry, target / entry.name)


def cut_areas_for(reads: Sequence[str]) -> tuple[str, ...]:
    """The cut areas a policy's reads allow, in staging order."""
    areas = tuple(a for a in CUT_AREAS if a in reads)
    if BLINDED_AREA not in areas:
        raise StagingError(f"a cut is staged only for a zone that reads {BLINDED_AREA}/")
    return areas


def stage_cut(
    cut_dir: Path,
    trigger: Event,
    dest_root: Path,
    extra_blinded: Sequence[Path] = (),
    areas: Sequence[str] = CUT_AREAS,
) -> Path:
    """Copy the given cut areas' parquet files and write the trigger. Returns the stage.

    extra_blinded: further blinded areas staged into blinded/ (a safety agent gets the
    blinded area of the cut's safety database snapshot; never an unblinded/ folder)."""
    if not set(areas) <= set(CUT_AREAS) or BLINDED_AREA not in areas:
        raise StagingError(f"cannot stage areas {sorted(areas)}")
    stage = dest_root / STAGE_NAME
    stage.mkdir(parents=True, exist_ok=False)
    for area in areas:
        (stage / area).mkdir()
        _copy_area(cut_dir / area, stage / area, area)
    for extra in extra_blinded:
        if extra.name != BLINDED_AREA:
            raise StagingError(f"only a blinded area may be staged, not {extra}")
        _copy_area(extra, stage / BLINDED_AREA, BLINDED_AREA)
    (stage / META).mkdir()
    (stage / META / TRIGGER_FILE).write_text(canonical_json(trigger.model_dump(mode="json")))
    write_manifest(stage, at=stage / META / MANIFEST_FILE)  # last: it covers the rest
    assert_clean_stage(stage, areas)
    return stage


def assert_clean_stage(stage: Path, areas: Sequence[str] = CUT_AREAS) -> None:
    """Raise unless the stage holds only the given areas' *.parquet and meta/ with the
    trigger and the inputs manifest."""
    allowed = {*areas, META}
    top = {p.name for p in stage.iterdir()}
    if top != allowed:
        raise StagingError(f"stage holds {sorted(top)}; only {sorted(allowed)}")
    for path in stage.rglob("*"):
        rel = path.relative_to(stage)
        if path.is_symlink():
            raise StagingError(f"symlink in stage: {rel}")
        if "unblinded" in rel.parts or "ground_truth" in rel.parts:
            raise StagingError(f"forbidden path in stage: {rel}")
        if path.is_dir():
            if len(rel.parts) != 1:
                raise StagingError(f"nested directory in stage: {rel}")
            continue
        if len(rel.parts) != 2:
            raise StagingError(f"unexpected file in stage: {rel}")
        area, name = rel.parts
        if area == META:
            if name not in (TRIGGER_FILE, MANIFEST_FILE):
                raise StagingError(f"unexpected file in stage: {rel}")
        elif area not in areas or path.suffix != ".parquet":
            raise StagingError(f"unexpected file in stage: {rel}")


def uploads(stage: Path) -> list[tuple[Path, str]]:
    """(file, /stage/<area>) for every staged file: each lands directly in its area."""
    return [
        (path, stage_path(path.parent.name))
        for path in sorted(stage.rglob("*"))
        if path.is_file() and path.parent.parent == stage
    ]
